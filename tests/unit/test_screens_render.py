"""dv-4cd8 phase 2: the PNG renderer and ``scripts/screens``.

Phase 1 gave the mechanical checks; phase 2 draws the screen the way the
phone shows it so review (phase 3) can look at it. Contract these tests pin:

* ``src.bot.screens.render`` imports without Pillow (Pillow is imported
  lazily, only to draw); the layout is pure:
  ``render.layout(screen) -> list[list[DrawnButton]]``, one entry per
  keyboard button in ``screen.rows`` order, each with ``.label`` (the text
  drawn: ``metrics.visible_text`` of the label), ``.flagged`` (the button has
  a violation from ``checks.check``) and ``.outline`` (an RGB tuple:
  ``render.RED`` when flagged).
* ``render.render(screen) -> bytes``: a PNG at 2x the 390-pt phone width
  (780 px), deterministic, offline.
* ``src.bot.screens.cli.main(argv) -> int`` behind ``scripts/screens``:
  ``check`` exits 0 when every registry screen passes or is ``known_bad``
  (the frozen fixtures are shipped mistakes that are meant to fail and do
  not fail the command), 1 on any other violation; ``render --out DIR
  [--base REV]`` writes ``DIR/after/<id>.png``, ``DIR/before/<id>.png`` and
  ``DIR/report.json``.

The tests that draw pixels need Pillow and skip without it (the bot venv
gets it through a devops task, owner 2026-10-27); the layout and ``check``
tests do not.
"""

import asyncio
import json
import shutil
import socket
import struct
import subprocess
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[2]
PNG_MAGIC = b"\x89PNG\r\n\x1a\n"
PHONE_PX = 2 * 390


def _fixture(name: str):
    from src.bot import screens
    from src.bot.screens.model import Screen

    path = Path(screens.__file__).parent / "fixtures" / name
    return Screen.from_json(json.loads(path.read_text(encoding="utf-8")))


def _current_model():
    """Current /model (df11ac2) through the real handler, Codex logged out."""
    from src.bot.screens.registry import SCREENS

    built = asyncio.run(SCREENS["model.claude.logged_out"].build())
    assert built
    return built[0]


def _png_size(data: bytes):
    assert data[:8] == PNG_MAGIC, "not a PNG"
    width, height = struct.unpack(">II", data[16:24])
    return width, height


def _is_red(rgb) -> bool:
    r, g, b = rgb[:3]
    return r >= 200 and g <= 80 and b <= 80


def _count_exact(png: bytes, rgb) -> int:
    from io import BytesIO

    from PIL import Image

    img = Image.open(BytesIO(png)).convert("RGB")
    return sum(1 for px in img.getdata() if px == tuple(rgb[:3]))


@pytest.fixture
def no_network(monkeypatch):
    """Any socket connect (Telegram or otherwise) fails the test."""

    def refuse(self, *args, **kwargs):
        raise AssertionError(f"network used while rendering: {args!r}")

    monkeypatch.setattr(socket.socket, "connect", refuse)
    monkeypatch.setattr(socket.socket, "connect_ex", refuse)


# --- scenario 1: current /model at phone width, labels as shown -----------


def test_current_model_layout_draws_every_label_as_the_phone_shows_it():
    from src.bot.screens import metrics, render

    screen = _current_model()
    drawn = render.layout(screen)

    assert [[d.label for d in row] for row in drawn] == [
        [metrics.visible_text(b.text, len(row)) for b in row] for row in screen.rows
    ]
    # df11ac2 fits: what is drawn is the label itself, nothing flagged.
    assert [[d.label for d in row] for row in drawn] == [
        [b.text for b in row] for row in screen.rows
    ]
    assert not any(d.flagged for row in drawn for d in row)
    assert not any(_is_red(d.outline) for row in drawn for d in row)


def test_current_model_renders_a_png_at_390pt_phone_width(no_network):
    pytest.importorskip("PIL")
    from src.bot.screens import render

    png = render.render(_current_model())

    width, height = _png_size(png)
    assert width == PHONE_PX
    assert height > 0
    # A passing screen carries no red violation mark.
    assert _count_exact(png, render.RED) == 0


# --- scenario 2: a truncated button is drawn ellipsised, flagged red -------


def test_truncated_button_is_drawn_with_its_ellipsised_text_and_flagged_red():
    from src.bot.screens import metrics, render

    screen = _fixture("model_7fca8f0.json")
    drawn = render.layout(screen)
    by_text = {
        button.text: d
        for row, src in zip(drawn, screen.rows)
        for d, button in zip(row, src)
    }

    assert _is_red(render.RED)
    for name in ("Claude Sonnet", "Claude Opus", "Claude Fable"):
        d = by_text[name]
        assert d.label == metrics.visible_text(name, 3)
        assert d.label.startswith("Clau") and metrics.ELLIPSIS in d.label
        assert d.flagged
        assert d.outline == render.RED
    assert by_text["Claude Fable"].label == "Clau…able"
    # Logout next to Login is an `exclusive` violation: flagged, not truncated.
    assert by_text["Logout Codex"].label == "Logout Codex"
    assert by_text["Logout Codex"].flagged
    # A button with no violation is drawn plain.
    assert by_text["Codex GPT-5.5"].label == "Codex GPT-5.5"
    assert not by_text["Codex GPT-5.5"].flagged
    assert not _is_red(by_text["Codex GPT-5.5"].outline)


def test_truncated_screen_png_carries_the_red_flag(no_network):
    pytest.importorskip("PIL")
    from src.bot.screens import render

    png = render.render(_fixture("model_7fca8f0.json"))

    assert _png_size(png)[0] == PHONE_PX
    assert _count_exact(png, render.RED) > 0


# --- scenario 3: deterministic, no real Telegram --------------------------


@pytest.mark.parametrize(
    "name", ["model_7fca8f0.json", "menu_6104_settings.json", "menu_6104_pump.json"]
)
def test_same_input_twice_gives_byte_identical_pngs(name, no_network):
    pytest.importorskip("PIL")
    from src.bot.screens import render

    first = render.render(_fixture(name))
    second = render.render(_fixture(name))

    assert first == second
    assert _png_size(first)[0] == PHONE_PX


def test_current_model_renders_identically_twice_offline(no_network):
    pytest.importorskip("PIL")
    from src.bot.screens import render

    assert render.render(_current_model()) == render.render(_current_model())


def test_render_module_imports_without_telegram_or_pillow_at_import_time():
    """The checks and layout run in the bot venv even without Pillow; the
    renderer never talks to Telegram."""
    from src.bot.screens import render

    source = Path(render.__file__).read_text(encoding="utf-8")
    assert "import telegram" not in source
    assert "from telegram" not in source
    assert "api.telegram.org" not in source


# --- scenario 4: render --base REV writes before/, after/, report.json ----


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(
        ["git", *args],
        cwd=cwd,
        check=True,
        capture_output=True,
        env={
            "GIT_AUTHOR_NAME": "t",
            "GIT_AUTHOR_EMAIL": "t@example.invalid",
            "GIT_COMMITTER_NAME": "t",
            "GIT_COMMITTER_EMAIL": "t@example.invalid",
            "HOME": str(cwd),
            "PATH": "/usr/bin:/bin",
        },
    )


@pytest.fixture
def scratch_repo(tmp_path):
    """A throwaway git repo holding this tree's ``src`` and ``scripts/screens``,
    committed once, so ``--base HEAD`` has a revision with the renderer."""
    pytest.importorskip("PIL")
    repo = tmp_path / "repo"
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    shutil.copytree(REPO / "src", repo / "src", ignore=ignore)
    (repo / "scripts").mkdir()
    shutil.copy2(REPO / "scripts" / "screens", repo / "scripts" / "screens")
    _git(repo, "init", "-q")
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "base")
    return repo


def test_render_base_writes_before_after_pngs_and_report(scratch_repo, tmp_path):
    pytest.importorskip("PIL")
    out = tmp_path / "out"

    proc = subprocess.run(
        [
            sys.executable,
            "scripts/screens",
            "render",
            "--out",
            str(out),
            "--base",
            "HEAD",
        ],
        cwd=scratch_repo,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert (out / "report.json").exists(), proc.stdout + proc.stderr

    for side in ("before", "after"):
        png = out / side / "model.claude.logged_out.png"
        assert png.exists(), f"{png} missing\n{proc.stdout}{proc.stderr}"
        assert _png_size(png.read_bytes())[0] == PHONE_PX

    report = json.loads((out / "report.json").read_text(encoding="utf-8"))
    dumped = json.dumps(report)
    assert "model.claude.logged_out" in dumped
    # dv-f6af rebuilt the last known_bad screens (menu.root, menu.settings):
    # every registered screen passes the gate now.
    assert report["failed"] == [], dumped
    # Base and head are the same code here: the same screen, the same bytes.
    assert (out / "before" / "model.claude.logged_out.png").read_bytes() == (
        out / "after" / "model.claude.logged_out.png"
    ).read_bytes()


def test_render_without_base_writes_only_after(scratch_repo, tmp_path):
    pytest.importorskip("PIL")
    out = tmp_path / "out"

    proc = subprocess.run(
        [sys.executable, "scripts/screens", "render", "--out", str(out)],
        cwd=scratch_repo,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert (out / "report.json").exists(), proc.stdout + proc.stderr
    assert (out / "after" / "model.claude.logged_out.png").exists()
    assert not (out / "before").exists() or not any((out / "before").iterdir())


# --- added: `screens check` exit codes -------------------------------------


def test_screens_check_exits_zero_on_the_current_registry(capsys):
    from src.bot.screens import cli

    assert cli.main(["check"]) == 0


def test_screens_check_exits_one_on_a_failing_screen_not_known_bad(monkeypatch, capsys):
    from src.bot.screens import cli, registry
    from src.bot.screens.model import Button, Screen

    async def build():
        return [
            Screen(
                "test.bad",
                "Pick one.",
                None,
                [
                    [
                        Button("A very long label nobody can read", "x:1"),
                        Button("Another very long label too", "x:2"),
                    ]
                ],
            )
        ]

    monkeypatch.setitem(
        registry.SCREENS,
        "test.bad",
        registry.ScreenSpec("test.bad", "pick one", build),
    )
    assert cli.main(["check"]) == 1
    assert "test.bad" in capsys.readouterr().out
