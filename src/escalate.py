"""
Layer 2: escalation routing. Plain code, not a model call -- decides which of
Jev's Layer-1 classifications get a second pass from Claude.

The threshold is a parameter, not a constant: cost_model.py sweeps
--confidence-threshold across a range to plot cost-saved vs. risk-of-missed-
escalation, so the routing rule lives here as an importable pure function
rather than being inlined into a one-off script.

Escalation rule (all optional, combine with OR -- any one reason escalates):
  1. low_confidence:  primary_confidence < --confidence-threshold, AND the
                      label is one Claude can actually act on (see
                      ACTIONABLE_LABELS below) -- Jev being unsure about a
                      general_sentiment_other/shipping_delivery/
                      customer_service review isn't worth a Claude call,
                      since Layer 3's job is sizing-guide/PDP-copy/
                      merchandising writeups, not more classification.
  2. high_severity:   severity_score >= --severity-threshold, regardless of
                      confidence (a confidently-detected severe defect still
                      deserves a human-facing writeup).
  3. multi_issue:     secondary_issue_choice is not "none" (see labels.py --
                      added after the pilot hand-label pass found reviews
                      like "advertised as suede, isn't; cheap hardware;
                      arrived late" that a single primary label collapses).

Usage:
    python src/escalate.py --in data/reviews_12000_classified.parquet --confidence-threshold 0.7
"""
import argparse
import sys
from pathlib import Path

import pandas as pd

# Labels Layer 3 can turn into an actual recommendation (sizing-guide
# language, PDP copy fix, merchandising note). shipping_delivery and
# customer_service are logistics/support issues outside that scope;
# general_sentiment_other has nothing to act on. Escalating those on low
# confidence alone would waste Claude calls on reviews with no writeup to
# produce -- override with --no-actionable-filter to escalate on confidence
# across all labels instead.
ACTIONABLE_LABELS = {"fit_issue", "quality_defect", "product_as_described_mismatch"}


def should_escalate(
    row: dict,
    confidence_threshold: float = 0.7,
    actionable_labels: set | None = ACTIONABLE_LABELS,
    severity_threshold: float | None = None,
    escalate_multi_issue: bool = True,
) -> tuple[bool, list[str]]:
    reasons = []
    label = row.get("primary_choice")
    confidence = row.get("primary_confidence")

    label_is_actionable = actionable_labels is None or label in actionable_labels

    if label_is_actionable and confidence is not None and confidence < confidence_threshold:
        reasons.append("low_confidence")

    severity = row.get("severity_score")
    if severity_threshold is not None and severity is not None and severity >= severity_threshold:
        reasons.append("high_severity")

    secondary = row.get("secondary_issue_choice")
    if escalate_multi_issue and secondary not in (None, "none"):
        reasons.append("multi_issue")

    return (len(reasons) > 0, reasons)


def apply_escalation(
    df: pd.DataFrame,
    confidence_threshold: float = 0.7,
    actionable_labels: set | None = ACTIONABLE_LABELS,
    severity_threshold: float | None = None,
    escalate_multi_issue: bool = True,
) -> pd.DataFrame:
    """Returns df with two new columns: escalated (bool), escalation_reasons
    (comma-joined string, empty if not escalated)."""
    results = df.apply(
        lambda row: should_escalate(
            row.to_dict(), confidence_threshold, actionable_labels, severity_threshold, escalate_multi_issue
        ),
        axis=1,
        result_type="expand",
    )
    out = df.copy()
    out["escalated"] = results[0]
    out["escalation_reasons"] = results[1].apply(lambda r: ",".join(r))
    return out


def summarize(df: pd.DataFrame) -> None:
    n = len(df)
    n_esc = int(df["escalated"].sum())
    print(f"Escalated {n_esc}/{n} reviews ({n_esc / n:.1%})", file=sys.stderr)
    print("By primary label:", file=sys.stderr)
    print(df.groupby("primary_choice")["escalated"].agg(["sum", "count", "mean"])
          .rename(columns={"sum": "escalated", "count": "total", "mean": "rate"}), file=sys.stderr)
    print("By reason (rows can have multiple, so these overlap):", file=sys.stderr)
    for reason in ["low_confidence", "high_severity", "multi_issue"]:
        count = df["escalation_reasons"].str.contains(reason, na=False).sum()
        print(f"  {reason}: {count}", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--in", dest="in_path", required=True, help="classified reviews parquet (from classify_jev.py)")
    parser.add_argument("--out", dest="out_path", default=None, help="output parquet with escalated/escalation_reasons columns")
    parser.add_argument("--confidence-threshold", type=float, default=0.7)
    parser.add_argument("--severity-threshold", type=float, default=None,
                        help="e.g. 2.5 on the 0-3 severity scale; disabled by default")
    parser.add_argument("--no-actionable-filter", action="store_true",
                        help="apply the confidence threshold across all labels, not just fit_issue/quality_defect/product_as_described_mismatch")
    parser.add_argument("--no-multi-issue", action="store_true", help="disable the multi-issue escalation override")
    args = parser.parse_args()

    in_path = Path(args.in_path)
    out_path = Path(args.out_path) if args.out_path else in_path.with_name(in_path.stem + "_escalated.parquet")

    df = pd.read_parquet(in_path)
    result = apply_escalation(
        df,
        confidence_threshold=args.confidence_threshold,
        actionable_labels=None if args.no_actionable_filter else ACTIONABLE_LABELS,
        severity_threshold=args.severity_threshold,
        escalate_multi_issue=not args.no_multi_issue,
    )
    result.to_parquet(out_path, index=False)
    summarize(result)
    print(f"Wrote {out_path}", file=sys.stderr)


if __name__ == "__main__":
    main()
