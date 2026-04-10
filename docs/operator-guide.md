# Operator Guide

## Daily Flow

1. Initialize config with `claude-orchestrator config-init`.
2. Export `ANTHROPIC_API_KEY`.
3. Enqueue work with `claude-orchestrator enqueue --prompt-file task.txt` or `claude-orchestrator enqueue --backend codex_cli --prompt-file task.txt`.
4. Start a worker with `claude-orchestrator run-worker`.
5. Open the dashboard with the FastAPI app to inspect retries, errors, artifacts, provider, backend, and workspace details.

## Provider and Backend

- Provider identifies the upstream family: `anthropic` or `openai`.
- Backend identifies the execution path: `messages_api`, `message_batches`, `agent_sdk`, `claude_code_cli`, or `codex_cli`.
- Provider is inferred from backend during enqueue unless you pass both explicitly.
- A mismatch such as `provider=anthropic` with `backend=codex_cli` is rejected up front.

## Recovery Playbook

- `waiting_retry` means the retry scheduler has already classified the failure as transient and recorded the next attempt time.
- `queued` with `followup_type=model_continuation` means the model hit `max_tokens` and the worker will prioritize a continuation turn ahead of ordinary queued work.
- `failed` means the failure was classified as permanent or attempts were exhausted; use `retry JOB_ID` only after correcting the root cause.
- On restart, stale `running` jobs are moved back into a recoverable state during worker startup.
- During normal execution, running jobs renew their leases with background heartbeats. If the worker shuts down mid-run, interrupted jobs are persisted back into a recoverable retry state instead of being left `running`.
- `codex_cli` jobs are durable and inspectable like Anthropic jobs, but they currently run as bounded subprocesses rather than streamed event sessions.

## Questions to Answer Quickly

- Why is the job paused?
  Look at `inspect JOB_ID`, `inspect JOB_ID --json`, or the most recent `scheduler_events` entry.
- When will it retry?
  Check `next_retry_at` in `inspect JOB_ID` or the dashboard.
- What upstream signal caused the retry?
  The relevant event stores retry reason, headers, and any `retry-after` value.
- Is the failure permanent?
  `failed` means yes for the current input/config; `waiting_retry` means no.
