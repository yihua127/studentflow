# StudentFlow

Turn email into a clear study plan.

StudentFlow is a local student productivity application built for DSS5105 Track 4. It connects to Gmail with read-only access, helps review tasks from messages, and plans confirmed work around personal availability.

## Use the app

- **Inbox (Gmail):** connect an account, refresh the inbox and open selected messages.
- **Tasks:** review and save tasks with deadlines, estimated study time and supporting text.
- **Study Plan:** enter available time and commitments, generate a draft, then confirm before saving.
- **Attachments:** read PDF and Word attachments and search their source text.

The interface is in English. Original emails, quotations and user-entered content retain their source language. There is no file-upload or development-sample picker in the application.

[User guide](docs/USER_GUIDE.md) · [Gmail setup](docs/GMAIL_SETUP.md)

## Run locally

Requirements: Python 3.12 and [uv](https://docs.astral.sh/uv/).

```powershell
uv sync --locked
uv run --locked streamlit run app.py --server.address 127.0.0.1 --browser.gatherUsageStats false
```

Open http://localhost:8501. On Windows, an existing installation can also be started with `start.ps1`.

Follow the [Gmail setup guide](docs/GMAIL_SETUP.md) to create a desktop OAuth client and save its configuration as `credentials/gmail_client.json`. Each person signs in on their own computer.

## Optional AI extraction

Copy `.env.example` to `.env` and configure `OPENAI_API_KEY` and `OPENAI_MODEL`. Task extraction uses LangChain and a configured OpenAI API model. It runs only after explicit consent for the selected message. Suggestions must be reviewed before saving.

Manual task entry, planning and attachment search work without a model API key. Gmail authorization is separate from AI model billing.

## Data and permissions

- Gmail requests only `gmail.readonly`. The app does not send or delete email or change read status.
- Inbox queries list at most 5–20 messages; opening a selected message fetches its text and attachments.
- Tasks, preferences, plans and OAuth tokens stay in the local `data/` directory. Credentials and local data are excluded from Git.
- Disconnecting removes the local Gmail sign-in and clears opened messages. Previously saved tasks remain.
- Availability and commitments are entered by the user. The app does not assume a supplied class timetable or sync a calendar automatically.
- Source messages, API keys, OAuth files, local databases and group answer files must not be committed.

## Current limits

AI extraction quality still needs evaluation with a configured model. Attachment search currently uses keywords, without generated RAG answers. Image/scanned-text recognition, live calendar synchronization, Teams integration, semantic cross-message deduplication and route-time lookup remain future work.

Personal Teams accounts do not support Microsoft Graph chat-message retrieval. Organization-controlled accounts may require administrator approval. [Track scope and integration notes](docs/课程要求与接入范围.md)

## Development and tests

```powershell
uv run --locked python -m pytest -q
```

Tests use synthetic inputs and mocked OAuth/network services. An optional local-material parser test skips when the private development pack is absent. Fixtures are kept outside the user interface and are not loaded into a model or retrieval index.

The interface tests cover consent, Gmail message opening, task review, English labels, personal planning and stale-plan invalidation. Automated tests are separate from live-account and model-quality validation.

Entry point: `app.py`. Application modules: `src/studentflow/`. Tests: `tests/`.
