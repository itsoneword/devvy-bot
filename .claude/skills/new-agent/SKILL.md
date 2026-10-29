---
name: new-agent
description: Create a new bot agent (profile) through a short interview — purpose, access, persona — then scaffold it and offer a restart. Use when the user says "new agent:", asks to create/add an agent or profile, or arrived via the ➕ New agent button.
---

# New agent

You are creating a **profile**: an isolated agent with its own workspace,
memory, persona and permissions, defined by one entry in
`config/profiles.yaml`. The registry is read once at boot, so the last step is
always a restart. Reference for every key: `config/profiles.example.yaml`.

Run this as a conversation, not a form. The user is on Telegram: **one short
question per turn, only the ones whose answer changes the config.** Infer what
you can from their first message and confirm it in a single line rather than
asking again.

## 1. Interview

Collect, in this order, skipping anything already given:

| # | Decide | Default if unsaid |
|---|--------|-------------------|
| 1 | **Purpose** — one paragraph: what it does, what "done" looks like | — (must have) |
| 2 | **Slug + name** — slug `[a-z][a-z0-9_-]*`, short; name is the button label | derive from purpose |
| 3 | **Who** — owner-only, or guests too | owner-only |
| 4 | **Sharing** — per-user workspace/memory, or one shared | per-user |
| 5 | **Extra dirs** — folders *outside* its workspace it must **write** (a notes vault, another checkout). Reads need nothing. | none |
| 6 | **Data tools** — only if it tracks meals/training/health: groups from `logs, products, portions, docs, issues, export` | none |
| 7 | **Persona voice** — language, tone, anything it must never do | terse peer, replies in the user's language |

Rules you enforce, and say so if the user asks for the opposite:

- **Never `sandbox: false` for a new agent.** Full access is what `devops` is
  for. A folder it needs to write goes in `extra_dirs` (dv-3103).
- `extra_dirs` is **owner-only**: if guests may use it, it gets no extra dirs.
- `extra_dirs` must already exist, cannot be `/` or `~`, and cannot overlap
  another profile's `working_dir`/`memory_dir` (this repo included). Check with
  `ls -ld <dir>` before writing anything.
- Web research (WebSearch/WebFetch) is available to every profile; nothing to
  configure.
- Running code in another project = that project's path in `extra_dirs` + a
  persona line saying where the project lives and how to run it.
- An agent that builds or changes a service gets a persona line: "follow
  `docs/standards/observability.md` in the devvy repo" (events, LLM usage,
  spans, health).

## 2. Scaffold

Shipped templates live in `data/templates/profiles/`; **owner-created ones go
to `data/templates/local/<slug>/`** — same layout, but that directory never
ships in the public export and is exempt from the "no absolute paths" template
test, so real machine paths (the vault, another checkout) are fine there.
Create:

```
data/templates/local/<slug>/profile.yaml   # one-item YAML list, @SLUG@ placeholder
data/templates/local/<slug>/persona.md     # the persona
```

`profile.yaml` shape (drop keys that equal the default):

```yaml
# <Name> -- <one line purpose>. Created <date> via the new-agent interview.
- slug: "@SLUG@"
  name: <Name>
  working_dir: ${DEVVY_WORKSPACES}/@SLUG@             # always these three lines, verbatim:
  memory_dir: ${DEVVY_WORKSPACES}/@SLUG@/memory       # the registry points DEVVY_WORKSPACES at
  persona: ${DEVVY_WORKSPACES}/@SLUG@/persona.md      # <DEVVY_DATA_ROOT>/workspaces
  # permission_policy:            # only if guests were requested
  #   allowed_roles: [owner, guest]
  # shared: true                  # only if one shared workspace was requested
  # extra_dirs:                   # owner-only; absolute paths
  #   - /home/<user>/notes
  # user_data_tools: [logs]       # only for tracking profiles
```

`persona.md` — follow the standard shape (dv-8cde): **role → goal → tool
pointers → per-turn rules → safety block → 2–3 worked examples.** Instruction
prose in English; state the reply language explicitly. Name every extra dir
and what goes where (e.g. "Research notes go to `~/notes/study/<topic>.md`
as Obsidian markdown: frontmatter `tags:`, wikilinks for related notes"). Keep
it under ~4k chars; the persona is injected on every turn.

Then, in this order — stop and report at the first failure:

```bash
python3 scripts/install_profiles.py --add <slug>          # finds local/<slug>, appends to config/profiles.yaml
poetry run python scripts/check_install.py --skip-db      # real loader: creates workspace, validates paths + extra_dirs
```

`check_install.py` is the gate: if it fails, fix the template/YAML and re-run
it. **Never leave `config/profiles.yaml` in a state that fails this check** —
the bot will not boot.

## 3. Hand over

Reply with: slug, what it can write to, who can use it, and that a restart is
needed. Then either `/restart` (only as the very last action of the turn — see
the restart rules in your persona) or tell the user to tap **🔄 Restart** in
`/agents → ⚙ Manage`. After the restart, `/<slug>` and the `/agents` button
exist; the user should send `/new` in the new agent's chat once to start a
clean window.

If the user later wants it gone: `/agents → ⚙ Manage` disables it without
deleting anything. Deleting is removing the YAML entry and the workspace by
hand — do not do that without an explicit ask.
