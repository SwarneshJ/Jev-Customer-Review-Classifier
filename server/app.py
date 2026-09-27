"""
Local demo backend: streams live (or replayed) Jev classifications over
Server-Sent Events for the screen-recorded demo frontend.

Holds TYPESAFE_API_KEY server-side only -- the browser never sees it, only
this backend's own /api/stream endpoint. In "live" mode it calls Jev in
real packed-mode batches (genuine calls made during the recording, not
precomputed), then re-paces the results into a steady per-review SSE
stream so the frontend can animate a smooth flow of cards regardless of
how bursty the underlying batch responses are -- a batch of ~25 arrives
all at once every second or two, but reveal_delay_ms controls how fast
those get handed to the browser one at a time.

"replay" mode reads a previously classified parquet (generate one with
classify_jev.py ahead of time) and replays it at the same steady cadence.
It exists only as a fallback for a flaky connection during the actual
recording, per the project brief -- not the primary path.

Usage:
    uvicorn server.app:app --port 8787
    open http://localhost:8787/?mode=live&n=12000&reveal_delay_ms=120
"""
import asyncio
import json
import sys
import time
from pathlib import Path

import pandas as pd
from dotenv import load_dotenv
from fastapi import FastAPI, Request
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles

ROOT = Path(__file__).parent.parent
sys.path.insert(0, str(ROOT / "src"))
load_dotenv(ROOT / ".env")

import classify_jev as cj  # noqa: E402
from labels import PRIMARY_LABELS  # noqa: E402

app = FastAPI()

DATA_PATH = ROOT / "data" / "reviews_12000.parquet"
REPLAY_PATH = ROOT / "data" / "reviews_12000_classified.parquet"


def load_reviews(n: int) -> pd.DataFrame:
    df = pd.read_parquet(DATA_PATH)
    return df.head(n) if n else df


async def live_producer(df: pd.DataFrame, queue: asyncio.Queue, max_batch_tokens: int):
    client = cj.JevClient()
    rows = df[["review_id", "text"]].to_dict("records")
    batches = cj.build_token_budgeted_batches(rows, max_batch_tokens)
    text_by_id = dict(zip(df["review_id"], df["text"]))
    rating_by_id = dict(zip(df["review_id"], df["rating"]))
    loop = asyncio.get_event_loop()
    try:
        for batch_id, batch in enumerate(batches):
            payload = cj.build_packed_request(batch)
            body = await loop.run_in_executor(None, client.call, payload, batch_id, len(batch))
            per_item = {r["review_id"]: {"review_id": r["review_id"]} for r in batch}
            for qual_name, ans in body["answers"].items():
                name, rid = qual_name.split(cj.ITEM_SEP, 1)
                per_item[rid].update(cj.parse_answer(name, ans))
            for rid, rec in per_item.items():
                rec["text"] = text_by_id[rid]
                rec["rating"] = rating_by_id[rid]
                await queue.put(rec)
    except Exception as e:  # noqa: BLE001 -- surface any failure to the frontend rather than hanging it
        await queue.put({"error": str(e)})
    finally:
        client.close()
        await queue.put(None)


async def replay_producer(df: pd.DataFrame, queue: asyncio.Queue):
    if not REPLAY_PATH.exists():
        await queue.put({"error": "No precomputed classification found for replay mode. "
                                   "Run classify_jev.py ahead of time to generate one."})
        await queue.put(None)
        return
    classified = pd.read_parquet(REPLAY_PATH)
    merged = df[["review_id"]].merge(classified, on="review_id", how="inner")
    for _, row in merged.iterrows():
        await queue.put(row.to_dict())
        await asyncio.sleep(0)  # yield control so this behaves like a real async producer
    await queue.put(None)


async def event_stream(mode: str, n: int, reveal_delay_ms: int, max_batch_tokens: int):
    df = load_reviews(n)
    queue: asyncio.Queue = asyncio.Queue()

    if mode == "replay":
        producer = asyncio.create_task(replay_producer(df, queue))
    else:
        producer = asyncio.create_task(live_producer(df, queue, max_batch_tokens))

    # ~1,300-1,450 measured tokens/item with all 5 questions (see classify_jev.py) --
    # exact per-item cost isn't attributable across a packed batch, so the frontend
    # multiplies this estimate by the running count rather than the server
    # tracking a precise figure.
    est_cost_per_item = 1400 * cj.INPUT_TOKEN_COST_PER_M / 1_000_000
    yield f"event: meta\ndata: {json.dumps({'total': len(df), 'mode': mode, 'labels': PRIMARY_LABELS, 'est_cost_per_item': est_cost_per_item})}\n\n"

    classified_count = 0
    label_counts = {label: 0 for label in PRIMARY_LABELS}
    start = time.time()

    try:
        while True:
            rec = await queue.get()
            if rec is None:
                break
            if "error" in rec:
                yield f"event: error\ndata: {json.dumps(rec)}\n\n"
                break

            classified_count += 1
            label = rec.get("primary_choice") or "general_sentiment_other"
            label_counts[label] = label_counts.get(label, 0) + 1
            elapsed = time.time() - start
            payload = {
                "review_id": rec.get("review_id"),
                "text": (rec.get("text") or "")[:280],
                "rating": rec.get("rating"),
                "primary_choice": label,
                "primary_confidence": rec.get("primary_confidence"),
                "fit_direction_score": rec.get("fit_direction_score"),
                "severity_score": rec.get("severity_score"),
                "count": classified_count,
                "total": len(df),
                "label_counts": label_counts,
                "rate_per_min": round(classified_count / max(elapsed, 1e-9) * 60, 1),
            }
            yield f"event: review\ndata: {json.dumps(payload)}\n\n"
            await asyncio.sleep(reveal_delay_ms / 1000)

        yield f"event: done\ndata: {json.dumps({'total_classified': classified_count, 'elapsed_s': round(time.time() - start, 1)})}\n\n"
    finally:
        # If the browser disconnects (tab closed, navigated away) this
        # generator is cancelled by Starlette -- without this, the live
        # producer task keeps calling the paid API in the background with
        # nothing left to consume its queue. Cancelling here stops real
        # spend the moment the viewer is gone, whether the stream ended
        # normally or was torn down early.
        producer.cancel()
        try:
            await producer
        except asyncio.CancelledError:
            pass


DEMO_MODE_CAP = 20


@app.get("/api/stream")
async def stream(request: Request):
    mode = request.query_params.get("mode", "live")
    if mode not in ("live", "replay"):
        mode = "live"
    n = int(request.query_params.get("n", 12000))
    # Enforced here too, not just in the frontend's button label/state --
    # a demo=1 request is capped server-side regardless of what n it asks
    # for, so a stale tab or a hand-edited URL can't bypass the cap.
    if request.query_params.get("demo") == "1":
        n = min(n, DEMO_MODE_CAP)
    reveal_delay_ms = int(request.query_params.get("reveal_delay_ms", 120))
    max_batch_tokens = int(request.query_params.get("max_batch_tokens", 28000))

    return StreamingResponse(
        event_stream(mode, n, reveal_delay_ms, max_batch_tokens),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


app.mount("/", StaticFiles(directory=str(ROOT / "frontend"), html=True), name="frontend")
