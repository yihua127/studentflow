"""Readable assistant text with inert formatting and human-readable source references.

Only tags generated here reach the HTML renderer. Model/source HTML is escaped;
Markdown links retain their visible text, and images retain an inert description.
No URL-bearing attributes, remote resources, or model-supplied HTML are rendered.
"""

from __future__ import annotations

import re
from html import escape

from markdown_it import MarkdownIt
from markdown_it.token import Token

from studentflow.source_labels import chronological_sources, source_preview_heading

_MARKDOWN = MarkdownIt("commonmark", {"html": True, "linkify": False}).enable(
    ["table", "strikethrough"]
)
_PAIRED_TAGS = {
    "paragraph": "p", "blockquote": "blockquote", "bullet_list": "ul",
    "ordered_list": "ol", "list_item": "li", "strong": "strong", "em": "em",
    "s": "s", "table": "table", "thead": "thead", "tbody": "tbody",
    "tr": "tr", "th": "th", "td": "td",
}


def source_reference_labels(sources: dict) -> dict[str, str]:
    """Map selected source identities to short labels without changing evidence."""
    chat = []
    other = []
    for identifier, source in sources.items():
        # The selected snapshot's mapping key is authoritative. Copy only for
        # sorting; the original source records and tool citations remain intact.
        record = dict(source, source_id=str(identifier))
        (chat if source.get("provider") == "google_chat" else other).append(record)
    labels = {
        source["source_id"]: f"Message {index}"
        for index, source in enumerate(chronological_sources(chat), 1)
    }
    labels.update({source["source_id"]: f"Source {index}" for index, source in enumerate(other, 1)})
    return labels


def _reference_pattern(labels: dict[str, str]) -> re.Pattern | None:
    identifiers = sorted((identifier for identifier in labels if identifier), key=len, reverse=True)
    if not identifiers:
        return None
    # Avoid replacing an identifier that is merely a prefix of another token.
    # Real source IDs are SHA-256 strings; escaping also supports test/import IDs.
    alternatives = "|".join(re.escape(identifier) for identifier in identifiers)
    return re.compile(r"(?<![\w-])(?:" + alternatives + r")(?![\w-])")


def replace_source_references(answer: str, sources: dict) -> str:
    """Replace only exact known source IDs in presentation text, never stored text."""
    labels = source_reference_labels(sources)
    pattern = _reference_pattern(labels)
    return pattern.sub(lambda match: labels[match.group()], answer) if pattern else answer


def response_source_references(sources: dict) -> list[dict[str, str]]:
    """Provide a readable legend; callers must not display the internal source_id."""
    labels = source_reference_labels(sources)
    return [
        {"source_id": identifier, "label": label,
         "heading": source_preview_heading(sources[identifier])}
        for identifier, label in labels.items()
    ]


def _tokens_html(tokens: list[Token]) -> str:
    result = []
    for token in tokens:
        kind = token.type
        if kind == "inline":
            result.append(_tokens_html(token.children or []))
        elif kind in {"text", "html_inline"}:
            result.append(escape(token.content, quote=False))
        elif kind in {"html_block", "code_block", "fence"}:
            result.append("<pre><code>" + escape(token.content, quote=False) + "</code></pre>")
        elif kind == "code_inline":
            result.append("<code>" + escape(token.content, quote=False) + "</code>")
        elif kind == "softbreak":
            result.append("\n")
        elif kind == "hardbreak":
            result.append("<br>")
        elif kind == "hr":
            result.append("<hr>")
        elif kind == "image":
            # This handles inline, reference-style and nested Markdown images.
            # Do not retain src, title, or any other model-supplied attributes.
            alt = token.content.strip()
            result.append("[Image" + (": " + escape(alt, quote=False) if alt else "") + "]")
        elif kind in {"link_open", "link_close"}:
            # Keep the link's children as ordinary text, including autolinks.
            continue
        elif kind in {"heading_open", "heading_close"}:
            tag = token.tag if token.tag in {"h1", "h2", "h3", "h4", "h5", "h6"} else "p"
            result.append(f"<{('/' if kind.endswith('_close') else '')}{tag}>")
        elif kind.endswith(("_open", "_close")):
            base, side = kind.rsplit("_", 1)
            tag = _PAIRED_TAGS.get(base)
            if tag:
                attribute = ""
                if base == "ordered_list" and side == "open":
                    start = str(token.attrGet("start") or "")
                    if start.isascii() and start.isdigit():
                        attribute = f' start="{int(start)}"'
                result.append(f"<{('/' if side == 'close' else '')}{tag}{attribute}>")
        elif token.content:
            # New/unknown parser extensions cannot introduce active markup.
            result.append(escape(token.content, quote=False))
    return "".join(result)


def safe_answer_html(answer: str, sources: dict) -> str:
    """Render a safe subset of Markdown, using friendly selected-source labels."""
    display = replace_source_references(answer, sources)
    return "<div>" + _tokens_html(_MARKDOWN.parse(display)) + "</div>"


def _is_plain_text(tokens: list[Token], original: str) -> bool:
    paragraphs = []
    for token in tokens:
        if token.type not in {"paragraph_open", "paragraph_close", "inline"}:
            return False
        if token.type == "inline":
            children = token.children or []
            if any(child.type not in {"text", "softbreak"} for child in children):
                return False
            paragraphs.append("".join(
                "\n" if child.type == "softbreak" else child.content for child in children
            ))
    # Escapes and entities can parse into plain text too, but should be displayed
    # as their decoded text rather than showing raw Markdown escape characters.
    return "\n\n".join(paragraphs) == original.strip("\n")


def render_response(answer: str, sources: dict) -> None:
    """Display formatted response text and a source legend without loading URLs."""
    import streamlit as st

    labels = source_reference_labels(sources)
    pattern = _reference_pattern(labels)
    mentioned = {match.group() for match in pattern.finditer(answer)} if pattern else set()
    display = replace_source_references(answer, sources)
    tokens = _MARKDOWN.parse(display)
    if not mentioned and _is_plain_text(tokens, display):
        st.text(display)
    else:
        # st.html avoids a second Markdown parse of our already escaped text.
        # Every rendered tag is generated by the allowlist above; no user/model
        # attributes, links, raw HTML, CSS, images or scripts pass through.
        st.html("<div>" + _tokens_html(tokens) + "</div>")
    if mentioned:
        with st.expander("Source references"):
            for reference in response_source_references(sources):
                if reference["source_id"] in mentioned:
                    st.text(f"{reference['label']} · {reference['heading']}")
