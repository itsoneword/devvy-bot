"""dv-a42c: owner feedback survey over feature chains (pure logic, offline)."""

import asyncio
import json
from datetime import UTC, datetime, timedelta
from pathlib import Path

from src.pump2 import feedback
from src.pump2.state import Board, Card

NOW = datetime(2026, 11, 7, 12, 0, tzinfo=UTC)


def mk(cid, title="t", deps=(), parent="", body="", pump2=None, tmp=Path("/tmp")):
    fm = {"id": cid, "title": title, "deps": list(deps), "parent": parent}
    if pump2:
        fm["pump2"] = pump2
    return Card(path=tmp / f"{cid}.md", fm=fm, body=body)


def merged_comment(when, sha="a" * 40):
    return f"\n### @pump2 — {when}\nmerge: merged {sha} into main\n"


def board():
    return Board(name="devvy", repo=Path("/tmp/repo"))


def gb_cards():
    return [
        mk("gb-1ac7", "admin panel"),
        mk("gb-1ae5", "admin tweak", deps=["gb-1ac7"]),
        mk("gb-1aec", "admin button", parent="gb-1ae5"),
        mk("gb-1af2", "owner feedback after testing gb-1aec: fix label"),
        mk("gb-9999", "unrelated"),
    ]


def test_build_chains_groups_linked_cards():
    chains = feedback.build_chains(board(), gb_cards())
    sets = sorted(sorted(k.card_id for k in c.links) for c in chains)
    assert sets == [["gb-1ac7", "gb-1ae5", "gb-1aec", "gb-1af2"], ["gb-9999"]]


def test_exec_mode_pump2_with_stages_and_returns():
    body = (
        "\n### @pump2 — 2026-11-06T09:00:00Z\nanalyze: plan ready\n"
        "\n### @pump2 — 2026-11-06T09:10:00Z\ntests: written\n"
        "\n### @pump2 — 2026-11-06T09:20:00Z\nbuild: done\n"
        "\n### @pump2 — 2026-11-06T09:30:00Z\nreview: returned to build\n"
        + merged_comment("2026-11-06T10:00:00Z")
    )
    c = mk("dv-1", body=body, pump2={"merged": "a" * 40})
    (chain,) = feedback.build_chains(board(), [c])
    (link,) = chain.links
    assert link.exec_mode == "pump2"
    assert {"analyze", "tests", "build"} <= set(link.stages)
    assert link.returns >= 1


def test_exec_mode_job_and_chat():
    jobbed, plain = mk("dv-2"), mk("dv-3")
    chains = feedback.build_chains(board(), [jobbed, plain], job_cards={"dv-2"})
    modes = {k.card_id: k.exec_mode for c in chains for k in c.links}
    assert modes == {"dv-2": "job", "dv-3": "chat"}


def _merged_chain(cid, when):
    c = mk(cid, pump2={"merged": "b" * 40}, body=merged_comment(when))
    (chain,) = feedback.build_chains(board(), [c])
    return chain


def _view(live=True):
    return {"prod_sha": "bbbbbbb", "prod_time": "", "staging": [("dv-1", "bbbbbbb")]}


def test_eligibility_filters():
    old = (NOW - timedelta(hours=30)).strftime("%Y-%m-%dT%H:%M:%SZ")
    new = (NOW - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    ok = _merged_chain("dv-1", old)
    young = _merged_chain("dv-4", new)
    unmerged = feedback.build_chains(board(), [mk("dv-5")])[0]
    done = _merged_chain("dv-6", old)
    view = {
        "prod_sha": "b",
        "prod_time": "",
        "staging": [("dv-1", "bbbbbbb"), ("dv-4", "bbbbbbb"), ("dv-6", "bbbbbbb")],
    }
    got = feedback.eligible_chains(
        [ok, young, unmerged, done],
        view=view,
        now=NOW,
        min_age_hours=24,
        answered={done.feature_id},
    )
    assert [c.feature_id for c in got] == [ok.feature_id]


def test_tick_one_question_until_answered(tmp_path):
    chains = [
        _merged_chain("dv-1", "2026-11-01T00:00:00Z"),
        _merged_chain("dv-7", "2026-11-01T00:00:00Z"),
    ]
    sent: list = []
    state = tmp_path / "feedback.json"
    kw = dict(notify=sent.append, state_path=state, interval_min=45)
    feedback.tick(chains, now=NOW, **kw)
    feedback.tick(chains, now=NOW + timedelta(minutes=60), **kw)
    assert len(sent) == 1  # question still open
    opened = json.loads(state.read_text())
    assert opened
    feedback.close_open(state, chains[0].feature_id)
    feedback.tick(chains, now=NOW + timedelta(minutes=10), **kw)
    assert len(sent) == 1  # interval not passed
    feedback.tick(chains[1:], now=NOW + timedelta(minutes=120), **kw)
    assert len(sent) == 2
    assert "pump2:feedback" in sent[0]


def test_question_text_and_fallback():
    chain = _merged_chain("dv-1", "2026-11-05T10:00:00Z")
    view = {"prod_sha": "b", "prod_time": "", "staging": [("dv-1", "bbbbbbb")]}

    async def boom(*a, **k):
        raise RuntimeError("model down")

    async def ok(*a, **k):
        return "It makes X do Y."

    text = asyncio.run(feedback.question_text(chain, view, complete=boom))
    assert "dv-1" in text and "2026-11-05" in text and "staging" in text
    assert "pump2" in text or "chat" in text or "job" in text
    text2 = asyncio.run(feedback.question_text(chain, view, complete=ok))
    assert "It makes X do Y." in text2
