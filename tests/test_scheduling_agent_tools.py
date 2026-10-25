"""Real specialist function tools for fixed events and recurring availability."""

import json
from copy import deepcopy
from datetime import datetime, time

from agents import Model, ModelResponse, Usage
from openai.types.responses import (
    ResponseFunctionToolCall,
    ResponseOutputMessage,
    ResponseOutputText,
)

from studentflow.agent_runtime import run_assistant
from studentflow.agent_tools import AssistantTools
from studentflow.availability import dated_override, weekly_rule
from studentflow.planning_ui import default_preferences

NOW = "2030-01-07T08:00:00+08:00"
QUOTE = "DSS5105 exam is on 7 January 2030 from 10:00 to 11:00 Singapore time."


def snapshot():
    day = datetime.fromisoformat(NOW).date()
    preferences = default_preferences()
    preferences["weekly_availability"] = [weekly_rule([day.weekday()], time(9), time(12))]
    preferences["availability_overrides"] = [dated_override(day, "remove", time(10), time(11))]
    return {"now": NOW, "preferences": preferences, "sources": [
        {"source_id": "mail-1", "account": "student@example.invalid", "provider": "gmail",
         "resource_id": "mail", "text": QUOTE, "title": "Exam schedule"}],
        "tasks": [{"id": "t1", "title": "Revise", "status": "confirmed",
                   "due_at": "2030-01-08T18:00:00+08:00", "estimated_minutes": 60,
                   "completed_minutes": 0}],
        "fixed_events": [{"id": "meeting", "account": "student@example.invalid",
                          "title": "Meeting", "kind": "meeting", "status": "confirmed",
                          "start_at": "2030-01-07T09:00:00+08:00", "end_at": "2030-01-07T09:30:00+08:00",
                          "in_person": False, "travel_minutes": 0,
                          "sources": [{"account": "student@example.invalid", "source_quote": "Meeting at 9 to 9:30."}]}]}


def proposal():
    return {"title": "DSS5105 exam", "event_key": "DSS5105 exam", "kind": "exam",
            "course": "DSS5105", "start_at": "2030-01-07T10:00:00+08:00",
            "end_at": "2030-01-07T11:00:00+08:00", "duration_minutes": None,
            "time_basis": "absolute", "start_evidence": QUOTE, "end_evidence": QUOTE,
            "duration_evidence": None, "location": None, "in_person": None,
            "citations": [{"source_id": "mail-1", "quote": QUOTE}], "needs_clarification": []}


def test_tools_compute_real_windows_and_avoid_confirmed_fixed_events():
    context = snapshot()
    before = deepcopy(context)
    tools = AssistantTools(context)
    windows = tools.invoke("resolve_available_study_time", {}, "planning")
    assert windows["ok"]
    assert windows["windows"][0]["end"] == "2030-01-07T10:00:00+08:00"
    assert tools.invoke("list_fixed_events", {}, "task")["events"][0]["id"] == "meeting"
    result = tools.invoke("generate_study_plan", {}, "planning")
    assert result["ok"] and result["scheduled_minutes"] == 60
    blocks = tools.artifacts["plans"][0]["result"]["blocks"]
    assert blocks[0]["start"] == "2030-01-07T09:30:00+08:00"
    assert all(not (block["start"] < "2030-01-07T11:00:00+08:00"
                   and block["end"] > "2030-01-07T10:00:00+08:00") for block in blocks)
    assert context == before


def test_fixed_event_tools_cannot_invent_evidence_or_save_or_cross_roles():
    tools = AssistantTools(snapshot())
    args = {"proposals": [proposal()]}
    assert not tools.invoke("record_fixed_event_proposals", args, "email")["ok"]
    args["proposals"][0]["citations"][0]["quote"] = "Invented exam details"
    assert not tools.invoke("record_fixed_event_proposals", args, "task")["ok"]
    assert not tools.artifacts["fixed_event_proposals"]
    args = {"proposals": [proposal() | {"end_evidence": None}]}
    assert tools.invoke("record_fixed_event_proposals", args, "task")["ok"]
    event = tools.artifacts["fixed_event_proposals"][0]
    assert event["end_at"] is None and event["status"] == "needs_review" and event["saved"] is False
    assert not tools.invoke("sync_calendar", {}, "planning")["ok"]


def test_large_selected_availability_and_event_evidence_remain_pageable():
    context = snapshot()
    context["preferences"]["availability_horizon_days"] = 90
    context["preferences"]["availability_overrides"] = []
    context["preferences"]["weekly_availability"] = [
        weekly_rule(list(range(7)), time(hour), time(hour + 1)) for hour in (9, 12, 15, 18)
    ]
    original = context["fixed_events"][0]
    context["fixed_events"] = [
        deepcopy(original) | {"id": f"event-{index}", "sources": [
            {"account": original["account"], "source_quote": "Evidence " * 1000}]}
        for index in range(50)
    ]
    context["fixed_events"][-1].update(
        start_at="2030-01-07T09:30:00+08:00", end_at="2030-01-07T10:00:00+08:00",
    )
    tools = AssistantTools(context)
    first = tools.invoke("resolve_available_study_time", {"limit": 40}, "planning")
    assert first["ok"] and first["total"] == 360 and first["next_offset"] == 40
    last = tools.invoke("resolve_available_study_time", {"offset": 320, "limit": 40}, "planning")
    assert last["ok"] and len(last["windows"]) == 40 and last["next_offset"] is None
    events = tools.invoke("list_fixed_events", {}, "planning")
    assert events["ok"] and len(events["events"]) == 10 and events["total"] == 50
    assert events["next_offset"] == 10 and "sources" not in events["events"][0]
    last_events = tools.invoke("list_fixed_events", {"offset": 40}, "task")
    assert last_events["ok"] and last_events["next_offset"] is None
    planning = tools.invoke("get_planning_context", {}, "planning")
    assert planning["ok"] and planning["fixed_events_total"] == 50
    assert planning["fixed_events_next_offset"] == 10
    # The last event is absent from the first page but must still block 09:30.
    generated = tools.invoke("generate_study_plan", {}, "planning")
    assert generated["ok"]
    plan = tools.artifacts["plans"][0]
    assert len(plan["inputs"]["slots"]) == 360
    assert len(plan["inputs"]["busy"]) == 50
    assert plan["result"]["blocks"][0]["start"] == "2030-01-07T12:00:00+08:00"


def call(name, arguments, identifier):
    return ResponseFunctionToolCall(type="function_call", name=name,
                                    arguments=json.dumps(arguments), call_id=identifier)


def final(text):
    return ResponseOutputMessage(id="message", role="assistant", status="completed", type="message",
                                 content=[ResponseOutputText(type="output_text", text=text, annotations=[])])


class Scripted(Model):
    def __init__(self, outputs):
        self.outputs = list(outputs)

    async def get_response(self, *args, **kwargs):
        return ModelResponse(output=self.outputs.pop(0), response_id=None,
                             usage=Usage(requests=1, input_tokens=10, output_tokens=5, total_tokens=15))

    async def stream_response(self, *args, **kwargs):
        raise AssertionError("No streaming or network")
        yield


def test_real_manager_delegates_fixed_event_extraction_and_recurring_planning():
    scripted = {
        "manager": Scripted([
            [call("consult_task", {"input": "Extract the exam from mail-1"}, "m1")],
            [call("consult_planning", {"input": "Plan selected tasks around confirmed fixed events"}, "m2")],
            [final("日程待审核，学习计划已生成预览。")],
        ]),
        "task": Scripted([
            [call("read_source", {"source_id": "mail-1", "offset": 0, "limit": 8000}, "t1")],
            [call("record_fixed_event_proposals", {"proposals": [proposal()]}, "t2")],
            [final("Exam proposal recorded for review")],
        ]),
        "planning": Scripted([
            [call("resolve_available_study_time", {}, "p1")],
            [call("generate_study_plan", {"task_ids": None}, "p2")],
            [final("Draft computed using recurring availability")],
        ]),
        "email": Scripted([]), "meeting": Scripted([]),
    }
    box = AssistantTools(snapshot())
    result = run_assistant("提取考试并根据每周空闲安排学习", box, model_override=scripted)
    assert result.status == "completed"
    assert len(result.artifacts["fixed_event_proposals"]) == 1
    assert result.artifacts["fixed_event_proposals"][0]["saved"] is False
    assert result.artifacts["plans"][0]["validation_errors"] == []
    tools = {event.get("tool") for event in result.events if event.get("status") == "ok"}
    assert {"consult_task", "consult_planning", "record_fixed_event_proposals",
            "resolve_available_study_time", "generate_study_plan"} <= tools
