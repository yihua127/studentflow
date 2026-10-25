"""Readable source labels that keep message identity separate from display text."""

from __future__ import annotations

from collections import Counter
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from zoneinfo import ZoneInfo

_SINGAPORE = ZoneInfo("Asia/Singapore")
_PREVIEW_LENGTH = 140


def _collapsed(value: object) -> str:
    return " ".join(str(value or "").split())


def _source_time(source: dict) -> datetime | None:
    value = source.get("sent_at")
    try:
        if isinstance(value, datetime):
            stamp = value
        elif isinstance(value, str) and value.strip():
            try:
                stamp = datetime.fromisoformat(value.strip())
            except ValueError:
                stamp = parsedate_to_datetime(value.strip())
        else:
            return None
        # A missing offset cannot safely be presented as Singapore time.
        if stamp.tzinfo is None or stamp.utcoffset() is None:
            return None
        return stamp.astimezone(_SINGAPORE)
    except (TypeError, ValueError, OverflowError):
        return None


def _time_label(source: dict) -> str:
    stamp = _source_time(source)
    return stamp.strftime("%Y-%m-%d %H:%M:%S SGT") if stamp else "Time not provided"


def _sender_label(source: dict) -> str:
    return (
        _collapsed(source.get("sender_name"))
        or _collapsed(source.get("sender_id"))
        or "Sender not provided"
    )


def _base_label(source: dict) -> str:
    provider = _collapsed(source.get("provider")).replace("_", " ").title() or "Source"
    return f"{provider} · {_collapsed(source.get('title')) or 'Untitled'}"


def chronological_sources(records: list[dict]) -> list[dict]:
    """Order known timestamps first, oldest first; preserve ties and unknown order."""
    latest = datetime.max.replace(tzinfo=UTC)
    return sorted(records, key=lambda source: _source_time(source) or latest)


def source_preview_heading(source: dict) -> str:
    """Identify preview evidence with actual Chat sender and time when available."""
    base = _base_label(source)
    if source.get("provider") == "google_chat":
        return f"{base} · {_time_label(source)} · {_sender_label(source)}"
    return base


def source_labels(records: list[dict]) -> dict[str, str]:
    """Return unique display labels keyed by stable normalized source IDs.

    Chat numbers follow chronological order, including stable ordering for equal
    timestamps. Labels never act as identifiers, so editing a message cannot
    redirect a selection to another message with matching text or metadata.
    """
    identifiers = [str(source.get("source_id") or "") for source in records]
    if not all(identifiers) or len(set(identifiers)) != len(identifiers):
        raise ValueError("Source records must have distinct, non-empty source IDs.")

    chat_ordinals = {
        str(source["source_id"]): index
        for index, source in enumerate(
            chronological_sources([
                source for source in records if source.get("provider") == "google_chat"
            ]), start=1,
        )
    }
    bases = [_base_label(source) for source in records]
    counts = Counter(bases)
    labels: dict[str, str] = {}
    used: set[str] = set()
    for index, (source, identifier, base) in enumerate(zip(records, identifiers, bases), start=1):
        if source.get("provider") == "google_chat":
            preview = _collapsed(source.get("text")) or "No readable text"
            if len(preview) > _PREVIEW_LENGTH:
                preview = preview[:_PREVIEW_LENGTH - 1].rstrip() + "…"
            label = (
                f"{chat_ordinals[identifier]}. {_time_label(source)} · "
                f"{_sender_label(source)} · {preview}"
            )
        elif counts[base] > 1:
            details = [_collapsed(source.get("sender"))]
            if _source_time(source):
                details.append(_time_label(source))
            label = " · ".join([base, *(part for part in details if part), f"Item {index}"])
        else:
            label = base
        # A title can itself contain another item's generated suffix; keep the
        # final labels distinct even for that unusual source content.
        while label in used:
            label += f" · Item {index}"
        labels[identifier] = label
        used.add(label)
    return labels
