# taskpump — unattended research → questions → implementation

The loop that removes the "sit and click yes" step. You move a task to
`in_progress` on the deviz board; the pump researches it, asks you at most
three questions on Telegram, waits for your answer, then implements and tests
it on a branch and hands it back at `review` for you to merge. A big task can
come back **split into phases** instead (see below).

## The shape of it

```
  you: board → in_progress
        │
        ▼
  [tick]  ──► bgjob: analyse in worktree pump/<id>   (opus, via scripts/agentjob)
        │        writes docs/research/<id>/analysis.md  (long, for the implementer)
        │        writes .pump/QUESTIONS.md              (short, for you)
        ▼
  [tick]  ──► reap → append to task → status waiting_user → Telegram:
        │        plan (3-6 bullets) + ≤3 questions, each with a default
        ▼
  you: reply "dv-4b71: 1 default, 2 no"  in the devops chat
        │
        ▼
  devops agent: taskpump answer dv-4b71 "<verbatim>"  → pump: answered
        │
        ▼
  [tick]  ──► (SCENARIOS? bgjob: test design first, see below)
        │    bgjob: implement on the same branch, run tests, commit
        │        writes .pump/RESULT.md
        ▼
  [tick]  ──► reap → status review → Telegram: branch + test result
        │
        ▼
  you: merge (or don't)
```

### SPLIT tasks (dv-ebf7)

The analysis picks `SMALL` (above, unchanged) or `SPLIT`: 2–5 phases, each a
one-line goal + its own WRITES + ≤3 questions. One Telegram message lists all
phases, each phase's test SCENARIOS (≤5 × 80 chars, cut first if the
message would exceed 3000 chars) and its questions numbered `1.1`, `2.3`…;
you answer once. Nothing builds before `taskpump answer` — your answer is the
approval of the plan and its scenarios (no timeout auto-approve). SMALL
reports may carry SCENARIOS too. The build prompt gets them as binding tests
(SPLIT: per phase from `.pump/PHASES.md`; SMALL: `.pump/SCENARIOS.md`).
Then:

```
  answer ──► [test design k] ──► reap: only tests/ changed? commit
                  ▲                │   "<id>: phase k/M tests", hashes → .pump/TESTFILES
                  │                ▼
                  │          build phase k ──► reap: protected tests untouched?
                  │                │   commit "<id>: phase k/M — <goal>", short note
                  │                ▼
                  └─── pump: answered, pump_phase k+1/M (same tick launches it)
  … last phase ──► [acceptance, whole branch] ──► status review, pump done
```

**Test design (step 2).** A step with approved SCENARIOS (a SPLIT phase, or
a SMALL task that lists them) gets a test-design job before its build
(`config/taskpump/testdesign.md`). It writes one test per scenario, plus up
to 3 obvious ones it marks `added:`, **only under `tests/`**, runs them and
reports which fail on current code (the new behaviour) and which pass
(guards):

```
TESTFILES
tests/unit/test_x.py
SCENARIOS
- <scenario> → <test name> → fails|passes
- added: <scenario> → <test name> → fails|passes
TESTS
<one line>
```

The pump diffs the worktree against a snapshot taken at launch: any path
outside `tests/` → stop with the paths. Otherwise it commits the `tests/`
files and records their blob hashes. The build prompt lists those files as
"make these pass, do not edit them; if one is wrong, ask under QUESTIONS".
After the build the pump re-hashes them (all test-design files on the
branch, earlier phases too): any change → stop with the `git diff --stat`,
nothing committed; the reply goes to a fresh test-design run, which keeps,
fixes or restores them, then the build resumes. A step without SCENARIOS
(and every SMALL task without them) skips test design — exactly today's
path. Phase notes say `N tests (k new failing → now pass)`.

A phase **stops** (`pump: asked`, `status: waiting_user`, `pump_phase`
unchanged, its work left uncommitted) when its test-design job crashes, writes
no report or writes outside `tests/`; when its build edits a protected test;
when its build crashes / exits non-zero / writes no report, its `TESTS` line says `FAIL` (or has no verdict and a
non-zero failed count) or is missing, or its report has a `QUESTIONS` block.
`taskpump answer` re-runs that phase on top of what it already did. Each phase
is one or two jobs (test design + build), and `MAX_CONCURRENT` and
`DAILY_CAP` count both — a task held by the daily fuse
waits in `answered` with its `pump_phase`; one at its own launch cap goes to
`capped` (see Caps).

The pump writes the answered plan to `.pump/PHASES.md` in the worktree; the
build prompt gets "you are phase k of M: <goal>; only do this phase".

Report format (analysis, literal):

```
SPLIT

PHASE 1: <goal, ≤90 chars>
WRITES
/abs/path
SCENARIOS
- <test scenario, ≤80 chars>
QUESTIONS
1. <question> (default: <…>)

PHASE 2: <goal>
…

TESTS
<one line>
```

### Cross-project tasks (dv-ebf7 step 3)

`repo: /abs/path` in a task's frontmatter makes the pump work in that git
repo (absent = this repo, unchanged). The worktree stays at
`../pump-worktrees/<id>` and is the only writable path (+ `pump_allow`), but
it is a checkout of that repo; branch `pump/<id>` and the pump's commits live
there, and the messages name `repo:` + branch. `.pump/` is kept out of the
commits by pathspec, never by editing that repo's ignore rules. The board
stays here; the other repo's own `.deviz/`, if any, is not touched.

Only repos listed in `config/taskpump/repos` (one absolute top-level path per
line, `#` comments, hand-edited only) are accepted. Unlisted, not a git
repo's top level, or missing → `pump: blocked`, `pump_blocked: repo:<new|answered>`,
and a message with the exact line to add; `taskpump unblock <id>` re-checks
and puts the task back where it was. The templates then tell the agent it is
in another repo and must find that repo's test command in its own docs
(CLAUDE.md / README / pyproject / package.json) and state it on the TESTS
line; the `PUMP_VENV` rule applies to this repo only.


### Other boards and intake (dv-57ba)

`config/taskpump/boards.json` lists other repos whose *own* deviz board the
pump serves (absent = this board only, as before). Each entry is a short
`name` and the repo's top level; listing it also allows that repo the way a
`repos` line does. Everything above then applies per task:

- **Ids.** Boards mint ids independently (`dv-xxxx` here, another prefix
  on an app board; any `<prefix>-<id>.md` is a task file), so another board's task
  is `<name>:dv-xxxx` in every message, `status`, `pending` and job name. This
  board's ids stay bare. Commands take either form; a bare id that exists on
  two boards exits naming both (`devvy:dv-xxxx`, `other:dv-xxxx`) — answering the
  wrong one would start a build nobody asked for.
- **Repo.** The task's repo is its board's repo (an explicit `repo:` still
  wins and is checked as usual). Worktree `../pump-worktrees/<name>/<id>`,
  branch `pump/<id>` in that repo, commits there; merging is yours.
- **Caps.** `PUMP_MAX_CONCURRENT` and `PUMP_DAILY_CAP` are global: the
  limits are this host's RAM and the subscription quota, not a board's. The
  per-task launch cap (below) is per task on every board.

**Intake.** A board may name an app-owned pair of commands (argv, run with
cwd = the board's repo): `list` prints a JSON array of open items, each with
an `id`; `close` is run once per item with `{id}` substituted. At the top of
every tick the pump files all *new* items as **one** `todo` task on
that board (label `intake`, created through `DevizBoard`, item fields dumped
into the description minus reporter ids — `user_id`, `telegram_id`, `chat_id`,
`username` — because board files are git-tracked), closes them, and sends one
`📥` message. No job runs on it: item text comes from outsiders, so it reaches
a model only when the owner moves the card to `in_progress`, and then every
job prompt carries an untrusted-origin note (dv-7c0e stopgap; the automatic
path is designed in `docs/research/dv-7c0e/design.md`). The pump knows nothing about
what an item is — an app's user-submitted reports are just its own
`tools/dump_reports.py --json` / `--mark-handled` (example). An item filed but not yet
closed is remembered in the state file (`intake`), so a failing `close` is
retried next tick instead of filing the item twice; if the task cannot be
created, nothing is closed. A broken `list` is a journal line, not a failed
tick.

`DevizBoard` needs the bot venv, so the pump re-execs itself under `PUMP_VENV`
when started by a bare `python3` (the systemd unit does). The whole pump,
its config and tests are already outside the public export
(`scripts/export_public.py`), so board names and repo paths stay on this host.

### Per-board profile, entry points, stages (dv-a438)

**Profile.** A boards.json entry may name `profile` (default `devops`): every
job of that board runs `bgjob --profile <it>`, so it gets that profile's tool
policy (app boards are usually sandboxed: no data-root `db/`/`secrets/`) and cost
attribution. cwd and CLAUDE.md already follow the worktree. `test` gives every
job the repo's test command.

**Card routing (dv-a9b0).** A board whose profile is not devops (an app board)
gets its ask and result cards in that profile's own chat: `agentctl notify
--deliver --profile <it> --ref pump:<board>:<id>` → `deliver_profile`, i.e. its
bound topic (e.g. the app's topic), else the owner DM. Pump gets a one-line `↗`
pointer per such card and keeps all step chatter (▶ started, ↩ returned) for
every board; devvy cards are unchanged (Pump, `--profile devops`). The outbox
keeps the route (`-ask@<id>`, `-res@<id>`). The board agent answers with the
owner-only tools `pump_status` / `pump_pending` / `pump_answer`, pinned to its
own board like `job_start(task_id)` (`status|pending --board <it>`, `answer
<board>:<id> -- <text>`); a sandboxed agent cannot run the script itself.

**Entry points.** One pipeline, two ways in: the owner moves a card to
`in_progress` (kanban; the timer picks it up; intake batches arrive as
`todo` and enter this way), or a chat agent calls `job_start(task_id)`, which runs
`taskpump board-of <its profile>` and then `taskpump start <board>:<id>`
(in_progress + a comment + a tick nudge; refuses a task whose job runs or
that waits on the owner; re-arms a done/error one). Chat agents never build
board tasks themselves. `job_start` without `task_id` stays the one-off job.

**Stages.** `stages` turns on extra jobs, each a separate process in its own
context (templates `config/taskpump/<stage>.md`); this board takes them from
`PUMP_OWN_STAGES` (default `architect,review`):

- `architect` — after the analysis, before the owner sees the card. Checks the
  plan against the repo's `docs/REQUIREMENTS.md` (load targets, no external
  I/O in hot loops, inventory of screens/buttons/commands, one mechanism) and
  writes acceptance criteria to `.pump/acceptance.md`. A repo with a feature
  map (`docs/features.yaml`, dv-224f) gets a lookup note in the analyze and
  architect prompts: grep the map, list `screen.feature` ids on a PLAN
  `features:` line; the architect returns duplicates, dropped scenarios and
  unmapped new features. `PASS` → the usual card
  plus `🏛 architect: PASS · N criteria`; `RETURN analyze` → the analysis reruns
  once with the findings; a check that fails to run sends the card with a note.
- `review` — after every committed build step. `PASS` or `RETURN build` with
  numbered comments; the rebuild gets them verbatim. `RETURN analyze` (dv-1bbe)
  means the plan is wrong: once per task (`pump_replan: 1`, kept across the
  owner hand-off, cleared by `reset`) the analysis reruns with the comments in
  `.pump/REPLAN.md`, old phases and test design are cleared, build commits stay,
  and the new card says "replanned"; a second one goes to the owner.
  architect.md/review.md list drift signs (easier problem solved, masked
  symptom, untraceable changes, unproven claims, padding); each finding must
  cite a task line plus file:line or command.
  **UI gate (dv-4cd8):** a step whose branch diff touches `src/bot/` (not
  only `src/bot/screens/fixtures/`; this repo only) gets a bgjob pre-step on
  its review: `scripts/screens render --out .pump/screens --base <branch
  base>` under testrun (10 min guard, log `.pump/screens/render.log`). The
  review's `{{PHASE}}` block makes any violation of a non-`known_bad` screen
  in `report.json`, or a missing report / render error, a RETURN, and asks
  per `after/*.png` whether a first-time user would understand the screen
  given the ticket's goal (answers under `## Screens` in REVIEW.md). The
  step's result message (▶ phase done or the ✅ card) carries up to 4 after
  PNGs (`agentctl notify --image`). `.pump/screens/` is cleared at every
  review launch.
- `acceptance` — after review PASS, only on a board with an `acceptance`
  block `{"command": [argv], "what": "…"}`. The job runs the command in the
  worktree against `.pump/acceptance.md` and judges every criterion: `PASS` /
  `FAIL` + findings in `.pump/ACCEPTANCE.md`. an app repo's command is its own
  acceptance script (seeded data, staging, a browser click-through,
  screenshots, query counts). No Android emulator: no vmx/svm.

  **The pump runs the command, not the agent** (dv-2514). The agent's Bash
  tool stops at 600 s and a full gymbro run takes ~24 min, so the job runs it
  as a step before its agent: `bgjob run <prompt> --pre-cmd "testrun --mem
  $BGJOB_MEMORY_MAX -- <command (+light_args)>" --pre-log
  .pump/acceptance/run.log --pre-timeout <max_minutes×60>`. run.sh runs it in
  the job's own bwrap box (it is code the build wrote), to completion, writes
  `pre_exit_code` in the job dir and appends `pre-step: exit N after XmYYs` to
  run.log; only then does the agent start. Any exit code hands over (failing
  scenarios are the agent's to judge); the agent reads run.log and the
  artefacts, may re-run one targeted check, never the full command.
  `.pump/acceptance/` is emptied at launch, so a `verdict.json` an older sha
  left is never judged. `acceptance.max_minutes` (default 60) is a hang
  guard, not a budget: past it `timeout` kills the command's process group
  (TERM, KILL 120 s later), bgjob writes `pre_timed_out`, exit_code 124 and
  skips the agent; the reaper treats it as a mechanical stop (not a FAIL, no
  RETURN.md, the step's return unspent) with **one** auto-retry whatever
  `PUMP_AUTO_RETRIES` says (owner 26.10.26), then the owner's card ("hang
  guard"). The step runs inside the `accepting` job, so `PUMP_MAX_ACCEPTANCE`
  still serialises it.

**Acceptance level** (dv-7a10). How much of the acceptance stage a task gets
is the pump-owned key `pump_accept_level`: `full` (default), `light` (the
command plus the board's `acceptance.light_args`; e.g. `--suite
post-deploy`, no full pytest tier; a board without `light_args` runs full) or
`skip` (review PASS finishes the step, the ✅ card says so). The architect sets
it on its `ACCEPTANCE <level>` line (bare `ACCEPTANCE` = full) and answers a
short PASS for a change with no architectural decision; the card shows a
lowered level. The owner overrides it in the answer ("acceptance full",
"приёмка: без"). A task whose title/labels/description name user or account
isolation (`ISOLATION_WORDS`) is always full. Review always runs. The build
cannot change the level: it lives on the board, outside the worktree.

**Level from the diff, test tier** (dv-87b4). `accept_decision(task)` returns
the level and a short reason the card shows (`Acceptance queued: light (no
heavy paths)`); `accept_level` is its level. Order: non-final phase → skip;
isolation → full; the owner's answer, kept in its own key
`pump_accept_owner` (the architect keeps `pump_accept_level`); then floors that
raise to full — a label in `FULL_LABELS` (perf, db, migration, load, scale), a
`pytest.` tag in `.pump/acceptance.md` (a light run never reaches that tier),
a branch diff (`git diff --name-only <branch_base> HEAD`) touching a board
`acceptance.full_paths` glob (fnmatch), or a diff git cannot produce ("branch
diff failed"); then the architect's level, and with none `light` on a board
with `light_args`, else full. The diff is read at review PASS / acceptance
launch, not at plan time. The architect prompt says "when unsure, light".
gymbro's `full_paths`: alembic, storage models/db, demo/seed, the test and
acceptance infra; not `storage/repository.py` (in 8 of 17 recent diffs; owner
26.10). On a board with acceptance, `test_run` (`{{TEST_RUN}}`) tells build,
review and test design to run changed-area tests only, never the whole suite:
the full acceptance run owns it. Boards without acceptance are unchanged: there
the build's tests are the only ones before merge. Measured on 17 recent gymbro
tasks (`docs/research/dv-87b4/analysis.md` §2-3): 14 full → ~6 full, ~8 fewer
full runs × ~14 min ≈ 2 h of acceptance, ≈8 000 fewer TRUNCATE cycles, and no
whole-suite build runs (≈100 min / ~44 DB cycles per 3 days).

**Acceptance once per task, on the last phase** (dv-41ef). `accept_level`
returns `skip` for phase k<M, so a non-final phase goes from review PASS to the
next phase ("Acceptance deferred to phase M/M"); phase M runs the task's level
over the whole branch. Isolation tasks defer too. The architect prompt gets the
level section and the tag rule only on a board with acceptance (`accept_note`,
`{{ACCEPT}}`): every UI/flow criterion ends with `[journey.<id>]` or
`[pytest.tests/…_flow.py]` (needs full), or the acceptance script's TAG path
cannot fail it. Test design lists the `pytest.tests/` tags of
`.pump/acceptance.md` as files to create, plus one existing-data / duplicate
case per flow. Acceptance reports a criterion with only source evidence as
FAIL `not exercised`.

**Question triage** (dv-84c2). The architect also triages every card
question (`config/taskpump/triage.md`, rendered into architect.md as
`{{TRIAGE}}` with `taskpump decisions` for the task's own board: the owner's
earlier answers, never another board's). It ends its report with `TRIAGE`:
`KEEP <n> <category>` or `DECIDE <n>: <choice> — <reason>`, numbers as on the
card. `PUMP_TRIAGE` decides what the card does with it:

- `shadow` (default, owner 25.10): every question asked as before, a
  `🤖 would decide: …` note under each DECIDE. Watch-only data.
- `on`: a DECIDE is not asked; it stays on the card under its own number as
  `🤖 1 decided: <choice> — <reason>`, numbers never shift, `pump_open` lists
  only the kept ones. Nothing kept → "reply “go”"; the task still waits. The
  owner overrides by number ("1 no, …"); implement.md tells the build that a
  decided item stands unless his reply names it.
- `off`: no triage in the prompt, card as before.

A DECIDE on a question matching `TRIAGE_FLOOR` (keys, prod, deletion, money,
…) or on an isolation task is always asked, with `⚠ always asked` on the
card. No TRIAGE, no valid line, a RETURN or a failed architect → the card is
exactly the untriaged one. The architect's PASS comment records the marks;
its `Not asked (triage decided): …` line keeps a decided item the owner let
stand out of later precedents (the pump's answer is not his).

**One return per step.** `pump_return: 1` marks it spent. A review RETURN and
an acceptance FAIL share it: the first rebuilds (then review, then acceptance
again), the next goes to the owner with the comments on the card and in the
task file. A review/acceptance job that crashes, exits ≠0, writes nothing or
no verdict goes to the owner too, never to done; the reply re-runs that stage.

Jobs are reaped on the following tick, so a result reaches Telegram within ~10
minutes of the job ending. The one exception is `taskpump answer`, which pokes
the timer immediately — that is the single step where you are actually sitting
at the keyboard waiting.

No LLM runs in the pump process itself. It reads frontmatter, reaps finished
jobs, starts new ones. Every judgement call happens inside a `bgjob`; every
message you see is rendered in Python, so a chatty agent cannot turn a question
block into an essay.

## State machine

One frontmatter key, `pump`, kept separate from the human `status` column:

| `pump` | meaning | next |
| --- | --- | --- |
| *(absent)* | eligible if `status: in_progress` | analysis job |
| `analyzing` | analysis job running | reap → `asked`, or `answered` + `pump_architect: due` |
| `architecting` | architect job running | `asked` (PASS, 2nd RETURN, crash), or analysis rerun |
| `asked` | questions sent, `status: waiting_user` | your answer |
| `blocked` | plan writes outside the sandbox, or `repo:` not usable; **not answerable** | `pump_allow:` / a `repos` line, then `taskpump unblock` |
| `answered` | you replied, or the previous phase finished | test-design job if the step has SCENARIOS and no tests recorded for it, else build job |
| `testing` | test-design job running | reap → `answered` (tests committed), or stop (`asked`) |
| `building` | build job running | reap → `done`, next phase (`answered`), `answered` + `pump_review: due`, or stop (`asked`; also on any protected-test edit) |
| `reviewing` | review job running | PASS → `done` / next phase / `pump_accept: due`; RETURN → rebuild once, then `asked` |
| `accepting` | acceptance job running (the command step, then the agent) | hang guard / pre-step OOM / BLOCKED (environment, dv-6423) → one auto-retry (`ENV_RETRIES`), then `asked` ("not a FAIL", reply re-runs acceptance); PASS → `done` / next phase (a `DEFERRED` finding on an `(after deploy)` criterion adds "owed after deploy"); FAIL → rebuild if the step's return is unspent, else `asked` |
| `done` | implemented, `status: review` | you merge |
| `error` | an analysis or SMALL build failed and its auto-retries are spent | `taskpump retry <id>` (same step) or `reset <id>` (start over) |
| `capped` | the task made `PUMP_STALL_CAP` launches with no progress; `status: waiting_user`, everything kept | `taskpump retry <id> [--more N]` or answer "go" / "more N" → the same step |
| `skip` | pump ignores this task forever | `taskpump reset <id>` |

`pump_job` and `pump_branch` record which bgjob and which branch own the task;
`pump_phase: k/M` (SPLIT tasks only) the phase that builds next;
`pump_open` the question numbers sent and not yet answered; `pump_blocked`
why a task is blocked on its repo; `pump_failed` the step an `error` task
re-arms to on `retry` (`analyzing` or `answered`); `pump_stall` the
stage launches made since the task's last progress, `pump_cap` its raised
limit (empty = `PUMP_STALL_CAP`), `pump_capped` where a capped task resumes (`answered` or
`new`), `pump_autoretry` the mechanical stops of the current step that
were retried automatically (see Bounded auto-retry). Owner-set keys: `repo`, `pump_allow`, `deps`, `start_at` (see
Launch order below). The
test-design record is `.pump/TESTFILES` in the worktree (step, commit, blob
hash per file); `reset` deletes it.

### Cards the pump leaves alone: label `designloop` (dv-9133)

A card labelled `designloop` is run by the designloop runner, never the pump
(`is_designloop()`): the tick does not launch it even when it is
`in_progress` or `answered`, it takes no slot, `is_queued` is false (no pin
**Queued**, no "eligible next tick"), and `taskpump start` / `urgent` refuse
it with one line and leave the card file as it was. The plain label `design`
is a topic tag (gb-1aa9, gb-1a80 mid-pipeline) and filters nothing. The UI
task a design card serves carries `deps: [<designloop card>]`, so the dep
hold below keeps it waiting until the owner's pick sets the design card
`done`.

**The runner** (phase 3): `taskpump design run <board:id>`, started from the
board agent's chat with `job_start(command=…)` / `scripts/bgjob run --cmd`,
never by the tick. The card needs `labels: [designloop]`, `design_screen:
<features.yaml screen id>` and the problem in its `## Description`; the
board needs a `design` block in `boards.json` (render / gate argv, the
board's python). Steps: brief (`src/designloop/brief.py`) → one
`scripts/agentjob` turn per variant on the `design_generate` job route
(Claude, sonnet, not opus: owner 27.10), each in its own worktree
`pump-worktrees/<board>/<card>-vN` on branch `design/<card>/vN`, committed by
the runner → render + mechanical gate per variant (a failure goes back to the
generator and is not a round) → Codex critic (`design_critic`) → stop on all
ready, 3 rounds, or the same notes twice (`src/designloop/loop.py`). A ready
run sets `design_state: awaiting_pick` + `design_variants: [...]` and sends
ONE collage to the board's chat (`notify(images=…)`, outbox-safe); anything
else sets `design_state: failed` and sends a short "design failed: reasons"
note, no choice (owner 26.10 22:31). Unresolved critic notes go in the card
comment, per variant.

**The pick** uses the usual answer channel: `taskpump pending` lists an
`awaiting_pick` card as `DESIGN PICK`, and `taskpump answer <card> "2"` sets
`design_pick: v2`, `design_branch`, `status: done`. Anything but a number of
an offered variant exits non-zero with "ask again", the card unchanged. The
build card's dep is then met, and every stage prompt of it gets a "Design
input" block naming the branch (`design_input_note`).

**Who files designloop cards:** the gymbro agent or the owner, by hand, when
a task adds a screen or a block; text and spacing tweaks skip it. The build
card gets `deps: [<designloop card>]`. The gymbro side (`scripts/design_render.py`,
`scripts/design_gate.py`, the `frontend-design` skill) is a gymbro card
(owner 3.1); until it lands `design run` refuses with the missing script.

### Launch order: deps and start_at (dv-67a6)

A queued task (`in_progress` with no `pump`, or `answered`) is **held** —
not launched, takes no slot, a lower-ranked task gets the slot — while:

- a `deps: [dv-a, app:dv-b]` entry is unmet. Met = `status: done` or the file
  is in the board's `.deviz/tasks/archive/` (deviz `isReady`); `dep_met()`
  is the one place dv-2263 switches to "live". A bare id is on the task's own
  board, `board:dv-x` names one. An unknown id stays unmet: `(unknown)`.
- its `start_at:` (ISO 8601; naive or a bare date = UTC) is in the future.
  The owner renamed `not_before` to `start_at` (25.10); `not_before` is not read.

The gate covers every launch, not only the first, so a dep added to a
mid-pipeline task holds its next stage too. A hold is recomputed each tick
and written nowhere. Pin: **Waiting** section (`⏸ dv-x · waits dv-y`,
`🕑 dv-x · starts 2026-10-26 02:00 UTC`). A dep cycle (over unmet deps
only) or an unparseable `start_at` holds too and shows under **Needs you**
(`🔁 dv-a · dep cycle dv-a → dv-b → dv-a`) — pin only, no card, so it does
not repeat every 5 minutes. `taskpump status` prints `dv-x  held: <why>` and
leaves it out of "eligible next tick"; `taskpump start` says when it is held.

### Waiting on you: the hourly summary (dv-5e12)

A hold that only the owner can release used to be silent (gb-1a84/1a85 sat
39 h behind an unmerged dep). `notify_waits()` runs every tick and collects
what waits on the owner: a queued task whose unmet dep is in review
(`pump: done`, unmerged; age from `pump_done_at`, else `updated`), asked,
capped, blocked, failed, or not in the pump at all (todo / hand-set
`waiting_user`); a dep cycle or bad `start_at`; an open pump question
(`asked`), a capped / blocked / failed task; and a card in `waiting_user`
with no `pump` key (`⚠ gb-x · waiting_user outside the pump: nothing will
run it → taskpump start gb-x`, also a Needs-you line in the pin). A dep still
queued or running is not the owner's and is never listed.

Once one of them has waited `PUMP_HOLD_NOTIFY_HOURS` (1) and was not told
yet, the board's chat (home board: Pump topic; others `--profile P
--deliver`) gets one plain message `🔔 <board> · waiting on you (k)` with
every due wait on that board, oldest first, e.g. `⏸ gb-1a84 waits on
gb-1a7c — in review 10h → merge it`. At most one per board per hour; a told
wait is not repeated alone, only listed again in a later summary that a new
wait triggers. Nothing new = no message (owner 27.10 09:44). The state file
keeps `waits` (`<id>|<blocker>` → first seen, told) and `waits_told`
(board → last summary); the hold itself stays computed. A changed blocker is
a new wait. "First seen" is the first tick that sees it, except a hand-set
`waiting_user` card, which counts from its `updated`.

### Size (dv-67a6 phase 2; L-hold dropped in dv-5e12)

`size: S|M|L` is an owner-visible key (not `pump_*`; `reset` keeps it). The
analysis report may carry a top-level `SIZE: L` line; the pump writes it only
when the task has no size, so an owner-set size wins. An answer containing
`size M` sets it. The question card shows `Size L`.

- **S** reaches the architect prompt as a hint (`{{SIZE}}`: expect the fast
  PASS path). It never changes the acceptance level: the architect sets that
  (dv-7a10) and the isolation floor stays.
- **L** holds nothing (dv-5e12, DECISIONS 2026-10-27): L tasks on one repo
  build in parallel, like M and S, and their conflicts are resolved at merge.
  The old one-L-per-repo hold serialised builds but not merges (the holder
  reaches HEAD only when you merge it), so it never cut the conflict rate
  and held answered work for up to 39 h. After merging the second of two
  branches, re-run the board's tests on the result. `pump_build_started` is
  still written at every task's first build launch and cleared by `reset`.
- Before an L task's first build, the pump merges the repo's current HEAD
  into its branch (it was cut at analysis). Conflict: `git merge --abort`, a
  comment and a note, and the build runs on the old base.

## Commands

```bash
scripts/taskpump tick                  # one pass (what the timer runs)
scripts/taskpump status [--board app]   # one line per task + today's job budget
scripts/taskpump status --json          # [{id, title, pump, status, board, group, why, hours, first}] (/menu Pump)
scripts/taskpump pending [--board app]  # open question blocks (+ phase, repo)
scripts/taskpump answer dv-4b71 "..."  # record an answer, queue implementation
scripts/taskpump unblock dv-3668       # re-arm after pump_allow / a repos line
scripts/taskpump decisions [--board app]   # owner's past answers = triage precedents
scripts/taskpump retry dv-4b71         # re-run the failed step (pump: error)
scripts/taskpump retry dv-4b71 --more 10   # capped: 10 more launches, same step
scripts/taskpump skip dv-4b71          # take a task out of the pump
scripts/taskpump reset dv-4b71 [--drop-worktree]   # put it back in
scripts/taskpump gc [--dry-run]        # remove finished cards' worktrees (dv-4b6f)
```

`status` lines, compact for Telegram:

```
dv-ebf7   building  · phase 2/3 · build · job running
dv-4b71   asked     · phase 1/3 · waiting: 1.2, 2.1 · repo /home/x/other
dv-77aa   blocked   · blocked: repo · repo /home/x/unlisted
```

### Usage-limit pause (dv-7ad9)

A stage whose agentjob ends on a usage limit ("You've hit your weekly limit ·
resets 9am (UTC)") did not fail. `src/model_router/quota.py::parse_limit`
classifies the text; agentjob then exits `QUOTA_EXIT` (4) and prints
`agentjob quota: <message> · until <ISO UTC>` on stderr (the model_calls row
stays `outcome=error`). A job from before that change (exit 1 with the raw
`agentjob failed: <limit text>` line) is recognised too; only agentjob's own
lines count.

`reap` checks this first, before any per-stage handling, and pauses through
the one freeze (dv-b17e), no second pause path:

- the task goes back to its step as a freeze leaves it (`pump: answered`, or
  `""` for analysis, `pump_frozen: <stage>`); no auto-retry, no question, no
  `error`; its launch and the day's launch are refunded;
- `frozen = {by: "quota", until, reason}`; `until` is the parsed reset
  (bare clock = next occurrence; `Oct 27, 9am (Europe/X)`; `resets in 2h`),
  else now + 1h. Every other running job is stopped, the urgent one too;
- one Telegram line per pause: `⏸ лимит кончился, Pump на паузе до HH:MM UTC`.
  A second job hitting the limit only moves `until` later;
- the first tick at or after `until` thaws (`thaw_all`, the code behind
  `taskpump thaw`), silently, and relaunches in the same tick. If the limit
  is still on, that relaunch hits it and pauses again: that is the re-probe.

An owner freeze (`taskpump freeze`, `urgent`) is never given an `until` and
never auto-thawed; a limit hit then only stops that task (an urgent task also
gives up its lane, with one line). `taskpump freeze` during a quota pause
turns it into the owner's.

**Shared limit record (phase 2).** Any bgjob that ends on the limit (exit 4
plus the quota line; pump stages are bgjobs too) is recorded by `bgjob
summarize` in `DataLayout.model_quota`
(`<root>/workspaces/devops/model_quota.json`): `quota.record(limit, source)`.
`quota.active(now)` returns it until its reset and `None` after; nobody
deletes it, and an unreadable file blocks nothing. An earlier reset never
shortens an active record. Readers:

- every pump tick: an active record freezes the pump exactly like its own
  stage would (`by: quota`, one line), or moves a quota pause's `until` out.
  So a merge job that hits the limit first pauses the pump too. An owner
  freeze is left alone;
- `bgjob run` of a prompt job is refused with the reset time (`--force`
  starts it anyway); `--cmd` jobs make no model call and always run;
- `agentjob` itself refuses at start (exit 4 and the quota line, no model
  call), for any caller that skips bgjob; bgjob `--force` passes
  `--ignore-quota`.

The finish ping of an exit-4 job reads `⏸ … stopped on usage limit (resets
HH:MM UTC)` instead of `FAILED (rc=4)`. Any failed job whose cwd is left with
`MERGE_HEAD` / `rebase-merge` / `rebase-apply` adds `⚠ <cwd> is mid-merge
(mid-rebase) — resolve by hand`; nothing aborts it automatically.

**Расход / outage panel.** `/limits` (owner, no LLM; same text as /menu →
Расход) shows the Claude line (`Claude: лимит кончился, до HH:MM UTC` from
`quota.active()`, or "записи нет"), paid spend today/7d per provider, and the
Codex limits only if Codex was used in the last 7 days. It has no buttons:
freeze/thaw (`taskpump freeze|thaw --by owner`) lives on the Pump screen
only, the chat Claude/Codex switch is /model, the jobs switch is
/menu → 🧠 Модели. Old taps still work: `lim:*` (freeze/thaw/chat switch) go to
`_handle_limits_callback`, only `limits:jobs:*` lands in `switch_jobs`. A chat or scheduled Claude turn whose
result is a limit line is `is_error`, `error_type "quota"` (model_calls
`outcome=error`) and writes the same record (`sdk_integration`, once on the
final result), so the next pump tick freezes too.

**Owner /menu (dv-6104).** `/menu` (owner, no LLM,
`src/bot/orchestration/admin_menu.py`) is the hub over the same pieces:
Расход = the /limits text (`limits_panel`), Pump = ONE `status --json` call (dv-f6af): "работает N/max", "Ждут
тебя" (needs-group rows and review cards older than 6 h) as buttons that open
the task's card (`menu:task:<id>`: ⚡ first, 🚨 urgent — asks "freeze
everything?" first —, "всё по умолчанию" / "Ещё 5" = `answer <id> go`), running
and held tasks one line each, Freeze/Thaw (the `lim:` handler above, only on
this screen) and the scoped /stop. `status --json` rows come from `pin_items`,
the classifier the status pin also renders from: `group` is
needs/running/queued/waiting/stopped/review, `why` the pin's reason, `hours`
the running elapsed or review age, `first` the ⚡ flag. `src/model_router`
imports `router` lazily so `taskpump` no longer loads claude_agent_sdk
(`status --json` 2.3 s → ~1 s). Every `menu:*` tap is
owner-gated in the callback itself and edits the menu message in place.

### Status pin (dv-3d3d)

One pinned post in the Pump topic, edited in place: **Needs you** (answer /
`taskpump unblock <id>` / `taskpump reset <id>`), **Running (n/MAX_CONCURRENT)**
with stage, phase k/M and minutes, **Queued**, **Waiting** (held by deps /
`start_at`, dv-67a6), **In review (k)** (`👀 id · in review 10h → merge it`,
built and unmerged longer than `PUMP_REVIEW_HOURS` (6), dv-5e12; not idle),
and **Today** (launched
X/DAILY_CAP, agent tasks Y/AGENT_DAILY_CAP). Idle: `🟢 Pump idle · today X/200`;
the pin is never retired. `render_status_pin()` reads every board's
frontmatter, no LLM; `refresh_pin()` pushes it with
`agentctl notify --source taskpump:status --pin-as pump` at the end of every
tick (early cap/slot exits too) and after `answer`, `start`, `unblock`, `skip`,
`reset`. Bot down = skipped, never outboxed; the next tick redraws. The
started/result cards still post (owner 24.10.26: the pin shows state, the
topic the events).

Every task line in the pin, `taskpump status` (held and `eligible next tick`
lines too) and the digest names the task's title next to its id, cut to 40
chars with … (`_short`); the FROZEN header reads `urgent: <id> — <title>`
(dv-6104).

Service control:

```bash
systemctl --user status  taskpump.timer
systemctl --user start   taskpump.service   # force a tick now
systemctl --user stop    taskpump.timer     # pause the whole loop
journalctl --user -u taskpump.service -n 50
```

### Merged and on prod (dv-2263)

`pump: done` / `status: review` means the branch is ready, not merged, and a
merge is not prod. Every tick `check_deploys()` looks at each `pump: done`
card without `pump_live` (pure git, no job):

- **Merged** = the branch tip (`pump_tip`, recorded at the ✅ card; older
  cards fall back to `pump/<id>` when its tip is a pump commit) is in the
  repo's checked-out `HEAD`. Written once as `pump_merged: <sha12> <time>`:
  the merge commit (oldest first-parent commit of HEAD descending from the
  tip) or, on a fast-forward, the tip. Squash and cherry-pick merges are not
  seen. Status stays `review`.
- **Live** = the board's `deploy` source (boards.json) runs a build that
  contains the merge. `{"log": path}` replays a deploy.py journal (gymbro):
  `ok` lines move prod to their target, `rolled-back` lines move it back;
  live since the first line of the last run of deploys containing the merge,
  with that line's `build=`. `{"started": argv}` (pl: `docker inspect`
  Created; this board: claude-bot's `ExecMainStartTimestamp`) counts the card
  live once prod started after the merge commit. Written once as
  `pump_live: <build> <time>`, and `review` → `done` (owner, Q1.1).
- No `deploy` block (or a task in another repo than its board's) → merged is
  tracked, live never set. A source that fails (missing log, docker down,
  garbage output) is skipped for the tick; the tick never fails on it. Each
  source is read once per board per tick, only when a merged card needs it.
- Comments `Merged @ …` / `Live on prod: …` on the card; merges older than
  7 days (the first run catching up) get the keys without comments. No
  Telegram message — the daily digest reports.
- The pin gains **Merged, not on prod (k)**: `🕓 <id> · merged 31h ago, not
  live` for cards merged more than `PUMP_UNDEPLOYED_HOURS` (24) ago on a
  board with a deploy source. It counts as not idle. `status` shows
  `merged <sha>, not live` / `live <build>` instead of `review`.
- **Daily prod digest** (phase 2): the first tick at/after `PUMP_DIGEST_HOUR`
  (7) UTC sends each board one plain message `📦 <board> · prod digest`:
  `On prod since the last digest` (`✅ id — title (build X)`), `Merged, not
  deployed` (`🕓 id — title · merged 2d 4h ago`, every one still waiting) and,
  for a board without a deploy source, `Merged (no deploy source)` (cards
  merged since the last digest, so each once), and `In review, not merged`
  (`👀 id — title · in review 10h`, built cards unmerged over
  `PUMP_REVIEW_HOURS`, dv-5e12). Nothing to say → no message.
  The home board's goes to the Pump topic, a board with its own profile's to
  that chat (`--profile P --deliver`). The day is marked in the state file
  (`digest.<board>`) when composed, so a digest the bot could not take waits
  in the outbox (`-dig@<profile>`) and is not composed twice. A board's first
  digest looks back 24 h.

### Worktree gc (dv-4b6f)

A finished card's worktree (archived, `status: done` or `pump_merged`) is
only disk: its commits are on `pump/<id>`, and `ensure_worktree` makes the
tree again from that branch on a reset. `taskpump gc` walks `PUMP_WORKTREES`
(top-level ids and `<board>/<id>`), moves each finished tree's `.pump/` to
`PUMP_WORKTREES/.reports/<id>/` (`.reports/<board>/<id>/` off the home board),
then `git worktree remove` + `prune` in the task's repo. The branch stays.
Never removed: a tree with changes outside `.pump` (printed `dirty`), a card
with a stage running, a dir no live or archived card names (printed, kept).
`--dry-run` only lists. A merge younger than 7 days gets a card comment.

The tick does the same right after the merge check only with
`PUMP_REAP_WORKTREES=1`. Default off: owner 26.10, no automatic deletion for
now. Run gc from the live checkout: `PUMP_WORKTREES` defaults to
`<repo>/../pump-worktrees`, which from a pump worktree is the wrong dir.

## Why each constraint exists

- **Isolated git worktree per task** (`../pump-worktrees/<id>`, branch
  `pump/<id>`). The live bot imports from the main checkout; an unattended
  agent editing it would change the running bot's code mid-request. bwrap makes
  the worktree the only writable path, so this is enforced, not merely asked
  for. A checkout is ~6.5 MB of tracked files.
- **Deleting inside the job's own worktree needs no owner** (dv-58db). The
  job's tool policy gets the worktree as `free_root` (agentjob logs
  `free_root=…` at start); `rm`/`unlink`/`rmdir`/apply_patch deletes strictly
  inside it run without an ask. Everything else still asks, and a job ask
  waits 1 h (`ASK_TIMEOUT_SECONDS`) before it is denied.
- **Widening the sandbox is a per-task, hand-typed decision.** Add
  `pump_allow: [/abs/path, …]` to a task's frontmatter and both its jobs get
  that path read-write (`bgjob --allow-dir`). Nothing infers it, and there is no
  global setting — granting an unattended agent write access to live user data
  should cost you the deliberate act of typing the path.
- **The agent declares, the pump verifies.** The analysis template requires a
  `WRITES` block listing every path the implementation will need. The pump
  checks that list against the real writable set and parks the task in
  `blocked` — refusing to accept an answer — if anything falls outside. Prompt
  rules are advisory and an agent can ignore them; this check is not, and the
  worst case of a missing `WRITES` block is the old behaviour (the
  implementation job fails loudly and lands in `error`).
- **The pump commits, the jobs don't.** A worktree keeps its index and HEAD in
  the *main* repo's `.git/worktrees/<name>/`, which the sandbox mounts
  read-only — so every job's `git add` fails on `index.lock`. Granting `.git/`
  would hand an unattended agent the whole object store and every ref including
  main's. Instead the jobs leave their work in the working tree and the pump
  commits it at reap time. (This shipped broken: for the first three days every
  pump branch was empty and the "merge `pump/xxx`" messages pointed at nothing.)
- **The pump is the only writer of `.deviz/`.** Jobs are confined to their
  worktree and physically cannot reach the board, which is what keeps status
  single-writer while an implementation agent is running. Jobs report through
  `.pump/QUESTIONS.md` / `.pump/RESULT.md` instead.
- **`PUMP_MAX_CONCURRENT=1`** (bgjob's own cap is 2). The host has ~3.8 GB RAM,
  the bot is capped at 2.5 GB, and parallel `claude` processes have already
  caused one OOM reboot. Two is the right ceiling when a human is watching a
  run; unattended it is not.
- **Per-task launch cap = launches with no progress, daily fuse** (dv-32a5,
  reworked by dv-5e12, `docs/DECISIONS.md` 2026-10-27). Every stage launch the
  tick makes (analysis, architect, tests, build, review, acceptance; reruns
  after a RETURN, auto-retries and owner retries too) adds one to the task's
  `pump_stall`. Progress — an owner answer (`answer`, not "go") or a finished
  phase — zeroes it and drops any grant. A new commit is not progress: a loop
  can commit every round. At `PUMP_STALL_CAP` (12) the next launch is not
  made: the task goes to `capped` / `waiting_user` with branch, worktree,
  phase, answers and return state untouched, gets one
  `⏸ <id> · no progress in 12 launches (cap 12)` card
  (stage and phase it stopped before) and a "Needs you" line in the pin; it
  shows in `pending` and `status`. `taskpump retry <id>` grants
  `PUMP_TASK_GRANT` (5) more, `--more N` grants N, and so does a chat reply
  "go" / "more N" through `answer` (board agents' `pump_answer`); the grant
  is recorded as a @taskpump comment, not a @user one, so it adds to the cap
  instead of zeroing the count. The count survives retry and RETURN reruns;
  progress, `reset` and a fresh `start` zero it. A same-stage loop inside one
  window needs no rule of its own: a step returns once and auto-retries
  `PUMP_AUTO_RETRIES` times, then asks the owner. A failed launch is not counted. `PUMP_DAILY_CAP` (200, was 16,
  live 100) is only a fuse against a runaway pump / the subscription quota.
- **A systemd timer, not the bot's APScheduler.** That scheduler has known
  weekday and workspace defects (dv-3e91), caps a turn at 300 s, and stops
  reaping while the bot restarts.
- **Bounded auto-retry** (dv-6ffc, `docs/DECISIONS.md` 2026-10-25; replaces
  "failures do not retry"). A mechanical stop — crash, exit code, no report,
  no TESTS line / verdict, tests fail — of analysis, architect, test design,
  build (phase or SMALL), review or acceptance reruns the same stage,
  at most `PUMP_AUTO_RETRIES` (2) times per step (`pump_autoretry`), with the
  earlier reports in the rerun's prompt (`.pump/RETRY.md`) and the work kept
  in the tree. Each rerun is a normal launch: it counts in `pump_stall`,
  the cap and the daily fuse. After N, the old stop runs and its card says
  `auto-retried N×` with each cause. QUESTIONS, a build editing protected
  tests and test design writing outside tests/ go to the owner at once. A
  failing build whose report names a protected test file is retried through
  a test-design run (`retest: auto`): it may change a protected test only
  with a `CHANGED <path> — <reason>` line; a change without one, or a
  deleted file, stops for the owner; an explained one is committed, shown to
  review and put on the card (`⚠ protected test changed`). The count survives
  that hop; a stage with a usable report (not test design), an answer,
  `retry` and `reset` clear it. SMALL builds with red tests now stop like
  phases (owner 25.10). `PUMP_AUTO_RETRIES=0` restores stop-and-ask.
- **`error` is terminal** until you run `retry` or `reset` — reached only
  once auto-retries are spent. `retry` (dv-c5ac) re-runs only the step
  that failed: a crashed analysis re-analyses (nothing was asked yet), a crashed
  SMALL build or a failed launch rebuilds with the answers, branch and
  `pump_return` kept. `reset` wipes all of that and re-asks the owner. Cards
  that errored before `pump_failed` existed are read from the job name.
- **Crashes say why.** A job that dies without an exit code is looked up in
  its unit's journal (`--collect` units leave nothing else); an OOM kill reads
  "OOM-killed at the <BGJOB_MEMORY_MAX> memory cap (peak 2G)" in the card and on
  Telegram, at every stage. No journal outcome → the old "died without writing
  an exit code".
- **Tests run in their own memory scope** (dv-c5ac phase 2). The memory of a
  pump job is the tests the agent starts, not Claude (journal: Claude-only jobs
  peak at 220-650M, heavy-suite builds at 1.5-2G). Every `{{TEST_RUN}}` line tells
  the agent to run them as `scripts/testrun -- <cmd>`: a `systemd-run --user
  --scope` with `MemoryMax=PUMP_TEST_MEMORY_MAX` (1500M). An oversized run is
  OOM-killed alone, the agent survives and reads `testrun: OOM-killed at 1500M
  (peak 1500M)`. A job's worst case is therefore `BGJOB_MEMORY_MAX` +
  `PUMP_TEST_MEMORY_MAX`, times `PUMP_MAX_CONCURRENT` for the host. Two limits:
  the agent opts in by prompt only (pytest run directly is not scoped), and it
  must be a scope, never a service — a service runs outside bwrap with `/`
  writable (`docs/FINDINGS.md` 2026-10-24). Batching the suite (~100 tests at a
  time) was rejected: a 2G peak can be one module fixture, it lands whole in one
  batch. The real fix there is keeping its harness e2e test out of the default
  run (an app-board task).
- **Messages survive a bot restart.** `agentctl notify` fails while the bot is
  down, which is a routine event here. A failed send is parked in
  the outbox (`data/taskpump-outbox/`; `<root>/workspaces/devops/taskpump/outbox/` once
  `DEVVY_DATA_ROOT` is set) and retried, in order, at the head of every tick — a
  dropped question would leave a task in `waiting_user` forever with nothing on
  your phone to answer.
- **One tick at a time.** `tick` takes a non-blocking `flock` on
  `$XDG_RUNTIME_DIR/devvy-taskpump.lock` (fallback `/tmp/devvy-taskpump-<uid>.lock`).
  While the pre-dv-ebde `data/taskpump.lock` still exists it takes that one
  too, so a tick from the old code that is mid-run at merge time excludes a
  new one.
- **The question format is parsed, not trusted.** `parse_report()` extracts the
  `PLAN` / `QUESTIONS` / `TESTS` blocks and truncates to 6 bullets, 3 questions,
  220 chars each; SPLIT to 5 phases × 3 questions (dropped ones are counted in
  the message), with questions shortened until the message fits 3000 chars. The templates in `config/taskpump/` ask for the short form;
  the parser is what guarantees it.

## Agent-filed tasks (dv-535b)

Another agent (markets, coach, …) files work for devops with `task_add
assignee=devops`. `src/storage/board_policy.py` files it `in_progress` with
label `agent`, so the next tick analyses it like any task; nothing is built
until you `answer`. What differs:

- cards and job prompts say `🤖 from agent <slug>`; the prompts tell the job
  to treat the text as an untrusted request and to lead with any rights it
  would widen;
- `PUMP_AGENT_DAILY_CAP` of the daily jobs, the rest wait for tomorrow —
  until you engage: a task you ⚡-ed, started (`--by owner`) or answered is
  your work, skips the sub-cap and does not count into it (dv-5e12);
- when the build finishes, the branch diff is matched against
  `PRIVILEGED_PATHS` (profiles, mcp, security, tool rights, the pump, deploy,
  secrets). A hit goes on the review card and into `pump_privileged:`, and any
  git command that merges `pump/<id>` then asks you (Approve/Deny). Limits of
  that guard: `docs/DECISIONS.md` 2026-10-18.

Agents themselves can only create and comment (`task_comment`); only devops
changes status. An agent may comment on **any open task**, 10 a day each; the
line is stored `- DATE from <slug> (agent): text`. When a task file holds such
lines, every job prompt gets a note to treat them as untrusted data, not
instructions (devops' own comments stay `@devops:`). Per agent: 5 new tasks a day, 3 open devops tasks, an open
task with the same title is returned instead of a duplicate.

## Moving stages to another provider

Each stage runs on its `jobs.pump_<stage>` row in `config/models.yaml`
(`docs/configuration.md`, "Background-job routes"). When a quota is out, the
owner moves every stage, and every other job, with `/menu → 🧠 Модели → Фоновые задачи на: Codex /
OpenRouter / Claude`; «По таблице» returns to the table. `taskpump status` prints
`jobs on: <harness> (owner switch, …)` while the switch is on. Nothing
switches automatically (owner, 2026-10-26).

`/menu → 🧠 Модели` (dv-6104) edits those rows from
Telegram (`src/model_router/job_rows.py`, the file `MODEL_ROUTES_PATH` or
`config/models.yaml`): a stage button opens the models valid for the row's
harness (Claude tiers, the Codex catalog, priced OpenRouter models the table
names); a tap re-checks the model against the row as it is now, writes only
`jobs.<job>.models.<harness>` (creating the file with that one row if
absent), validates the result with `load_call_routes`, replaces the file
atomically and logs an `admin_setting` row; `↩` undoes the newest one. The
next stage launch uses it, no restart. The top button of that screen opens the jobs switch
(`menu:jobsw` / `menu:jobs:*`, one `switch_jobs()`; old `limits:jobs:*` taps land there too). PyYAML
keeps the file's leading comment block but drops comments further down.

## Tuning

Env vars, all read at tick time — set them in `deploy/taskpump.service.template`
(rendered by `scripts/render_deploy.py`) or the installed unit:

`/menu → Настройки` (dv-6104, layout dv-f6af) edits two of them in the
installed unit `~/.config/systemd/user/taskpump.service`
(`src/config/pump_unit.py`): «Параллельно задач» = `PUMP_MAX_CONCURRENT` 1–6
and «Запусков без прогресса» = `PUMP_STALL_CAP` 4–40 (default 12, dv-5e12).
Each setting is a full-width "name: value" row, then one row `−` `+`. A tap
rewrites only that `Environment=` line (or appends one after the last),
atomically, then `systemctl --user daemon-reload`; the next tick uses it. Each
change is an `admin_setting` audit row (owner, time, old→new, unit path); `↩`
undoes the newest one. A missing unit, a non-int value or a name set twice →
"edit by hand", nothing written.

bgjob slots are not a setting (dv-f6af, owner answer 3.1): the tick launches
each `bgjob run` with `BGJOB_MAX_CONCURRENT = max(the unit's value or 2,
PUMP_MAX_CONCURRENT + 1)` (`BGJOB_SLOTS` / `bgjob_env()` in
`scripts/taskpump`), so raising parallel never starves pump jobs of slots. A
higher value set by hand in the unit stays (live: 8). The bot's own
`BGJOB_MAX_CONCURRENT` (its env file, for chat `job_start` jobs) is not shown.

| var | default | |
| --- | --- | --- |
| `PUMP_MAX_CONCURRENT` | `1` | simultaneous pump jobs |
| `PUMP_STALL_CAP` | `12` | stage launches with no owner answer or finished phase before the task stops in `capped` (was `PUMP_TASK_CAP` 15 over the whole task) |
| `PUMP_AUTO_RETRIES` | `2` | automatic reruns of a mechanically stopped step before the owner's card; `0` = stop and ask at once |
| `PUMP_TASK_GRANT` | `5` | launches one `retry` / "go" on a capped task adds |
| `PUMP_DAILY_CAP` | `200` | fuse: jobs launched per UTC day across all tasks |
| `PUMP_AGENT_DAILY_CAP` | `4` | of those, jobs for agent-filed tasks (label `agent`) |
| `PUMP_ANALYZE_MODEL` | `opus` | **deprecated (dv-d63d)**: use `jobs.pump_analyze` in `config/models.yaml`; still applied below the file, with a warning. Opus = the devops chat tier (was `sonnet` until 2026-10-04) |
| `PUMP_IMPLEMENT_MODEL` | `opus` | deprecated, `jobs.pump_implement` |
| `PUMP_TESTDESIGN_MODEL` | `PUMP_IMPLEMENT_MODEL` | deprecated, `jobs.pump_testdesign` |
| `PUMP_ARCHITECT_MODEL` | `sonnet` | deprecated, `jobs.pump_architect` (reading, owner answer dv-a438 2.2) |
| `PUMP_REVIEW_MODEL` | `sonnet` | deprecated, `jobs.pump_review` |
| `PUMP_ACCEPTANCE_MODEL` | `sonnet` | deprecated, `jobs.pump_acceptance` |
| `PUMP_TRIAGE` | `shadow` | architect's question triage on the card: `off`, `shadow` (asks all, notes "would decide"), `on` (decided ones not asked) — dv-84c2 |
| `PUMP_OWN_STAGES` | `architect,review` | this board's optional stages |
| `PUMP_OWN_PROFILE` | `devops` | this board's job profile |
| `PUMP_UNDEPLOYED_HOURS` | `24` | a card merged this long and not on prod shows in the pin (dv-2263) |
| `PUMP_DIGEST_HOUR` | `7` | UTC hour from which a tick sends each board's daily prod digest (dv-2263) |
| `PUMP_REVIEW_HOURS` | `6` | a built card unmerged this long shows in the pin and the digest (dv-5e12) |
| `PUMP_HOLD_NOTIFY_HOURS` | `1` | a wait on the owner this old goes into its board's hourly summary (dv-5e12) |
| `PUMP_WORKTREES` | `../pump-worktrees` | |
| `PUMP_REAP_WORKTREES` | off | `1`: the tick removes finished cards' worktrees, as `taskpump gc` (dv-4b6f) |
| `PUMP_TEST_MEMORY_MAX` | `1500M` | `scripts/testrun` scope cap for a job's tests, on top of `BGJOB_MEMORY_MAX`; bgjob forwards it into the job unit |
| `PUMP_VENV` | `poetry env info -p`, else `sys.prefix` | the one venv with the full dep set |

Timer interval lives in `deploy/taskpump.timer.template` (`OnUnitActiveSec=10min`).

## Outcome analytics (dv-3162)

`scripts/pump_outcomes.py` reports how the pump's stages perform. It breaks the
figures down by stage × harness/model × prompt version:

- first-pass accept;
- returns and rebuilds;
- launches per task;
- owner questions and the triage "would decide" count;
- tokens and cost-equivalent;
- wall time;
- time to merge and to prod;
- post-merge fixes and reverts;
- the top cost sinks.

It is read-only and adds no pipeline step. It reads:

- the job dirs;
- `model_calls` by `job_name`;
- the llm_analytics index, for older jobs;
- the task comments of every board, loaded through this script's own
  `load_tasks` / `board_cards`;
- git.

```bash
poetry run python scripts/pump_outcomes.py --out docs/research/pump-outcomes-$(date -u +%F).md
poetry run python scripts/pump_outcomes.py --json --since 2026-11-01   # machine-readable
```

**Stop classes.** Every stop is either mechanical or a real verdict. Mechanical
stops are quota, launch cap, timeout / no report, failed to run, infra, crash
and freeze. Real verdicts are architect or review RETURN, acceptance FAIL, and a
build whose tests fail. Only real verdicts count toward first pass and returns.
The comment table that does the classifying is `RULES` in the script. When a
new pump comment is added here, give it a row there.

**Prompt version.** It is the short sha of the last commit of
`config/taskpump/<stage>.md` before the job started. A template commit
therefore starts a new group.

**Baseline.** `--baseline` defaults to 2026-10-26 16:24 UTC, the restart with
dv-7ad9, dv-2514 and dv-d63d. A task belongs to the window of its first job.
Older tasks print in a last section headed "Pre-baseline".

Reports live in `docs/research/pump-outcomes-<date>.md`.

## Known sharp edges

- **Answer capture is the one LLM-mediated hop.** The pump's question message
  is an out-of-band `agentctl notify`, so it is not in the devops session
  transcript; the agent recovers context by running `taskpump pending`. The
  rule lives in `CLAUDE.md`. If an answer is ever mis-filed, `taskpump reset`
  and re-answer.
- **Branches accumulate.** `pump/<id>` and its worktree survive until you
  `reset --drop-worktree` or remove them by hand. `git worktree list` to audit.
- **Worktrees branch from `HEAD` at creation.** A task analysed last week is
  implemented against last week's `main`. For a long-parked task, `reset
  --drop-worktree` before answering so it re-forks from current `main`.
- **Disk.** `/` was at 93 % (3.3 GB free) when this was built. Worktrees are
  small, but `jobs/` grows without bound and nothing prunes it.
