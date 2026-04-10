# claude-orchestrator

`claude-orchestrator` is a local-first orchestration layer for durable Anthropic workflows. It queues work, executes it through official Anthropic interfaces, persists all state locally in SQLite, and treats rate limits and upstream availability as normal operational states instead of edge cases.

This project is intentionally compliance-bounded:

- It does not scrape Claude.ai.
- It does not automate the Claude consumer website.
- It does not inspect consumer session-limit counters.
- It relies only on supported Anthropic APIs, SDKs, and documented CLI-style integrations.

## Architecture

The codebase is one-language Python and splits responsibilities cleanly:

- `claude_orchestrator/repository.py`: durable SQLite persistence for jobs, runs, conversation state, artifacts, scheduler events, and message batch metadata.
- `claude_orchestrator/services/orchestrator.py`: central orchestration logic for enqueue, retry, cancel, crash recovery, batch polling, and state transitions.
- `claude_orchestrator/services/worker.py`: scheduler/worker loop with concurrency limits and duplicate-claim prevention.
- `claude_orchestrator/backends/`: pluggable backend adapters.
- `claude_orchestrator/web/`: FastAPI + Jinja2 + HTMX dashboard.
- `claude_orchestrator/guardrails.py`: explicit compliance boundaries that reject consumer-site automation directions.

## Backends

### `messages_api`

Default backend for direct Anthropic API requests. The backend treats the upstream API as stateless, stores conversation history locally, compacts older turns into `compact_summary`, and prioritizes model-continuation follow-ups when a response stops at `max_tokens`.

### `message_batches`

Optional bulk backend for many independent non-urgent jobs. Compatible queued jobs are grouped into Message Batches, then polled back into individual job outcomes.

### `agent_sdk`

Optional scaffold for documented Anthropic Agent SDK workflows. It is designed around bounded workspaces, tool allowlists, checkpoint-aware resume, and artifact capture.

### `claude_code_cli`

Optional wrapper for explicitly configured documented CLI flows. It does not assume undocumented flags or browser automation, bounds stdout/stderr capture, and requires explicit hook allowlists before any local hook command can run.

## Job State Model

Jobs always live in one of:

- `queued`
- `running`
- `waiting_retry`
- `completed`
- `failed`
- `cancelled`

The durable worker uses SQLite-backed claiming to prevent duplicate execution, renews leases with background heartbeats while jobs are running, and drains in-flight work during graceful shutdown. On startup it scans expired `running` leases and moves those jobs into a recoverable state based on checkpoint availability.

## Retry Model

Retry classification is centralized and testable:

- Retryable: `429`, `5xx`, transient timeouts, network/transport issues, and temporary conflicts.
- Permanent: invalid auth, malformed requests, unsupported features, deterministic input errors, and operator cancellation.

Backoff rules:

- `Retry-After` always wins when present.
- Otherwise: bounded exponential backoff with jitter from a configurable base delay.
- Every decision is written to `scheduler_events`, so operators can see why the job is waiting and when it will be retried.

## Persistence Schema

SQLite tables:

- `jobs`
- `job_runs`
- `conversation_state`
- `artifacts`
- `scheduler_events`
- `message_batches`

Migrations live in [`claude_orchestrator/migrations/0001_initial.sql`](/Users/hhargrave2024/Documents/GitHub/claudewatcher/claude_orchestrator/migrations/0001_initial.sql).

## Installation

1. Create a Python environment with Python 3.11+.
2. Install the package and development dependencies:

```bash
pip install -e '.[dev]'
```

3. Initialize config:

```bash
claude-orchestrator config-init
```

4. Export your Anthropic API key:

```bash
export ANTHROPIC_API_KEY=...
```

## Configuration

The default config format is TOML. A full example lives at [`claude-orchestrator.example.toml`](/Users/hhargrave2024/Documents/GitHub/claudewatcher/claude-orchestrator.example.toml).

Key settings include:

- `default_backend`
- `model`
- `worker.max_concurrency`
- `worker.lease_seconds`
- `worker.heartbeat_interval_seconds`
- `worker.shutdown_drain_timeout_seconds`
- `worker.continuation_priority_bonus`
- `retry.base_delay_seconds`
- `retry.max_delay_seconds`
- `retry.max_attempts`
- `backends.messages_api.api_key_env`
- `backends.messages_api.compaction_message_threshold`
- `backends.messages_api.compaction_keep_recent_messages`
- `backends.agent_sdk.allowed_tools`
- `backends.claude_code_cli.command_template`
- `backends.claude_code_cli.allow_hooks`
- `backends.claude_code_cli.allowed_hook_executables`
- `storage.sqlite_path`
- `logging.persist_payloads`
- `privacy.enabled`
- `ui.refresh_seconds`

## CLI Usage

Core commands:

```bash
claude-orchestrator enqueue --prompt-file task.txt
claude-orchestrator run-worker
claude-orchestrator retry-due
claude-orchestrator status
claude-orchestrator inspect JOB_ID
claude-orchestrator inspect JOB_ID --json
claude-orchestrator cancel JOB_ID
claude-orchestrator retry JOB_ID
claude-orchestrator list
claude-orchestrator purge-completed
claude-orchestrator export-logs exported-logs.json
```

Useful extras:

```bash
claude-orchestrator run-daemon
claude-orchestrator doctor
claude-orchestrator migrate
claude-orchestrator config-init
```

## Dashboard

The dashboard is a lightweight FastAPI + Jinja2 app with HTMX polling. It shows:

- state counts
- recent jobs
- retry timing
- last error summaries
- job detail pages
- retry/cancel actions
- JSON status endpoints at `/api/status`, `/api/jobs`, and `/api/jobs/{job_id}`

To run it:

```bash
uvicorn claude_orchestrator.web.app:build_app --factory --reload
```

## Security Notes

- Secrets come from environment variables only.
- `.env` loading is optional and intended only for local development.
- Log formatting redacts common secret patterns and configured environment-backed secrets.
- Privacy mode stores prompts outside SQLite and keeps them out of normal payload logs.
- Workspace paths are rooted under a configured directory and checked before use.
- No browser cookies, no DOM scraping, and no unofficial endpoints are used.

## Compliance / Non-Goals

This tool is not a consumer-site automation layer. It will not:

- inspect Claude.ai usage or reset counters
- use Playwright, Selenium, Puppeteer, or browser devtools against Claude.ai
- reuse browser cookies or local storage
- reverse engineer private consumer endpoints

The rationale and forbidden-pattern checks live in [`claude_orchestrator/guardrails.py`](/Users/hhargrave2024/Documents/GitHub/claudewatcher/claude_orchestrator/guardrails.py).

## Crash Recovery Model

Workers call recovery on startup:

1. Find stale `running` jobs whose leases have expired.
2. If the backend can resume from checkpoint/history, return the job to `queued`.
3. Otherwise schedule a safe retry using the normal retry policy.

During normal execution, background heartbeats keep leases fresh so long-running jobs are not falsely recovered. During shutdown, workers stop claiming new jobs, wait for active work up to a configurable drain timeout, then cancel and persist interrupted jobs into recoverable retry states.

## Tests

The test suite focuses on:

- state machine transitions
- retry-after and exponential backoff behavior
- permanent vs transient classification
- stale-running crash recovery
- lease heartbeat renewal
- graceful worker shutdown with active jobs
- duplicate-claim prevention
- SQLite persistence
- log redaction
- backend adapter behavior
- message compaction and continuation follow-up
- packaged web template loading
- CLI backend hook/failure hardening
- guardrail enforcement

Run the tests with:

```bash
PYTHONPYCACHEPREFIX=/tmp/claude-orchestrator-pyc pytest
```

## Operator Guide

The short operator playbook lives at [`docs/operator-guide.md`](/Users/hhargrave2024/Documents/GitHub/claudewatcher/docs/operator-guide.md).
