# Using StudentFlow

## Inbox (Gmail)

Connect Gmail once on your device. StudentFlow restores the saved connection when you reopen the window or restart the app, and shows **Gmail connected**. Select **Switch account** to use another account or **Disconnect Gmail** to sign out of this device.

On Windows, double-click the **StudentFlow** desktop shortcut to open the app. After a code update, the shortcut loads the updated version automatically. If the analysis workflow has changed, select your messages and choose **Analyze selected messages** to refresh the suggestions.

If startup reports that an application-control policy blocked a file, note the stage and target file shown in the error. A blocked Python launcher requires a trusted signed Python environment. Repair the environment without removing saved tasks, attachments or account settings; keep Windows security controls enabled. An administrator must approve the runtime on a managed device.

In **Message analysis**, refresh the inbox and choose one or more messages. After consent, **Analyze selected messages** automatically opens their text and readable attachments and distinguishes study tasks from fixed appointments. **View selected messages** is optional and lets you read them without AI analysis. AI analysis uses the selected messages' subjects, text, dates and readable attachment text, along with relevant saved task details when checking for duplicates.

If the connection is temporarily unavailable, select **Retry connection**. If Google requires renewed authorization, select **Reconnect Gmail** to sign in again.

The inbox loads up to 20 messages at a time. Select **Load more** to add the next 20 to the list. The loaded count shows how many messages are available to browse. **Refresh inbox** starts a fresh list with the latest messages.

To add a task in **Message review**, select the message or attachment under **Task evidence**, enter a name, a deadline in Singapore time, an estimated study duration and a matching passage from that source. Confirm the review and select **Save task**. **Confirmed** requires a deadline and estimated study time greater than 0 minutes. Tasks with missing details are saved as **Needs details**; complete them in **Tasks**. Saving the same task name from the same message updates that record and preserves recorded study progress.

### Analyze tasks and fixed appointments

1. In **Message analysis**, refresh the inbox and use **Load more** to include older messages if needed.
2. Choose one or more messages under **Messages to analyze**. You do not need to open or classify them first.
3. **Suggest study time when it is not stated** is on by default; turn it off if you want to enter missing durations yourself. Allow the selected messages, readable attachment text and related saved task details to be sent to OpenAI, then select **Analyze selected messages**. The results separate flexible tasks from fixed appointments; a message can produce both.
4. Review each task and its supporting text. The table separates **Source-stated time (min)** from **AI suggestion (min)** and shows the reasoning and assumptions under **Estimate basis**. Accept or edit the final **Study time (min)**, review names and deadlines, and check **Include** for the tasks to save.
5. Confirm that the selected tasks, supporting text and study-time estimates have been reviewed, then select **Save selected tasks**. Check the saved tasks in **Tasks**, then open **Study Plan** to generate and review a schedule.
6. If fixed appointments are found, review their exact times and evidence in the separate fixed-event table, select the events to keep, confirm that review and select **Save selected fixed events**. Reviewing or saving one category does not save the other.

AI estimates are requested only for tasks whose source does not state a duration. They describe total active work time, including any work already completed, and remain suggestions until you review and save them. Set **Study time (min)** to 0 to leave it unknown; such a task cannot yet be scheduled. If AI estimation fails or lacks enough information, the extracted tasks remain available for manual review.

The results show progress and any messages that need another attempt. Tasks can come from the message or a text-based PDF/Word attachment, including an attachment whose covering email only says to see the file. Check the source file and supporting quotation for each suggestion. Original quotations retain their original language.

Successful source analysis is saved on this device and reused for the same account, content and model, including after reopening the app. Identical attachment copies reuse the same task analysis and identities. A complete analysis is reused when sources, model, saved task references and settings are unchanged. If one part fails, successful results remain available and the incomplete part is reported. To request a fresh analysis, select **Analyze completed messages again** before **Analyze selected messages**.

If the app reports that analysis or duplicate checking has been updated, select **Analyze selected messages** again. Existing source analysis can be reused; **Analyze completed messages again** is only needed when you want a fresh extraction.

## Tasks

Each message can produce several tasks with separate deadlines. Repeated copies of an extracted task are combined during review. Related suggestions and saved tasks are also compared using their meaning and source evidence; uncertain matches stay separate for review. **Source message** lists the related messages, and **Review notes** identifies combined mentions. Saving a recognized task again keeps its existing details and study progress; use the Tasks page to change its details. **Original supporting text** shows the retained quotations and their sources.

Body and attachment mentions can be combined when their evidence identifies the same action, deliverable and assignment occurrence without conflicting details. Different deadlines or conflicting stated durations remain separate for review. Read both sources when suggestions may overlap, and select only the tasks you intend to keep.

Choose **Unfinished** or **Completed**, then select a task under **Edit task**. Update its name, deadline, total estimated study time or completed minutes. Review the details, check **I have reviewed these task details**, and select **Save changes**. Original supporting text and source references remain attached to the same task.

For tasks with a reviewed estimate, **Study-time estimate history** shows the source-stated duration, AI suggestion, final confirmed minutes and review decision, together with the saved reasoning and assumptions. An AI suggestion is kept separate from the source facts and the total used for scheduling.

**Confirmed** means a deadline and a positive estimate are available for planning. **Completed** means the recorded completed minutes have reached the total estimate. These describe different things. For example, an estimate of 120 minutes with 40 completed leaves 80 minutes to plan. Entering completed minutes replaces that task's cumulative total. To reopen a task or allow more study time, adjust the completed total or increase its estimate.

The task list shows total, completed and remaining minutes. **Download tasks** exports both categories as CSV.

To delete a task from either category, select it under **Edit task**, expand **Delete task**, check **I want to delete this task**, and select **Delete task**. It is removed from the task list, saved plan and upcoming daily reviews. Saved attachments and previously recorded study time remain available.

## Study Plan

1. Under **Available study time**, use **Add weekly availability** to select weekdays and a time range. Add another rule for a second daily range. These rules repeat each week within the planning horizon.
2. Use **Change a particular day** for extra free time, a shorter available period or a whole day off. Add classes, meetings and other commitments; for in-person events, specify travel time each way. Reviewed fixed events are also treated as commitments.
3. Review **Study preferences**, including the daily limit, study-session duration and breaks. Edit **Meal breaks** using one `HH:MM-HH:MM` interval per line, for example `12:15-13:00`. Add, change or remove intervals and select **Save preferences**.
4. Select **Generate study plan**. Review both the scheduled sessions and any work that remains unscheduled.
5. Confirm your review and select **Save plan**. Download the schedule as CSV if needed.

After updating tasks, availability, commitments or meals, generate and save a fresh plan. **Replan remaining tasks** schedules only unfinished work, giving earlier deadlines priority while respecting availability, breaks and the daily study limit. Review the draft and select **Save plan** to make it the current schedule. The saved plan remains visible after reopening the app.

Study sessions prefer whole-hour starts. For example, if it is 12:14 and enough time remains before the deadline, the next session can start at 13:00. Today's elapsed availability is skipped; a future day's availability starting at 09:00 can still be used from 09:00, subject to meals and commitments. When waiting for a whole hour would leave more work unfinished, the planner can use other start times. Session durations are kept as needed, so a 45-minute session starting at 13:00 ends at 13:45. Exams, presentations and other fixed appointments retain their exact times. This preference applies to newly generated plans in both Study Plan and Assistant; an already saved plan changes only after you generate, review and save a replacement.

### Weekly availability and date changes

Use **Edit or remove weekly rules** to change a recurring rule or remove several rules together. Use **Review or remove dated changes** to remove date exceptions or older individual availability entries together. Removing an exception restores the weekly pattern for that date. Date exclusions take priority over additions: remove an exclusion before adding that time back.

The upcoming availability table combines weekly rules and date changes. Meal breaks, fixed commitments, travel and study-session breaks are applied when generating the plan. The planning horizon defaults to 28 days and can be changed from 1 to 90 days.

Expired individual availability entries and date exceptions are cleaned up when Study Plan opens or refreshes. A time range is expired only after its end; today's remaining time stays available. Weekly rules, saved schedules and recorded progress are retained. Cleanup does not run while the app is closed.

### Exams, presentations and other fixed events

1. In **Inbox (Gmail) → Message analysis**, select the relevant messages, give consent and choose **Analyze selected messages**.
2. The same analysis finds fixed events and flexible preparation tasks from message text and readable PDF/Word attachments. No separate event-extraction action is needed; messages containing only appointments are supported too.
3. Review the supporting text, event type, exact start and end, location and travel time. Check **Include** for the desired events, confirm the review and select **Save selected fixed events**. Missing times must be filled before saving; an exam's duration is not an AI estimate of preparation time.
4. In **Study Plan → Saved fixed events**, use **Edit or remove a fixed event** for corrections. Generate and save a new study plan after a change.

For example, an exam on 28 October from 14:00 to 16:00 blocks that exact interval. Studying for the exam remains a separate flexible task. A submission deadline alone is not treated as an appointment. Repeat saves preserve the existing event and human corrections. Source quotations and file/page references remain with the saved event. To retain an original attachment separately, use the attachment library's **Save attachment** action.

### Google Calendar sync

1. Enable Google Calendar API in the Google project used for your desktop sign-in client. In **Study Plan → Google Calendar sync**, select **Connect or change Calendar sync account** and authorize your Google account.
2. Keep **Include saved study sessions** selected to export a current saved plan together with confirmed fixed events. If the plan is out of date, generate and save a fresh plan first, or leave this option off to sync only fixed events.
3. Select **Preview Calendar changes**. Review the account and every event to add, update or remove, then check the review box and select **Sync reviewed changes**.
4. Use **Open Google Calendar** and display the **StudentFlow** calendar. After replanning or editing fixed events, repeat preview and sync.

This is one-way, manually confirmed synchronization from StudentFlow to a separate calendar. It does not import edits made in Google. Repeating the same sync does not create another copy. Replanning updates the app's future entries; past or already-started entries are kept. Fixed-only sync preserves study entries already on the calendar. Only confirmed fixed events belonging to the selected Google account are exported; the preview reports events excluded for another account. No invitations are sent.

Calendar sync requests Google's [`calendar.app.created` permission](https://developers.google.com/workspace/calendar/api/auth) to create and manage its own secondary calendar, plus calendar-list reading and Gmail profile identification through the existing read scope. Its sign-in is separate from the Inbox and Assistant read-only connections. **Disconnect Calendar sync** removes that local connection; events already on Google Calendar remain. Authorization alone does not synchronize events.

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

## Assistant

Choose **Email replies**, **Meetings & discussions** or **Other assistance** under **Assistant workspace**. Each has its own request, selected information and results. Switching preserves those inputs and any edited draft during this browser session; the next model request needs fresh consent. Only Email replies loads or saves your writing sample and reply preferences.

For a discussion, choose **Meetings & discussions**, choose a space under **Google Chat discussions** and select **Read selected discussion**. Choose one or more **Messages to analyze**, and use the prefilled English analysis request or edit it. Optional attachments are selected separately under **Supporting attachments (optional)**. There is no need to replace your saved email request or disable your email writing sample. The result includes a discussion summary, decisions and action items for review, with supporting text when available.

Check the **Discussion summary** card for confirmed decisions, action items, **Open questions**, **Details still to confirm**, an agenda and a follow-up draft. Unstated owners and deadlines stay **Not specified**. If the available discussion cannot support the requested summary, **Discussion details needed** lists the questions to resolve. A text answer alone does not establish that a structured summary was recorded; **Processing details** shows `record_meeting_summary` when that tool succeeds. The formatted answer uses readable source references such as **Message 1**, with source details available for review.

Select **Download minutes (Word)** at the bottom of the **Discussion summary** card to save an editable Word document (`.docx`). It includes the summary, decisions, action items with owners and deadlines, open questions, details still to confirm, suggested agenda, follow-up draft and supporting quotations. **Download minutes (TXT)** and **Download minutes (Markdown)** provide alternative formats. Downloading does not run the assistant again. An incomplete analysis is marked in the exported file; unknown details remain unspecified. Clarification-only results have no minutes download. If you change the request or selected information, run the assistant again before downloading the updated result.

If recording the minutes fails validation, the assistant can correct the generated fields once within the current run's limits. If it still cannot record a result, **Processing details** shows a safe error reason, such as a quotation mismatch, unsupported owner or invalid deadline format. Failed records are not offered as completed minutes or downloads; source text and credentials are excluded from the diagnostic log.

1. For a reply, select one message in **Inbox (Gmail)** and choose **Reply with assistant**. StudentFlow opens the email, switches to **Assistant → Email replies** and selects it for you. For other requests, open relevant messages in Inbox; kept attachments are also available as sources.
2. Choose **Information to use** for email or general sources, or **Messages to analyze** for a loaded discussion. **Saved tasks to use** and **Confirmed fixed events to use** are under **Other assistance**. Selections start empty except for an explicit Inbox reply shortcut.
3. For planning in **Other assistance**, include saved study preferences and, when relevant, loaded calendar commitments. Add academic-calendar dates if the request depends on reading week or another named period.
4. For a reply in your writing style, paste an example under **My writing style** and confirm that it is your own writing. Enter reusable instructions in **What would you like help with?**, then choose **Save reply preferences** to remember both fields and the writing permission for this Gmail account. Saved fields reload on reopening; **Load saved reply preferences** restores them and **Clear saved reply preferences** removes them. Before running, check the email and approved writing sample counts under **Selected for this request**. The Inbox shortcut selects the target automatically; ordinary **View selected messages** only makes it available for manual selection.
5. Enter the request, review the selection, give consent and choose **Run assistant**.
6. Review the answer, structured suggestions and **Supporting text**. Edit and download reply drafts if useful.
7. To update a selected saved task's study time, open its suggestion details. Review the reasoning, **Total study time (min)**, completed minutes and resulting remaining time. Accept or edit the total, check **I have reviewed this total study time and its reasoning**, then select **Save study time**. Changing the value requires a new review. Open **Study Plan** to plan with the saved total.

The assistant can suggest classifications, replies, task additions or revisions, fixed events, discussion decisions and action items, study schedules and deadline clusters. A reviewed study-time update preserves the task's title, deadline and completed minutes. If the task or its progress changed after the suggestion was prepared, run the assistant again and review the latest version before saving. A successful study-time save clears the old result. Fixed-event suggestions use the same exact-time and supporting-text review as Inbox; select and confirm them before saving.

The coordinator delegates to email, task, planning and meeting specialists. Specialists call scoped tools: fixed-event proposals are checked against selected sources, weekly availability and date exceptions are expanded by a calculation tool, and scheduling avoids selected confirmed appointments. Check **Processing details** to see tool activity. Newly proposed fixed events must be reviewed and saved before they can be selected as confirmed planning constraints.

For a styled reply, the email specialist reads the target message and the approved writing sample separately. Its instructions distinguish the sample's tone and your signature from facts in the current message, and direct it to resolve template placeholders from supported information. Unknown names or dates should be clarified rather than guessed, and a sample's promises should not be copied into an acknowledgement. Review the actual draft before using it. Writing samples up to the field's 6,000-character limit are read in full, including a signature at the end. **Processing details** shows the source reads, `get_style_samples` and `record_reply_draft` when used; a delegation alone does not prove a draft was created.

The email specialist receives your original request as well as the coordinator's objective, so signature and wording constraints remain available. When the coordinator requests a reply, the runtime checks for a recorded draft or a structured clarification. If the specialist has read the required information but returns only prose, one bounded follow-up can record the draft or ask for clarification. Missing or invalid results are reported as incomplete; a user request to clarify first does not force a draft. Successful drafts appear under **Reply draft**, with editable **Draft subject** and **Draft message** fields.

New flexible tasks, other task changes and plans remain previews; use the Inbox review flow to save new tasks. Agents cannot send messages or directly write Calendar. Use the reviewed Gmail controls below for sending and the reviewed sync flow in Study Plan for Calendar. Changing selected information, the request or model hides the old result. Generated results remain in the current browser session; saved reply preferences persist locally per account. Usage statistics contain no source text.

### Send a reviewed Gmail reply

1. Edit **Draft subject** and **Draft message**. Remove remaining template markers such as `[Surname]`; if the source does not establish a teacher's name or title, a neutral greeting is appropriate. New agent drafts containing recognized template markers are rejected for correction.
2. Expand **Send with Gmail**. On first use, choose **Connect Gmail sending** and authorize the same account as the original inbox email. This permission is separate from reading the inbox. See [Gmail setup](GMAIL_SETUP.md#enable-reviewed-reply-sending).
3. Choose **Review for sending**. StudentFlow verifies the original conversation and its Reply-To/From recipient. Read the displayed From, To, subject and exact message. Keep the original subject with an optional Re: prefix so the reply stays in its conversation.
4. Check **I have reviewed the recipient and this exact message**, then choose **Send with Gmail**. Any edits require another review. A successful result displays **Sent with Gmail**.

An identical successful reply cannot be sent twice from this flow. If delivery is uncertain after a timeout, check Gmail Sent; the app blocks repeating that payload instead of retrying automatically. Authorization or generating a draft never sends a message. Saved writing permission does not authorize sending, and the current AI-processing consent is not saved with your preferences.

### Google Chat and Calendar

1. In **Meetings & discussions**, expand **Google Chat discussions**. For the combined Chat and Calendar panel, use **Other assistance → Google Chat and Calendar**. Allow reading and select **Connect Google**. If an earlier permission-check authorization is available, the app validates it before reuse; otherwise complete Google sign-in.
2. Select **Load Google Chat spaces**, choose a space and select **Read selected discussion**. **Messages to analyze** contains that space's readable messages in chronological order, each with a time in Singapore time, sender and text preview. Select several messages individually, use **Select all messages**, or use **Clear message selection**; these buttons do not change selected attachments. Check the selected count and **Preview selected information** before running. Reading the same space again preserves choices that still exist; changing spaces clears the previous message selection. Messages without readable text are reported but not analyzed.
3. For planning in **Other assistance**, select **Load Google calendars**, choose calendars and dates, then select **Read selected calendars**. Check **Include the loaded calendar commitments** before asking for a plan. Changing the calendar selection requires another read. These calendar controls are hidden in Meetings & discussions.
4. Use **Switch Google account** to sign in with another account, or **Disconnect Google** to remove this connection from the device. Switching accounts clears loaded discussions and calendar commitments.

This sign-in is separate from the Inbox Gmail connection. Google Chat API and Google Calendar API must be enabled in the project used by the desktop OAuth client. Loading data here does not send it to OpenAI; the separate assistant consent covers the selected information for that request. The page does not record meetings or retrieve Meet transcripts.
