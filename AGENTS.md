# AI Telescreen AGENTS.md

## Mission
Build AI Telescreen as a browser-first project cockpit for coding agents.

## Product priorities
- Browser-first UX
- Clear operator actions
- Minimal confusion
- Incremental changes
- Preserve architecture
- No broad rewrites unless explicitly requested

## Architecture rules
- Keep business logic in services and repository layers.
- Keep route handlers thin.
- Preserve orchestrator, worker, and backend separation.
- Preserve saved projects, diagnostics, project manager, and job systems.
- Prefer additive changes over refactors.

## UI rules
- Default UI should be simple and conversational.
- Hide technical details behind disclosure.
- Use compact cards, badges, and bullets.
- Avoid wasted space.
- Avoid raw metadata dumps in primary views.

## Project Manager rules
- Project Manager is advisory by default.
- It should sound like a concise technical project lead.
- It should not expose internal schema labels by default.
- It should offer direct actions like Run it, Edit, and Ask Follow-up.
- It should ask for manual testing when needed.

## Coding-agent output rules
Be concise.
Do not narrate obvious actions.
Only report:
- PLAN
- CHANGED
- RESULT
- TESTS
- BLOCKERS
- NEXT

## Task rules
- Prefer small safe passes.
- Preserve the current architecture.
- Avoid introducing heavy dependencies unless necessary.
- Add or update tests when behavior changes.
- Keep docs aligned with real behavior.

## Validation rules
- Run relevant tests after code changes.
- Make a best effort to verify browser and operator flows when UI changes.
- Do not claim success without checking.

## Communication rules
- Be short.
- Be practical.
- No filler.
- No long essays unless explicitly asked.
