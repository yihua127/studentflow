"""Diagnostics explain local validation failures without persisting source data."""

import json
from types import SimpleNamespace

import pytest

from studentflow.assistant_context import run_metadata
from studentflow.run_diagnostics import diagnostic_errors, diagnostic_events, safe_error_code


@pytest.mark.parametrize("code, wording", [
    ("invalid_citation", "quotation"),
    ("invalid_datetime", "timezone"),
    ("unsupported_owner", "owner"),
    ("invalid_tool_arguments", "format"),
    ("meeting_needs_correction", "correction"),
])
def test_known_failures_have_specific_safe_explanations(code, wording):
    events = [{"role": "meeting", "tool": "record_meeting_summary", "status": "error",
               "error_code": code, "arguments": "PRIVATE", "message": "PRIVATE"}]
    displayed = diagnostic_events(events)
    assert displayed[0]["error_code"] == code
    assert wording in displayed[0]["reason"]
    assert "PRIVATE" not in json.dumps(displayed)
    assert "reason" not in diagnostic_errors(events)[0]


@pytest.mark.parametrize("untrusted", ["PRIVATE api-key text", {"secret": "PRIVATE"}, None, 42])
def test_unknown_error_values_are_never_echoed(untrusted):
    assert safe_error_code(untrusted) == "tool_error"
    displayed = diagnostic_events([{
        "role": "PRIVATE", "tool": "PRIVATE", "name": "PRIVATE", "status": "error",
        "error_code": untrusted, "quote": "PRIVATE", "exception": "PRIVATE",
    }])
    assert displayed[0]["error_code"] == "runtime_error"
    assert "PRIVATE" not in json.dumps(displayed)


def test_usage_log_retains_validation_codes_but_no_payload_or_exception():
    result = SimpleNamespace(
        status="partial", usage={}, answer="PRIVATE", artifacts={"summary": "PRIVATE"},
        events=[
            {"role": "meeting", "status": "ok", "tool": "read_source", "text": "PRIVATE"},
            {"role": "meeting", "status": "error", "tool": "record_meeting_summary",
             "error_code": "invalid_citation", "arguments": "PRIVATE", "exception": "PRIVATE"},
            {"role": "meeting", "status": "error", "error_code": "meeting_needs_correction"},
        ],
    )
    metadata = run_metadata(result, "offline-model", "2026-10-26", 1.0)
    assert metadata["errors"] == [
        {"role": "meeting", "status": "error", "tool": "record_meeting_summary",
         "error_code": "invalid_citation"},
        {"role": "meeting", "status": "error", "error_code": "meeting_needs_correction"},
    ]
    assert metadata["tool_event_count"] == 3
    assert "PRIVATE" not in json.dumps(metadata)
