# Profile templates

One directory per shippable profile. Each holds everything needed to bring that
profile into existence on a fresh machine:

| File | What it is |
|---|---|
| `profile.yaml` | The registry entry, as a one-item YAML list. `@SLUG@` is substituted with the target slug; `${DEVVY_ROOT}` is resolved by the registry at boot. |
| `persona.md` | The persona, copied to the profile's `persona:` path on first boot if that file does not exist. |

`devops` deliberately has **no** `persona.md`: it reuses the single-profile
agent persona and names it with an explicit `persona_template:` key, so there
is one copy of that text rather than two that drift.

## What gets installed

`install.sh` instantiates **`devops` + `general`** into `config/profiles.yaml`
(which is gitignored — it is per-machine deployment config, not source).
`chef`, `coach`, `pm` and `channel` ship as templates only.

```bash
python3 scripts/install_profiles.py --list          # what is available/installed
python3 scripts/install_profiles.py --add coach     # append one, idempotent
python3 scripts/install_profiles.py --add pm --slug work   # under a different slug
```

Adding a profile is a config change, and `PROFILES_CONFIG_PATH` is read once at
boot — restart the bot (`/restart` in Telegram, or
`sudo systemctl restart claude-bot`) before the new profile appears.

## Adding your own

From Telegram: `/agents → ➕ New agent` switches you to `devops`, which runs
the `new-agent` skill (`.claude/skills/new-agent/SKILL.md`) — a short
interview that writes a template directory under **`../local/<slug>/`** (same
layout, but machine-specific paths are allowed there and it never ships),
runs `--add`, validates with `scripts/check_install.py --skip-db`, and offers
the restart. `--add` looks in `local/` first, then here.

By hand:

Make a directory, drop in `profile.yaml` and `persona.md`, and it is offered by
`--list` immediately. Two rules the loader enforces, so get them right here:

- A profile that allows the `guest` role can never resolve to `sandbox: false`.
  `install_profiles.py` refuses such a template up front rather than writing a
  config that aborts startup.
- Every path goes through `${DEVVY_ROOT}` (or is relative to `config/`). An
  absolute `/home/you/...` in a template makes it useless on any other machine.
