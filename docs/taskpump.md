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
`DAILY_CAP` count both — a capped
task waits in `answered` with its `pump_phase`.

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

- **Ids.** Both boards mint `dv-xxxx` independently, so another board's task
  is `<name>:dv-xxxx` in every message, `status`, `pending` and job name. This
  board's ids stay bare. Commands take either form; a bare id that exists on
  two boards exits naming both (`devvy:dv-xxxx`, `other:dv-xxxx`) — answering the
  wrong one would start a build nobody asked for.
- **Repo.** The task's repo is its board's repo (an explicit `repo:` still
  wins and is checked as usual). Worktree `../pump-worktrees/<name>/<id>`,
  branch `pump/<id>` in that repo, commits there; merging is yours.
- **Caps.** `PUMP_MAX_CONCURRENT` and `PUMP_DAILY_CAP` are global: the
  limits are this host's RAM and billing, not a board's.

**Intake.** A board may name an app-owned pair of commands (argv, run with
cwd = the board's repo): `list` prints a JSON array of open items, each with
an `id`; `close` is run once per item with `{id}` substituted. At the top of
every tick the pump files all *new* items as **one** `in_progress` task on
that board (label `intake`, created through `DevizBoard`, item fields dumped
into the description), closes them, and sends one `📥` message; the same tick
then starts its analysis like any other task. The pump knows nothing about
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
| `analyzing` | analysis job running | reap → `asked` |
| `asked` | questions sent, `status: waiting_user` | your answer |
| `blocked` | plan writes outside the sandbox, or `repo:` not usable; **not answerable** | `pump_allow:` / a `repos` line, then `taskpump unblock` |
| `answered` | you replied, or the previous phase finished | test-design job if the step has SCENARIOS and no tests recorded for it, else build job |
| `testing` | test-design job running | reap → `answered` (tests committed), or stop (`asked`) |
| `building` | build job running | reap → `done`, next phase (`answered`), or stop (`asked`; also on any protected-test edit) |
| `done` | implemented, `status: review` | you merge |
| `error` | a job failed; **no automatic retry** | `taskpump reset <id>` |
| `skip` | pump ignores this task forever | `taskpump reset <id>` |

`pump_job` and `pump_branch` record which bgjob and which branch own the task;
`pump_phase: k/M` (SPLIT tasks only) the phase that builds next;
`pump_open` the question numbers sent and not yet answered; `pump_blocked`
why a task is blocked on its repo. Owner-set keys: `repo`, `pump_allow`. The
test-design record is `.pump/TESTFILES` in the worktree (step, commit, blob
hash per file); `reset` deletes it.

## Commands

```bash
scripts/taskpump tick                  # one pass (what the timer runs)
scripts/taskpump status                # one line per task + today's job budget
scripts/taskpump pending               # open question blocks (+ phase, repo)
scripts/taskpump answer dv-4b71 "..."  # record an answer, queue implementation
scripts/taskpump unblock dv-3668       # re-arm after pump_allow / a repos line
scripts/taskpump skip dv-4b71          # take a task out of the pump
scripts/taskpump reset dv-4b71 [--drop-worktree]   # put it back in
```

`status` lines, compact for Telegram:

```
dv-ebf7   building  · phase 2/3 · build · job running
dv-4b71   asked     · phase 1/3 · waiting: 1.2, 2.1 · repo /home/x/other
dv-77aa   blocked   · blocked: repo · repo /home/x/unlisted
```

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
- **`PUMP_DAILY_CAP=16`** (8 until 2026-10-08; a 3-phase task is 5–7 jobs). An unattended loop with no ceiling is a billing
  incident waiting for a bad night's sleep.
- **A systemd timer, not the bot's APScheduler.** That scheduler has known
  weekday and workspace defects (dv-3e91), caps a turn at 300 s, and stops
  reaping while the bot restarts.
- **Failures do not retry.** `pump: error` is terminal until you run `reset`.
  A loop that retries a failing job every 10 minutes burns tokens all night and
  tells you nothing new.
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

Env vars, all read at tick time — set them in `deploy/taskpump.service`:

| var | default | |
| --- | --- | --- |
| `PUMP_MAX_CONCURRENT` | `1` | simultaneous pump jobs |
| `PUMP_DAILY_CAP` | `16` | jobs (analysis, test design, each build) launched per UTC day |
| `PUMP_AGENT_DAILY_CAP` | `4` | of those, jobs for agent-filed tasks (label `agent`) |
| `PUMP_ANALYZE_MODEL` | `opus` | same tier the devops chat uses (was `sonnet` until 2026-10-04: all 26 jobs to then ran Sonnet 5) |
| `PUMP_IMPLEMENT_MODEL` | `opus` | |
| `PUMP_TESTDESIGN_MODEL` | `PUMP_IMPLEMENT_MODEL` | test-design job |
| `PUMP_WORKTREES` | `../pump-worktrees` | |
| `PUMP_VENV` | `…-pfFVaw4a-py3.12` | the one venv with the full dep set |

Timer interval lives in `deploy/taskpump.timer` (`OnUnitActiveSec=10min`).

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
