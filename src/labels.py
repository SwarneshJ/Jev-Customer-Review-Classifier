"""Shared label schema for Jev's Layer-1 classification.

Kept in one place so fetch/classify/calibration/cost scripts agree on
wording, and so the label set can be revised after the pilot without
hunting through multiple files.
"""

PRIMARY_LABELS = [
    "fit_issue",
    "quality_defect",
    "shipping_delivery",
    "customer_service",
    "product_as_described_mismatch",
    "general_sentiment_other",
]

FIT_DIRECTION_SCORE = ["runs_small", "true_to_size", "runs_large"]

# Secondary "free rider" questions sent in the same Jev request as the
# primary Choice classification (nearly free once the input is scored).
SECONDARY_QUESTIONS = {
    "fit_direction": {
        "type": "score",
        "options": FIT_DIRECTION_SCORE,
        "prompt": "How does the sizing run relative to true to size?",
    },
    "severity": {
        "type": "score",
        "prompt": "How severe is the complaint or issue described, from none to severe?",
    },
    "is_quality_defect": {
        "type": "noul",
        "prompt": "Does this review describe a quality defect (materials, construction, damage)?",
    },
    # Added after the pilot hand-label pass: several reviews raised more than
    # one issue (e.g. "advertised as suede, isn't; cheap metal hardware;
    # arrived a day late" hits mismatch + quality_defect + shipping in one
    # review). Forcing a single primary label silently drops the rest, so
    # this asks Jev to name a second label if the review raises more than one.
    "secondary_issue": {
        "type": "choice",
        "options": PRIMARY_LABELS + ["none"],
        "prompt": (
            "If this review raises a second, distinct issue beyond the primary label "
            "(from the same label set), name it; otherwise 'none'."
        ),
    },
}

# Pilot hand-label pass (n=40, rating-stratified, see results/pilot_hand_labels.csv)
# surfaced two overlaps that needed an explicit tie-break rule in the prompt:
#  - fit_issue vs product_as_described_mismatch: a review complaining the cut/
#    style/color/material differs from the listing is a mismatch, even if it
#    also happens to affect how the item fits (e.g. "advertised as tapered,
#    mine has a wide leg opening" is a mismatch, not a sizing complaint).
#  - customer_service came back with zero clean hits in the hand-label pass:
#    reviews that merely state "I returned it" are describing a fit/quality
#    consequence, not a support complaint. Tightened to require an explicit
#    gripe about the support/return process or seller conduct, since without
#    that qualifier the label just re-catches fit/quality reviews.
PRIMARY_PROMPT = (
    "Classify the primary reason for this clothing/shoes/jewelry review into exactly one label: "
    "fit_issue (the item's size runs small/large/tight/loose relative to the size ordered — "
    "sizing-scale complaints only, not style or material), "
    "quality_defect (materials, construction, damage, falling apart, arrived used/damaged), "
    "shipping_delivery (late, lost, damaged in transit, packaging), "
    "customer_service (an explicit complaint about the support interaction, return/exchange "
    "process being difficult, or seller conduct — merely stating the item was returned does not "
    "qualify unless the process itself is criticized), "
    "product_as_described_mismatch (color, material, style, cut, sheerness, or any other "
    "attribute differs from the listing/photos — including cases where that mismatch happens to "
    "affect fit, e.g. a garment advertised as one cut but shaped like another), "
    "general_sentiment_other (anything else, including purely positive reviews)."
)
