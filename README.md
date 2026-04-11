# AI Telescreen

AI Telescreen is a local-first orchestration layer for durable coding-agent workflows. The current Python package and CLI command still use legacy internal names such as `claude_orchestrator` and `claude-orchestrator`, but the product surface is now AI Telescreen. It queues work, executes it through supported provider interfaces, persists all state locally in SQLite, and treats rate limits and upstream availability as normal operational states instead of edge cases.

This project is intentionally compliance-bounded:

- It does not scrape Claude.ai.
- It does not automate the Claude consumer website.
- It does not inspect consumer session-limit counters.
- It relies only on supported Anthropic APIs, SDKs, documented CLI-style integrations, and explicitly configured Codex CLI execution.

## Architecture

The codebase is one-language Python and splits responsibilities cleanly:

- `claude_orchestrator/repository.py`: durable SQLite persistence for jobs, runs, conversation state, artifacts, scheduler events, and message batch metadata.
- `claude_orchestrator/workspaces.py`: safe per-job workspace preparation, optional git worktree creation, and conservative cleanup helpers.
- `claude_orchestrator/services/orchestrator.py`: central orchestration logic for enqueue, retry, cancel, crash recovery, batch polling, and state transitions.
- `claude_orchestrator/services/worker.py`: scheduler/worker loop with concurrency limits and duplicate-claim prevention.
- `claude_orchestrator/backends/`: pluggable backend adapters.
- `claude_orchestrator/web/`: FastAPI + Jinja2 + HTMX dashboard.
- `claude_orchestrator/guardrails.py`: explicit compliance boundaries that reject consumer-site automation directions.

## Provider vs Backend

Provider and backend are separate concepts:

- Provider: `anthropic` or `openai`
- Backend: `messages_api`, `message_batches`, `agent_sdk`, `claude_code_cli`, or `codex_cli`

Examples:

- `anthropic + messages_api`
- `anthropic + message_batches`
- `anthropic + agent_sdk`
- `anthropic + claude_code_cli`
- `openai + codex_cli`

The enqueue path infers `provider` from `backend` by default and validates mismatches if you pass both explicitly. Jobs and runs persist both fields so the CLI and dashboard can monitor both providers side by side.

## Backends

### `messages_api` (`provider=anthropic`)

Default backend for direct Anthropic API requests. The backend treats the upstream API as stateless, stores conversation history locally, compacts older turns into `compact_summary`, and prioritizes model-continuation follow-ups when a response stops at `max_tokens`.

### `message_batches` (`provider=anthropic`)

Optional bulk backend for many independent non-urgent jobs. Compatible queued jobs are grouped into Message Batches, then polled back into individual job outcomes.

### `agent_sdk` (`provider=anthropic`)

Optional scaffold for documented Anthropic Agent SDK workflows. It is designed around bounded workspaces, tool allowlists, checkpoint-aware resume, and artifact capture.

### `claude_code_cli` (`provider=anthropic`)

Optional wrapper for explicitly configured documented CLI flows. It does not assume undocumented flags or browser automation, bounds stdout/stderr capture, and requires explicit hook allowlists before any local hook command can run.

### `codex_cli` (`provider=openai`)

Optional wrapper for explicitly configured Codex CLI flows. It is intentionally bounded: configurable executable and static args, cwd-based workspace execution, direct prompt delivery from Python, timeout handling, capped stdout/stderr capture, retry classification for transient CLI failures, durable incremental stream events, and no browser automation or consumer-site state.

The preferred Codex path is now:

- prepare the workspace or worktree in Python
- launch Codex inside that directory via subprocess `cwd`
- pass the prompt directly as the final CLI argument
- avoid `--workspace` entirely

On this machine, the installed Codex CLI exposes `codex exec [PROMPT]`, so AI Telescreen defaults to that non-interactive form when `backends.codex_cli.args` is empty. Legacy `command_template` config is still accepted for backward compatibility, but it is no longer the recommended style.

## Workspaces and Worktrees

Coding jobs now run in real isolated workspaces:

- Default mode: an app-created directory under the configured `workspace_root`
- Optional mode: a dedicated git worktree under `workspace_root/<job-id>/worktree`

Worktree-backed jobs use metadata such as:

- `repo_path`
- `use_git_worktree`
- `base_branch`
- `branch_name`
- `cleanup_policy`

The orchestrator persists the resulting `workspace_path`, `worktree_path`, `branch_name`, `base_branch`, and `workspace_kind` back onto the job so the CLI and dashboard can inspect them.

Example enqueue for a worktree-backed Codex job:

```bash
claude-orchestrator enqueue \
  --backend codex_cli \
  --prompt-file task.txt \
  --metadata '{"repo_path":"/path/to/repo","use_git_worktree":true,"base_branch":"main","cleanup_policy":"none"}'
```

Cleanup remains conservative by default:

- `cleanup_policy="none"` is the default
- `cleanup_policy="on_success"` removes only app-created workspaces after successful completion
- `cleanup_policy="on_completion"` removes only app-created workspaces after any terminal state

The original repository working tree is never deleted. For git worktrees, cleanup is skipped if the worktree contains user-visible changes.

## Integration Awareness

AI Telescreen now distinguishes between local-only jobs and workspaces with Claude-side integration configuration.

For each job workspace, AI Telescreen can discover and summarize:

- project-scoped Claude settings in `.claude/settings.json`
- project-scoped MCP definitions in `.mcp.json`
- optional user-scoped Claude settings in `~/.claude/settings.json`
- optional user-scoped MCP definitions in `~/.mcp.json`

The discovery pass is intentionally conservative:

- it parses configuration defensively and records notes for malformed files instead of failing the job
- it summarizes configured capabilities and config paths without attempting a full MCP runtime implementation
- it does not inherit integrations from hosted consumer apps or scrape any hosted product UI

Each job records a durable integration summary so operators can answer:

- whether the job is local-only or integration-enabled
- which project or user config files were discovered
- which MCP or external tool capabilities appear to be configured
- whether the selected backend is integration-aware or effectively local-only

## Saved Projects and Browser Launch

AI Telescreen is now web-first for normal operator control:

- create jobs from the browser
- save projects with repo and launch defaults
- launch jobs from project pages
- duplicate existing jobs
- re-run jobs through a prefilled browser form
- retry or cancel jobs from dashboard and detail views

Saved projects capture lightweight defaults such as:

- `repo_path`
- `default_backend`
- `default_provider`
- `default_base_branch`
- `default_use_git_worktree`
- `notes`

## Stream Events

Long-running coding jobs now emit durable execution stream events into a dedicated SQLite table:

- `process_started`
- `stdout_line`
- `stderr_line`
- `phase_changed`
- `process_completed`
- `process_failed`
- `process_timeout`
- `process_interrupted`

These are exposed in:

- `claude-orchestrator inspect JOB_ID`
- `/api/jobs/{job_id}`
- `/api/jobs/{job_id}/stream-events`
- the job detail page's live activity panel

Claude Code CLI jobs also emit an `integration_context_loaded` stream event when workspace integrations are discovered and passed into the backend context.

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
- `job_stream_events`
- `message_batches`
- `workspace_integrations`
- `saved_projects`

Migrations live in [`claude_orchestrator/migrations/0001_initial.sql`](claude_orchestrator/migrations/0001_initial.sql).
Provider support is added in [`claude_orchestrator/migrations/0002_add_provider_columns.sql`](claude_orchestrator/migrations/0002_add_provider_columns.sql).
Stream event support is added in [`claude_orchestrator/migrations/0003_add_job_stream_events.sql`](claude_orchestrator/migrations/0003_add_job_stream_events.sql).
Integration summary support is added in [`claude_orchestrator/migrations/0004_add_workspace_integrations.sql`](claude_orchestrator/migrations/0004_add_workspace_integrations.sql).
Saved project support is added in [`claude_orchestrator/migrations/0005_add_saved_projects.sql`](claude_orchestrator/migrations/0005_add_saved_projects.sql).

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

The default config format is TOML. A full example lives at [`claude-orchestrator.example.toml`](claude-orchestrator.example.toml).

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
- `backends.codex_cli.args`
- `backends.codex_cli.timeout_seconds`
- `backends.codex_cli.auth_mode`
- `backends.codex_cli.use_git_worktree`
- `storage.sqlite_path`
- `logging.persist_payloads`
- `privacy.enabled`
- `ui.refresh_seconds`

Preferred Codex config:

```toml
[backends.codex_cli]
enabled = true
executable = "codex"
args = []
timeout_seconds = 1800
auth_mode = "auto"
use_git_worktree = true
max_output_bytes = 1048576
```

`args = []` uses the backend's default non-interactive `codex exec PROMPT` behavior. If you need a custom static invocation, add fixed arguments there. Legacy `command_template` remains supported for older configs.

## CLI Usage

The CLI still works, but the browser is now the primary operator surface for everyday launching and monitoring.

Core commands:

```bash
claude-orchestrator enqueue --prompt-file task.txt
claude-orchestrator enqueue --backend codex_cli --prompt-file task.txt
claude-orchestrator enqueue --backend codex_cli --provider openai --prompt-file task.txt
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

The dashboard is now the main cockpit for operator control. It shows:

- state counts
- prominent Create Job and Add Project entry points
- saved projects with launch shortcuts
- a one-pass worker convenience control
- recent jobs across Anthropic and OpenAI providers
- provider/backend badges and workspace paths
- compact integration status badges for local-only vs discovered project or user integrations
- workspace kind, branch/worktree indicators, and recent phase/activity
- integration detail panels with config paths, capability summaries, backend support, and parser notes
- retry timing
- last error summaries
- job detail pages with duplicate and re-run/edit actions
- live progress panels for active codex_cli jobs
- retry/cancel/duplicate actions from the browser
- JSON status endpoints at `/api/status`, `/api/jobs`, and `/api/jobs/{job_id}`
- JSON stream endpoint at `/api/jobs/{job_id}/stream-events`
- simple provider/backend/status filtering

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

The rationale and forbidden-pattern checks live in [`claude_orchestrator/guardrails.py`](claude_orchestrator/guardrails.py).

## Crash Recovery Model

Workers call recovery on startup:

1. Find stale `running` jobs whose leases have expired.
2. If the backend can resume from checkpoint/history, return the job to `queued`.
3. Otherwise schedule a safe retry using the normal retry policy.

During normal execution, background heartbeats keep leases fresh so long-running jobs are not falsely recovered. During shutdown, workers stop claiming new jobs, wait for active work up to a configurable drain timeout, then cancel and persist interrupted jobs into recoverable retry states.
For subprocess-backed Codex jobs, cancellation also terminates the active child process and records an interruption event before the job is moved into a recoverable retry state.

## Current Limitations

- `codex_cli` remains a bounded subprocess backend in this pass rather than a full interactive session protocol.
- saved projects are intentionally lightweight; this pass does not add credentials, secret storage, or branch-management policy beyond existing local config and environment handling.
- The current live activity model is polling-based and deliberately simple; it does not use websockets.
- Workspace cleanup is intentionally conservative and defaults to `none`.
- The dashboard is intentionally operational and lightweight rather than a full multi-agent control plane.

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
