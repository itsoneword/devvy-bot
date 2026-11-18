"""assetmap: MAP.md built from folders, logs, verdicts and cards."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from src.designloop import assetmap

PNG = b"\x89PNG\r\n"


def _log(path: Path, *recs: dict) -> None:
    path.write_text("\n".join(json.dumps(r) for r in recs) + "\n", encoding="utf-8")


def _board(tmp: Path, **design: Any) -> SimpleNamespace:
    repo = tmp / "repo"
    refs = repo / "docs" / "references"
    (refs / "owner-mockups").mkdir(parents=True)
    (refs / "README.md").write_text("refs", encoding="utf-8")
    (refs / "owner-mockups" / "feed.png").write_bytes(PNG)
    (refs / "owner-mockups" / "feed-2.png").write_bytes(PNG)
    (repo / "docs" / "features.yaml").write_text(
        "version: 1\nscreens:\n  feed:\n    passport: x\n  event_form:  # c\n"
        "    passport: y\n  mine:\n    passport: z\nother_top:\n  nope: 1\n",
        encoding="utf-8",
    )
    (refs / "owner-verdicts.md").write_text(
        "# head\n\n## 2026-11-13 15:53 UTC · gb-ca4a · feed · rejected all 3\n\n"
        "none, because: old kit\nmore\n\n## 2026-11-14 06:00 UTC · remark\n\nx\n"
        "\n## 2026-11-14 19:57 UTC · staging · profile tab after kit\n\nShots\n",
        encoding="utf-8",
    )
    tasks = tmp / "tasks"
    tasks.mkdir()
    (tasks / "gb-1111.md").write_text(
        '---\nid: gb-1111\ntitle: "Redo the feed screen"\nstatus: todo\n'
        "updated: 2026-11-10T00:00:00Z\n---\nbody\n"
    )
    (tasks / "gb-2222.md").write_text(
        "---\nid: gb-2222\ntitle: feed\nstatus: done\n---\nfeed\n"
    )
    (tasks / "gb-3333.md").write_text(  # mentions feed only in the body: noise
        "---\nid: gb-3333\ntitle: Fix login\nstatus: todo\n---\nfeed feed\n"
    )
    (tasks / "gb-4444.md").write_text(
        "---\nid: gb-4444\ntitle: Variants\nstatus: todo\nlabels: [designloop]\n"
        "design_screen: event_form\nupdated: 2026-11-12T00:00:00Z\n---\nx\n"
    )
    d = {"references": "docs/references/README.md", "assets": str(tmp / "assets")}
    d.update(design)
    return SimpleNamespace(
        name="demo", repo=repo, profile="demo", design=d, tasks=tasks
    )


def test_map_pictures_failures_and_screens(tmp_path: Path) -> None:
    board = _board(tmp_path)
    root = tmp_path / "assets"
    ok = root / "elements" / "badges" / "badges"
    ok.mkdir(parents=True)
    for n in ("a", "b"):
        (ok / f"{n}.png").write_bytes(PNG)
    (root / "elements" / "badges" / "badges-sheet.png").write_bytes(PNG)
    _log(
        root / "elements" / "badges" / "log.jsonl",
        {"group": "badges", "name": "a", "ok": True},
        {
            "group": "badges",
            "name": "c",
            "ok": False,
            "error": "timeout",
            "job_id": "j9",
        },
    )
    failed = root / "elements" / "cover"
    failed.mkdir(parents=True)
    _log(failed / "log.jsonl", {"group": "g", "name": "n", "ok": False, "error": "x"})
    (root / "elements" / "hand").mkdir()
    (root / "elements" / "hand" / "x-1a2b3c.png").write_bytes(PNG)
    (root / "empty").mkdir()
    sprint = root / "mockups" / "20261115-100000" / "feed"
    sprint.mkdir(parents=True)
    (sprint / "mockup-1.png").write_bytes(PNG)

    path = assetmap.write_map(board, tmp_path)
    text = path.read_text()
    assert path == root / "MAP.md"
    row = next(r for r in text.splitlines() if "elements/badges`" in r)
    assert "| 2 |" in row and "partial" in row and "flux" in row and "c j9" in row
    cover = next(r for r in text.splitlines() if "elements/cover`" in r)
    assert "FAILED" in cover and "1 lost (no job id): g/n" in cover
    assert "fetchable" not in text
    assert "unknown (no log" in next(
        r for r in text.splitlines() if "elements/hand" in r
    )
    assert "EMPTY" in next(
        r for r in text.splitlines() if "`assets/empty`" in r or "`empty`" in r
    )
    feed = next(r for r in text.splitlines() if r.startswith("| feed "))
    assert "owner-mockups/feed.png" in feed and "owner-mockups/feed-2.png" in feed
    assert "build to pick" in feed
    assert "2026-11-13: none, because: old kit" in feed
    assert "mockups/20261115-100000" in feed
    assert "gb-1111 (todo)" in feed and "gb-2222" not in feed
    assert "gb-3333" not in text
    assert "| feed-2 " not in text and "| staging " not in text
    ef = next(r for r in text.splitlines() if r.startswith("| event_form "))
    assert "needs mockups" in ef and "gb-4444 (todo)" in ef
    mine = next(r for r in text.splitlines() if r.startswith("| mine "))
    assert "needs mockups" in mine and "| - |" in mine


def test_init_creates_structure_and_empty_map(tmp_path: Path) -> None:
    board = SimpleNamespace(
        name="new",
        repo=tmp_path / "r",
        profile="new",
        design=None,
        tasks=tmp_path / "t",
    )
    (tmp_path / "r").mkdir()
    # no `assets` key: default root is under the profile workspace (legacy layout)
    path = assetmap.write_map(board, tmp_path, init=True)
    root = tmp_path / "data" / "workspaces" / "new" / "design"
    assert path == root / "MAP.md"
    assert (root / "elements").is_dir() and (root / "mockups").is_dir()
    assert "nothing generated yet" in path.read_text()


def test_parse_verdicts_screen_header_not_card() -> None:
    v = assetmap.parse_verdicts(
        "## 2026-11-13 15:53 UTC · gb-ca4a · feed · rejected\n\nwords\n"
        "## 2026-11-14 10:00 UTC · event_page · picked 2\n\nbetter\n",
        ["feed", "event_page"],
    )
    assert v["feed"].line == "words" and v["event_page"].date == "2026-11-14"


def test_parse_verdicts_real_header_shapes() -> None:
    text = (
        "## 2026-11-14 19:57 UTC · staging · profile tab + event page\n\nShots\n\n"
        "## 14.11.26 22:01 — Feed hero art\nOwner: keep the outline\n\n"
        "## 15.11.26 08:10 — My events, Activity\n"
        "Owner picks: `owner-mockups/mine.png`, `owner-mockups/notes.png`.\n\n"
        "## 15.11.26 08:43 — Event form (no pick yet), Activity\nOwner (voice): "
        + "long " * 40
        + "\n\n## 2026-11-15 09:00 UTC · remark\n\nfeed event_form\n"
    )
    v = assetmap.parse_verdicts(text, ["feed", "mine", "notes", "event_form"])
    assert "staging" not in v
    assert v["feed"].date == "2026-11-14" and v["feed"].line.startswith("Owner: keep")
    assert v["mine"].date == "2026-11-15" and v["notes"].date == "2026-11-15"
    assert v["event_form"].date == "2026-11-15" and len(v["event_form"].line) <= 80
    assert v["event_form"].line.startswith("Owner (voice)")  # not the remark


def test_open_cards_cap_and_newest_first(tmp_path: Path) -> None:
    for i in range(7):
        (tmp_path / f"gb-{i:04x}.md").write_text(
            f"---\nid: gb-{i:04x}\ntitle: feed tweak {i}\nstatus: todo\n"
            f"updated: 2026-11-0{i + 1}T00:00:00Z\n---\n"
        )
    got = assetmap.open_cards_for(tmp_path, ["feed"])["feed"]
    assert len(got) == 5 and got[0] == "gb-0006 (todo)"


def test_legacy_non_mockup_folders_are_other(tmp_path: Path) -> None:
    board = _board(tmp_path)
    legacy = tmp_path / "mockups"
    (tmp_path / "assets").mkdir()
    for name in ("dist-before", "tools"):
        (legacy / name).mkdir(parents=True)
    (legacy / "dist-before" / "a.png").write_bytes(PNG)
    (legacy / "feed-en").mkdir()
    (legacy / "feed-en" / "a.png").write_bytes(PNG)
    root = tmp_path / "design"
    board.design["assets"] = str(root)
    (root / "mockups").mkdir(parents=True)
    text = assetmap.write_map(board, tmp_path).read_text()
    row = next(r for r in text.splitlines() if "mockups/dist-before" in r)
    assert "other (not mockup output)" in row
    assert "other (" in next(r for r in text.splitlines() if "mockups/tools" in r)
    assert "other" not in next(r for r in text.splitlines() if "mockups/feed-en" in r)
    feed = next(r for r in text.splitlines() if r.startswith("| feed "))
    assert "dist-before" not in feed and "mockups/feed-en" in feed
    assert "needs owner pick" not in feed  # a pick exists: build to pick


def test_refresh_never_raises(tmp_path: Path, capsys: Any) -> None:
    assert assetmap.refresh(SimpleNamespace(), tmp_path) is None
    assert "not updated" in capsys.readouterr().out


def _text(tmp_path: Path, board: Any) -> str:
    _, text = assetmap.build_map(
        name=board.name,
        repo=board.repo,
        profile=board.profile,
        design=board.design,
        tasks=board.tasks,
        repo_root=tmp_path,
    )
    return text


def _row(text: str, screen: str) -> str:
    return next(ln for ln in text.splitlines() if ln.startswith(f"| {screen} |"))


def test_mark_built_done_then_rebuild_when_pick_newer(tmp_path: Path) -> None:
    import os
    from datetime import UTC, datetime

    board = _board(tmp_path)
    root = Path(board.design["assets"])
    assert "build to pick" in _row(_text(tmp_path, board), "feed")
    when = datetime(2026, 11, 14, 22, 6, 52, tzinfo=UTC)
    pick = board.repo / "docs/references/owner-mockups/feed.png"
    os.utime(pick, (when.timestamp() - 3600,) * 2)
    os.utime(pick.with_name("feed-2.png"), (when.timestamp() - 3600,) * 2)
    path = assetmap.mark_screen(root, "feed", "built", sha="cfb26393ab", at=when)
    assert path.name == "status.yaml"
    row = _row(_text(tmp_path, board), "feed")
    assert "done (built 2026-11-14)" in row and "cfb26393 2026-11-14" in row
    os.utime(pick, (when.timestamp() + 3600,) * 2)
    assert "rebuild: pick newer than build" in _row(_text(tmp_path, board), "feed")
    assetmap.mark_screen(root, "feed", "built", at=datetime.now(UTC))
    assetmap.mark_screen(root, "feed", "redo", at=datetime(2099, 1, 1, tzinfo=UTC))
    assert "rebuild: marked redo" in _row(_text(tmp_path, board), "feed")
    assert assetmap.read_status(root)["feed"]["built"]["at"]


def test_mark_rejects_unknown_state(tmp_path: Path) -> None:
    import pytest

    with pytest.raises(ValueError):
        assetmap.mark_screen(tmp_path, "feed", "shipped")


def test_pump_variants_and_awaiting_pick(tmp_path: Path) -> None:
    board = _board(tmp_path)
    (board.tasks / "gb-5555.md").write_text(
        "---\nid: gb-5555\ntitle: Event form kit\nstatus: todo\n"
        "labels: [designloop]\ndesign_screen: event_form\n"
        "design_state: awaiting_pick\n---\nx\n"
    )
    d = board.repo / ".screens" / "design" / "gb-5555"
    for sub in ("v1", "v2", "mockups"):
        (d / sub).mkdir(parents=True)
    (d / "mockups" / "mockup-1.png").write_bytes(PNG)
    row = _row(_text(tmp_path, board), "event_form")
    assert "needs owner pick (gb-5555)" in row
    assert "`.screens/design/gb-5555` (variants: 2, mockups: 1)" in row
    # a built screen wins over a waiting card, which is named as possibly stale
    assetmap.mark_screen(Path(board.design["assets"]), "event_form", "built")
    row = _row(_text(tmp_path, board), "event_form")
    assert "done (built " in row and "card still waiting: gb-5555 (stale?)" in row


def test_reports_section_lists_folders_not_files(tmp_path: Path) -> None:
    board = _board(tmp_path)
    ws = assetmap.workspace_root("demo", tmp_path)
    (ws / "reports" / "kit-blocks").mkdir(parents=True)
    (ws / "reports" / "kit-blocks" / "1.jpg").write_bytes(PNG)
    (ws / "reports" / "kit-blocks" / "2.png").write_bytes(PNG)
    (ws / "reports" / "notes-only").mkdir()
    (ws / "reports" / "notes-only" / "a.md").write_text("x")
    text = _text(tmp_path, board)
    sec = text.split("## Build sheets and screenshots")[1]
    assert "| `reports/kit-blocks` | 2 |" in sec
    assert "notes-only" not in sec and "1.jpg" not in sec


def test_default_out_dirs_one_place(tmp_path: Path) -> None:
    assert assetmap.default_elements_out(tmp_path, Path("x/badges-prompts.yaml")) == (
        tmp_path / "elements" / "badges"
    )
    assert assetmap.default_mockups_out(tmp_path).parent == tmp_path / "mockups"


def test_default_elements_out_prefixes_the_author(tmp_path):
    from src.designloop import assetmap

    p = tmp_path / "badges-prompts.yaml"
    assert assetmap.default_elements_out(tmp_path, p).name == "badges"
    assert assetmap.default_elements_out(tmp_path, p, by="packops").name == (
        "packops-badges"
    )
    q = tmp_path / "packops-badges.yaml"
    assert assetmap.default_elements_out(tmp_path, q, by="packops").name == (
        "packops-badges"
    )
