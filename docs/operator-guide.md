# Operator Guide

## Daily Flow

1. Initialize config with `claude-orchestrator config-init`.
2. Export `ANTHROPIC_API_KEY`.
3. Enqueue work with `claude-orchestrator enqueue --prompt-file task.txt`.
4. Start a worker with `claude-orchestrator run-worker`.
5. Open the dashboard with the FastAPI app to inspect retries, errors, and artifacts.

## Recovery Playbook

- `waiting_retry` means the retry scheduler has already classified the failure as transient and recorded the next attempt time.
- `queued` with `followup_type=model_continuation` means the model hit `max_tokens` and the worker will prioritize a continuation turn ahead of ordinary queued work.
- `failed` means the failure was classified as permanent or attempts were exhausted; use `retry JOB_ID` only after correcting the root cause.
- On restart, stale `running` jobs are moved back into a recoverable state during worker startup.
- During normal execution, running jobs renew their leases with background heartbeats. If the worker shuts down mid-run, interrupted jobs are persisted back into a recoverable retry state instead of being left `running`.

## Questions to Answer Quickly

- Why is the job paused?
  Look at `inspect JOB_ID`, `inspect JOB_ID --json`, or the most recent `scheduler_events` entry.
- When will it retry?
  Check `next_retry_at` in `inspect JOB_ID` or the dashboard.
- What upstream signal caused the retry?
  The relevant event stores retry reason, headers, and any `retry-after` value.
- Is the failure permanent?
  `failed` means yes for the current input/config; `waiting_retry` means no.
