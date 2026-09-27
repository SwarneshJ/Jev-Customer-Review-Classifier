"""Shared label schema for Jev's Layer-1 classification.

Kept in one place so fetch/classify/calibration/cost scripts agree on
wording, and so the label set can be revised after the pilot without
hunting through multiple files.

Field shapes here (`instructions` + `criteria`) mirror Vercel AI Gateway's
evaluation API question format directly (confirmed against
https://vercel.com/docs/ai-gateway/modalities/evaluation), so classify_jev.py
can pass these straight through with no reshaping:
  - choice:  {"type": "choice",  "instructions": str, "criteria": {option: description}}
  - score:   {"type": "score",   "instructions": str, "criteria": [label, ...]}  (low -> high)
  - noul:    {"type": "noul",    "instructions": str, "criteria": {"true": str, "false": str}}
    (TypeSafe's own term for a calibrated true/false probability; the Gateway's
    provider-agnostic /v1/evaluate endpoint calls the same thing "boolean" --
    classify_jev.py translates the type tag when it builds the wire request.)
"""

# Pilot hand-label pass (n=40, rating-stratified, see results/pilot_hand_labels.csv)
# surfaced two overlaps that needed an explicit tie-break rule:
#  - fit_issue vs product_as_described_mismatch: a review complaining the cut/
#    style/color/material differs from the listing is a mismatch, even if it
#    also happens to affect how the item fits (e.g. "advertised as tapered,
#    mine has a wide leg opening" is a mismatch, not a sizing complaint).
#  - customer_service came back with zero clean hits in the hand-label pass:
#    reviews that merely state "I returned it" are describing a fit/quality
#    consequence, not a support complaint. Tightened to require an explicit
#    gripe about the support/return process or seller conduct, since without
#    that qualifier the label just re-catches fit/quality reviews.
PRIMARY_CRITERIA = {
    "fit_issue": (
        "The item's size runs small/large/tight/loose relative to the size ordered -- "
        "sizing-scale complaints only, not style or material."
    ),
    "quality_defect": (
        "Materials, construction, or damage: falling apart, arrived used/damaged, poorly made."
    ),
    "shipping_delivery": "Late, lost, damaged in transit, or packaging problems.",
    "customer_service": (
        "An explicit complaint about the support interaction, the return/exchange process "
        "being difficult, or seller conduct. Merely stating the item was returned does not "
        "qualify unless the process itself is criticized."
    ),
    "product_as_described_mismatch": (
        "Color, material, style, cut, sheerness, or any other attribute differs from the "
        "listing/photos -- including cases where that mismatch happens to affect fit, e.g. "
        "a garment advertised as one cut but shaped like another."
    ),
    "general_sentiment_other": "Anything else, including purely positive reviews.",
}

PRIMARY_LABELS = list(PRIMARY_CRITERIA.keys())

PRIMARY_INSTRUCTIONS = "Classify the primary reason for this clothing/shoes/jewelry review."

FIT_DIRECTION_CRITERIA = ["runs_small", "true_to_size", "runs_large"]

SEVERITY_CRITERIA = [
    "none: no problem described",
    "minor: mild inconvenience, wouldn't stop a repurchase",
    "moderate: clear problem that affects usability",
    "severe: significant harm, waste of money, or complete failure",
]

# Secondary "free rider" questions sent in the same Jev request as the
# primary Choice classification (nearly free once the input is scored).
SECONDARY_QUESTIONS = {
    "fit_direction": {
        "type": "score",
        "instructions": "How does the sizing run relative to true to size?",
        "criteria": FIT_DIRECTION_CRITERIA,
    },
    "severity": {
        "type": "score",
        "instructions": "How severe is the complaint or issue described in this review?",
        "criteria": SEVERITY_CRITERIA,
    },
    "is_quality_defect": {
        "type": "noul",
        "instructions": "Does this review describe a quality defect?",
        "criteria": {
            "true": "Materials, construction, or damage problem (falling apart, arrived damaged, poorly made)",
            "false": "No quality defect described",
        },
    },
    # Added after the pilot hand-label pass: several reviews raised more than
    # one issue (e.g. "advertised as suede, isn't; cheap metal hardware;
    # arrived a day late" hits mismatch + quality_defect + shipping in one
    # review). Forcing a single primary label silently drops the rest, so
    # this asks Jev to name a second label if the review raises more than one.
    "secondary_issue": {
        "type": "choice",
        "instructions": (
            "If this review raises a second, distinct issue beyond the primary label "
            "(from the same label set), name it; otherwise 'none'."
        ),
        "criteria": {**PRIMARY_CRITERIA, "none": "The review raises only one issue."},
    },
}
