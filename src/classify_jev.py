"""
Layer 1: classify reviews with Jev (TypeSafe AI's "System One" model) through
Vercel AI Gateway's evaluation API.

Endpoint contract confirmed against Vercel's docs (2026-09):
  https://vercel.com/docs/ai-gateway/modalities/evaluation
  POST https://ai-gateway.vercel.sh/v1/evaluate
  { "model": "typesafe-ai/jev", "state": <str|obj>, "questions": {name: {type, instructions, criteria}} }
  ->
  { "answers": {name: {type, choice|score|probability, probabilities}}, "usage": {inputTokens, outputTokens} }

Two modes:
  --mode single  one review per request (used for the live demo, where a
                 human-watchable pace matters more than throughput)
  --mode packed  many reviews packed into one request as named questions
                 over a shared state object, for bulk throughput (this is
                 the documented Jev pattern for amortizing one round trip
                 across many independent items -- see README for the
                 published unpacked-vs-packed benchmark this design targets).
                 Batches are sized by a token budget, not a fixed item
                 count: repeating the label criteria per item costs
                 ~1,300-1,450 tokens/item with all 5 questions, which is
                 the dominant and roughly constant cost per item, so a
                 fixed count either wastes headroom or blows the 32k limit.

Usage:
    python src/classify_jev.py --in data/reviews_12000.parquet --mode packed --max-batch-tokens 28000
    python src/classify_jev.py --in data/reviews_12000.parquet --mode single --limit 50
"""
import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx
import pandas as pd
import tiktoken
from dotenv import load_dotenv
from tqdm import tqdm

sys.path.insert(0, str(Path(__file__).parent))
from labels import PRIMARY_CRITERIA, PRIMARY_INSTRUCTIONS, SECONDARY_QUESTIONS

load_dotenv()

GATEWAY_URL = "https://ai-gateway.vercel.sh/v1/evaluate"
MODEL_ID = os.environ.get("JEV_MODEL_ID", "typesafe-ai/jev")
INPUT_TOKEN_COST_PER_M = 0.042  # output tokens are free (see README)

# TypeSafe's own name for a calibrated true/false probability is "noul";
# the Gateway's provider-agnostic /v1/evaluate endpoint spells the same
# question type "boolean". Translate at the wire boundary so labels.py can
# keep using the vocabulary the project brief and hand-label pass used.
WIRE_TYPE = {"noul": "boolean", "choice": "choice", "score": "score"}

BASE_QUESTIONS = {
    "primary": {"type": "choice", "instructions": PRIMARY_INSTRUCTIONS, "criteria": PRIMARY_CRITERIA},
    **SECONDARY_QUESTIONS,
}

ITEM_SEP = "::"  # joins a question name to a review_id in packed mode, e.g. "primary::rev_0000042"

# tiktoken's cl100k_base is a stand-in for budgeting, not Jev's actual
# tokenizer (undocumented) -- close enough to keep packed requests safely
# under the 32k combined state+question limit.
_ENCODING = tiktoken.get_encoding("cl100k_base")


def estimate_tokens(payload: dict) -> int:
    return len(_ENCODING.encode(json.dumps(payload)))


def to_wire_question(qdef: dict) -> dict:
    return {
        "type": WIRE_TYPE[qdef["type"]],
        "instructions": qdef["instructions"],
        "criteria": qdef["criteria"],
    }


def build_single_request(text: str) -> dict:
    return {
        "model": MODEL_ID,
        "state": text,
        "questions": {name: to_wire_question(qdef) for name, qdef in BASE_QUESTIONS.items()},
    }


def build_packed_request(rows: list[dict]) -> dict:
    state = {r["review_id"]: r["text"] for r in rows}
    questions = {}
    for r in rows:
        rid = r["review_id"]
        for name, qdef in BASE_QUESTIONS.items():
            wire = to_wire_question(qdef)
            wire["instructions"] = (
                f"For the review keyed '{rid}' in the shared state (ignore other keys): "
                + wire["instructions"]
            )
            questions[f"{name}{ITEM_SEP}{rid}"] = wire
    return {"model": MODEL_ID, "state": state, "questions": questions}


def parse_answer(name: str, ans: dict) -> dict:
    """Flatten one Gateway answer into a few output columns, with a derived
    scalar confidence (Gateway's /v1/evaluate doesn't return a confidence
    field directly -- only per-option/per-rung probabilities)."""
    out = {}
    if ans["type"] == "choice":
        probs = ans.get("probabilities", {})
        out[f"{name}_choice"] = ans.get("choice")
        out[f"{name}_confidence"] = max(probs.values()) if probs else None
        out[f"{name}_probabilities"] = json.dumps(probs)
    elif ans["type"] == "score":
        probs = ans.get("probabilities", {})
        out[f"{name}_score"] = ans.get("score")
        out[f"{name}_confidence"] = max(probs.values()) if probs else None
        out[f"{name}_probabilities"] = json.dumps(probs)
    elif ans["type"] == "boolean":
        p = ans.get("probability")
        out[f"{name}_probability"] = p
        out[f"{name}_confidence"] = max(p, 1 - p) if p is not None else None
    return out


class JevClient:
    def __init__(self, max_retries: int = 5):
        api_key = os.environ.get("AI_GATEWAY_API_KEY")
        if not api_key:
            raise RuntimeError("AI_GATEWAY_API_KEY not set -- copy .env.example to .env and fill it in")
        self.client = httpx.Client(
            headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
            timeout=30,
        )
        self.max_retries = max_retries
        self.usage_log: list[dict] = []

    def call(self, payload: dict, batch_id: int, n_items: int) -> dict:
        for attempt in range(self.max_retries):
            t0 = time.time()
            resp = self.client.post(GATEWAY_URL, json=payload)
            latency_ms = (time.time() - t0) * 1000
            if resp.status_code == 429:
                retry_after = float(resp.headers.get("retry-after", 2 ** attempt))
                tqdm.write(f"429 rate limited, backing off {retry_after:.1f}s (attempt {attempt + 1})")
                time.sleep(retry_after)
                continue
            if resp.status_code >= 500:
                backoff = 2 ** attempt
                tqdm.write(f"{resp.status_code} server error, retrying in {backoff}s (attempt {attempt + 1})")
                time.sleep(backoff)
                continue
            resp.raise_for_status()
            body = resp.json()
            usage = body.get("usage", {})
            input_tokens = usage.get("inputTokens", 0)
            output_tokens = usage.get("outputTokens", 0)
            self.usage_log.append({
                "batch_id": batch_id,
                "n_items": n_items,
                "input_tokens": input_tokens,
                "output_tokens": output_tokens,
                "cost_usd": input_tokens * INPUT_TOKEN_COST_PER_M / 1_000_000,
                "latency_ms": latency_ms,
            })
            return body
        raise RuntimeError(f"Gave up after {self.max_retries} retries on batch {batch_id}")

    def close(self):
        self.client.close()


def classify_single_mode(df: pd.DataFrame, client: JevClient) -> pd.DataFrame:
    records = []
    for batch_id, (_, row) in enumerate(tqdm(df.iterrows(), total=len(df), desc="Classifying (single)")):
        payload = build_single_request(row["text"])
        body = client.call(payload, batch_id, n_items=1)
        rec = {"review_id": row["review_id"]}
        for name, ans in body["answers"].items():
            rec.update(parse_answer(name, ans))
        records.append(rec)
    return pd.DataFrame(records)


def build_token_budgeted_batches(rows: list[dict], max_batch_tokens: int) -> list[list[dict]]:
    """Pack rows into batches by estimated token cost rather than a fixed
    count. Each item repeats the full question/criteria text (~1,300-1,450
    tokens/item measured with all 5 questions), which is the dominant cost
    and roughly constant per item regardless of review length -- so a fixed
    item count either wastes headroom on short reviews or blows the 32k
    combined state+question budget on longer ones. Marginal cost is
    estimated once per item (as a one-item packed request) and items are
    accumulated greedily; each item's own single-item request must itself
    fit under the budget, or it's placed alone with a warning."""
    batches: list[list[dict]] = []
    current: list[dict] = []
    current_tokens = 0
    for row in rows:
        item_tokens = estimate_tokens(build_packed_request([row]))
        if item_tokens > max_batch_tokens:
            tqdm.write(f"Warning: {row['review_id']} alone estimates {item_tokens} tokens, "
                       f"over --max-batch-tokens={max_batch_tokens}; sending it solo anyway")
            if current:
                batches.append(current)
                current, current_tokens = [], 0
            batches.append([row])
            continue
        if current and current_tokens + item_tokens > max_batch_tokens:
            batches.append(current)
            current, current_tokens = [], 0
        current.append(row)
        current_tokens += item_tokens
    if current:
        batches.append(current)
    return batches


def classify_packed_mode(df: pd.DataFrame, client: JevClient, max_batch_tokens: int) -> pd.DataFrame:
    records = []
    rows = df[["review_id", "text"]].to_dict("records")
    batches = build_token_budgeted_batches(rows, max_batch_tokens)
    avg_size = len(rows) / len(batches) if batches else 0
    tqdm.write(f"Packed {len(rows)} reviews into {len(batches)} batches "
               f"(avg {avg_size:.1f} reviews/batch, budget={max_batch_tokens} tokens/batch)")
    for batch_id, batch in enumerate(tqdm(batches, desc="Classifying (packed)")):
        payload = build_packed_request(batch)
        body = client.call(payload, batch_id, n_items=len(batch))
        per_item: dict[str, dict] = {r["review_id"]: {"review_id": r["review_id"]} for r in batch}
        for qual_name, ans in body["answers"].items():
            name, rid = qual_name.split(ITEM_SEP, 1)
            per_item[rid].update(parse_answer(name, ans))
        records.extend(per_item.values())
    return pd.DataFrame(records)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--in", dest="in_path", required=True, help="input reviews parquet (from fetch_data.py)")
    parser.add_argument("--out", dest="out_path", default=None, help="output parquet with classification columns")
    parser.add_argument("--mode", choices=["single", "packed"], default="packed")
    parser.add_argument("--max-batch-tokens", type=int, default=28000,
                        help="token budget per packed request, leaving headroom under Jev's 32k "
                             "combined state+question limit (measured ~1,300-1,450 tokens/item "
                             "with all 5 questions, so this yields roughly 19-21 reviews/batch)")
    parser.add_argument("--limit", type=int, default=None, help="only classify the first N reviews (smoke test)")
    args = parser.parse_args()

    in_path = Path(args.in_path)
    out_path = Path(args.out_path) if args.out_path else in_path.with_name(in_path.stem + "_classified.parquet")

    df = pd.read_parquet(in_path)
    if args.limit:
        df = df.head(args.limit)

    client = JevClient()
    try:
        if args.mode == "single":
            result = classify_single_mode(df, client)
        else:
            result = classify_packed_mode(df, client, args.max_batch_tokens)
    finally:
        client.close()

    merged = df.merge(result, on="review_id", how="left")
    merged.to_parquet(out_path, index=False)

    usage_df = pd.DataFrame(client.usage_log)
    usage_path = out_path.with_name(out_path.stem + "_usage.csv")
    usage_df.to_csv(usage_path, index=False)

    total_cost = usage_df["cost_usd"].sum()
    total_requests = len(usage_df)
    elapsed = usage_df["latency_ms"].sum() / 1000
    print(f"Classified {len(merged):,} reviews in {total_requests} request(s), "
          f"${total_cost:.4f} total, {len(merged) / max(elapsed, 1e-9):.1f} items/sec "
          f"(sequential latency sum; wall time may differ)", file=sys.stderr)
    print(f"Wrote {out_path}", file=sys.stderr)
    print(f"Wrote usage log -> {usage_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
