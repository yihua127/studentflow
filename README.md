# StudentFlow

Turn email into a clear study plan.

StudentFlow is a local productivity application that connects to Gmail, helps turn messages into reviewed tasks, and plans study time around personal availability.

## Use the app

- **Inbox (Gmail):** connect an account, browse messages and extract task suggestions from one or several selected emails.
- **Tasks:** edit saved task details, track study progress, and browse or delete tasks in Unfinished and Completed.
- **Study Plan:** set availability, commitments and meal breaks, review a draft, and replan remaining work with earlier deadlines first.
- **Attachments:** keep task-related originals and text on this device, search PDF and Word content, download files and delete saved attachments.
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

Task extraction uses LangChain and the selected OpenAI model to analyze message text and readable PDF/Word attachments. In **Task extraction**, refresh the inbox, select one or more messages, give consent and choose **Extract tasks**. Review the suggestions together, edit their details and select the tasks to save. **Confirmed** requires a deadline and estimated study time greater than zero. Each task retains its source message, supporting text and attachment reference where applicable. Successful results are reused within the current session when retrying unchanged messages and attachments with the same model.

Select **View selected messages** to read the chosen messages and load their attachments. Tasks can also be entered manually in the message review form.

## Track study progress

At a saved plan's daily study end time, StudentFlow asks how much of the planned work was completed. Confirm all planned minutes or enter actual minutes for each task. Saved progress reduces remaining study time; tasks with zero remaining minutes move to **Completed**. Select **Replan remaining tasks**, review the new draft and save it.

The app checks for daily reviews while its window is open and shows missed reviews on the next visit. Saved plans and progress remain available after restarting.

## Data and permissions

- Gmail connections use the `gmail.readonly` permission.
- Gmail sign-in is remembered on this device and restored automatically when StudentFlow opens. **Disconnect Gmail** signs out of the device; **Switch account** selects another account.
- The inbox loads up to 20 messages at a time. Select **Load more** to append the next 20, or **Refresh inbox** to start again with the latest messages. Opening a selected message loads its text and attachments.
- Tasks, progress, preferences, plans, attachment originals and sign-in data are stored locally in the `data/` directory.
- Attachments associated with extracted or saved tasks stay available until manually deleted. Other opened attachments can be kept using **Save attachment**.
- Disconnecting removes the local Gmail sign-in and clears opened messages. Previously saved tasks remain.

## Development and tests

```powershell
uv run --locked python -m pytest -q
```

Tests cover email parsing, attachment evidence and retention, task editing, progress accounting, scheduling and Gmail integration using synthetic inputs and mocked network services. Interface checks cover extraction, task review, editable preferences, daily progress and restart persistence.

Entry point: `app.py`. Application modules: `src/studentflow/`. Tests: `tests/`.
