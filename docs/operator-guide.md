# Operator Guide

## Daily Flow

1. Initialize config with `claude-orchestrator config-init`.
2. Export `ANTHROPIC_API_KEY`.
3. Enqueue work with `claude-orchestrator enqueue --prompt-file task.txt` or `claude-orchestrator enqueue --backend codex_cli --prompt-file task.txt`.
4. Start a worker with `claude-orchestrator run-worker`.
5. Open the dashboard with the FastAPI app to inspect retries, errors, artifacts, provider, backend, workspace details, and live coding-agent activity.

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

## Live Activity

- `codex_cli` jobs emit durable stream events while running.
- Recent progress is visible in `inspect JOB_ID`, `/api/jobs/{job_id}/stream-events`, and the job detail page.
- Common event types include `process_started`, `stdout_line`, `stderr_line`, `phase_changed`, `process_completed`, and `process_interrupted`.

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
- When will it retry?
  Check `next_retry_at` in `inspect JOB_ID` or the dashboard.
- What upstream signal caused the retry?
  The relevant event stores retry reason, headers, and any `retry-after` value.
- Is the failure permanent?
  `failed` means yes for the current input/config; `waiting_retry` means no.
