"""Shared checks for unresolved reply templates, including user-edited drafts."""

from __future__ import annotations

import re
from email.errors import HeaderParseError
from email.header import decode_header, make_header

_MAX_SUBJECT_CHARS = 998
_ENCODED_WORD = re.compile(r"=\?[^?\s]+\?[bq]\?[^?]*\?=", re.IGNORECASE)


def _decoded_subject(value: str) -> str | None:
    if (not isinstance(value, str) or not value.strip() or len(value) > 8192
            or any(ord(character) < 32 or ord(character) == 127 for character in value)):
        return None
    # decode_header leaves malformed encoded words untouched rather than always
    # raising; never offer such text as a verified thread subject.
    if "=?" in _ENCODED_WORD.sub("", value):
        return None
    try:
        decoded = str(make_header(decode_header(value))).strip()
    except (HeaderParseError, LookupError, UnicodeError, ValueError, TypeError):
        return None
    if (not decoded or len(decoded) > _MAX_SUBJECT_CHARS
            or any(ord(character) < 32 or ord(character) == 127 for character in decoded)):
        return None
    return decoded


def _without_reply_prefix(value: str) -> str:
    while value.casefold().startswith("re:"):
        value = value[3:].lstrip()
    return value


def original_reply_subject(original: str) -> str | None:
    """Derive a safe reply title from a selected original, never model wording."""
    decoded = _decoded_subject(original)
    if decoded is None or not _without_reply_prefix(decoded):
        return None
    reply = decoded if decoded.casefold().startswith("re:") else "Re: " + decoded
    return reply if len(reply) <= _MAX_SUBJECT_CHARS else None


def reply_subject_matches(subject: str, original: str) -> bool:
    """Match safe subjects using Gmail sending's Re-prefix and casefold rules."""
    left, right = _decoded_subject(subject), _decoded_subject(original)
    if left is None or right is None:
        return False
    left, right = _without_reply_prefix(left), _without_reply_prefix(right)
    return bool(left and right) and left.casefold() == right.casefold()

_TOKENS = re.compile(r"\[[^\[\]\n]{1,100}\]|\{\{[^{}\n]{1,100}\}\}|<[^<>\n]{1,100}>")
_PLACEHOLDER = re.compile(
    r"(?:surname|first\s*name|last\s*name|full\s*name|your\s+name|recipient(?:\s+name)?|"
    r"sender(?:\s+name)?|teacher(?:\s+name)?|professor(?:\s+name)?|name|signature|"
    r"assignment(?:\s+(?:name|title|details))?|course(?:\s+(?:name|code))?|"
    r"project(?:\s+(?:name|title|details))?|deadline|due\s*date|date|time|(?:original\s+|current\s+)?subject|title|"
    r"topic|insert\b.*|enter\b.*|fill\b.*|your\b.*|todo|tbd|姓名|姓氏|老师姓名|"
    r"教师姓名|作业名称|截止日期|日期|时间|待填写|待补充|待定|\?+)", re.IGNORECASE,
)


def unresolved_placeholders(text: str) -> list[str]:
    """Recognize template markers without blocking citations such as [1] or [Smith 2026]."""
    found = []
    for match in _TOKENS.finditer(str(text or "")):
        token = match.group()
        content = token.strip("[]{}<>").strip().replace("_", " ").replace("-", " ")
        if token.startswith("{{") or _PLACEHOLDER.fullmatch(content):
            found.append(token)
    return list(dict.fromkeys(found))
