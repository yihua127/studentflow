# Validation status

## Discussions and Assistant workspaces — 5 November 2026

**120 affected offline regression tests passed**, covering workspace isolation, source selection and reply handoff, saved preferences, edited drafts, consent and stale-result invalidation, reviewed sending and study-time updates, structured minutes and downloads, Google connection/account changes, calendar coverage and application navigation. Ruff and whitespace checks passed.

Browser checks used isolated sample data and simulated services at 1536×1000 and 1100×800. The source/request panels, three assistance modes, discussion notes, reply editor and connection controls rendered without application errors or document-level horizontal overflow. Word download and retention of an edited reply across mode changes were also verified. These checks made no real Google or paid model requests.

## Overview calendar and to-do list — 5 November 2026

**30 affected offline regression tests passed**, covering the new calendar and existing application navigation. Calendar checks include timezone conversion, midnight-exclusive multi-day events, unknown deadlines, escaped source titles, date and month selection, and omission of completed tasks. Task shortcuts preserve the selected record and clear unrelated filters without changing saved tasks.

Browser checks at 1536×1000, 1100×800 and 1280×720 confirmed that Settings and the account footer remain visible in the sidebar without scrolling. The calendar, to-do list, month navigation and task-detail shortcut were checked with isolated sample data, with no application errors or document-level horizontal overflow. Ruff and whitespace checks passed; no external services were called.

## Tasks and study-plan workspaces — 5 November 2026

The redesigned task browser and study-plan sections were checked with **85 passing affected offline regression cases**. Coverage includes task search and selection, editing and progress, retained source evidence and estimate history, deletion, exports, weekly availability and date changes, fixed events, plan validation and saving, Calendar preview and confirmation, daily progress, and sidebar navigation. Ordinary received input values survive navigation; review and deletion approvals are not restored. Unsubmitted browser form edits still need to be saved before leaving the page.

Browser checks used isolated sample tasks and plans at 1536×1000 and 1100×800. The task list and editor, saved and proposed agendas, availability settings and commitments sections rendered without application errors or document-level horizontal overflow. The plan save action remained disabled until review. Ruff and whitespace checks passed. These checks did not use real Gmail or Calendar writes or paid model requests.

## Sidebar and email workspace — 5 November 2026

The sidebar and shared message browser were checked with **155 passing offline regression tests**. Coverage includes source selection, pagination, account changes, reading without AI consent, reply source handoff, preserved edited drafts, task and fixed-event review edits across navigation, and explicit save/send confirmation. Existing classification, label editing, assistant and daily progress tests also passed. Ruff and whitespace checks passed.

Browser checks used isolated sample messages at 1536×1000 and 1100×800. The sidebar, reader, category review and task-extraction views rendered without application errors or document-level horizontal overflow. No real Gmail writes or model requests were made during these checks.

## Email categories and Gmail labels — 5 November 2026

The Inbox category workflow now uses the existing manager and email specialist to classify selected message text. Results are checked for complete per-message coverage, valid categories and exact quotations. Categories can be reviewed, edited, saved locally and filtered. Gmail synchronization has a separate authorization and a read-only preview; explicit confirmation is required before StudentFlow labels can be changed.

The initial focused offline regression passed **195 tests** covering classification, persistent reviews, identical-content message identity, UI consent and filtering, Gmail label permission and confirmation, stale or cross-account previews, partial failures and retries, plus existing Inbox extraction and reply flows. The user subsequently confirmed that the classification and Gmail label workflow worked in their live account. No classification-accuracy percentage is claimed by these tests.

Manual label editing adds per-message category changes and multiple custom labels, account-specific label creation and renaming, persistence across reanalysis, and filtering by custom label. Renamed Gmail labels migrate only on selected messages after confirmation. The updated focused regression passed **153 tests**, including UI edits, stale-preview invalidation, catalog conflicts, selected-message migration, retry behavior and preservation of unrelated Gmail labels. Ruff and whitespace checks passed. These new editing and migration checks use simulated Google services; the expanded workflow has not yet had a user-observed live-account check.

The earlier prototype record follows below.

This record describes the StudentFlow prototype as of 26 October 2026. It separates user-observed integration checks from automated tests and does not claim exhaustive testing or measured extraction accuracy.

## Demonstrated core workflows

| Workflow | Evidence and result |
| --- | --- |
| Gmail task and appointment extraction | The user reviewed results separating flexible preparation tasks from fixed exams and presentations, then saved them through their respective review controls. |
| Availability and study planning | User screenshots show recurring availability, date exceptions, appointment exclusions and generated study sessions. Later schedules use whole-hour starts where feasible; today's elapsed time is excluded. |
| Google Calendar sync | The user confirmed study sessions and fixed appointments in the dedicated StudentFlow calendar. Repeating the unchanged sync reported that no calendar changes were needed. |
| Styled email reply and sending | The user reviewed an editable reply, connected sending, explicitly sent it, and saw the self-addressed test reply in the inbox. This verifies the demonstrated account and path, not delivery to every recipient or account type. |
| Google Chat discussion summary | The user loaded selected messages from a test discussion space and successfully generated structured minutes after the recording-recovery fix. The local metadata records completed status and no error events for that run. |
| Word minutes download | The user downloaded the generated DOCX. All two rendered pages were checked: decisions, action owners and deadlines, unknown details, supporting quotations and layout were consistent with the supplied test discussion. |

The latest minutes check found one unrelated-language word in the suggested follow-up draft. A reviewed copy corrected that word without changing the other content or layout. The meeting instructions now explicitly request a language check across all newly authored sections while preserving original names and exact quotations. This prompt refinement has not had an additional live-model verification; generated wording still requires review.

The downloaded minutes, account data, usage logs and visual QA files remain local. They are not included in this repository.

## Automated checks

The test suite covers extraction and evidence validation, task identity and duplicates, local persistence, scheduling and independent schedule validation, account isolation, reviewed email and calendar writes, duplicate-send prevention, agent budgets and recovery, workspace selection, safe rendering and minutes exports. External operations in these tests use controlled doubles or scripted models; a passing offline test is not a live integration claim.

Local release checks on 26 October 2026:

- Full offline regression: **1,453 passed**, no failures or skips.
- After the final maintenance cleanup: **166 affected tests passed**; the coordinator and meeting runtime checks after the language-instruction refinement added a separate **80-test passing run**.
- Anonymized signature fixtures: **94 affected tests passed**.
- Repository-wide Ruff and whitespace checks passed. The lockfile resolved **102 packages** offline; the installed dependency check found **101 compatible packages**.

These counts overlap and must not be added together as a distinct-test total. The final focused runs validate changes made after the full regression. No new paid-model or Google requests were made during this release check.

From the repository root, use the locked project environment:

```powershell
uv sync --locked
uv run --locked pytest -q
uv run --locked ruff check .
uv pip check
```

## Further testing and scope limits

- An independent, previously unseen evaluation set is still needed to measure extraction and summary accuracy. Development examples and usage-token counts are not an accuracy benchmark.
- Larger discussions, long attachments, real network interruptions, expired or revoked permissions and multiple real accounts need broader end-to-end coverage. Offline failure tests do not replace these live checks.
- Progress reporting and replanning, preference persistence, cross-account isolation and repeated-send protection have automated coverage; this acceptance record does not claim a new manual run of every control.
- Native Google Meet transcript retrieval and Microsoft Teams integration are not implemented. Google Chat attachments and cards are not analyzed through the Chat reader; readable files selected separately can be supplied as sources.
- A clean installation on another computer and a complete presentation rehearsal remain useful before delivery. The repository has no configured GitHub Actions workflow; local checks are recorded explicitly.

The demonstrated core prototype can be used for a reviewed presentation after the documented local checks pass. This is not a claim of production readiness or completion of all possible agent tests.
