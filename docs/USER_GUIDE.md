# Using StudentFlow

## Inbox (Gmail)

Connect Gmail once on your device. StudentFlow restores the saved connection when you reopen the window or restart the app, and shows **Gmail connected**. Select **Switch account** to use another account or **Disconnect Gmail** to sign out of this device.

In **Task extraction**, refresh the inbox and choose one or more messages. Select **View selected messages** to read their text and load attachments locally. AI extraction sends the selected messages' subjects, text and dates to the configured model after your consent.

If the connection is temporarily unavailable, select **Retry connection**. If Google requires renewed authorization, select **Reconnect Gmail** to sign in again.

The inbox loads up to 20 messages at a time. Select **Load more** to add the next 20 to the list. The loaded count shows how many messages are available to browse. **Refresh inbox** starts a fresh list with the latest messages.

To add a task, enter a name, a deadline in Singapore time, an estimated study duration and a matching passage from the message. Confirm the review and select **Save task**. Tasks with a deadline and estimated duration are saved as **Confirmed** and can be included in a study plan. Incomplete tasks are saved as **Needs details** for further review. Saving the same task name from the same message updates that record.

### Extract tasks

1. In **Task extraction**, refresh the inbox and use **Load more** to include older messages if needed.
2. Choose one or more messages under **Messages to analyze**. The same selection is used for viewing messages and extracting tasks.
3. Allow the selected messages' subjects, text and dates to be sent to OpenAI, then select **Extract tasks**.
4. Review each suggestion, including its source message and supporting text. Edit task names, deadlines and study durations, and check **Include** for the tasks to save.
5. Confirm that the selected tasks have been reviewed, then select **Save selected tasks**. View the saved tasks in **Tasks**.

The results show progress and any messages that need another attempt. Successful extractions are reused in the current session for the same message and model. Each selected task is checked before the group is saved. Original message quotations retain their original language.

## Tasks

View saved tasks, their deadlines and their source messages. Review supporting text or download the task list as CSV. To complete missing information, reopen the source message and save the same task name with the corrected details.

## Study Plan

1. Add the dates and times when you are available to study.
2. Add classes, meetings and other commitments. For in-person events, specify travel time each way.
3. Review **Study preferences**, including the daily limit, study-session duration and breaks. Meals remain reserved at the displayed times.
4. Select **Generate study plan**. Review both the scheduled sessions and any work that remains unscheduled.
5. Confirm your review and select **Save plan**. Download the schedule as CSV if needed.

After updating tasks, availability or commitments, generate a fresh plan. Planning preferences and confirmed plans are saved on your device.

## Attachments

Open a message with a PDF or Word attachment, then select **Read attachments** in the Attachments tab. Search using keywords from the document and inspect the matching source text.

## Settings

Enter an OpenAI API key in **Settings**, keep the suggested model or enter another model ID, and select **Save AI settings**. The key is saved on your device and the input field is cleared. Leave the key field blank when changing only the model.

Select **Test AI connection** to run a short built-in example. This uses the API balance. Once the check succeeds, return to **Inbox (Gmail)** to extract tasks. Follow the [OpenAI setup guide](OPENAI_SETUP.md) for account, billing and API key instructions.
