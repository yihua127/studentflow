"""Deterministic, source-scoped discussion minutes for local downloads."""

from __future__ import annotations

import re
import string
import unicodedata

from studentflow.response_display import replace_source_references, source_reference_labels
from studentflow.source_labels import source_preview_heading

_INCOMPLETE_WARNING = (
    "Incomplete analysis: this analysis did not complete. Review these minutes "
    "against the discussion and confirm any missing information."
)
_MARKDOWN_PUNCTUATION = re.compile("[" + re.escape(string.punctuation) + "]")
_UNSAFE_FILENAME = re.compile(r'[<>:"/\\|?*]')


def _value(value: object, default: str = "Not specified") -> str:
    return value if isinstance(value, str) and value.strip() else default


def meeting_minutes_document(item: dict, sources: dict, *, incomplete: bool = False) -> dict:
    """Build shared plain-text content for Word, TXT and Markdown renderers.

    Only selected, cited source headers and quotations enter supporting_text.
    Internal IDs remain lookup keys only. Quotes retain their original text;
    known source references in generated prose use the same labels as the UI.
    """
    labels = source_reference_labels(sources)
    quotes: dict[str, list[str]] = {}

    def content(value: object, default: str = "Not specified") -> str:
        return replace_source_references(_value(value, default), sources)

    def citations(values: object) -> list[str]:
        references = []
        for citation in values if isinstance(values, list) else []:
            if not isinstance(citation, dict):
                continue
            identifier = citation.get("source_id")
            quote = citation.get("quote")
            if identifier not in labels or not isinstance(quote, str) or not quote.strip():
                continue
            label = labels[identifier]
            if label not in references:
                references.append(label)
            excerpts = quotes.setdefault(identifier, [])
            if quote not in excerpts:
                excerpts.append(quote)
        return references

    def text_list(key: str) -> list[str]:
        return [content(value) for value in item.get(key, [])
                if isinstance(value, str) and value.strip()]

    summary_references = citations(item.get("citations", []))
    decisions = [
        {"text": content(decision.get("text")),
         "references": citations(decision.get("citations", []))}
        for decision in item.get("decisions", []) if isinstance(decision, dict)
    ]
    actions = [
        {"title": content(action.get("title")), "owner": content(action.get("owner")),
         "deadline": content(action.get("due_at")),
         "references": citations(action.get("citations", []))}
        for action in item.get("action_items", []) if isinstance(action, dict)
    ]
    supporting = []
    for identifier, label in labels.items():
        if identifier not in quotes:
            continue
        # A user ID is useful internally but is not a readable person's name.
        # Keep the actual sender name, and explicitly mark a missing name.
        source = dict(sources[identifier], sender_id="")
        supporting.append({
            "label": label, "heading": source_preview_heading(source),
            "quotes": list(quotes[identifier]),
        })
    return {
        "title": content(item.get("title"), "Discussion minutes"),
        "incomplete": bool(incomplete),
        "warning": _INCOMPLETE_WARNING if incomplete else None,
        "summary": content(item.get("summary")), "summary_references": summary_references,
        "decisions": decisions, "action_items": actions,
        "open_questions": text_list("open_questions"),
        "missing_information": text_list("missing_information"),
        "agenda": text_list("agenda"), "follow_up": content(item.get("follow_up")),
        "supporting_text": supporting,
    }


def _source_line(references: list[str]) -> str:
    return "Supporting sources: " + ", ".join(references)


def meeting_minutes_text(item: dict, sources: dict, *, incomplete: bool = False) -> str:
    """Create full readable text; no files or remote records are written."""
    document = meeting_minutes_document(item, sources, incomplete=incomplete)
    lines = ["Discussion minutes", "Title: " + document["title"]]
    if document["warning"]:
        lines.extend(["", document["warning"]])
    lines.extend(["", "Summary", document["summary"]])
    if document["summary_references"]:
        lines.append(_source_line(document["summary_references"]))
    lines.extend(["", "Decisions"])
    for index, decision in enumerate(document["decisions"], 1):
        lines.append(f"{index}. {decision['text']}")
        if decision["references"]:
            lines.append("   " + _source_line(decision["references"]))
    if not document["decisions"]:
        lines.append("None recorded.")
    lines.extend(["", "Action items for review"])
    for index, action in enumerate(document["action_items"], 1):
        lines.extend([
            f"{index}. {action['title']}", "   Owner: " + action["owner"],
            "   Deadline: " + action["deadline"],
        ])
        if action["references"]:
            lines.append("   " + _source_line(action["references"]))
    if not document["action_items"]:
        lines.append("None recorded.")
    for key, heading in (
        ("open_questions", "Open questions"),
        ("missing_information", "Details still to confirm"),
        ("agenda", "Suggested agenda"),
    ):
        lines.extend(["", heading])
        lines.extend(f"{index}. {text}" for index, text in enumerate(document[key], 1))
        if not document[key]:
            lines.append("None recorded.")
    lines.extend(["", "Follow-up draft", document["follow_up"], "", "Supporting text"])
    for source in document["supporting_text"]:
        lines.extend(["", source["label"] + " · " + source["heading"]])
        for index, quote in enumerate(source["quotes"], 1):
            lines.append(f"Quote {index}:")
            lines.extend("    " + line for line in quote.splitlines())
    if not document["supporting_text"]:
        lines.append("No supporting quotations recorded.")
    return "\n".join(lines) + "\n"


def _markdown_literal(value: str) -> str:
    """Keep external text literal; only our fixed document structure is Markdown."""
    return _MARKDOWN_PUNCTUATION.sub(lambda match: "\\" + match.group(), value)


def meeting_minutes_markdown(item: dict, sources: dict, *, incomplete: bool = False) -> str:
    """Create Markdown with inert model/source text and no embedded resources."""
    document = meeting_minutes_document(item, sources, incomplete=incomplete)
    literal = _markdown_literal
    lines = ["# Discussion minutes", "", "**Title:** " + literal(document["title"])]
    if document["warning"]:
        lines.extend(["", "**" + literal(document["warning"]) + "**"])
    lines.extend(["", "## Summary", "", literal(document["summary"])])
    if document["summary_references"]:
        lines.extend(["", literal(_source_line(document["summary_references"]))])
    lines.extend(["", "## Decisions", ""])
    for index, decision in enumerate(document["decisions"], 1):
        lines.append(f"{index}. {literal(decision['text'])}")
        if decision["references"]:
            lines.append("   " + literal(_source_line(decision["references"])))
    if not document["decisions"]:
        lines.append("None recorded.")
    lines.extend(["", "## Action items for review", ""])
    for index, action in enumerate(document["action_items"], 1):
        lines.extend([
            f"{index}. {literal(action['title'])}",
            "   - **Owner:** " + literal(action["owner"]),
            "   - **Deadline:** " + literal(action["deadline"]),
        ])
        if action["references"]:
            lines.append("   - " + literal(_source_line(action["references"])))
    if not document["action_items"]:
        lines.append("None recorded.")
    for key, heading in (
        ("open_questions", "Open questions"),
        ("missing_information", "Details still to confirm"),
        ("agenda", "Suggested agenda"),
    ):
        lines.extend(["", "## " + heading, ""])
        lines.extend(f"{index}. {literal(text)}" for index, text in enumerate(document[key], 1))
        if not document[key]:
            lines.append("None recorded.")
    lines.extend(["", "## Follow-up draft", "", literal(document["follow_up"]),
                  "", "## Supporting text", ""])
    for source in document["supporting_text"]:
        lines.extend(["### " + literal(source["label"]), "", literal(source["heading"]), ""])
        for index, quote in enumerate(source["quotes"], 1):
            lines.extend([f"Quote {index}:", ""])
            lines.extend("> " + literal(line) for line in quote.splitlines())
            lines.append("")
    if not document["supporting_text"]:
        lines.append("No supporting quotations recorded.")
    return "\n".join(lines).rstrip() + "\n"


def meeting_minutes_filename(item: dict, extension: str) -> str:
    """Return a bounded Windows-safe basename, preserving readable Unicode."""
    suffix = extension.removeprefix(".").casefold()
    if suffix not in {"txt", "md", "docx"}:
        raise ValueError("Choose a TXT, Markdown or Word filename extension.")
    title = _value(item.get("title"), "")
    title = "".join(" " if unicodedata.category(char).startswith("C") else char for char in title)
    title = _UNSAFE_FILENAME.sub(" ", title)
    title = re.sub(r"\s+", "-", title).strip(" .-_")
    # Bound bytes as well as code points so a Unicode filename remains portable.
    title = title[:80]
    while len(title.encode("utf-8")) > 160:
        title = title[:-1]
    title = title.rstrip(" .-_")
    return "discussion-minutes" + ("-" + title if title else "") + "." + suffix
