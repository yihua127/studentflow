"""Shared checks for unresolved reply templates, including user-edited drafts."""

from __future__ import annotations

import re

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
