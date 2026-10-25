"""Minutes downloads preserve reviewed content without leaking uncited sources."""

from copy import deepcopy
from pathlib import PureWindowsPath

import pytest
from markdown_it import MarkdownIt

from studentflow.meeting_export import (
    meeting_minutes_document,
    meeting_minutes_filename,
    meeting_minutes_markdown,
    meeting_minutes_text,
)

MESSAGE_ONE = "a" * 64
MESSAGE_TWO = "b" * 64
PRIVATE_SOURCE = "c" * 64
ATTACHMENT = "d" * 64
FIRST_QUOTE = "We agreed to use the existing campus survey dataset."
ACTION_QUOTE = "Alex will clean the dataset by 27 October 2026 at 18:00."


def fixture():
    sources = {
        MESSAGE_TWO: {
            "provider": "google_chat", "title": "StudentFlow Test",
            "text": ACTION_QUOTE + "\nUnquoted paragraph must not be exported.",
            "sender_name": "Alex", "sender_id": "users/opaque-user-id",
            "sent_at": "2026-10-26T02:00:00Z", "account": "private-account@example.invalid",
            "resource_id": "spaces/opaque/messages/private-resource",
        },
        MESSAGE_ONE: {
            "provider": "google_chat", "title": "StudentFlow Test",
            "text": FIRST_QUOTE, "sender_name": "Maya", "sent_at": "2026-10-26T01:00:00Z",
        },
        PRIVATE_SOURCE: {
            "provider": "gmail", "title": "Private unselected-for-citation message",
            "text": "Highly confidential email body", "account": "other@example.invalid",
        },
        ATTACHMENT: {
            "provider": "attachment", "title": "consent-notes.pdf",
            "text": "The consent form needs review.\nDo not export the full document.",
        },
    }
    item = {
        "id": "internal-artifact-id", "title": "Campus survey project discussion",
        "summary": f"The group agreed to use existing data. See {MESSAGE_ONE}.",
        "citations": [{"source_id": MESSAGE_ONE, "quote": FIRST_QUOTE}],
        "decisions": [{"text": "Use the existing campus survey dataset.",
                       "citations": [{"source_id": MESSAGE_ONE, "quote": FIRST_QUOTE}]}],
        "action_items": [
            {"title": "Clean the campus survey dataset", "owner": "Alex",
             "due_at": "2026-10-27T18:00:00+08:00",
             "citations": [{"source_id": MESSAGE_TWO, "quote": ACTION_QUOTE}]},
            {"title": "Review the consent form", "owner": None, "due_at": None,
             "citations": [{"source_id": ATTACHMENT, "quote": "The consent form needs review."}]},
        ],
        "open_questions": ["When will the presentation take place?"],
        "missing_information": ["Owner and deadline for the consent review are not agreed."],
        "agenda": ["Confirm the presentation date.", "Review the survey slides."],
        "follow_up": "Hi team,\nPlease confirm the presentation date and consent-review owner.\nThank you.",
    }
    return item, sources


def markdown_content(markdown):
    tokens = MarkdownIt("commonmark", {"html": True}).parse(markdown)
    result = []

    def walk(items):
        for token in items:
            assert token.type not in {"image", "link_open", "html_inline", "html_block"}
            if token.children:
                walk(token.children)
            elif token.type in {"text", "code_inline", "code_block", "fence"}:
                result.append(token.content)

    walk(tokens)
    return "\n".join(result)


def test_text_and_markdown_contain_every_reviewed_section_and_same_facts():
    item, sources = fixture()
    text = meeting_minutes_text(item, sources)
    rendered_markdown = markdown_content(meeting_minutes_markdown(item, sources))
    for output in (text, rendered_markdown):
        for fact in (
            item["title"], "The group agreed to use existing data. See Message 1.",
            "Use the existing campus survey dataset.", "Clean the campus survey dataset",
            "Alex", "2026-10-27T18:00:00+08:00", "Review the consent form", "Not specified",
            item["open_questions"][0], item["missing_information"][0],
            *item["agenda"], *item["follow_up"].splitlines(), FIRST_QUOTE, ACTION_QUOTE,
            "The consent form needs review.", "2026-10-26 09:00:00 SGT", "Maya",
            "2026-10-26 10:00:00 SGT", "Message 1", "Message 2", "consent-notes.pdf",
        ):
            assert fact in output
        for heading in (
            "Summary", "Decisions", "Action items for review", "Open questions",
            "Details still to confirm", "Suggested agenda", "Follow-up draft", "Supporting text",
        ):
            assert heading in output


def test_canonical_content_is_shared_without_ids_accounts_or_entire_source_bodies():
    item, sources = fixture()
    original_item, original_sources = deepcopy(item), deepcopy(sources)
    document = meeting_minutes_document(item, sources)
    assert document["action_items"][1]["owner"] == "Not specified"
    assert document["action_items"][1]["deadline"] == "Not specified"
    assert document["summary_references"] == ["Message 1"]
    assert document["decisions"][0]["references"] == ["Message 1"]
    assert document["action_items"][0]["references"] == ["Message 2"]
    assert [entry["label"] for entry in document["supporting_text"]] == [
        "Message 1", "Message 2", "Source 2",
    ]
    assert document["supporting_text"][0]["quotes"] == [FIRST_QUOTE]
    assert item == original_item and sources == original_sources
    outputs = [str(document), meeting_minutes_text(item, sources), meeting_minutes_markdown(item, sources)]
    for output in outputs:
        for private in (
            *sources.keys(), "internal-artifact-id", "private-account@example.invalid",
            "users/opaque-user-id", "spaces/opaque/messages/private-resource",
            "Private unselected-for-citation message", "Highly confidential email body",
            "Unquoted paragraph must not be exported.", "Do not export the full document.",
        ):
            assert private not in output


@pytest.mark.parametrize("renderer", [meeting_minutes_text, meeting_minutes_markdown])
def test_partial_analysis_has_a_clear_warning_but_keeps_available_content(renderer):
    item, sources = fixture()
    partial = renderer(item, sources, incomplete=True)
    complete = renderer(item, sources)
    assert "Incomplete analysis" in partial
    assert "did not complete" in partial
    assert "Incomplete analysis" not in complete
    assert "Clean the campus survey dataset" in partial


def test_no_date_or_participants_are_invented_when_details_are_missing():
    item = {"title": "Meeting with missing details", "summary": "Only a proposal was discussed.",
            "action_items": [{"title": "Follow up", "owner": " ", "due_at": ""}]}
    document = meeting_minutes_document(item, {})
    assert document["action_items"][0] == {
        "title": "Follow up", "owner": "Not specified", "deadline": "Not specified", "references": [],
    }
    output = meeting_minutes_text(item, {})
    assert "None recorded." in output
    assert "No supporting quotations recorded." in output
    assert "2026" not in output and "Participants:" not in output
    assert document["follow_up"] == "Not specified"


def test_citation_deduplication_keeps_multiple_distinct_exact_quotes():
    item, sources = fixture()
    other = "First line.\n  Second line, with original spacing."
    item["citations"].extend([
        {"source_id": MESSAGE_ONE, "quote": FIRST_QUOTE},
        {"source_id": MESSAGE_ONE, "quote": other},
        {"source_id": "unavailable-source-id", "quote": "Unavailable content"},
    ])
    supporting = meeting_minutes_document(item, sources)["supporting_text"]
    assert supporting[0]["quotes"] == [FIRST_QUOTE, other]
    assert "Unavailable content" not in meeting_minutes_text(item, sources)


def test_missing_readable_sender_does_not_export_opaque_user_id():
    item, sources = fixture()
    sources[MESSAGE_TWO]["sender_name"] = None
    output = meeting_minutes_text(item, sources)
    assert "Sender not provided" in output
    assert "opaque-user-id" not in output


def test_markdown_and_html_in_model_or_quote_text_remain_inert_and_literal():
    hostile = (
        '![tracker][pixel]\n\n[pixel]: https://example.invalid/track\n'
        '<img src="https://example.invalid/track" onerror="alert(1)">\n'
        '[link](https://example.invalid/open) **literal emphasis** &amp; <script>alert(1)</script>'
    )
    item = {"title": "<img src=x> Test", "summary": hostile,
            "citations": [{"source_id": MESSAGE_ONE, "quote": hostile}],
            "follow_up": "```html\n<script>alert(1)</script>\n```"}
    sources = {MESSAGE_ONE: {"provider": "google_chat", "title": "![title](https://example.invalid/x)",
                             "text": hostile, "sender_name": "<img src=x>", "sent_at": None}}
    markdown = meeting_minutes_markdown(item, sources)
    rendered = markdown_content(markdown)
    assert '<img src="https://example.invalid/track" onerror="alert(1)">' in rendered
    assert "![tracker][pixel]" in rendered
    assert "**literal emphasis** &amp; <script>alert(1)</script>" in rendered
    assert "![title](https://example.invalid/x)" in rendered
    assert meeting_minutes_document(item, sources)["supporting_text"][0]["quotes"] == [hostile]


def test_unicode_and_long_content_are_exported_completely_without_preview_truncation():
    long_summary = "讨论记录：" + "完整内容 " * 850 + "最终决定。"
    long_follow_up = "后续消息：" + "review " * 850 + "END OF FOLLOW-UP"
    long_quote = "引用：" + "重要证据 " * 850 + "END OF QUOTE"
    item = {"title": "校园调查讨论 — 会议纪要", "summary": long_summary,
            "follow_up": long_follow_up, "citations": [{"source_id": MESSAGE_ONE, "quote": long_quote}]}
    sources = {MESSAGE_ONE: {"provider": "google_chat", "title": "测试讨论室", "text": long_quote,
                             "sender_name": "杨同学", "sent_at": "2026-10-26T09:00:00+08:00"}}
    for output in (meeting_minutes_text(item, sources), markdown_content(meeting_minutes_markdown(item, sources))):
        assert long_summary in output and long_follow_up in output and long_quote in output
        assert "校园调查讨论 — 会议纪要" in output and "杨同学" in output


@pytest.mark.parametrize("title", [
    "../../CON: report?*<>|", "NUL", "COM1", "PRN.txt", "AUX.", "..", "\x00\n\r\t",
    "报告" * 200, "x" * 600, "folder\\subfolder/report", "Emoji discussion 😀" * 80,
])
@pytest.mark.parametrize("extension", ["txt", "md", "docx"])
def test_filename_is_a_bounded_safe_windows_basename(title, extension):
    filename = meeting_minutes_filename({"title": title}, extension)
    assert filename.startswith("discussion-minutes") and filename.endswith("." + extension)
    assert PureWindowsPath(filename).name == filename
    assert not any(char in filename for char in '<>:"/\\|?*\x00\n\r\t')
    assert len(filename.encode("utf-8")) <= 190
    assert not filename.endswith((" ", "."))
    assert PureWindowsPath(filename).stem.upper() not in {"CON", "NUL", "PRN", "AUX", "COM1", "LPT1"}


def test_filename_preserves_ordinary_unicode_and_does_not_invent_a_date():
    assert meeting_minutes_filename({"title": "校园调查 讨论"}, ".DOCX") == "discussion-minutes-校园调查-讨论.docx"
    assert meeting_minutes_filename({}, "txt") == "discussion-minutes.txt"


@pytest.mark.parametrize("extension", ["pdf", "html", "../docx", "txt/../../file", ""])
def test_filename_rejects_unrequested_or_unsafe_extensions(extension):
    with pytest.raises(ValueError, match="TXT, Markdown or Word"):
        meeting_minutes_filename({"title": "Meeting"}, extension)
