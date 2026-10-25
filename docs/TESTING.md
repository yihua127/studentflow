# Validation status

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
