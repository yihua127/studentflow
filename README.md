# StudentFlow

Turn email into a clear study plan.

StudentFlow is a local productivity application that connects to Gmail, helps turn messages into reviewed tasks, and plans study time around personal availability.

## Use the app

- **Inbox (Gmail):** connect an account, browse messages and extract task suggestions from one or several selected emails.
- **Tasks:** review and save tasks with deadlines, estimated study time and supporting text.
- **Study Plan:** enter available time and commitments, generate a draft, then confirm before saving.
- **Attachments:** read PDF and Word attachments and search their source text.
- **Settings:** save an OpenAI API key on this device, choose a model and test the connection.

The interface is in English. Original emails, quotations and user-entered content retain their source language.

[User guide](docs/USER_GUIDE.md) · [Gmail setup](docs/GMAIL_SETUP.md) · [OpenAI setup](docs/OPENAI_SETUP.md)

## Run locally

Requirements: Python 3.12 and [uv](https://docs.astral.sh/uv/).

```powershell
uv sync --locked
uv run --locked streamlit run app.py --server.address 127.0.0.1 --browser.gatherUsageStats false
```

Open http://localhost:8501. On Windows, an existing installation can also be started with `start.ps1`.

### Desktop shortcut (Windows)

Run `powershell -NoProfile -ExecutionPolicy Bypass -File .\install-desktop-shortcut.ps1` from the project folder once. Then double-click **StudentFlow** on the desktop. The launcher starts the app in the background and opens the default browser, reusing an existing StudentFlow instance when available.

After updating the application, restart StudentFlow to load the new code. The saved Gmail sign-in is restored on startup.

Follow the [Gmail setup guide](docs/GMAIL_SETUP.md) to create a desktop OAuth client and save its configuration as `credentials/gmail_client.json`. Each person signs in on their own computer.

## Optional AI extraction

Open **Settings**, enter an OpenAI API key, choose a model and select **Save AI settings**. Select **Test AI connection** to check the saved configuration with a short example. Settings are stored in the local `.env` file. See the [OpenAI setup guide](docs/OPENAI_SETUP.md) for billing and API key steps.

Task extraction uses LangChain and the selected OpenAI model. In **Task extraction**, refresh the inbox, select one or more messages, give consent and choose **Extract tasks**. Review the suggestions together, edit their details and select the tasks to save. Each task retains its source message and supporting text. Successful results are reused within the current session when retrying the same messages with the same model.

Select **View selected messages** to read the chosen messages and load their attachments. Tasks can also be entered manually in the message review form.

## Data and permissions

- Gmail connections use the `gmail.readonly` permission.
- Gmail sign-in is remembered on this device and restored automatically when StudentFlow opens. **Disconnect Gmail** signs out of the device; **Switch account** selects another account.
- The inbox loads up to 20 messages at a time. Select **Load more** to append the next 20, or **Refresh inbox** to start again with the latest messages. Opening a selected message loads its text and attachments.
- Tasks, preferences, plans and sign-in data are stored locally in the `data/` directory.
- Disconnecting removes the local Gmail sign-in and clears opened messages. Previously saved tasks remain.

## Development and tests

```powershell
uv run --locked python -m pytest -q
```

Tests cover email parsing, attachment text extraction, task validation, scheduling and Gmail integration using synthetic inputs and mocked network services. Interface tests cover authorization, message opening, AI settings, batch extraction, task review and study planning.

Entry point: `app.py`. Application modules: `src/studentflow/`. Tests: `tests/`.
