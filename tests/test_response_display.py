"""Response formatting cannot load source/model URLs or expose internal source IDs."""

from copy import deepcopy
from html.parser import HTMLParser

import pytest
from streamlit.testing.v1 import AppTest

from studentflow.response_display import (
    replace_source_references,
    response_source_references,
    safe_answer_html,
    source_reference_labels,
)

CHAT_FIRST = "a" * 64
CHAT_SECOND = "b" * 64
DOCUMENT = "c" * 64


def sources():
    # Deliberately reverse Chat insertion order: labels follow the messages.
    return {
        CHAT_SECOND: {"provider": "google_chat", "title": "StudentFlow Test",
                      "sent_at": "2026-10-26T02:00:00Z", "sender_name": "Maya",
                      "text": "Maya will prepare slides."},
        DOCUMENT: {"provider": "attachment", "title": "Meeting notes.pdf",
                   "text": "Meeting notes"},
        CHAT_FIRST: {"provider": "google_chat", "title": "StudentFlow Test",
                     "sent_at": "2026-10-26T01:00:00Z", "sender_name": "Alex",
                     "text": "We will use the survey dataset."},
    }


class ParsedHTML(HTMLParser):
    def __init__(self, value):
        super().__init__(convert_charrefs=True)
        self.elements = []
        self.text = []
        self.feed(value)

    def handle_starttag(self, tag, attrs):
        self.elements.append((tag, dict(attrs)))

    def handle_data(self, data):
        self.text.append(data)


def assert_inert(html):
    parsed = ParsedHTML(html)
    allowed = {
        "div", "p", "h1", "h2", "h3", "h4", "h5", "h6", "strong", "em", "s",
        "ul", "ol", "li", "blockquote", "br", "hr", "pre", "code", "table", "thead",
        "tbody", "tr", "th", "td",
    }
    for tag, attrs in parsed.elements:
        assert tag in allowed
        assert not attrs or (tag == "ol" and list(attrs) == ["start"] and attrs["start"].isdigit())
    return parsed


def test_summary_headings_emphasis_lists_and_table_have_readable_markup():
    answer = """## Discussion summary

The group **agreed** to use *existing data*.

- Clean the dataset.
- Prepare slides.

| Task | Owner | Deadline |
| --- | --- | --- |
| Check consent | Not specified | Not specified |

3. Confirm the presentation date.
4. Review the slides.
"""
    html = safe_answer_html(answer, {})
    parsed = assert_inert(html)
    assert "<h2>Discussion summary</h2>" in html
    assert "<strong>agreed</strong>" in html
    assert "<em>existing data</em>" in html
    assert "<ul>" in html and '<ol start="3">' in html
    assert "<table>" in html and "<td>Not specified</td>" in html
    assert "##" not in "".join(parsed.text) and "**" not in "".join(parsed.text)


@pytest.mark.parametrize("answer", [
    "![Pixel](https://tracker.example.invalid/open?token=secret)",
    "![Pixel][tracker]\n\n[tracker]: https://tracker.example.invalid/open",
    "![tracker][]\n\n[tracker]: https://tracker.example.invalid/open",
    "![tracker]\n\n[tracker]: https://tracker.example.invalid/open",
    "[![Pixel](https://tracker.example.invalid/image)](https://tracker.example.invalid/link)",
    "<img src='https://tracker.example.invalid/open' onerror='alert(1)'>",
    "<script src='https://tracker.example.invalid/code.js'></script>",
    "<iframe src='https://tracker.example.invalid/frame'></iframe>",
    "<style>body { background: url(https://tracker.example.invalid/pixel) }</style>",
    "<svg onload='alert(1)'><image href='https://tracker.example.invalid/pixel'/></svg>",
    "[Click](javascript:alert%281%29) and [file](file:///secret)",
    "[Safe-looking](https://tracker.example.invalid/link)",
    "<https://tracker.example.invalid/autolink>",
    "<a href='javascript:alert(1)'>Hello</a>",
    "&lt;img src=x onerror=alert(1)&gt;",
])
def test_model_or_source_markup_cannot_create_active_or_url_loading_elements(answer):
    assert_inert(safe_answer_html(answer, {}))


def test_reference_image_becomes_only_an_inert_description():
    html = safe_answer_html("![Meeting diagram][pic]\n\n[pic]: https://example.invalid/secret", {})
    assert "[Image: Meeting diagram]" in html
    assert "https://example.invalid/secret" not in html
    assert "<img" not in html


def test_html_and_code_are_shown_as_literal_text_without_execution():
    answer = """The code is `<img src=x onerror=alert(1)>`.

```html
<script>alert('test')</script>
```

<div onclick="alert(1)">Raw HTML</div>
"""
    html = safe_answer_html(answer, {})
    parsed = assert_inert(html)
    text = "".join(parsed.text)
    assert "<img src=x onerror=alert(1)>" in text
    assert "<script>alert('test')</script>" in text
    assert '<div onclick="alert(1)">Raw HTML</div>' in text
    assert "<pre><code>" in html


def test_source_hashes_become_stable_message_and_source_references_without_mutation():
    selected = sources()
    before = deepcopy(selected)
    answer = f"## Decisions\n\nAgreed **survey dataset** [source_id: `{CHAT_FIRST}`].\n\nSee {CHAT_SECOND} and {DOCUMENT}."
    html = safe_answer_html(answer, selected)
    assert source_reference_labels(selected) == {
        CHAT_FIRST: "Message 1", CHAT_SECOND: "Message 2", DOCUMENT: "Source 1",
    }
    assert "Message 1" in html and "Message 2" in html and "Source 1" in html
    assert all(identifier not in html for identifier in selected)
    assert selected == before
    assert CHAT_FIRST in answer  # Original stored answer remains unchanged.


def test_unknown_hashes_and_longer_identifiers_are_not_given_false_provenance():
    selected = sources()
    unknown = "d" * 64
    text = f"{unknown}; x{CHAT_FIRST}; {CHAT_FIRST}x; {CHAT_FIRST}-extra; ({CHAT_FIRST})"
    result = replace_source_references(text, selected)
    assert result == f"{unknown}; x{CHAT_FIRST}; {CHAT_FIRST}x; {CHAT_FIRST}-extra; (Message 1)"


def test_source_legend_uses_actual_sender_timestamp_and_title():
    references = response_source_references(sources())
    assert [item["label"] for item in references] == ["Message 1", "Message 2", "Source 1"]
    assert "2026-10-26 09:00:00 SGT · Alex" in references[0]["heading"]
    assert "2026-10-26 10:00:00 SGT · Maya" in references[1]["heading"]
    assert references[2]["heading"] == "Attachment · Meeting notes.pdf"
    assert all(item["source_id"] not in item["heading"] for item in references)


def test_plain_text_preserves_unicode_and_literal_comparisons():
    answer = "讨论记录：Alex & Maya\n\nChoose 2 < 3, then review."
    html = safe_answer_html(answer, {})
    parsed = assert_inert(html)
    assert "Alex & Maya" in "".join(parsed.text)
    assert "2 < 3" in "".join(parsed.text)


def test_bare_link_retains_text_without_creating_a_navigation_element():
    html = safe_answer_html("Read [the brief](https://example.invalid/brief) before the meeting.", {})
    assert "the brief" in html
    assert "https://example.invalid/brief" not in html
    assert_inert(html)


def test_streamlit_plain_response_keeps_existing_text_display():
    app = AppTest.from_string("""
from studentflow.response_display import render_response
render_response('The selected discussion was reviewed.', {})
""").run(timeout=15)
    assert not app.exception
    assert [item.value for item in app.text] == ["The selected discussion was reviewed."]


def test_short_confirmation_does_not_add_a_legend_for_unmentioned_selected_sources():
    app = AppTest.from_string(f"""
from studentflow.response_display import render_response
render_response('The discussion summary is ready below.', {sources()!r})
""").run(timeout=15)
    assert not app.exception
    assert not app.expander
    assert [item.value for item in app.text] == ["The discussion summary is ready below."]


def test_markdown_escapes_and_entities_are_decoded_in_the_display():
    app = AppTest.from_string(r"""
from studentflow.response_display import render_response
render_response(r'\*Example\* &amp; details', {})
""").run(timeout=15)
    assert not app.exception
    html = app.get("html")[0].proto.body
    assert "".join(assert_inert(html).text) == "*Example* & details"


def test_streamlit_formatted_response_and_legend_never_show_hash_or_active_image():
    app = AppTest.from_string(f"""
from studentflow.response_display import render_response
answer = '## Summary\\n\\n**Confirmed** by `{CHAT_FIRST}`. ![pixel][x]\\n\\n[x]: https://example.invalid/track'
sources = {{{CHAT_FIRST!r}: {{'provider': 'google_chat', 'title': 'Project discussion',
    'sent_at': '2026-10-26T01:00:00Z', 'sender_name': 'Alex'}}}}
render_response(answer, sources)
""").run(timeout=15)
    assert not app.exception
    assert app.expander[0].label == "Source references"
    assert [item.value for item in app.text] == [
        "Message 1 · Google Chat · Project discussion · 2026-10-26 09:00:00 SGT · Alex",
    ]
    elements = app.get("html")
    assert len(elements) == 1
    html = elements[0].proto.body
    assert "<h2>Summary</h2>" in html
    assert "<strong>Confirmed</strong>" in html
    assert "Message 1" in html and CHAT_FIRST not in html
    assert "https://example.invalid/track" not in html
    assert_inert(html)
