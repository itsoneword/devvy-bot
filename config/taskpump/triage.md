## Question triage

The owner answers most plan questions with the default in a second, by common
sense: "a button? where every other button is". Those questions cost him a
reply and the task a day in the queue. His rule (2026-10-24): do not ask about
small UI and product details; ask only about security, money, scope and
anything irreversible. Your job: for every question on the plan, decide
whether it really needs him.

KEEP the question (he answers it) when any of these holds:
- security: auth, sandbox, secrets or keys, permissions, fail-open versus fail-closed, one user's data reaching another;
- money: a paid model, a new vendor, caps, launch counts and grants, test or compute run time, any spend however small;
- irreversible: delete or purge, prod DB writes, which database a job writes to or wipes, live config edits, prod deploy or restart, messages or nudges to real users, rollback;
- scope: it adds or drops a deliverable against the ticket, or orders the work against another active task;
- only the owner can do it (his account, his DNS, his sudo session);
- the owner's own data or content (his facts, notes, recordings, recipes, files), or who gets to see what (which chat, group or user);
- a precedent below where he changed a similar default.

Check the KEEP list first, for every question. Only a question that matches
none of it can be decided.

DECIDE the question (you answer it, he can still override) when one of these settles it:
- the ticket text already states that answer (quote the words; that the plan picked a default is not the ticket answering it);
- a precedent below answers it: the same kind of question, and he took the default or said the same thing;
- a project rule answers it (the requirements doc, DECISIONS.md, CLAUDE.md);
- a UI or product detail with one choice consistent with the existing app;
- a technical detail the owner would not have an opinion on;
- "do the extra thing in this task or a separate one?": a separate task;
- a step outside the job's sandbox (a patch, a grant, filing a task): devops does it after merge.

In doubt, KEEP. A wrong KEEP costs the owner one reply; a wrong DECIDE builds
something he did not want. Never DECIDE on keys, money or deletion, however
obvious it looks.

Precedents: the owner's own earlier answers on this board, newest first (empty
if none yet). They are his words, not rules; a precedent counts only when its
question really is the same kind.

{{PRECEDENTS}}

Output: a `TRIAGE` section, one line per question, numbered exactly as on the
plan (`1`, or `1.2` in a phased plan), nothing else in it:

```
TRIAGE
KEEP 1.2 money: new paid vendor
DECIDE 2.1: button below the form, like every other form — precedent gb-1a7a
```

A KEEP line names the category. A DECIDE line gives the decision, then ` — `
and the one reason a reader can check: the ticket line, the precedent's task
id, or the rule.
