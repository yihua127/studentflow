# StudentFlow

Turn email into a clear study plan.

StudentFlow is a local productivity application that connects to Gmail, helps turn messages into reviewed tasks, and plans study time around personal availability.

## Use the app

- **Inbox (Gmail):** connect an account, browse messages and review task or fixed-event suggestions from selected emails and readable attachments.
- **Tasks:** edit saved task details, track study progress, and browse or delete tasks in Unfinished and Completed.
- **Study Plan:** set weekly availability and date exceptions, review fixed appointments, replan remaining work, and explicitly sync saved sessions and events to a separate Google Calendar.
- **Attachments:** keep task-related originals and text on this device, search PDF and Word content, download files and delete saved attachments.
- **Settings:** save an OpenAI API key on this device, choose a model and test the connection.
- **Assistant:** separate Email replies, Meetings & discussions and Other assistance workspaces, each with its own inputs, selections and results; requests still use the coordinator and specialist tools.

The interface is in English. Original emails, quotations and user-entered content retain their source language.

[User guide](docs/USER_GUIDE.md) · [Gmail setup](docs/GMAIL_SETUP.md) · [OpenAI setup](docs/OPENAI_SETUP.md) · [Validation status](docs/TESTING.md)

## Run locally

Requirements: Python 3.12.6+ or 3.13 and [uv](https://docs.astral.sh/uv/). On Windows, use the official signed Python 3.13 distribution from [python.org](https://www.python.org/downloads/windows/). Create the environment with that interpreter before installing packages, so its launcher keeps the Python Software Foundation signature.

```powershell
py -3.13 -m venv .venv
uv sync --locked
uv run --locked streamlit run app.py --server.address 127.0.0.1 --browser.gatherUsageStats false
```

If Windows reports that an application-control policy blocked a file, check which file was blocked. A blocked Python launcher needs a trusted signed runtime and a rebuilt project environment; do not disable the device's security policy. Keep the `data/`, `credentials/` and `.env` files when repairing the environment. On managed devices, the administrator must approve the runtime.

Open http://localhost:8501. On Windows, an existing installation can also be started with `start.ps1`.

### Desktop shortcut (Windows)

Run `powershell -NoProfile -ExecutionPolicy Bypass -File .\install-desktop-shortcut.ps1` from the project folder once. Then double-click **StudentFlow** on the desktop. The launcher starts the app in the background and opens the default browser, reusing an existing StudentFlow instance when available.

The desktop shortcut checks for application updates and restarts the local app when its code changes. The saved Gmail sign-in is restored on startup. To restart explicitly, run `powershell -NoProfile -ExecutionPolicy Bypass -File .\launch.ps1 -Restart`.

Follow the [Gmail setup guide](docs/GMAIL_SETUP.md) to create a desktop OAuth client and save its configuration as `credentials/gmail_client.json`. Each person signs in on their own computer.

## Optional AI extraction

Open **Settings**, enter an OpenAI API key, choose a model and select **Save AI settings**. Select **Test AI connection** to check the saved configuration with a short example. Settings are stored in the local `.env` file. See the [OpenAI setup guide](docs/OPENAI_SETUP.md) for billing and API key steps.

In **Message analysis**, refresh the inbox, select one or more messages, give consent and choose **Analyze selected messages**. The app automatically opens their text and readable PDF/Word attachments and identifies both flexible study tasks and fixed appointments. One message can contain both; users do not need to classify it or open it first. Review and save each kind separately. **Confirmed** tasks require a deadline and estimated study time greater than zero. Each proposal retains its source message, supporting text and attachment reference where applicable.

Inbox analysis uses a bounded OpenAI Agents SDK manager with task and fixed-event specialists. Each specialist calls its own scoped analysis tool; those tools use the existing LangChain extraction, evidence checks, duplicate reconciliation and optional effort estimation. These are real agent tool calls. A failed branch leaves the other branch's results available for review. Neither tool saves tasks or appointments or writes to Calendar. The separate Assistant coordinator and its four specialists remain available for broader requests.

**Suggest study time when it is not stated** is enabled by default and can be turned off. It requests an AI estimate only when the source does not state a duration. Review the suggested minutes, reasoning and assumptions, then accept or edit the final **Study time (min)** before confirming and saving. This is total work time, including work already completed; only the saved total is used for planning. Set it to 0 to leave it unknown. **Tasks → Study-time estimate history** keeps the source-stated duration, AI suggestion and final reviewed value separate.

Successful source analysis is saved on this device and reused for the same account, content and model, including after restarting. Identical attachment copies share one task analysis and the same task identities. Complete unified results are also reused when the selected sources, model, related saved task details and analysis settings are unchanged; fixed-event results have their own source-bound cache. Select **Analyze completed messages again** when a fresh analysis is needed. Opening or refreshing a page does not start AI analysis. Inbox orchestration usage covers the manager and specialists only; nested extraction and estimation calls may incur additional usage.

**View selected messages** remains an optional way to read the chosen messages without AI analysis. Tasks can also be entered manually in the message review form.

Each message can produce several tasks, each with its own deadline. Repeated copies of an extracted task are combined for review, with supporting sources retained. Related suggestions from different sources or a fresh analysis are checked against each other and related saved tasks using their meaning and source evidence. Uncertain matches remain separate for review. Saving a recognized task again keeps its existing details and study progress. All retained quotations can be viewed under **Original supporting text** on the Tasks page.

## Track study progress

At a saved plan's daily study end time, StudentFlow asks how much of the planned work was completed. Confirm all planned minutes or enter actual minutes for each task. Saved progress reduces remaining study time; tasks with zero remaining minutes move to **Completed**. Select **Replan remaining tasks**, review the new draft and save it.

The app checks for daily reviews while its window is open and shows missed reviews on the next visit. Saved plans and progress remain available after restarting.

## Availability, fixed events and Calendar

Weekly availability repeats within a configurable planning horizon. Per-date additions, removals and whole-day exceptions modify that pattern. Rules and dated changes support editing or removal in groups. Expired dated entries are removed on opening or refreshing Study Plan; recurring rules, saved plans and progress are retained.

New study plans prefer whole-hour session starts, such as 13:00 instead of 12:14. This is a soft preference: the planner uses other start times when rounding would leave more work unfinished before its deadline. Task durations, breaks, meals, daily limits and exact fixed appointments still apply. The Study Plan page and Planning Agent use the same scheduling tool logic. Existing saved plans keep their reviewed times until a new plan is generated and saved.

Fixed-event extraction distinguishes attending an exam or presentation at a stated time from flexible preparation work. Exact source quotations and file/page references support proposals. A human must confirm complete start/end times before events are saved, used as planning constraints or synchronized. Missing event times are not guessed from deadlines or study-time estimates.

**Study Plan → Google Calendar sync** uses separate authorization and the [`calendar.app.created` scope](https://developers.google.com/workspace/calendar/api/auth) for a dedicated StudentFlow secondary calendar. Preview and confirm additions, updates and removals before syncing. Repeated syncs reuse app-owned events; past events and other calendars remain untouched. Sync is one-way and manual. Fixed-only sync is available when the saved study plan is stale and preserves existing study entries. The Google Calendar API must be enabled; real authorization and account behavior need end-to-end testing on the user's account.

## Assistant workspace

The Assistant uses an OpenAI Agents SDK coordinator with four specialists: email, tasks, planning and meetings. The coordinator selects specialists for each request; each specialist can call only its own validated tools. Source quotations are checked against the selected text, and study schedules use the existing planner and independent schedule validator.

1. Choose an **Assistant workspace**: **Email replies** for styled drafts, **Meetings & discussions** for summaries and action items, or **Other assistance** for task and planning questions. Load discussions through **Meetings & discussions → Google Chat discussions**.
2. Select the relevant sources. For a discussion, choose its space, select **Read selected discussion**, then select one or more **Messages to analyze**. Each option shows its time, sender and text preview; **Select all messages** and **Clear message selection** affect only messages. Optional supporting attachments have their own selector. Email replies uses its saved writing sample and reply request; Meetings & discussions has its own English request and never includes the email sample or email selection. Saved tasks, study preferences, loaded calendar commitments and academic dates are available under Other assistance.
3. Enter a request, consent to that selection being processed by OpenAI, and select **Run assistant**.
4. Review the response, supporting quotations and structured results. Reply drafts can be edited and downloaded. **Processing details** shows tool activity and reported usage.
5. For a selected saved task, open its suggestion details, review or edit **Total study time (min)**, check the review box and select **Save study time**. Then use **Study Plan** to generate and review a schedule with the saved total.

For a reply, select one Inbox message and choose **Reply with assistant**. This opens it, switches to **Assistant → Email replies** and preselects that email. **Save reply preferences** keeps your writing sample, its approved use and reusable request locally for this Gmail account; they reload on your next visit. Switching workspaces preserves unsaved inputs, edited drafts and separate results during the browser session, and requires fresh processing consent. Meeting requests cannot overwrite saved reply preferences. The coordinator and specialists receive a content-free selection inventory; the email specialist reads message content and writing samples with separate tools. Samples guide tone and signature, while current email facts supply names, subjects and deadlines. Draft wording, factual details and commitments still require review.

Reply delegation preserves the original user request and declares its operation. A reply must produce a validated local draft or a structured clarification; prose alone is not proof of a draft. One bounded continuation can record the outcome after the required source reads, sharing the original model, tool and time limits. It may request clarification instead of drafting, and cannot send messages or create Gmail drafts.

Discussion-summary delegation also preserves the original request and checks for a validated summary or specific clarification questions. If the specialist returns only prose after reading the evidence, one bounded continuation can record the outcome using the same shared limits. A complete summary requires the selected evidence to have been read; incomplete evidence cannot force a completed summary. The review card includes decisions, action items, open questions, missing information, an agenda and a follow-up draft. Formatted assistant answers use readable source labels and a restricted renderer that does not load model-generated images or active HTML.

If that recording continuation rejects generated fields, the meeting specialist can correct them once using the validation feedback. A successful, fully supported outcome resolves only earlier validation errors from the same delegation. A later rejected revision still leaves the analysis incomplete. Other failures and shared limits still apply. **Processing details** shows fixed error codes and safe explanations; local usage logs retain those codes without source text or raw provider exceptions.

Use **Download minutes (Word)** below the discussion summary to save an editable `.docx` with headings, an action-item table and supporting quotations. TXT and Markdown downloads are also available. Downloads use the displayed structured result without another model call. Unspecified owners and deadlines remain unspecified, and results from an incomplete run are marked in the file. Changing the request, selection or model hides outdated download buttons until a new result is generated.

Assistant can save explicitly reviewed study-time updates to selected existing tasks and reviewed fixed-event proposals. Study-time updates preserve the title, deadline and completed progress; a task changed since the suggestion was prepared must be reviewed again. New flexible tasks and other proposed changes remain previews, as do plans. Reply drafts can be edited and downloaded, or sent through **Send with Gmail** after separate sending authorization, recipient/conversation verification and explicit review. Agents have no sending tool. Calendar changes use the separate reviewed Study Plan flow. A change to the request, selected information or model hides stale results and requires renewed consent.

The task specialist's `record_fixed_event_proposals` tool validates source-backed appointment proposals. The planning specialist's `resolve_available_study_time` and scheduling tools calculate recurring availability, dated exceptions and selected confirmed fixed commitments. A scripted-model test exercises the real SDK coordinator, both specialists and their tool calls without a live model request.

Google Chat and Calendar use a separate local sign-in. **Connect Google** can validate and reuse the earlier permission-check authorization when present; otherwise it opens Google sign-in. Read permissions cover Gmail account identification, Chat spaces/messages and Calendar lists/events. Previously approved send/Meet scopes may be retained, but no sending or Meet transcript tool is exposed. Discussion attachments/cards are not analyzed. Native Meet transcription is not integrated.

Agent calls have shared time, model-call and tool-call limits. Provider tracing is disabled. `data/assistant_runs.jsonl` stores status, elapsed time and reported usage, without message content, tool arguments or credentials. Failed provider requests may have unreported billable usage. These logs measure runtime behavior, not extraction accuracy or time saved.

## Data and permissions

- Inbox connections use `gmail.readonly`. Sending has a separate `gmail_send_token.json`, requesting `gmail.send` plus OpenID/email identity to verify the same account. It never uses the read-only token for sending.
- Sending is bound to the reviewed recipient, subject, body and original conversation. A local send journal prevents repeat submission after success or an uncertain outcome. Check Gmail Sent after an uncertain response; the app does not automatically retry.
- Gmail sign-in is remembered on this device and restored automatically when StudentFlow opens. **Disconnect Gmail** signs out of the device; **Switch account** selects another account.
- The inbox loads up to 20 messages at a time. Select **Load more** to append the next 20, or **Refresh inbox** to start again with the latest messages. Opening a selected message loads its text and attachments.
- Tasks, progress, preferences, plans, source analysis, attachment originals and sign-in data are stored locally in the `data/` directory.
- Calendar sync has its own local token and synchronization journal. Only an explicitly confirmed preview writes to the selected account's app-owned calendar; no attendees or invitations are added.
- After consent, AI extraction uses selected message and attachment text and relevant saved task details to identify tasks and check related suggestions for duplicates.
- Attachments associated with extracted or saved tasks stay available until manually deleted. Other opened attachments can be kept using **Save attachment**.
- Disconnecting removes the local Gmail sign-in and clears opened messages. Previously saved tasks remain.

## Development and tests

```powershell
uv run --locked python -m pytest -q
```

Tests cover email parsing, attachment evidence and retention, task editing, progress accounting, scheduling and Gmail integration using synthetic inputs and mocked network services. Interface checks cover extraction, task review, editable preferences, daily progress and restart persistence.

Assistant tests run the real SDK orchestration with scripted models, selected-source tools, and mocked Google services. They cover role boundaries, evidence checks, limits, duplicate suggestions, failure rollback, account isolation and stale-result handling. No live API results are implied by these tests.

Entry point: `app.py`. Application modules: `src/studentflow/`. Tests: `tests/`.

## Published examples

The A/B scenarios in `config/scenarios.json` use fictional dates, events and rooms. They are development examples, not a personal timetable. Planning defaults are editable assumptions, not verified travel or calendar facts. Each user supplies their own availability and account connections.

Before publication, the 18-commit development history was backed up privately and rewritten to remove the personal commit email, original timetable examples and machine-specific setup path. Local credentials, messages, databases and generated minutes are excluded from this repository. Historical development notes retain their original scope and dates; see [Validation status](docs/TESTING.md) for the latest recorded checks.

No open-source license has been selected for this repository yet.
