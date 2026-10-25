"""Keep source effort, AI advice and a person's confirmed estimate separate."""

from __future__ import annotations

from datetime import UTC, datetime


def _minutes(value, label: str) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= 10000:
        raise ValueError(f"{label} must be a whole number from 0 to 10000.")
    return value or None


def _text(value, label: str, limit: int) -> str:
    if value is None:
        return ""
    if not isinstance(value, str) or len(value) > limit:
        raise ValueError(f"{label} must be text of at most {limit} characters.")
    return value.strip()


def _review(suggestion: dict, final_minutes: int | None, *, model: str) -> dict:
    if not isinstance(suggestion, dict):
        raise ValueError("The study-time suggestion must be a record.")  # noqa: TRY004
    stated = _minutes(suggestion.get("stated_minutes", suggestion.get("estimated_minutes")),
                      "Source-stated study time")
    suggested = _minutes(suggestion.get("suggested_minutes"), "Suggested study time")
    final = _minutes(final_minutes, "Confirmed study time")
    reason = _text(suggestion.get("estimate_reason"), "The estimate reason", 4000)
    if suggested is not None and not reason:
        raise ValueError("An AI study-time suggestion needs an estimate reason.")
    assumptions = suggestion.get("assumptions")
    if assumptions is None:
        assumptions = []
    if not isinstance(assumptions, list) or len(assumptions) > 20:
        raise ValueError("Estimate assumptions must be a list of at most 20 statements.")
    assumptions = [_text(item, "An estimate assumption", 2000) for item in assumptions]
    if any(not item for item in assumptions):
        raise ValueError("Estimate assumptions must contain nonempty text.")
    decision = "not_used"
    if suggested is not None and final is not None:
        decision = "accepted" if suggested == final else "adjusted"
    return {
        "stated_minutes": stated,
        "suggested_minutes": suggested,
        "estimate_reason": reason,
        "assumptions": assumptions,
        "model": _text(model, "The estimate model", 200),
        "confirmed_minutes": final,
        "decision": decision,
    }


def build_effort_review(suggestion: dict, final_minutes: int | None, *, model: str = "") -> dict:
    """Build a review only from the explicit final value supplied by the UI.

    Model-provided confirmation, decision, and review timestamps are ignored.
    Zero represents unknown effort, consistently with the existing task editor.
    """
    return _review(suggestion, final_minutes, model=model) | {
        "reviewed_at": datetime.now(UTC).isoformat(),
    }


def validate_effort_review(review: dict, estimated_minutes: int | None) -> dict:
    """Validate a locally built review before storing its official task value."""
    if not isinstance(review, dict):
        raise ValueError("The study-time review must be a record.")  # noqa: TRY004
    if "confirmed_minutes" not in review:
        raise ValueError("The study-time review needs an explicit confirmed value.")
    result = _review(review, review["confirmed_minutes"], model=review.get("model", ""))
    if result["confirmed_minutes"] != _minutes(estimated_minutes, "Estimated study time"):
        raise ValueError("The task estimate must match its confirmed study-time review.")
    if review.get("decision") != result["decision"]:
        raise ValueError("The study-time decision does not match the confirmed value.")
    timestamp = review.get("reviewed_at")
    try:
        parsed = datetime.fromisoformat(timestamp)
        if parsed.utcoffset() is None:
            raise ValueError
    except (ValueError, TypeError):
        raise ValueError("The study-time review needs a valid timestamp with a timezone.") from None
    result["reviewed_at"] = timestamp
    return result
