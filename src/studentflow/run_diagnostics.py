"""Fixed, content-free diagnostics for assistant results and local usage logs."""

from __future__ import annotations

ERROR_REASONS = {
    "invalid_citation": "A supporting quotation did not exactly match its selected source.",
    "unsupported_owner": "An action owner was not supported by its quotation.",
    "invalid_datetime": "A deadline was not a valid date and time with a timezone.",
    "invalid_meeting_detail": "A discussion question or missing-information note was blank.",
    "invalid_question": "A clarification question was blank.",
    "invalid_tool_arguments": "The generated tool fields did not match the required format.",
    "invalid_arguments": "The generated tool fields did not match the required format.",
    "invalid_data": "The selected data or generated fields could not be validated.",
    "source_out_of_scope": "The operation referenced information outside the selected sources.",
    "task_out_of_scope": "The operation referenced a task outside the selected tasks.",
    "tool_not_allowed": "The specialist requested a tool outside its permitted role.",
    "unresolved_placeholders": "The reply contained unresolved template fields.",
    "unknown_recipient": "The reply recipient could not be verified from the selected email.",
    "invalid_title": "The proposed task had no usable title.",
    "invalid_relation": "The proposed task relationship was invalid.",
    "missing_estimate_reason": "A study-time estimate had no explanation.",
    "ambiguous_estimate": "Source-stated time and estimated study time were not separated.",
    "missing_source_account": "The source account for the appointment was unavailable.",
    "invalid_calendar_coverage": "The loaded calendar date range was invalid.",
    "calendar_range_incomplete": "The loaded calendar did not cover the planning range.",
    "input_limit": "The generated tool input exceeded its size limit.",
    "output_limit": "The tool result exceeded its size limit.",
    "call_limit": "The local tool-call limit was reached.",
    "model_call_limit": "The shared model-call limit was reached.",
    "tool_call_limit": "The shared tool-call limit was reached.",
    "time_limit": "The analysis reached its time limit.",
    "turn_limit": "The coordinator reached its turn limit.",
    "specialist_turn_limit": "A specialist reached its turn limit.",
    "missing_meeting_summary": "No validated discussion summary or clarification was recorded.",
    "incomplete_meeting_evidence": "Some selected discussion information was not fully read.",
    "meeting_needs_correction": "The discussion result still needs correction before recording.",
    "missing_reply_draft": "No validated reply draft was recorded.",
    "reply_needs_correction": "The reply still needs correction before recording.",
    "empty_output": "The analysis returned no answer.",
    "model_error": "The model request could not complete.",
    "tool_error": "A tool operation could not complete.",
    "tool_failed": "A tool operation could not complete.",
    "specialist_error": "A specialist could not complete its analysis.",
    "runtime_error": "The assistant encountered an unexpected processing error.",
    "cleanup_error": "The model connection could not be closed normally.",
}
_ROLES = {"manager", "email", "task", "planning", "meeting", "unknown"}
_TOOLS = {
    "consult_email", "consult_task", "consult_planning", "consult_meeting",
    "list_sources", "read_source", "search_tasks", "get_academic_dates", "calculate_date",
    "list_fixed_events", "get_style_samples", "record_triage", "record_reply_draft",
    "request_reply_clarification", "record_task_proposals", "record_fixed_event_proposals",
    "check_deadline_risks", "get_planning_context", "resolve_available_study_time",
    "generate_study_plan", "record_meeting_summary", "request_meeting_clarification",
}
_STATUSES = {"started", "ok", "error", "repairing", "recovered"}


def safe_error_code(value: object, fallback: str = "tool_error") -> str:
    """Never allow arbitrary exception text or source content into diagnostics."""
    return value if isinstance(value, str) and value in ERROR_REASONS else fallback


def diagnostic_events(events: list[dict]) -> list[dict]:
    result = []
    for event in events:
        if not isinstance(event, dict):
            continue
        row = {}
        for key, allowed in (("role", _ROLES), ("tool", _TOOLS), ("status", _STATUSES)):
            value = event.get(key)
            if isinstance(value, str) and value in allowed:
                row[key] = value
        if "error_code" in event:
            code = safe_error_code(event["error_code"], "runtime_error")
            row.update(error_code=code, reason=ERROR_REASONS[code])
        if row:
            result.append(row)
    return result


def diagnostic_errors(events: list[dict]) -> list[dict]:
    """Store only fixed identifiers, never provider errors, quotes or arguments."""
    return [{key: value for key, value in event.items() if key != "reason"}
            for event in diagnostic_events(events)
            if event.get("status") == "error" and event.get("error_code")]
