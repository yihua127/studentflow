"""Conservative course-code hints for task identity checks.

Compact IDs (DSS5105) are case-insensitive. Separated IDs require an uppercase
prefix (DSS 5105), unless immediately introduced by a course/module label.
This deliberately leaves ordinary lowercase prose such as ``about 200`` out.
These are lexical hints, not validation against an institution's course catalog.
"""

from __future__ import annotations

import re
import unicodedata

_CANDIDATE = re.compile(
    r"(?<![A-Za-z0-9_])(?P<prefix>[A-Za-z]{2,8})"
    r"(?P<separator>[ \t]*-[ \t]*|[ \t]+|)"
    r"(?P<number>\d{3,5}[A-Za-z]?)(?![A-Za-z0-9_])"
)
_EXPLICIT_LABEL = re.compile(
    r"\b(?:course|module)(?:[ \t]+(?:code|id|number))?[ \t]*[:#]?[ \t]*$",
    re.IGNORECASE,
)
_MONTH_NAMES = (
    "january", "february", "march", "april", "may", "june", "july",
    "august", "september", "october", "november", "december",
)
_DATE_OR_POSITION_LABELS = frozenset(_MONTH_NAMES) | {
    name[:3] for name in _MONTH_NAMES
} | {"sept", "year", "years", "page", "pages"}
_QUANTITY_UNIT = re.compile(
    r"^[ \t]*(?:-[ \t]*)?(?:words?|pages?|minutes?|hours?|days?|weeks?|months?|years?|%)"
    r"(?![A-Za-z])",
    re.IGNORECASE,
)


def course_codes(value: str) -> set[str]:
    """Return normalized course-code candidates without treating prose as IDs.

    Horizontal spaces and hyphens are accepted inside explicitly formatted IDs;
    line breaks never join an ordinary word to a number on the next line. Missing
    catalog context is intentionally not guessed for separated lowercase forms.
    """
    if not isinstance(value, str) or not value:
        return set()
    text = unicodedata.normalize("NFKC", value)
    result = set()
    for match in _CANDIDATE.finditer(text):
        prefix = match["prefix"]
        if prefix.casefold() in _DATE_OR_POSITION_LABELS:
            continue
        if _QUANTITY_UNIT.match(text[match.end():]):
            continue
        if (match["separator"] and not prefix.isupper()
                and not _EXPLICIT_LABEL.search(text[:match.start()])):
            continue
        result.add((prefix + match["number"]).casefold())
    return result
