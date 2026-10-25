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
  … last phase ──► status review, pump done, branch + one line per phase
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
- `acceptance` — after review PASS, only on a board with an `acceptance`
  block `{"command": [argv], "what": "…"}`. The job runs the command in the
  worktree against `.pump/acceptance.md` and judges every criterion: `PASS` /
  `FAIL` + findings in `.pump/ACCEPTANCE.md`. an app repo's command is its own
  acceptance script (seeded data, staging, a browser click-through,
  screenshots, query counts). No Android emulator: no vmx/svm.

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
| `accepting` | acceptance job running | PASS → `done` / next phase; FAIL → rebuild if the step's return is unspent, else `asked` |
| `done` | implemented, `status: review` | you merge |
| `error` | a job failed; **no automatic retry** | `taskpump retry <id>` (same step) or `reset <id>` (start over) |
| `capped` | the task used its launch cap; `status: waiting_user`, everything kept | `taskpump retry <id> [--more N]` or answer "go" / "more N" → the same step |
| `skip` | pump ignores this task forever | `taskpump reset <id>` |

`pump_job` and `pump_branch` record which bgjob and which branch own the task;
`pump_phase: k/M` (SPLIT tasks only) the phase that builds next;
`pump_open` the question numbers sent and not yet answered; `pump_blocked`
why a task is blocked on its repo; `pump_failed` the step an `error` task
re-arms to on `retry` (`analyzing` or `answered`); `pump_launches` the
stage launches made for the task, `pump_cap` its raised limit (empty =
`PUMP_TASK_CAP`), `pump_capped` where a capped task resumes (`answered` or
`new`). Owner-set keys: `repo`, `pump_allow`, `deps`, `start_at` (see
Launch order below). The
test-design record is `.pump/TESTFILES` in the worktree (step, commit, blob
hash per file); `reset` deletes it.

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

### Size and one L build per repo (dv-67a6 phase 2)

`size: S|M|L` is an owner-visible key (not `pump_*`; `reset` keeps it). The
analysis report may carry a top-level `SIZE: L` line; the pump writes it only
when the task has no size, so an owner-set size wins. An answer containing
`size M` sets it. The question card shows `Size L`.

- **S** reaches the architect prompt as a hint (`{{SIZE}}`: expect the fast
  PASS path). It never changes the acceptance level: the architect sets that
  (dv-7a10) and the isolation floor stays.
- **L** builds alone on its repo. An L task whose next launch is its first
  build stage (testdesign/implement) is held while another L task on the same
  repo has `pump_build_started` set and is not merged (`done`/archived, the
  same `dep_met()`; a built task in `review` still holds). Pin: `⏸ dv-b · L:
  waits dv-a`. Analysis and architect are never held; a task already building
  is never held, so two L tasks cannot deadlock. In one tick the higher-ranked
  L wins. `pump_build_started` is written at every task's first build launch
  and cleared by `reset`.
- Before an L task's first build, the pump merges the repo's current HEAD
  into its branch (it was cut at analysis). Conflict: `git merge --abort`, a
  comment and a note, and the build runs on the old base.

## Commands

```bash
scripts/taskpump tick                  # one pass (what the timer runs)
scripts/taskpump status [--board app]   # one line per task + today's job budget
scripts/taskpump pending [--board app]  # open question blocks (+ phase, repo)
scripts/taskpump answer dv-4b71 "..."  # record an answer, queue implementation
scripts/taskpump unblock dv-3668       # re-arm after pump_allow / a repos line
scripts/taskpump retry dv-4b71         # re-run the failed step (pump: error)
scripts/taskpump retry dv-4b71 --more 10   # capped: 10 more launches, same step
scripts/taskpump skip dv-4b71          # take a task out of the pump
scripts/taskpump reset dv-4b71 [--drop-worktree]   # put it back in
```

`status` lines, compact for Telegram:

```
dv-ebf7   building  · phase 2/3 · build · job running
dv-4b71   asked     · phase 1/3 · waiting: 1.2, 2.1 · repo /home/x/other
dv-77aa   blocked   · blocked: repo · repo /home/x/unlisted
```

### Status pin (dv-3d3d)

One pinned post in the Pump topic, edited in place: **Needs you** (answer /
`taskpump unblock <id>` / `taskpump reset <id>`), **Running (n/MAX_CONCURRENT)**
with stage, phase k/M and minutes, **Queued**, **Waiting** (held by deps /
`start_at`, dv-67a6), and **Today** (launched
X/DAILY_CAP, agent tasks Y/AGENT_DAILY_CAP). Idle: `🟢 Pump idle · today X/200`;
the pin is never retired. `render_status_pin()` reads every board's
frontmatter, no LLM; `refresh_pin()` pushes it with
`agentctl notify --source taskpump:status --pin-as pump` at the end of every
tick (early cap/slot exits too) and after `answer`, `start`, `unblock`, `skip`,
`reset`. Bot down = skipped, never outboxed; the next tick redraws. The
started/result cards still post (owner 24.10.26: the pin shows state, the
topic the events).

Service control:

```bash
systemctl --user status  taskpump.timer
systemctl --user start   taskpump.service   # force a tick now
systemctl --user stop    taskpump.timer     # pause the whole loop
journalctl --user -u taskpump.service -n 50
```

## Why each constraint exists

- **Isolated git worktree per task** (`../pump-worktrees/<id>`, branch
  `pump/<id>`). The live bot imports from the main checkout; an unattended
  agent editing it would change the running bot's code mid-request. bwrap makes
  the worktree the only writable path, so this is enforced, not merely asked
  for. A checkout is ~6.5 MB of tracked files.
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
- **Per-task launch cap, daily fuse** (dv-32a5, `docs/DECISIONS.md`
  2026-10-24). Every stage launch the tick makes (analysis, architect, tests,
  build, review, acceptance; reruns after a RETURN too) adds one to the task's
  `pump_launches`. At `PUMP_TASK_CAP` (15) the next launch is not made: the
  task goes to `capped` / `waiting_user` with branch, worktree, phase, answers
  and return state untouched, gets one `⏸ <id> · launch cap 15/15` card
  (stage and phase it stopped before) and a "Needs you" line in the pin; it
  shows in `pending` and `status`. `taskpump retry <id>` grants
  `PUMP_TASK_GRANT` (5) more, `--more N` grants N, and so does a chat reply
  "go" / "more N" through `answer` (board agents' `pump_answer`); the grant
  is recorded as a @taskpump comment, not a @user one. The count survives
  retry, answers, RETURN reruns and phases; only `reset` and a fresh `start`
  zero it. A failed launch is not counted. `PUMP_DAILY_CAP` (200, was 16,
  live 100) is only a fuse against a runaway pump / the subscription quota.
- **A systemd timer, not the bot's APScheduler.** That scheduler has known
  weekday and workspace defects (dv-3e91), caps a turn at 300 s, and stops
  reaping while the bot restarts.
- **Failures do not retry.** `pump: error` is terminal until you run `retry`
  or `reset`. A loop that retries a failing job every 10 minutes burns tokens
  all night and tells you nothing new. `retry` (dv-c5ac) re-runs only the step
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
- `PUMP_AGENT_DAILY_CAP` of the daily jobs, the rest wait for tomorrow;
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

## Tuning

Env vars, all read at tick time — set them in `deploy/taskpump.service.template`
(rendered by `scripts/render_deploy.py`) or the installed unit:

| var | default | |
| --- | --- | --- |
| `PUMP_MAX_CONCURRENT` | `1` | simultaneous pump jobs |
| `PUMP_TASK_CAP` | `15` | stage launches per task before it stops in `capped` |
| `PUMP_TASK_GRANT` | `5` | launches one `retry` / "go" on a capped task adds |
| `PUMP_DAILY_CAP` | `200` | fuse: jobs launched per UTC day across all tasks |
| `PUMP_AGENT_DAILY_CAP` | `4` | of those, jobs for agent-filed tasks (label `agent`) |
| `PUMP_ANALYZE_MODEL` | `opus` | same tier the devops chat uses (was `sonnet` until 2026-10-04: all 26 jobs to then ran Sonnet 5) |
| `PUMP_IMPLEMENT_MODEL` | `opus` | |
| `PUMP_TESTDESIGN_MODEL` | `PUMP_IMPLEMENT_MODEL` | test-design job |
| `PUMP_ARCHITECT_MODEL` | `sonnet` | architect job (reading, owner answer dv-a438 2.2) |
| `PUMP_REVIEW_MODEL` | `sonnet` | review job |
| `PUMP_ACCEPTANCE_MODEL` | `sonnet` | acceptance job |
| `PUMP_OWN_STAGES` | `architect,review` | this board's optional stages |
| `PUMP_OWN_PROFILE` | `devops` | this board's job profile |
| `PUMP_WORKTREES` | `../pump-worktrees` | |
| `PUMP_TEST_MEMORY_MAX` | `1500M` | `scripts/testrun` scope cap for a job's tests, on top of `BGJOB_MEMORY_MAX`; bgjob forwards it into the job unit |
| `PUMP_VENV` | `poetry env info -p`, else `sys.prefix` | the one venv with the full dep set |

Timer interval lives in `deploy/taskpump.timer.template` (`OnUnitActiveSec=10min`).

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
