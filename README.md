# StudentFlow

Turn email into a clear study plan.

StudentFlow is a local productivity application that connects to Gmail, helps turn messages into reviewed tasks, and plans study time around personal availability.

## Use the app

- **Inbox (Gmail):** connect an account, refresh the inbox, load more messages and open selected emails.
- **Tasks:** review and save tasks with deadlines, estimated study time and supporting text.
- **Study Plan:** enter available time and commitments, generate a draft, then confirm before saving.
- **Attachments:** read PDF and Word attachments and search their source text.

The interface is in English. Original emails, quotations and user-entered content retain their source language.

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

Tasks can also be entered manually in the message review form.

## Data and permissions

- Gmail connections use the `gmail.readonly` permission.
- Choose 5, 10 or 20 messages per batch. Select **Load more** to append the next batch, or **Refresh inbox** to start again with the latest messages. Opening a selected message loads its text and attachments.
- Tasks, preferences, plans and sign-in data are stored locally in the `data/` directory.
- Disconnecting removes the local Gmail sign-in and clears opened messages. Previously saved tasks remain.

## Development and tests

```powershell
uv run --locked python -m pytest -q
```

Tests cover email parsing, attachment text extraction, task validation, scheduling and Gmail integration using synthetic inputs and mocked network services. Interface tests cover authorization, message opening, task review and study planning.

Entry point: `app.py`. Application modules: `src/studentflow/`. Tests: `tests/`.
