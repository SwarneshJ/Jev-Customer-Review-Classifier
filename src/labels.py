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
}

PRIMARY_PROMPT = (
    "Classify the primary reason for this clothing/shoes/jewelry review into exactly one label: "
    "fit_issue (runs small/large, wrong size), quality_defect (materials, construction, damage), "
    "shipping_delivery (late, lost, packaging), customer_service (returns, support interactions), "
    "product_as_described_mismatch (item differs from listing/photos, not a fit or quality issue), "
    "general_sentiment_other (anything else, including purely positive reviews)."
)
