# Upgrading Devvy

Written for the agent that runs the upgrade when the owner says "update to
latest". Run the steps in order from the repository root; each one is safe to
run again. Stop and report on the first step that fails.

Before step 1, note the version you start from: the top `## <version>` heading
of `CHANGELOG.public.md` (1.5 and 1.6 still ran the old task pump,
`scripts/taskpump` and `taskpump.timer`).

## Steps

1. **Pull.** `git status --short` must show no tracked changes (commit or ask
   the owner first), then `git pull`.
2. **Install.** `./install.sh`. It is idempotent: it keeps `.env`, the token,
   the profiles and the owner, and installs new dependencies.
3. **Migrate the pump boards and cards.** Dry run first, read the output, then
   write:

   ```bash
   scripts/pump2 migrate            # dry run: prints what would change
   scripts/pump2 migrate --apply    # writes; boards.json.bak keeps the old file
   ```

   It adds `"pump2": true` to each board in `config/taskpump/boards.json`,
   drops the old-pump keys (`stages`, `acceptance`, `mergecheck`, `intake`;
   `publish` stays, `scripts/release` reads it), adds `pump2_home` for this repo's own board (and creates the file
   when there was none), and clears `pump:` / `pump_*` keys off every card. An
   open card keeps its old state in a `### @pump2` comment; no card is
   started. A second `--apply` must report `0 card(s) changed`.
4. **Timers.** Retire the old pump timer and install the pump2 one:

   ```bash
   systemctl --user disable --now taskpump.timer   # "not loaded" is fine
   python3 scripts/render_deploy.py
   cp deploy/rendered/pump2.{service,timer} ~/.config/systemd/user/
   systemctl --user daemon-reload && systemctl --user enable --now pump2.timer
   ```

   macOS / `./install.sh --local`: the installer's launchd agents already run
   the pump tick; skip this step.
5. **Model routes.** If `config/models.yaml` exists and has `pump_architect` or
   `pump_acceptance` rows under `purposes:`, delete those rows (the stages are
   retired; `scripts/pump2 migrate` names any it finds).
6. **Check.** Both must be clean before the restart:

   ```bash
   poetry run python scripts/check_install.py --preflight
   scripts/pump2 doctor
   ```

   A doctor line about `pump2_test` for the home board: set `pump2_test` in
   `pump2_home` to the repo's test command and run doctor again.
7. **Restart.** From an agent turn use the `restart_bot` tool when you have
   it; otherwise ask the owner to send `/restart` in Telegram (or run
   `sudo systemctl restart claude-bot` from a shell outside the bot).
8. **Offer what is new.** In `CHANGELOG.public.md`, read the `### Enable`
   block of every entry newer than the version you started from (the
   Unreleased one included). List them to the owner in one message and enable
   only what they pick. Nothing there is needed for the bot to run.

## New installs

A fresh `./install.sh` needs steps 4 (pump2 timer, when you use the pump) and
8 (the `### Enable` blocks); `docs/pump.md` sets up a board.
