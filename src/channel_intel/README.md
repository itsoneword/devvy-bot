# channel_intel — Channel Place Knowledge Base

A self-contained, **place-centric knowledge base** for the bot's `channel` profile
(a local-area guide: hikes, restaurants, hotels, viewpoints, products). It will run
as a **standalone HTTP MCP daemon** that the channel profile mounts via its `mcp`
seam — the bot core stays untouched and other bots can reuse the same KB later.

This is **Spec B** of the profiles rework. Full design and task-by-task plans:

- Design: `docs/superpowers/specs/2026-05-30-channel-place-kb-design.md`
- Plans: `docs/superpowers/plans/2026-05-31-channel-kb-{1-storage,2-mcp-server,3-ingestion}.md`

## Status (2026-05-31)

| Plan | Scope | State |
|------|-------|-------|
| **1 — Storage** | this package: schema, repos, FTS, dedup, `Store` façade | ✅ **DONE** (14 tests, lint/mypy clean) |
| 2 — MCP server + wiring | FastMCP HTTP daemon + per-profile mount in `sdk_integration.py` + `channel-intel.service` | ⛔ not started |
| 3 — Ingestion | OpenRouter vision tagging, confirm-flow, workflow smoke tests | ⛔ not started |

## What's here now (Plan 1)

- `db.py` — owns its **own** SQLite DB (separate from the bot's `bot.db`). Full schema:
  `places` + `place_images`, `place_references`/`reference_places`, `visits`, `posts`,
  `preferences`, dormant `kb_users`/`comments`, and 3 FTS5 tables. **FTS is kept in
  sync by triggers** — repositories must NOT hand-maintain the FTS index.
- `models.py` — dataclasses (`Place`, `PlaceImage`, `Reference`, `Visit`, `Post`,
  `Preferences`) with `from_row`. JSON columns decode to list/dict.
- `repositories.py` — six repos. Key semantics:
  - `upsert_place` dedups on **name + ~500m proximity**; merges a single match,
    returns `ambiguous` candidate ids on a multi-match (no blind merge).
  - `find_places` does **broad** retrieval with soft feature/type boosts and **never
    drops places for missing attributes** — unknown stays `None` and visible. Hard
    constraints are the caller's job, not the query's.
  - every table carries an `owner` column (always `"me"` in v1).
- `store.py` — `Store` façade over all repos (one connection). This is the surface
  Plan 2's MCP tools call: `await Store.open(path)` then `upsert_place`, `find_places`,
  `record_visit`, `attach_images`, `get_preferences`, …

## Conventions

- Imports use the `src.` prefix: `from src.channel_intel.store import Store`.
- Tests: `tests/unit/channel_intel/` (`poetry run pytest tests/unit/channel_intel`).
- Timezone-aware UTC datetimes; full type hints; black/isort/flake8/mypy clean.
