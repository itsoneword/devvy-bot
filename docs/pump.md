# Pump: the task pipeline

The pump takes a card from a [deviz](https://github.com/itsoneword/deviz) board
and walks it through a pipeline of agent jobs (per board, see Custom stages) until its branch is merged
into the repo's integration branch. It runs no model itself: each stage is one
detached background job (`scripts/bgjob`) under the board's bot profile, and
each stage's model comes from the `jobs:` section of `config/models.yaml`
(`pump_<stage>` rows, see `docs/configuration.md`).

## Stages

A card in `in_progress` goes **analyze → tests → build → review → merge → done**.
Each stage runs in the card's own git worktree, `<repo>/.pump-worktrees/<id>/`,
on the branch `pump2/<id>`, and writes its report into that worktree's `.pump/`.

| stage | what it does | output in `.pump/` |
|---|---|---|
| `analyze` | reads the card and the repo, writes the plan; may ask you questions | `plan.md` (goal, acceptance criteria, touches, questions) |
| `tests` | writes failing tests for the plan (skipped when the plan touches docs only: build then writes the docs with no test run) | test files, `tests.json` |
| `build` | makes the tests pass; may not edit the files in `tests.json` | code |
| `review` | reads the diff; passes or returns the work to build once | `review.md` |
| `merge` | merges the integration branch into the card branch, runs the board's test command, fast-forwards on green | `merge.log` |
| `mergefix` | only when the mechanical merge did not land: resolves conflicts and says land / retest / stop | `mergefix.md` |
| `stopped` | the card needs a person; the last `@pump2` comment says why | — |

A failed stage reruns once automatically, then the card is `stopped`. A stop
shows the command, its exit code and the tail of the job log. Only `merge`
writes to the integration branch.

## The card's state

The pump keeps one frontmatter key per card, the `pump2:` block (`stage`,
`attempt`, `since`, `branch`, the running `job`), and appends `### @pump2`
comments to the card body (stage results, questions, stops). Everything else
lives in the worktree's `.pump/`. There is no database. Do not hand-edit the
`pump2:` block or set a status the pump manages: use the commands below.

## Set up from scratch

The bot is installed and runs (README). Then, in the bot's checkout:

1. **The repo.** A git repo with one commit, an integration branch and a deviz
   board (`.deviz/tasks/`). Add `.pump-worktrees/` to its `.gitignore`.
2. **A bot profile for the board.** Stage jobs run under it, so the pump needs
   `config/profiles.yaml` even if the bot runs single-profile
   (`PROFILES_CONFIG_PATH` unset: one profile, no file): without the file
   every job fails with `unknown profile`. Copy
   `config/profiles.example.yaml`, keep one profile (any slug) and point its
   `working_dir` at the repo, or keep its workspace and add the repo to
   `extra_dirs` (never `sandbox: false`). `/restart` the bot so its chat
   knows the profile.
3. **The board.** Copy `config/taskpump/boards.example.json` to
   `config/taskpump/boards.json`; set `name`, `repo`, `profile` (the slug
   from step 2) and `pump2_test` (a command that passes on the integration
   branch, absolute paths).
4. **Models (optional).** Without `config/models.yaml` every stage runs on the
   built-in rows: `pump_analyze`, `pump_testdesign` (tests), `pump_implement`
   (build), `pump_review`, `pump_mergefix`; a custom stage `x` on `pump_x`
   (else `jobs.default`). To change one, copy only that row from
   `config/models.example.yaml` (`jobs:`) into `config/models.yaml`; the
   table is in `docs/configuration.md`.
5. **The timer.** `python3 scripts/render_deploy.py`, then
   `cp deploy/rendered/pump2.{service,timer} ~/.config/systemd/user/`,
   `systemctl --user daemon-reload`,
   `systemctl --user enable --now pump2.timer`, and
   `loginctl enable-linger $USER` so it ticks while you are logged out.
   Logs: `journalctl --user -u pump2`. macOS: `render_deploy.py --launchd`
   and the `com.devvy.pump2` launchd agent it prints. Stage jobs start with
   `systemd-run --user`, so tick from a shell or unit that reaches your user
   systemd (not from inside an agent job's box); without systemd they run
   as plain processes.
6. **The pinned summary and messages.** Each tick edits one pinned summary
   of all boards in the `NOTIFICATION_CHAT_IDS` chat, and sends each new
   question or stop into the board profile's chat, all through
   `scripts/agentctl notify`, so the bot must be running. A failed send only
   prints a warning; the tick goes on and retries next time.
7. **Check.** `scripts/pump2 doctor` names every problem with its fix (in
   the bot's checkout: without the bot's settings it skips the profile
   check and says so); then
   move a card to `in_progress` and watch `scripts/pump2 status`.

## Running it

- **Start a card:** move it to `in_progress` (the next tick picks it up at
  `analyze`), or run `scripts/pump2 start <board:id>`.
- **See the pipeline:** `scripts/pump2 status [board]` (`--json` for scripts).
- **One step by hand:** `scripts/pump2 tick [board]` launches whatever is due
  and reaps finished jobs. It fails if `config/taskpump/boards.json` is missing.
- **The timer:** `deploy/pump2.timer.template` runs `scripts/pump2 tick` every
  two minutes; step 5 of Set up from scratch installs it.
- **Another boards file:** `scripts/pump2 --boards <file> <command>` (a trial
  board, a second install).
- **Pause:** `scripts/pump2 freeze` stops new launches, `thaw` ends it,
  `urgent <board:id>` freezes everything but one card.
- `scripts/pump2 --help` lists every command.

## Answering a question

Only `analyze` asks. Its questions land in the card as an `@pump2` comment and
the card waits.

1. `scripts/pump2 pending` lists every card waiting on you, with its `board:id`.
2. `scripts/pump2 answer <board:id> "<your reply>"` resumes it. The reply is
   passed on verbatim: the next stage reads your words.

In the bot, the devops agent does the same with its `pump_pending` /
`pump_answer` tools.

## Repo requirements

- A git repo with at least one commit, and an integration branch (`main` by
  default, `pump2_branch` otherwise).
- A deviz board in it: `.deviz/tasks/` (one `<id>.md` per card).
- A bot profile whose workspace or `extra_dirs` covers the repo: the jobs run
  under that profile, and the worktrees live in `<repo>/.pump-worktrees/`
  (add it to the repo's `.gitignore`).
- A test command the jobs can run from the worktree (`pump2_test`, required:
  merge refuses a card without one).
- An entry in `config/taskpump/boards.json` with `"pump2": true`. The default
  boards file drops every board without it. Start from
  `config/taskpump/boards.example.json`. Upgrading, and what each release lets you switch on: `docs/upgrading.md`.

## Board keys

Each entry of `boards` in `config/taskpump/boards.json`:

| key | required | meaning |
|---|---|---|
| `name` | yes | short lowercase board name; cards show as `<name>:<id>` |
| `repo` | yes | absolute path to the repo top level |
| `"pump2": true` | yes | the pump serves only boards with this flag |
| `profile` | no | bot profile every job runs under; default `devops` |
| `pump2_test` | yes* | the board's test command, run by merge and the build gate; falls back to `test`. *One of the two is required: `pump2 doctor` fails without it |
| `test` | yes* | used when `pump2_test` is unset |
| `pump2_test_core` | no | a faster core test command for the stages |
| `pump2_python` | no | the board venv's python; default: the test command's `.../pytest` with `pytest` replaced by `python`, else `python3` |
| `pump2_test_files` | no | build-gate command template, `{files}` = the shell-quoted test paths (e.g. `node --test {files}`); default `<pump2_python> -m pytest <files>` |
| `pump2_branch` | no | the integration branch; default `main` |
| `merge.mem` | no | memory cap of the merge test run (`{"merge": {"mem": "4G"}}`) |
| `after_sync` | no | argv run in the worktree after each merge of the integration branch into the card branch, before the tests; changes are committed on the card branch, a non-zero exit stops the card. Anything but a list of strings is ignored |
| `deploy.log` | no | absolute path to a deploy.py-style journal (`<ts> <old> -> <new> build=<b> ok`): a merged card is marked live once prod runs it |
| `design` | no | the design-loop block, below |
| `pipeline` | no | the board's ordered stages, built-in names and custom stage objects; see Custom stages. Without it: analyze, tests, build, review, merge |
| `stage_labels` | no | the closed list of card labels a custom stage's `when.labels` may name; any other label switches no stage on |

## Custom stages

A board can add its own judge stages (a copywriter, a UX check, a product
check) with `pipeline`. The order in the list is the only order:

```json
"pipeline": ["analyze", "tests", "build",
  {"stage": "copy", "prompt": "config/taskpump/copy.md", "report": "copy.md",
   "writes": false, "return_to": "build",
   "when": {"touches": ["ui", "docs"], "kind": ["feature", "new screen"]}},
  "review", "merge"],
"stage_labels": ["growth"]
```

| key | meaning |
|---|---|
| `stage` | the stage name, `[a-z0-9]+`, not a built-in name |
| `prompt` | the prompt file, relative to the board's repo |
| `report` | the file the stage writes in `.pump/`; default `<stage>.md` |
| `writes` | `false` (default): the stage may change nothing but its report; `true`: its changes are committed. Only stages before `review` may write |
| `return_to` | the earlier stage a `return` sends the card back to |
| `when` | optional: run only for matching cards. Keys `touches` (code, db, ui, docs), `size` (S, M, L), `kind` (bug, feature, new screen, docs, ops), `labels` (from `stage_labels`). All keys must match, any value within one key. analyze sets touches, size and kind; a card that has none of them does not match |

The prompt may use `{{CARD}}`, `{{PLAN}}`, `{{DIFF}}`, `{{WORKTREE}}`,
`{{BRANCH}}` and `{{PREV}}` (the reports of earlier custom stages). The pump
appends the report contract. The report has these sections:

```
## Verdict
pass | return
## Remarks
1. [C2] the button says "Submit", the plan asks for "Send" | evidence: ui/form.tsx:12
2. the empty state could be friendlier
## Scores
C1: 8 | the copy matches the plan
C2: 4 | one label is wrong
```

A remark returns the work only when it names a BLOCK criterion of the plan
(`Cn`) or a defect class (D1 red tests, D2 security, D3 regression with a
reproduction, D4 change outside the plan) and carries `| evidence:`. Every
other remark is advice in the card comment, and a `return` without a blocking
remark counts as a pass. A card returns once per run: a second return from any
judge stage stops it. A missing or malformed report stops the card with the
error and the raw text. `## Scores` (review writes it too) is stored for
comparison and never changes the verdict. Missing or bad scores only add a
warning line.

A copywriter prompt file (`config/taskpump/copy.md`; a fuller one ships as
`config/taskpump/copy.example.md`) could read:

```
You are the copywriter. Read every user-facing string the diff adds or changes
and judge it against the plan: tone, clarity, spelling, consistent terms.
{{CARD}}
{{PLAN}}
{{DIFF}}
```

Each custom stage runs on its own `config/models.yaml` row
`jobs: pump_<stage>:` (`pump_copy` here, e.g. `{models: {claude: sonnet}}`).
Without that row it uses `jobs.default`.

The pipeline is checked when the boards file is loaded. Any of the following
raises an error that names the board and the rule:

- it does not start with `analyze` or end with `merge`;
- it has no `build`;
- a writing stage sits at or after `review`;
- a name is bad, duplicated or unknown;
- `return_to` is not an earlier stage;
- the prompt file is missing;
- a `when` key or value is unknown.

## Design loop (advanced)

A card labelled `designloop` is not picked up by the tick. Run it with
`scripts/pump2 design run <board:id>`: it generates 2–3 UI variants, renders
each, runs a gate and critic rounds, and posts the variants for you to pick;
your pick is answered like any pump question. The card names its screen with
a front-matter key `design_screen: <id>` (an id from `docs/features.yaml`).

The board needs a `design` block. Start from the two examples
`config/taskpump/design_render.example.py` and
`config/taskpump/design_gate.example.py`: copy them into your repo (e.g.
`scripts/`) and **commit** them (variant worktrees are cut from HEAD).

```json
"design": {
  "render": ["/abs/repo/.venv/bin/python", "scripts/design_render.py",
             "--worktree", "{worktree}", "--out", "{out}", "--screen", "{screen}"],
  "gate": ["/abs/repo/.venv/bin/python", "scripts/design_gate.py",
           "--worktree", "{worktree}", "--renders", "{renders}",
           "--refs", "{refs}", "--screen", "{screen}"],
  "python": "/abs/repo/.venv/bin/python",
  "variants": 3
}
```

| key | meaning |
|---|---|
| `render` | argv; `{worktree}` `{out}` `{screen}` substituted |
| `gate` | argv; `{worktree}` `{renders}` `{refs}` `{screen}` substituted |
| `python` | a python with Pillow (the collage step and the example gate) |
| `variants` | optional, how many variants; clamped to 2–3 |

If any key is malformed the whole block is ignored and the loop is off for
that board. `scripts/pump2 doctor <board>` checks the block: the scripts exist
in the repo, the first argv token is executable, `{out}` / `{renders}` are
there, and `python` can `import PIL`.

**Render.** Runs with cwd = the variant's worktree (`{worktree}`); once more
for the current app, whose PNGs become `{refs}`. Write one
`<state>-<theme>.png` per state into `{out}`, themes `dark` and `light`, all
at one fixed size. Exit non-zero only when nothing rendered: a failing state
is left out and the gate reports it missing. Two rules:

- **Per-run temp DB.** Variants render in parallel. Every run seeds its own
  throwaway DB or data dir; renders sharing one database wipe each other's
  seed data mid-shot.
- **No build artefacts.** A worktree is a fresh checkout: no installed
  dependencies, no build output. Build or serve the app from the worktree's
  own code.

**Gate.** Mechanical, no model. Print one JSON list of failures (or
`{"failures": [...]}`) on stdout; `[]` = pass; non-JSON output counts as a
broken gate. The example fails a missing or extra state, a wrong size, a
blank image, identical dark/light, or a render identical to its ref.

**Brief.** The loop reads `docs/features.yaml` (what must stay reachable),
`docs/SCREENS.md` (the screen's passport, shared tokens), `docs/UI_GUIDE.md`
(rules) and `docs/screens/<screen>/<state>-<theme>.png` (committed refs).

Output lands in `<repo>/.screens/design/<card>`: add `.screens/` to
`.gitignore`.

## How it actually works

What runs where, with the code behind each claim. The stage list and what each
stage writes: [Stages](#stages); a board's own stages: [Custom stages](#custom-stages).

**The tick is a script, not a model.** `pump2.timer` fires every 2 min
(`deploy/pump2.timer.template:8`) and runs `scripts/pump2 tick`
(`deploy/pump2.service.template:12`). `tick()` (`src/pump2/runner.py:257`) only
reads cards, applies holds (`hold_reason`, `runner.py:296`), reaps finished
jobs and launches due ones. No stage runs inside it; its one model call is
the pin recommendation (`pump_summary`, `deploy/pump2.service.template:21-23`).

**Each model stage is a fresh, detached agent job.** `bgjob_launch_stage`
(`src/pump2/runner.py:209-254`) runs `scripts/bgjob run --profile <board
profile> --cwd <the card's worktree> --route pump_<stage> --setting-sources
project <prompt>`. bgjob hands `--setting-sources` on to `scripts/agentjob`
(`scripts/bgjob:1179-1183`), which builds a `JobSpec` (`scripts/agentjob:222-240`).
The job has no memory of earlier stages: no session is resumed, and the stage's
whole context is the prompt that code builds:

- the card digest, `card_digest` (`src/pump2/state.py:532`): frontmatter + body
  verbatim, every non-pump comment, only the last `@pump2` comment, the card path;
- the stage's files from `.pump/`: tests and build get `plan.md`
  (`src/pump2/stages/tests.py:20`, `build.py:25`), build also `tests.json`
  (`build.py:24`), review gets the plan, the diff, `build.md` and its earlier
  `review.md` remarks (`review.py:58-63`, `:190-192`), a custom stage gets
  `{{PLAN}}`, `{{DIFF}}`, `{{PREV}}` (`src/pump2/stages/custom.py:66-80`),
  mergefix gets `merge-facts.md` (`mergefix.py:59-61`).

A prompt over 100 KB goes to `.pump/prompt-<stage>.md` and the job is told to
read it (`runner.py:214-225`).

**What the stage agent sees** (Claude harness, `job_options`,
`src/model_router/job.py:392-430`):

| question | answer | evidence |
|---|---|---|
| the board repo's `CLAUDE.md` | **yes**, the worktree's own copy (cwd = worktree, project source) | `job.py:405` `setting_sources=list(spec.setting_sources)`, i.e. `["project"]` from `--setting-sources project`; owner/ancestor CLAUDE.md files are dropped by `claudeMdExcludes` (`job.py:408`). A probe of a real stage job listed only the worktree CLAUDE.md |
| the repo's `.claude/skills/` | **yes**: the project source loads them; user/plugin skills and the deviz SessionStart hook do **not** load | `src/model_router/job.py:104-108` (JobSpec comment), FINDINGS 2026-10-25 |
| the profile persona | **no**: `--profile` gives policy flags only (sandbox, unrestricted, extra dirs); no `system_prompt` is set and no `PromptComposer` runs on the job path | `scripts/agentjob:92`, `:190-240`; `job.py:392-430` has no `system_prompt`; `grep -n "persona\|PromptComposer\|system_prompt"` over `job.py`, `job_codex.py`, `agentjob`, `bgjob` finds nothing |
| subagents (Agent/Task tool) | **yes** on Claude: no tool is disallowed, and subagents run on `CLAUDE_CODE_SUBAGENT_MODEL` | `job.py:392-430` (no `allowed_tools` / `disallowed_tools`), `job.py:438` |

On the Codex harness, the stage gets the repo's `CLAUDE.md` as its project doc
(`src/model_router/job_codex.py:17-18`, `:80`), file and shell tools only: no
bot MCP tools and **no** subagents (`job_codex.py:18-19`). Either harness runs
every tool call through the one policy hook (`job.py:413-424`).

**Stage table.** The route is `STAGE_ROUTE` (`src/pump2/runner.py:45-51`); a
custom stage `x` runs on `pump_x` (`route_of`, `runner.py:56-63`). Its model is
the `jobs:` row of that name in `config/models.yaml` (reference:
`config/models.example.yaml:85-100`), else the built-in row in
`src/model_router/routes.py:397-412`, else `jobs.default` (`routes.py:389`).

| stage | script or agent | input | output | model route |
|---|---|---|---|---|
| analyze | agent (`stages/analyze.py:35`) | card digest, an earlier `plan.md` if any | `.pump/plan.md`; `finish` sets `touches`, `size`, `kind`, `criteria_sha` (`analyze.py:181-188`) | `pump_analyze` |
| tests | agent (`stages/tests.py:19`) | digest + `plan.md` | test files, `.pump/tests.json` | `pump_testdesign` |
| build | agent (`stages/build.py:23`) | digest + `plan.md` + `tests.json`, review remarks on a return | code, `.pump/build.md` | `pump_implement` |
| custom `x` | agent (`stages/custom.py:66`) | the board's prompt file with `{{CARD}}` … `{{PREV}}` | `.pump/<report>` | `pump_x` (no built-in row: `jobs.default` until configured) |
| review | agent (`stages/review.py:158`) | digest + plan + diff + `build.md` | `.pump/review.md` | `pump_review` |
| merge | **script, no model**: `bgjob_launch` runs `scripts/pump2 merge` with no `--route` (`runner.py:160-186`) | the card branch, the board's test command (`src/pump2/merge.py:40`) | `.pump/merge.log`; fast-forward or hand-off to mergefix (`merge.py:296`) | none |
| mergefix | agent (`stages/mergefix.py:59`) | digest + `.pump/merge-facts.md` | `.pump/mergefix.md` | `pump_mergefix` |

A card whose `touches` are docs only skips the tests stage: analyze sends it
to build (`src/pump2/stages/analyze.py:191`), which writes the docs from a
docs-only prompt and commits with no test run when there is no `tests.json`
(`build.py:42`, `:113`), then review.

## Writing a card

The one set of card rules for every board. Agents: read this before you create
a card; personas do not repeat it.

**What the author writes** (frontmatter + body):

| field | meaning |
|---|---|
| `title` | one line: what changes, not why |
| description (body) | the problem, the wanted result, links or file paths you already know; the owner's words when the card is theirs |
| `labels` | topic tags. Only the labels listed below change the path |
| `deps` | `[dv-a, dv-b]`: ids **on the same board**. The card is held until each is `done` (`src/pump2/state.py:475-488`); a missing id holds it forever (`dv-x(missing)` in `pump2 status`). A `board:id` prefix is not resolved: it reads as missing |
| `start_at` | ISO time; held until then. No zone = UTC (`state.py:460-466`, `:472-474`) |
| `status` | `todo` when filed; `in_progress` starts the pump. Never set `waiting_user`, `stopped` or `done` yourself |

**What analyze computes, not the author:** `touches` (code, db, ui, docs),
`size` (S, M, L), `kind` (bug, feature, new screen, docs, ops; lists:
`src/pump2/criteria.py:20-22`) and the acceptance criteria. All of them go in
`.pump/plan.md`, and the first three go in the `pump2:` block too
(`analyze.py:181-188`). Writing them on the card changes nothing. Put what
must be true in the description; analyze turns it into criteria.

**Never touch:** the `pump2:` block and any old `pump*` key (see
[The card's state](#the-cards-state)). Use `scripts/pump2` commands.

**Labels that change the path** (closed list; any other label switches nothing on):

| label | what it does |
|---|---|
| `designloop` | the tick skips the card (`runner.py:288`); it runs only via `scripts/pump2 design run <board:id>` ([Design loop](#design-loop-advanced)). Hard-coded: `src/pump2/design.py:41`, `:73` |
| a board's `stage_labels` entry (e.g. `growth`) | switches on a custom stage whose `when.labels` names it (`runner.py:73-90`, [Custom stages](#custom-stages)). No board in `config/taskpump/boards.json` sets `stage_labels` today, so on every live board this row is empty |

On a board that sets `stage_labels`, the card gets one comment naming its other
labels: "they switch no stage on" (`warn_labels`, `runner.py:123-134`).

**Minimum fields.** Give the title, a description that says what should be
true afterwards, and `deps` / `start_at` only when they are real. Leave out
everything analyze computes or the pump writes.

Bad:

```yaml
---
title: fix pump
labels: [code, urgent, M]
size: S
touches: [code]
deps: [app:ab-1234]
pump2: {stage: build}
---
it's broken
```

(No problem or result. `size`/`touches` are ignored. `code`, `urgent` and `M`
switch nothing on. The cross-board dep holds the card forever. The `pump2:`
block skips analyze.)

Good:

```yaml
---
title: pump2 status shows why a held card waits
labels: [devops]
deps: [ab-0042]
---
`scripts/pump2 status` lists held cards without the reason. Show the
`hold_reason` text (deps / start_at) next to each held card, so the owner
does not open the card file.
```
