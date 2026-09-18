"""
Deterministic priority rules.

This is intentionally NOT AI. Per the project brief: conventional code
should handle anything that can be handled with clear, explainable logic.
Priority routing is a good example - a human can read this function and
know exactly why a ticket got the priority it did, which matters for
trust and for debugging when someone asks "why was my ticket low priority?"

Keep this simple. Resist the urge to make it "smarter" with AI - that's
explicitly a non-goal for this piece of the system.
"""

HIGH_PRIORITY_KEYWORDS = [
    "down", "outage", "cannot access", "can't access", "urgent",
    "not working", "crashed", "data loss", "security", "breach",
]

LOW_PRIORITY_KEYWORDS = [
    "question", "how do i", "how to", "feature request", "suggestion",
]


def compute_priority(description: str) -> str:
    """
    Returns 'high', 'medium', or 'low' based on simple keyword matching.
    """
    text = description.lower()

    if any(keyword in text for keyword in HIGH_PRIORITY_KEYWORDS):
        return "high"

    if any(keyword in text for keyword in LOW_PRIORITY_KEYWORDS):
        return "low"

    return "medium"
