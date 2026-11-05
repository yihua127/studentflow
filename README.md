# StudentFlow

Turn email and discussions into a clear study plan.

StudentFlow is a local productivity application for students. It connects to Gmail, Google Chat and Google Calendar to help organize tasks, plan study time, draft replies and summarize discussions.

The interface is in English. Original messages, quotations and user-entered content retain their source language. A sidebar connects the workspaces; Inbox pairs a searchable, multi-select message list with reading, task extraction and category review. Discussions also has a direct sidebar entry.

## Features

- **Email categories and labels:** classify selected emails as Needs reply, Needs action or No action needed. Review evidence and correct categories, rename labels or add custom labels, then optionally preview and confirm Gmail updates.
- **Email to tasks:** identify flexible study tasks and fixed appointments from selected emails and readable PDF or Word attachments. Review deadlines, supporting text and suggested study time before saving.
- **Study planning:** plan around weekly availability, date exceptions, meals, breaks and fixed commitments. Track completed work and replan remaining tasks.
- **Calendar sync:** preview and sync saved study sessions and confirmed appointments to a dedicated StudentFlow calendar.
- **Email replies:** generate editable drafts using an optional writing sample, save reply preferences and send reviewed replies through Gmail.
- **Meetings and discussions:** select messages from a Google Chat space, summarize decisions and action items, and prepare agendas and follow-up drafts. Download minutes in Word, TXT or Markdown format.
- **Attachment library:** retain, search, download and remove saved attachments on your device.

## Getting started

Requirements:

- Python 3.12 (3.12.6 or later) or Python 3.13, plus [uv](https://docs.astral.sh/uv/).
- A Google account and a desktop OAuth client for the Google integrations you use.
- An OpenAI API key for AI features; API usage charges apply.

From the project folder, create the environment and start the app. On Windows with Python 3.13 installed:

```powershell
py -3.13 -m venv .venv
uv sync --locked
uv run --locked streamlit run app.py --server.address 127.0.0.1 --browser.gatherUsageStats false
```

Open http://localhost:8501.

1. Follow the [Gmail setup guide](docs/GMAIL_SETUP.md) to configure Google access and save the OAuth client file as `credentials/gmail_client.json`.
2. Open **Settings** to save your OpenAI API key, choose a model and test the connection. See [OpenAI setup](docs/OPENAI_SETUP.md).
3. Connect Gmail from **Inbox (Gmail)**. Connect Google Chat or Calendar when you use those features; sending replies, syncing Calendar and updating Gmail labels require separate authorization.

Each user configures and authorizes their own accounts on their computer.

### Windows desktop shortcut

Run this once from the project folder:

```powershell
powershell -NoProfile -ExecutionPolicy Bypass -File .\install-desktop-shortcut.ps1
```

Then open **StudentFlow** from the desktop. An existing installation can also be started with `start.ps1`.

## Using StudentFlow

### Organize tasks and plan study time

In **Inbox (Gmail)**, select messages and choose **Analyze selected messages**. Review task and appointment suggestions, adjust missing details and save the items you want to use. Tasks need a deadline and a positive study-time estimate before they can be confirmed for planning.

In **Tasks**, search or filter your task list and open a card to review its details, supporting text and progress. The full table and CSV export remain available.

In **Study Plan**, use **Availability & preferences** to set your weekly hours and date changes, and **Commitments & calendar** to manage fixed events. In **Schedule**, generate a plan and review its daily agenda or table before saving. The planner prefers whole-hour starts when feasible while respecting deadlines and scheduling constraints. Record completed study time and use **Replan remaining tasks** to update the schedule.

Use **Google Calendar sync** to preview changes before applying them. Sync is manual and one-way from StudentFlow to its dedicated calendar; edits made in Google Calendar are not imported.

### Draft email replies

Open a message in **Inbox → Read email** and choose **Reply to this email**, or use **Reply with assistant** from task/category review. In **Assistant → Email replies**, optionally provide a writing sample and save your reply preferences. Review and edit the subject and message, then download the draft. To send it, verify the recipient under **Send with Gmail** and explicitly confirm.

### Classify email

In **Inbox → Email categories**, select up to 30 loaded messages and allow classification. Review the suggested categories and supporting quotations, edit any category and save locally. Use **Filter by label** to filter the inbox list and **Show category results** to narrow the review table. **Needs reply** takes priority when a message also requests another action; the explanation preserves that action. **No action needed** means informational mail, not deletion or spam.

Use **Manage labels** to rename the three categories or create additional labels such as Coursework or Urgent. Under **Message details → Edit labels**, choose a primary category and any custom labels, then **Save label changes**. Custom labels are selected manually; renaming a category does not change its AI classification rule.

To show labels in Gmail, choose **Connect Gmail labels**, then **Preview Gmail label changes**. Confirm the exact changes and select **Apply Gmail labels**. Only the selected messages' registered `StudentFlow/` labels are adjusted; other labels and read status are preserved. Renamed labels replace their previous names on the selected messages after confirmation; other messages keep their existing labels until synced. Classification uses email text rather than attachments; incomplete results remain unclassified.

### Summarize meetings and discussions

In **Assistant → Meetings & discussions**, load a Google Chat space and select the messages to analyze. Review the summary, decisions, action items, open questions and supporting quotations. Use **Download minutes (Word)** to save an editable document, or choose TXT or Markdown.

Email replies and discussions have separate inputs and preferences. **Other assistance** supports broader task and planning questions using the information you select.

## Data and user control

- Account credentials, tasks, preferences, attachments and saved plans are stored locally. They are excluded from this repository.
- Loading information from Google does not automatically send it to OpenAI. AI processing requires consent for the selected information; task analysis may also use related saved task details for duplicate checks.
- Generated replies, estimates, tasks and summaries require review. Missing dates, owners and event times may need clarification.
- Sending email, applying Calendar changes and syncing Gmail labels require explicit user confirmation. The assistant does not send messages automatically.
- Disconnecting a Google account removes its local sign-in; previously saved tasks remain available.

## Scope

StudentFlow uses a coordinator with email, task, planning and meeting specialists. Supporting quotations are checked against selected sources, and schedules are checked against planning rules.

For detailed workflows and setup help, see the [User guide](docs/USER_GUIDE.md). Validation coverage and remaining limitations are documented in [Testing](docs/TESTING.md).

## Development

Run the offline test suite from the project folder:

```powershell
uv run --locked python -m pytest -q
```

Tests use synthetic inputs, scripted models and mocked external services. They do not replace live account integration checks.

Entry point: `app.py`. Application modules: `src/studentflow/`. Tests: `tests/`.
