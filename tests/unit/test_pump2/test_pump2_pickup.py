"""dv-79f0: the tick picks up in_progress cards that have no pump2 block."""

from pathlib import Path

from src.pump2.state import find_card, set_pump2

from .test_pump2 import TASKS, Fake, board, card
from .test_pump2 import repo as repo  # noqa: F401


def _drop_default(repo: Path) -> None:
    (repo / TASKS / "dv-0001.md").unlink()


def _add_fm(path: Path, line: str) -> None:
    path.write_text(path.read_text().replace("title: t\n", f"title: t\n{line}\n", 1))


def test_pickup_sets_analyze_comments_and_launches(
    repo: Path, tmp_path: Path  # noqa: F811
) -> None:
    _drop_default(repo)
    b, f = board(repo, "true"), Fake()
    card(repo, "dv-0002")
    out = f.tick(b, tmp_path)
    c = find_card(b, "dv-0002")
    assert c.pump2["stage"] == "analyze" and c.pump2["attempt"] == 0
    assert "### @pump2" in c.body and "picked up from in_progress" in c.body
    assert "t:dv-0002 picked up from in_progress" in out
    assert [x[0] for x in f.launched] == ["dv-0002"]
    assert f.launched[0][1] == "analyze"


def test_pickup_with_open_dep_is_held(repo: Path, tmp_path: Path) -> None:  # noqa: F811
    b, f = board(repo, "true"), Fake()
    card(repo, "dv-0002", deps="[dv-0001]")
    set_pump2(repo / TASKS / "dv-0001.md", stage="stopped")
    out = f.tick(b, tmp_path)
    assert "t:dv-0002 picked up from in_progress" in out
    assert any(ln.startswith("t:dv-0002 held: deps") for ln in out)
    assert find_card(b, "dv-0002").pump2["stage"] == "analyze"
    assert f.launched == []


def test_old_pump_key_is_left_alone(repo: Path, tmp_path: Path) -> None:  # noqa: F811
    _drop_default(repo)
    b, f = board(repo, "true"), Fake()
    for cid, key in (("dv-0002", "pump: asked"), ("dv-0003", "pump: skip")):
        _add_fm(card(repo, cid), key)
    before = {
        c: (repo / TASKS / f"{c}.md").read_bytes() for c in ("dv-0002", "dv-0003")
    }
    f.tick(b, tmp_path)
    for c, data in before.items():
        assert (repo / TASKS / f"{c}.md").read_bytes() == data
    assert f.launched == []


def test_designloop_is_not_picked_up(repo: Path, tmp_path: Path) -> None:  # noqa: F811
    _drop_default(repo)
    b, f = board(repo, "true"), Fake()
    p = card(repo, "dv-0002")
    _add_fm(p, "labels: [designloop]")
    before = p.read_bytes()
    f.tick(b, tmp_path)
    assert p.read_bytes() == before and f.launched == []


def test_other_statuses_and_finished_stages_unchanged(
    repo: Path, tmp_path: Path  # noqa: F811
) -> None:
    _drop_default(repo)
    b, f = board(repo, "true"), Fake()
    paths = [
        card(repo, f"dv-00{i}", status=s)
        for i, s in enumerate(["todo", "review", "waiting_user", "done"], start=2)
    ]
    for i, st in enumerate(["done", "stopped"], start=6):
        p = card(repo, f"dv-00{i}")
        set_pump2(p, stage=st)
        paths.append(p)
    before = [p.read_bytes() for p in paths]
    f.tick(b, tmp_path)
    assert [p.read_bytes() for p in paths] == before
    assert f.launched == []


def test_pickup_is_idempotent(repo: Path, tmp_path: Path) -> None:  # noqa: F811
    _drop_default(repo)
    b, f = board(repo, "true"), Fake()
    card(repo, "dv-0002")
    f.tick(b, tmp_path)
    f.tick(b, tmp_path)
    assert find_card(b, "dv-0002").body.count("picked up from in_progress") == 1
