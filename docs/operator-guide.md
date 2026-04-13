# AI Telescreen Operator Guide

## Daily Flow

1. Initialize config with `claude-orchestrator config-init`.
2. Export `ANTHROPIC_API_KEY`.
3. Open the AI Telescreen dashboard and create or launch a job from the browser.
4. Start a worker with `claude-orchestrator run-worker`.
5. Open `/doctor` when you need a quick health check for config, backends, integrations, and Codex availability.
6. Use the browser for job monitoring, retry, cancel, duplication, saved-project launch, project editing, recent-launch history, project-manager recommendations, and optional one-pass worker cycles.

## Provider and Backend

- Provider identifies the upstream family: `anthropic` or `openai`.
- Backend identifies the execution path: `messages_api`, `message_batches`, `agent_sdk`, `claude_code_cli`, or `codex_cli`.
- Provider is inferred from backend during enqueue unless you pass both explicitly.
- A mismatch such as `provider=anthropic` with `backend=codex_cli` is rejected up front.

## Coding Workspaces

- Plain workspace mode creates an isolated directory under the configured workspace root.
- Git worktree mode creates a dedicated branch and worktree under the workspace root while leaving the main repo working tree untouched.
- Enable worktree mode per job with metadata such as `repo_path`, `use_git_worktree`, and `base_branch`.
- Cleanup is metadata-driven via `cleanup_policy` and defaults to `none`.
- Automatic cleanup only targets app-created workspaces and skips dirty worktrees to avoid destroying user-visible changes.
- `codex_cli` now relies on subprocess `cwd` for workspace context instead of passing a `--workspace` flag.
- The preferred Codex invocation shape is `codex exec PROMPT`, with static extra args configured in `backends.codex_cli.args` when needed.
- Legacy `codex_cli.command_template` settings are deprecated. They are ignored unless `use_legacy_command_template = true` is set explicitly.
- The Codex smoke test uses a short read-only ephemeral `codex exec` prompt so operators can distinguish missing executables, invalid config, auth problems, and plausible runnability.

## Browser-First Operations

- The dashboard is now the primary operator surface.
- Use `Create Job` to launch ad hoc work without touching the terminal.
- Use saved projects to prefill repo, backend, provider, base branch, and worktree defaults.
- Use the project edit page to adjust defaults without recreating the project.
- Use the doctor page for browser-based backend and environment diagnostics.
- Job detail pages now expose state-aware actions such as Run Now, Retry, Retry Now, Cancel, Duplicate, and Delete.
- The browser also exposes a lightweight `Run Queued Jobs Once` control for an ad hoc queue pass when you want to kick work forward without opening a terminal.
- Continuous background processing still works best with `claude-orchestrator run-worker`.

## Saved Projects

- Saved projects are first-class launch targets stored in SQLite.
- A project captures `name`, `repo_path`, default backend/provider, default base branch, default worktree preference, and notes.
- Launching from a project preserves those defaults while still using the same orchestration service path as CLI-created jobs.
- Project detail pages now show recent launches tied back to that project via a durable `project_id` job link.
- Projects are saved launch defaults, not active tasks themselves.

## Project Manager

- Every saved project now has a compact durable project-manager state.
- The manager ingests completed and failed project-linked jobs and keeps a rolling summary of what has happened.
- It tracks current phase, manual-testing status, recent important outcomes, saved project guidance, autonomy mode, workflow state, and the latest structured manager response.
- The manager keeps one compact project memory. Operator prompts, manager replies, job outcomes, operator feedback, and saved guidance all flow into that same memory and older details are summarized automatically.
- Common recommendation types include `request_manual_test`, `launch_followup_job`, `wait_for_operator`, and `mark_complete`.
- Manual-testing recommendations are especially likely after browser-facing or UI-heavy changes.
- Older manager events, guidance entries, and operator feedback are compacted into rolling summaries so memory stays bounded instead of growing around raw transcripts.
- The project page is now the main place to review dense manager bullet sections, recent ingested outcomes, and any draft follow-up task.
- When the manager recommends `launch_followup_job`, the browser can prefill a task draft or launch that draft directly with operator approval.
- The top of each project page now includes a Project Manager composer so you can type a new direction and get back a fresh structured recommendation without leaving the browser.
- Composer hints are lightweight and advisory: urgency nudges draft priority, Coding Agent chooses which executor should handle launched work, and execution mode nudges whether the next draft should stay read-only, make safe changes, or do a full coding pass.
- The Project Manager is the planner and memory layer. The Coding Agent selector does not change the manager's own brain.
- `Auto` means "use the best available coding agent, preferring Codex when available."
- Each saved project also now has an autonomy mode:
  - `minimal`: asks before every task and never auto-enqueues work
  - `partial`: runs the current supervised step, reports back, then asks whether it should keep going
  - `full`: keeps iterating until it hits a stop condition such as manual testing, low confidence, ambiguity, repeated failure, or the task limit
- The top of the project page now includes a compact Project Manager session card.
- That card shows whether the manager is idle, has a queued task waiting for a worker, is actively running a task, is waiting on the operator, or is paused for manual testing.
- Manager-launched tasks still use the same queue and worker model as every other job. `Waiting on worker` means the manager already queued the task, but no worker has picked it up yet.
- `Waiting on operator` means the manager finished the current supervised step or needs approval before it continues.
- In partial autonomy, the normal rhythm is: launch one task, report back, then show `Keep Going` while waiting on the operator.
- The default manager reply now reads like a concise technical lead instead of a raw schema renderer.
- The primary project-page actions are human-first: `Run it`, `Edit`, `Ask Follow-up`, `Keep Going`, and `Store as Project Guidance`.
- Raw decision labels, confidence, full draft prompts, and rationale are still available, but they live behind `Show details` instead of dominating the page.
- `Store as Project Guidance` records the current guidance in compact manager memory without launching work.
- Project guidance, operator prompts, manager replies, job outcomes, and operator feedback all feed the same compact project memory. Older details are summarized automatically instead of living in separate context buckets.
- `Ask Follow-up` keeps the conversation going from the same project page instead of forcing you into a separate job flow.
- In partial autonomy, the manager can launch the current obvious step, then come back and ask whether it should continue instead of silently looping forever.
- In full autonomy, the manager can continue iterating, and it now auto-starts obvious low-risk first steps such as read-only reviews instead of asking first. It still stops for manual testing, low confidence, ambiguity, repeated failure, or session/task-count limits.
- If you send a follow-up while the manager is already waiting on a worker, waiting on your continue decision, or paused for manual testing, AI Telescreen now says that explicitly instead of leaving the page feeling dead.
- Recent Project Manager conversation is stored in a bounded durable history so follow-up stays grounded without becoming an unbounded transcript dump.
- The same project page now includes an Operator Feedback form for manual-test outcomes and browser observations.
- Use Operator Feedback when you want the next recommendation to react to what a human actually saw, such as layout problems, broken actions, blocked settings, diagnostics issues, or screenshot-backed UI regressions.
- Feedback fields include outcome, notes, optional severity, optional area, optional screenshot or artifact reference, and a follow-up-required checkbox.
- Recent operator feedback is rendered back on the project page so you can see what the latest recommendation is reacting to.

## Browser Task Actions

- `queued`: use `Run Now` to start the task immediately, `Cancel` to stop it before it runs, or `Delete` to remove duplicates and test jobs.
- `running`: use `Cancel` to request a safe stop.
- `waiting_retry`: use `Retry Now` to start the next attempt immediately, or `Cancel` / `Delete` if you no longer want it.
- `failed`: use `Retry` to re-queue it, `Duplicate` to spin up a fresh copy, or `Delete` to clean it up.
- `completed`: use `Duplicate` when you want to run the same task again, or `Delete` for cleanup.
- `cancelled`: use `Duplicate` to recreate it, or `Delete` to remove it.

These actions are intentionally state-aware. Invalid actions should be hidden in the UI and rejected cleanly if called directly.

## Reading Project Manager Recommendations

- `request_manual_test` means AI Telescreen thinks a human should verify the recent change before another coding pass.
- `launch_followup_job` means the manager sees a likely next coding step and stores a structured draft task for the operator to launch or edit.
- `wait_for_operator` means the project should pause for review, clarification, or an environment/config fix.
- `mark_complete` means recent work looks stable enough that the current project phase may be done.
- The structured response also includes bullet lists for summary, recent changes, active focus, blockers, manual-test steps, and follow-up questions so the browser can render the manager panel predictably.
- The recommendation is guidance first. You stay in control of whether a new job is launched unless the saved project's autonomy mode explicitly allows the manager to launch the current supervised step.
- `Edit Draft` opens the existing New Job form prefilled from the latest manager draft.
- `Launch Task` uses the same saved-project orchestration flow as any other browser-created job; it does not create a hidden autonomous loop.
- A visible `reacting to operator feedback` badge on the project page means the latest recommendation was driven by recent operator feedback instead of only job outcomes or manager prompts.

## Concise Coding Drafts

- Project Manager drafts are now intentionally short.
- They use a compact prompt shape with `Goal`, `Scope`, `Do`, `Do not`, and `Output style`.
- The output-style section asks the coding agent to report only:
  - `PLAN`
  - `CHANGED`
  - `RESULT`
  - `TESTS`
  - `BLOCKERS`
  - `NEXT`
- This keeps coding work readable in the browser and easier for Project Manager memory to ingest afterward.

## AGENTS.md

- The repository root now includes `AGENTS.md`.
- It gives Codex a local repo contract for product priorities, architecture boundaries, UI expectations, Project Manager behavior, and concise execution output.
- That guidance supports both browser-launched drafts and direct coding work in the repo.

## Diagnostics

- Open `/doctor` in the browser for the current AI Telescreen health report.
- `claude-orchestrator doctor` exposes the same report in the terminal.
- `claude-orchestrator smoke-test codex_cli` runs the explicit Codex smoke test on demand.
- The doctor report includes config summary, backend checks, `git` availability, integration discovery status, and the Codex smoke-test result when applicable.
- Doctor also warns when a legacy Codex `command_template` is still configured or explicitly enabled, because smoke-test success only validates the direct read-only smoke path.
- The smoke test is confidence-building only. It does not guarantee that every real job prompt, repo, model, or network condition will behave the same way.

## Integration Awareness

- AI Telescreen records whether a job is local-only or has discovered project or user integration configuration.
- The discovery pass looks for `.claude/settings.json` and `.mcp.json` in the workspace or repo context, plus optional user-scoped config such as `~/.claude/settings.json` and `~/.mcp.json`.
- Discovery is metadata-only in this pass: it summarizes configured capabilities, config paths, and parser notes, but it does not implement arbitrary hosted-app inheritance or a full MCP runtime.
- `claude_code_cli` is the primary integration-aware backend today; `codex_cli` remains effectively local-only in this pass.
- Hosted consumer-app integrations are not automatically inherited. Claude-side integrations require explicit project or user configuration on the machine running the job.

## Live Activity

- `codex_cli` jobs emit durable stream events while running.
- `claude_code_cli` jobs emit an integration-context event when discovered workspace integrations are threaded into backend execution.
- Recent progress is visible in `inspect JOB_ID`, `/api/jobs/{job_id}/stream-events`, and the job detail page.
- Common event types include `process_started`, `stdout_line`, `stderr_line`, `phase_changed`, `process_completed`, and `process_interrupted`.
- Codex job request metadata now records direct prompt delivery rather than relying on prompt-file or workspace CLI flags in the preferred path.

## Recovery Playbook

- `waiting_retry` means the retry scheduler has already classified the failure as transient and recorded the next attempt time.
- `queued` with `followup_type=model_continuation` means the model hit `max_tokens` and the worker will prioritize a continuation turn ahead of ordinary queued work.
- `failed` means the failure was classified as permanent or attempts were exhausted; use `retry JOB_ID` only after correcting the root cause.
- On restart, stale `running` jobs are moved back into a recoverable state during worker startup.
- During normal execution, running jobs renew their leases with background heartbeats. If the worker shuts down mid-run, interrupted jobs are persisted back into a recoverable retry state instead of being left `running`.
- For subprocess-backed `codex_cli` jobs, worker shutdown also terminates the active subprocess and records a durable interruption event before scheduling a retry.

## Questions to Answer Quickly

- Why is the job paused?
  Look at `inspect JOB_ID`, `inspect JOB_ID --json`, or the most recent `scheduler_events` entry.
- How do I start a queued job right now?
  Use `Run Now` on the dashboard or job detail page, or use `Run Queued Jobs Once` if you want a one-pass browser-triggered queue sweep.
- How do I clean up accidental test jobs or duplicates?
  Use `Delete` on queued, failed, cancelled, waiting-retry, or completed jobs.
- Is Codex callable and plausibly authenticated from this machine?
  Open `/doctor` or run `claude-orchestrator smoke-test codex_cli`.
- Is this job local-only or integration-enabled?
  Look at the integration section in `inspect JOB_ID`, the job detail page, or `/api/jobs/{job_id}`.
- When will it retry?
  Check `next_retry_at` in `inspect JOB_ID` or the dashboard.
- What upstream signal caused the retry?
  The relevant event stores retry reason, headers, and any `retry-after` value.
- Is the failure permanent?
  `failed` means yes for the current input/config; `waiting_retry` means no.
