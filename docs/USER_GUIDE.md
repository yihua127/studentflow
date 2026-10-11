# Using StudentFlow

## Inbox (Gmail)

Connect Gmail once on your device. StudentFlow restores the saved connection when you reopen the window or restart the app, and shows **Gmail connected**. Select **Switch account** to use another account or **Disconnect Gmail** to sign out of this device.

In **Task extraction**, refresh the inbox and choose one or more messages. Select **View selected messages** to read their text and load attachments locally. AI extraction sends the selected messages' subjects, text, dates and readable attachment text to the configured model after your consent.

If the connection is temporarily unavailable, select **Retry connection**. If Google requires renewed authorization, select **Reconnect Gmail** to sign in again.

The inbox loads up to 20 messages at a time. Select **Load more** to add the next 20 to the list. The loaded count shows how many messages are available to browse. **Refresh inbox** starts a fresh list with the latest messages.

To add a task in **Message review**, select the message or attachment under **Task evidence**, enter a name, a deadline in Singapore time, an estimated study duration and a matching passage from that source. Confirm the review and select **Save task**. **Confirmed** requires a deadline and estimated study time greater than 0 minutes. Tasks with missing details are saved as **Needs details**; complete them in **Tasks**. Saving the same task name from the same message updates that record and preserves recorded study progress.

### Extract tasks

1. In **Task extraction**, refresh the inbox and use **Load more** to include older messages if needed.
2. Choose one or more messages under **Messages to analyze**. The same selection is used for viewing messages and extracting tasks.
3. Allow the selected messages and readable attachment text to be sent to OpenAI, then select **Extract tasks**.
4. Review each suggestion, including its source message and supporting text. Edit task names, deadlines and study durations, and check **Include** for the tasks to save.
5. Confirm that the selected tasks have been reviewed, then select **Save selected tasks**. View the saved tasks in **Tasks**.

The results show progress and any messages that need another attempt. Tasks can come from the message or a text-based PDF/Word attachment, including an attachment whose covering email only says to see the file. Check the source file and supporting quotation for each suggestion. Successful extractions are reused in the current session for unchanged message and attachment content with the same model. Original quotations retain their original language.

## Tasks

Each message can produce several tasks with separate deadlines. Repeated task mentions, including common variations in assignment titles, are combined during extraction review. **Source message** lists the related messages, and **Review notes** identifies combined mentions. Saving a recognized task again keeps the existing task; use the Tasks page to change its details. **Original supporting text** shows the retained quotations and their sources.

Choose **Unfinished** or **Completed**, then select a task under **Edit task**. Update its name, deadline, total estimated study time or completed minutes. Review the details, check **I have reviewed these task details**, and select **Save changes**. Original supporting text and source references remain attached to the same task.

**Confirmed** means a deadline and a positive estimate are available for planning. **Completed** means the recorded completed minutes have reached the total estimate. These describe different things. For example, an estimate of 120 minutes with 40 completed leaves 80 minutes to plan. Entering completed minutes replaces that task's cumulative total. To reopen a task or allow more study time, adjust the completed total or increase its estimate.

The task list shows total, completed and remaining minutes. **Download tasks** exports both categories as CSV.

To delete a task from either category, select it under **Edit task**, expand **Delete task**, check **I want to delete this task**, and select **Delete task**. It is removed from the task list, saved plan and upcoming daily reviews. Saved attachments and previously recorded study time remain available.

## Study Plan

1. Add the dates and times when you are available to study.
2. Add classes, meetings and other commitments. For in-person events, specify travel time each way.
3. Review **Study preferences**, including the daily limit, study-session duration and breaks. Edit **Meal breaks** using one `HH:MM-HH:MM` interval per line, for example `12:15-13:00`. Add, change or remove intervals and select **Save preferences**.
4. Select **Generate study plan**. Review both the scheduled sessions and any work that remains unscheduled.
5. Confirm your review and select **Save plan**. Download the schedule as CSV if needed.

After updating tasks, availability, commitments or meals, generate and save a fresh plan. **Replan remaining tasks** schedules only unfinished work, giving earlier deadlines priority while respecting availability, breaks and the daily study limit. Review the draft and select **Save plan** to make it the current schedule. The saved plan remains visible after reopening the app.

### Daily progress

At the daily study end time recorded in the saved plan, **Daily progress** asks whether all planned study time was completed. The app checks while the window is open and keeps missed reviews for the next visit.

1. Choose **Yes, all planned study time** to record the minutes in that day's plan, or **No, enter completed minutes** to enter actual minutes for each task. Use 0 for work not done.
2. Select **Save daily progress**. Remaining time is reduced by the credited minutes, and tasks with zero remaining time move to **Completed**. Each day is recorded once, including after a refresh or restart.
3. Select **Replan remaining tasks**, open **Study Plan**, review the new draft and select **Save plan**. Correct a recorded cumulative total later through the task editor if needed.

## Attachments

Attachments from messages with extracted or saved tasks are kept on this device with their original files and readable text. Open **Attachments** to view them after restarting, search source text or choose **Download original**. Use **Save attachment** to keep another opened file.

To remove an attachment, select its deletion checkbox and choose **Delete attachment**. The file and its indexed text are removed from the library; saved tasks and their supporting quotations remain. Deleted files stay removed when the same message is reopened.

## Settings

Enter an OpenAI API key in **Settings**, keep the suggested model or enter another model ID, and select **Save AI settings**. The key is saved on your device and the input field is cleared. Leave the key field blank when changing only the model.

Select **Test AI connection** to run a short built-in example. This uses the API balance. Once the check succeeds, return to **Inbox (Gmail)** to extract tasks. Follow the [OpenAI setup guide](OPENAI_SETUP.md) for account, billing and API key instructions.
