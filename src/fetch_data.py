"""
Download and prepare a sample of the Amazon Reviews 2023 (McAuley Lab) dataset,
category: Clothing, Shoes & Jewelry.

Source: https://huggingface.co/datasets/McAuley-Lab/Amazon-Reviews-2023
Citable, publicly released for research use.

The category's raw review file is ~28GB of jsonl (one category, not the
whole dataset) and its metadata file ~18GB, both hosted without a parquet
conversion (the repo ships a custom loading script, which recent versions
of `datasets` refuse to run). Rather than pull either file in full, this
script:

  1. Samples reviews by jumping to a random byte offset in the file via
     HTTP Range requests and reading a contiguous run of lines from there
     (a "random slice" sample, not a full-file uniform reservoir sample --
     good enough for a demo pipeline, not a statistically rigorous survey).
  2. Joins product category by streaming the metadata file from the start
     and matching parent_asins as they appear, up to a time budget, leaving
     any unmatched rows as "Unknown" rather than paying for a full 18GB scan.

Usage:
    python src/fetch_data.py --n 12000 --seed 42
    python src/fetch_data.py --n 50000 --out data/reviews_50k.parquet
"""
import argparse
import json
import random
import sys
import time
from pathlib import Path

import httpx
import pandas as pd
from tqdm import tqdm

REVIEWS_URL = (
    "https://huggingface.co/datasets/McAuley-Lab/Amazon-Reviews-2023/"
    "resolve/main/raw/review_categories/Clothing_Shoes_and_Jewelry.jsonl"
)
META_URL = (
    "https://huggingface.co/datasets/McAuley-Lab/Amazon-Reviews-2023/"
    "resolve/main/raw/meta_categories/meta_Clothing_Shoes_and_Jewelry.jsonl"
)

MIN_TEXT_CHARS = 20  # drop near-empty reviews, not useful for classification
RANGE_CHUNK_BYTES = 4_000_000  # bytes to pull per random offset before re-seeking


def _remote_size(client: httpx.Client, url: str) -> int:
    r = client.head(url)
    r.raise_for_status()
    return int(r.headers["content-length"])


def sample_reviews_by_offset(n: int, seed: int, n_offsets: int = 8) -> list[dict]:
    """Sample ~n reviews by reading contiguous runs from a handful of random
    byte offsets, spreading offsets across the file so no single product
    run dominates the sample."""
    rng = random.Random(seed)
    rows: list[dict] = []
    seen_ids: set[str] = set()

    with httpx.Client(follow_redirects=True, timeout=60) as client:
        size = _remote_size(client, REVIEWS_URL)
        offsets = sorted(rng.randint(0, max(size - RANGE_CHUNK_BYTES, 1)) for _ in range(n_offsets))
        per_offset_target = max(n // n_offsets, 1)

        pbar = tqdm(total=n, desc="Sampling reviews", unit="rows")
        for offset_i, start in enumerate(offsets):
            if len(rows) >= n:
                break
            got_here = 0
            pos = start
            while got_here < per_offset_target * 2 and len(rows) < n:
                end = pos + RANGE_CHUNK_BYTES - 1
                resp = client.get(REVIEWS_URL, headers={"Range": f"bytes={pos}-{end}"})
                if resp.status_code not in (200, 206):
                    break
                chunk = resp.content
                if not chunk:
                    break
                lines = chunk.split(b"\n")
                # first/last line likely partial (we didn't start/end on a boundary) -> drop them
                usable = lines[1:-1] if pos > 0 else lines[:-1]
                for raw_line in usable:
                    if not raw_line.strip():
                        continue
                    try:
                        row = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    rid = f"{row.get('asin')}|{row.get('user_id')}|{row.get('timestamp')}"
                    text = (row.get("text") or "").strip()
                    if len(text) < MIN_TEXT_CHARS or rid in seen_ids:
                        continue
                    seen_ids.add(rid)
                    rows.append(row)
                    got_here += 1
                    pbar.update(1)
                    if len(rows) >= n or got_here >= per_offset_target * 2:
                        break
                pos += RANGE_CHUNK_BYTES
                if pos >= size:
                    break
        pbar.close()
    return rows[:n]


def build_category_lookup(parent_asins: set[str], time_budget_s: float = 90.0) -> dict[str, str]:
    """Stream the metadata file from the start, matching parent_asins as
    they appear, bounded by a time budget rather than scanning all 18GB."""
    lookup: dict[str, str] = {}
    remaining = set(parent_asins)
    start_t = time.time()

    with httpx.Client(follow_redirects=True, timeout=60) as client:
        with client.stream("GET", META_URL) as resp:
            resp.raise_for_status()
            buf = b""
            pbar = tqdm(desc="Matching product metadata", unit="rows")
            for chunk in resp.iter_bytes():
                buf += chunk
                if time.time() - start_t > time_budget_s or not remaining:
                    break
                *complete_lines, buf = buf.split(b"\n")
                for raw_line in complete_lines:
                    pbar.update(1)
                    if not raw_line.strip():
                        continue
                    try:
                        row = json.loads(raw_line)
                    except json.JSONDecodeError:
                        continue
                    pa = row.get("parent_asin")
                    if pa in remaining:
                        # `categories` is a flat path, e.g.
                        # ["Clothing, Shoes & Jewelry", "Women", "Swimsuits & Cover Ups"]
                        categories = row.get("categories") or []
                        leaf = categories[-1] if categories else None
                        lookup[pa] = leaf or row.get("main_category") or "Unknown"
                        remaining.discard(pa)
                        if not remaining:
                            break
            pbar.close()

    for pa in remaining:
        lookup[pa] = "Unknown"
    print(f"Matched {len(parent_asins) - len(remaining)}/{len(parent_asins)} parent_asins to a category "
          f"within the {time_budget_s:.0f}s budget.", file=sys.stderr)
    return lookup


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--n", type=int, default=12000, help="number of reviews to sample")
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--n-offsets", type=int, default=8, help="number of random file offsets to sample from")
    parser.add_argument("--meta-timeout", type=float, default=600.0,
                        help="seconds to spend streaming metadata for category join "
                             "(the metadata file streams at roughly 5k rows/sec here; "
                             "raise this for better match coverage on a real pilot run)")
    parser.add_argument("--out", type=str, default=None, help="output parquet path")
    args = parser.parse_args()

    out_path = Path(args.out) if args.out else Path(f"data/reviews_{args.n}.parquet")
    out_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Sampling {args.n:,} reviews from {args.n_offsets} random offsets (seed={args.seed})...", file=sys.stderr)
    sample = sample_reviews_by_offset(args.n, args.seed, args.n_offsets)
    if len(sample) < args.n:
        print(f"Warning: only got {len(sample)}/{args.n} reviews; consider raising --n-offsets", file=sys.stderr)

    df = pd.DataFrame(sample)
    keep_cols = ["parent_asin", "asin", "rating", "title", "text", "timestamp", "verified_purchase", "helpful_vote"]
    df = df[[c for c in keep_cols if c in df.columns]]

    print("Joining product category metadata for sampled parent_asins...", file=sys.stderr)
    category_lookup = build_category_lookup(set(df["parent_asin"].dropna().unique()), args.meta_timeout)
    df["product_category"] = df["parent_asin"].map(category_lookup).fillna("Unknown")

    df["review_id"] = df.index.map(lambda i: f"rev_{i:07d}")
    df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", errors="coerce")

    df.to_parquet(out_path, index=False)
    print(f"Wrote {len(df):,} reviews -> {out_path}", file=sys.stderr)
    print(df["product_category"].value_counts().head(15))


if __name__ == "__main__":
    main()
