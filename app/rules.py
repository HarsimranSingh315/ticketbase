"""
Deterministic priority rules.
"""

HIGH_PRIORITY_KEYWORDS = [
    "down", "outage", "cannot access", "can't access", "urgent",
    "not working", "crashed", "data loss","charged twice","security", "breach",
]

LOW_PRIORITY_KEYWORDS = [
    "question", "how do i", "how to", "feature request", "suggestion",
]


def compute_priority(description: str) -> str:
    text = description.lower()

    if any(keyword in text for keyword in HIGH_PRIORITY_KEYWORDS):
        return "high"

    if any(keyword in text for keyword in LOW_PRIORITY_KEYWORDS):
        return "low"

    return "medium"
