# AI Telescreen Operator Guide

## Daily Flow

1. Initialize config with `claude-orchestrator config-init`.
2. Export `ANTHROPIC_API_KEY`.
3. Open the AI Telescreen dashboard and create or launch a job from the browser.
4. Start a worker with `claude-orchestrator run-worker`.
5. Open `/doctor` when you need a quick health check for config, backends, integrations, and Codex availability.
6. Use the browser for job monitoring, retry, cancel, duplication, saved-project launch, project editing, recent-launch history, and optional one-pass worker cycles.

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
- The Codex smoke test uses a short read-only ephemeral `codex exec` prompt so operators can distinguish missing executables, invalid config, auth problems, and plausible runnability.

## Browser-First Operations

- The dashboard is now the primary operator surface.
- Use `Create Job` to launch ad hoc work without touching the terminal.
- Use saved projects to prefill repo, backend, provider, base branch, and worktree defaults.
- Use the project edit page to adjust defaults without recreating the project.
- Use the doctor page for browser-based backend and environment diagnostics.
- Job detail pages support retry, cancel, duplicate, and re-run/edit flows.
- The browser also exposes a lightweight `Process Due Jobs Once` control for ad hoc worker passes.
- Continuous background processing still works best with `claude-orchestrator run-worker`.

## Saved Projects

- Saved projects are first-class launch targets stored in SQLite.
- A project captures `name`, `repo_path`, default backend/provider, default base branch, default worktree preference, and notes.
- Launching from a project preserves those defaults while still using the same orchestration service path as CLI-created jobs.
- Project detail pages now show recent launches tied back to that project via a durable `project_id` job link.

## Diagnostics

- Open `/doctor` in the browser for the current AI Telescreen health report.
- `claude-orchestrator doctor` exposes the same report in the terminal.
- `claude-orchestrator smoke-test codex_cli` runs the explicit Codex smoke test on demand.
- The doctor report includes config summary, backend checks, `git` availability, integration discovery status, and the Codex smoke-test result when applicable.
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
