# Review Classification Cascade

Two-tier AI pipeline for e-commerce review classification: Jev (TypeSafe AI's
"System One" model, called directly via TypeSafe's API) does fast bulk
structured classification; a confidence/label threshold escalates a subset
to Claude, which writes the human-facing recommendation (sizing-guide
language, PDP copy fixes, merchandising notes).

Portfolio/demo project — data is real (Amazon Reviews 2023, McAuley Lab,
UCSD), no simulated model calls except an explicit replay fallback for the
live demo recording.

## Status

- [x] Repo scaffold + data fetch script
- [ ] Pilot sample pulled and label set validated
- [ ] Jev classification (single + packed-batch modes)
- [ ] Escalation logic (parameterized threshold)
- [ ] Claude writeup layer
- [ ] Calibration / cost / distribution analysis
- [ ] Live-feed demo frontend + replay fallback

## Setup

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # fill in TYPESAFE_API_KEY, ANTHROPIC_API_KEY
```

## Pilot

```bash
python src/fetch_data.py --n 12000 --seed 42 --meta-timeout 900
```

The category's raw files (~28GB reviews, ~18GB metadata) aren't published
as parquet, so this script avoids downloading either in full:

- **Reviews** are sampled by jumping to random byte offsets in the file via
  HTTP `Range` requests and reading a contiguous run of lines from each —
  a "random slice" sample, not a rigorous uniform reservoir sample, but
  good enough for a demo pipeline and orders of magnitude faster.
- **Product category** is joined by streaming the metadata file from the
  start and matching `parent_asin`s as they appear, bounded by
  `--meta-timeout` rather than a full scan. Metadata streams at roughly
  5k rows/sec; unmatched rows fall back to `"Unknown"`. Raise the timeout
  for better category coverage on the real pilot/50k run.

## Label schema

Defined once in [src/labels.py](src/labels.py):

- Primary (6-way Choice): `fit_issue`, `quality_defect`, `shipping_delivery`,
  `customer_service`, `product_as_described_mismatch`,
  `general_sentiment_other`
- Secondary "free rider" questions on the same request: `fit_direction`
  (Score), `severity` (Score), `is_quality_defect` (Noul)

Wording is a placeholder pending pilot review of real label distribution.

## Repo structure

```
data/                    raw + sampled review parquet files (gitignored)
src/
  fetch_data.py          HF streaming sample + category join
  labels.py              shared label schema
  classify_jev.py        TypeSafe API calls (single + packed-batch)
  escalate.py            threshold-based routing
  classify_claude.py     escalated-slice writeup generation
  calibration.py         confidence-vs-accuracy analysis
  cost_model.py          token/cost/threshold-sensitivity analysis
server/                  local demo backend (SSE streaming)
frontend/                live-feed demo page
results/                 analysis outputs (gitignored)
```

## Known Jev constraints the pipeline designs around

- $0.042 / M input tokens, output free
- 250k tokens/sec, 1200 req/min rate limits (unstable as of writing) — retry
  with backoff, honor `retry-after`
- 64k tokens/request, 32k state+question combined
- Text-only, 70-500ms/call single-item; packed-batch mode for bulk throughput
- Confidence tends to sit in a 0.5-0.85 "mushy middle" on genuinely
  ambiguous inputs rather than near zero — checked explicitly in
  `calibration.py`
