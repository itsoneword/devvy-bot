"""dv-f34d: feedback v2 -- questions the owner can judge (offline).

Interfaces these tests pin (build must provide them):
  Facts(text, paths, screens, complaint, numbers, reverted)   dataclass
  collect_facts(board, card, git) -> Facts   (git log --grep=<id>, not merge_sha)
  validate_steps(steps, facts_text) -> list[str]
  compose(chain, view, complete, facts: {card_id: Facts}) -> Question | None
      (Question.text / .cls / .image); async
  tick(..., facts={card_id: Facts}) ; log "feedback: skipped internal <id>",
      "feedback: cannot describe <id>"
  preview(board, card_id, *, complete, git, now) -> str   (no state write)
  OPEN_TTL_H == 24, B_WAIT_DAYS == 3, LOOKBACK_DAYS == 5
"""

import asyncio
import json
import subprocess
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.pump2 import cli, feedback
from src.pump2.pin import _git
from src.pump2.state import Board, Card

NOW = datetime(2026, 11, 18, 12, 0, tzinfo=UTC)


def mk(cid, title="t", body="", deps=(), parent="", pump2=None):
    fm = {"id": cid, "title": title, "deps": list(deps), "parent": parent}
    if pump2:
        fm["pump2"] = pump2
    return Card(path=Path(f"/tmp/{cid}.md"), fm=fm, body=body)


def chain_of(cid, when="2026-11-10T00:00:00Z", **kw):
    c = mk(cid, pump2={"merged": "b" * 40}, **kw)
    c.body += f"\n### @pump2 — {when}\nmerge: merged {'b' * 40} into main\n"
    (ch,) = feedback.build_chains(Board(name="devvy", repo=Path("/tmp")), [c])
    return ch


VIEW = {"prod_sha": "bbbbbbb", "prod_time": "", "staging": []}


def jsn(**kw):
    d = {
        "class": "a",
        "change": "Новая кнопка Экспорт",
        "steps": ["Открой Настройки", "Нажми Экспорт"],
        "before": "не было",
        "after": "есть",
        "look_for": ["файл пришёл"],
        "metric": "",
    }
    d.update(kw)
    return json.dumps(d, ensure_ascii=False)


def completer(answer):
    async def c(*a, **k):
        return answer

    return c


FACTS = feedback.Facts(
    text='+ "export": "Экспорт"\nОткрой Настройки\nНажми Экспорт',
    paths=["src/bot/locales/ru.json"],
)


# --- C1 ---------------------------------------------------------------------


def test_C1_preview_prints_without_state_write(tmp_path, monkeypatch, capsys):
    state = tmp_path / "feedback.json"
    monkeypatch.setattr(feedback, "STATE", state)
    sent = []
    board = Board(name="devvy", repo=tmp_path)
    card = mk("dv-50c8", body="## Description\nэкспорт\n")
    monkeypatch.setattr(feedback, "collect_facts", lambda *a, **k: FACTS)
    text = feedback.preview(
        board,
        "dv-50c8",
        complete=completer(jsn()),
        git=lambda *a: "",
        now=NOW,
        cards=[card],
    )
    assert "dv-50c8" in text and "Нажми Экспорт" in text
    assert not state.exists() and not sent


def test_C1_cli_has_feedback_preview(monkeypatch, capsys):
    seen = []
    monkeypatch.setattr(
        cli, "load_boards", lambda *a, **k: {"devvy": Board("devvy", Path("/tmp"))}
    )
    monkeypatch.setattr(
        feedback, "preview_card", lambda boards, full, **k: seen.append(full) or "TEXT"
    )
    rc = cli.main(["feedback", "preview", "devvy:dv-50c8"])
    assert rc == 0 and seen == ["devvy:dv-50c8"]
    assert "TEXT" in capsys.readouterr().out


# --- C3 ---------------------------------------------------------------------


def _repo(tmp_path):
    r = tmp_path / "repo"
    r.mkdir()

    def g(*a):
        subprocess.run(["git", *a], cwd=r, check=True, capture_output=True)

    g("init", "-b", "main")
    g("config", "user.email", "t@t")
    g("config", "user.name", "t")
    (r / "a.txt").write_text("x\n")
    g("add", ".")
    g("commit", "-m", "init")
    (r / "ru.json").write_text('{"export_button": "Экспорт данных"}\n')
    g("add", ".")
    g("commit", "-m", "dv-77aa: add export button")
    first = _git(r, "rev-parse", "HEAD")
    (r / "other.txt").write_text("follow-up\n")
    g("add", ".")
    g("commit", "-m", "unrelated follow-up")
    return r, first


def test_C3_facts_from_grep_not_merge_sha(tmp_path):
    r, first = _repo(tmp_path)
    head = _git(r, "rev-parse", "HEAD")  # merge_sha points at the follow-up
    card = mk(
        "dv-77aa",
        body="## Description\nКнопка экспорта в настройках.\n## Comments\n"
        "### @x — 2026-11-10T00:00:00Z\nRESULT: кнопка Экспорт данных готова\n",
        pump2={"merged": head},
    )
    f = feedback.collect_facts(Board(name="devvy", repo=r), card, _git)
    assert "Экспорт данных" in f.text  # the real diff
    assert "follow-up" not in f.text
    assert "Кнопка экспорта в настройках" in f.text  # full description
    assert "RESULT: кнопка Экспорт данных готова" in f.text  # last result
    assert first in f.commits if hasattr(f, "commits") else True


def test_C3_validate_steps_drops_unmatched():
    text = "Открой Настройки и нажми Экспорт данных"
    steps = ["Открой Настройки", "Нажми Волшебную кнопку"]
    assert feedback.validate_steps(steps, text) == ["Открой Настройки"]


def test_C3_unmatched_step_dropped_in_question():
    ans = jsn(steps=["Открой Настройки", "Нажми Волшебную кнопку"])
    q = asyncio.run(
        feedback.compose(chain_of("dv-1"), VIEW, completer(ans), {"dv-1": FACTS})
    )
    assert "Открой Настройки" in q.text
    assert "Волшебную" not in q.text
    assert "сделано:" not in q.text


def test_C3_type_a_without_steps_not_asked(tmp_path):
    ans = jsn(steps=["Нажми Волшебную кнопку"])
    ch = chain_of("dv-1")
    sent = []
    state = tmp_path / "f.json"
    out = feedback.tick(
        [ch],
        now=NOW,
        notify=sent.append,
        state_path=state,
        view=VIEW,
        complete=completer(ans),
        facts={"dv-1": FACTS},
    )
    assert not sent
    assert any("cannot describe dv-1" in line for line in out)
    assert not (feedback.load_state(state)["open"])


# --- C4 ---------------------------------------------------------------------


def test_C4_internal_by_path_never_sent(tmp_path):
    ch = chain_of("dv-9")
    internal = feedback.Facts(text="x", paths=["tests/a.py", "docs/x.md"])
    called = []

    async def c(*a, **k):
        called.append(1)
        return jsn()

    sent = []
    state = tmp_path / "f.json"
    out = feedback.tick(
        [ch],
        now=NOW,
        notify=sent.append,
        state_path=state,
        view=VIEW,
        complete=c,
        facts={"dv-9": internal},
    )
    assert not sent and not called  # no model call for a path-internal one
    assert any("skipped internal dv-9" in line for line in out)
    st = feedback.load_state(state)
    assert st["asked"][ch.feature_id]["class"] == "c"
    assert st["open"] is None


def test_C4_model_class_c_never_sent(tmp_path):
    ch = chain_of("dv-9")
    sent = []
    out = feedback.tick(
        [ch],
        now=NOW,
        notify=sent.append,
        state_path=tmp_path / "f.json",
        view=VIEW,
        complete=completer(jsn(**{"class": "c"})),
        facts={"dv-9": FACTS},
    )
    assert not sent and any("skipped internal dv-9" in x for x in out)


def test_C4_type_b_needs_complaint_and_numbers(tmp_path):
    ans = jsn(**{"class": "b", "metric": "p95 2.1s -> 0.8s"})
    for complaint, numbers, expect in [
        (False, False, 0),
        (True, False, 0),
        (True, True, 1),
    ]:
        f = feedback.Facts(
            text=FACTS.text, paths=FACTS.paths, complaint=complaint, numbers=numbers
        )
        sent = []
        state = tmp_path / f"{complaint}{numbers}.json"
        feedback.tick(
            [chain_of("dv-5")],
            now=NOW,
            notify=sent.append,
            state_path=state,
            view=VIEW,
            complete=completer(ans),
            facts={"dv-5": f},
        )
        assert len(sent) == expect
        if not expect:
            st = feedback.load_state(state)
            (entry,) = st["asked"].values()
            retry = feedback.parse_time(entry["retry_at"])
            assert retry == NOW + timedelta(days=feedback.B_WAIT_DAYS)


def test_C4_constants():
    assert feedback.B_WAIT_DAYS == 3
    assert feedback.OPEN_TTL_H == 24
    assert feedback.LOOKBACK_DAYS == 5


# --- C5 ---------------------------------------------------------------------


def test_C5_open_question_expires_and_next_is_asked(tmp_path):
    state = tmp_path / "f.json"
    old = {
        "open": {"feature_id": "589152ec3a5a", "asked_at": "2026-11-14T09:45:45Z"},
        "asked": {
            "589152ec3a5a": {"n": 1, "last": "2026-11-14T09:45:45Z"},
        },
        "features": {"589152ec3a5a": {"board": "devvy", "links": []}},
        "last_sent": "2026-11-14T09:45:45Z",
        "last_feature": "589152ec3a5a",
    }
    state.write_text(json.dumps(old))
    ch = chain_of("dv-8")
    sent = []
    feedback.tick(
        [ch],
        now=NOW,
        notify=sent.append,
        state_path=state,
        view=VIEW,
        complete=completer(jsn()),
        facts={"dv-8": FACTS},
    )
    st = feedback.load_state(state)
    e = st["asked"]["589152ec3a5a"]
    assert e["expired"]
    assert feedback.parse_time(e["retry_at"]) == NOW + timedelta(days=3)
    assert len(sent) == 1  # another feature in the same tick
    assert st["open"]["feature_id"] == ch.feature_id


def test_C5_expiry_runs_before_open_early_return(tmp_path, monkeypatch):
    """run() must close a stale question even when nothing else is asked."""
    state = tmp_path / "f.json"
    monkeypatch.setattr(feedback, "STATE", state)
    state.write_text(
        json.dumps(
            {
                "open": {"feature_id": "abc", "asked_at": "2026-11-14T09:45:45Z"},
                "asked": {"abc": {"n": 1, "last": "2026-11-14T09:45:45Z"}},
                "features": {},
            }
        )
    )
    feedback.tick([], now=NOW, notify=lambda a: None, state_path=state)
    st = feedback.load_state(state)
    assert st["open"] is None and st["asked"]["abc"]["expired"]


def test_C5_fresh_open_question_stays(tmp_path):
    state = tmp_path / "f.json"
    asked = (NOW - timedelta(hours=3)).strftime("%Y-%m-%dT%H:%M:%SZ")
    state.write_text(
        json.dumps(
            {
                "open": {"feature_id": "abc", "asked_at": asked},
                "asked": {"abc": {"n": 1}},
            }
        )
    )
    sent = []
    feedback.tick(
        [chain_of("dv-8")],
        now=NOW,
        notify=sent.append,
        state_path=state,
        view=VIEW,
        complete=completer(jsn()),
        facts={"dv-8": FACTS},
    )
    assert not sent and feedback.load_state(state)["open"]["feature_id"] == "abc"
