"""dv-4cd8 phase 2: ``scripts/screens`` edge cases the scenario tests leave
out (``test_screens_render.py`` holds the approved scenarios)."""

import builtins
import json

from src.bot.screens import cli, registry
from src.bot.screens.model import Button, Screen


def _spec(screen_id, rows, known_bad=None):
    async def build():
        return [Screen(screen_id, "Pick one.", None, rows)]

    return registry.ScreenSpec(screen_id, "pick one", build, known_bad)


def _only(monkeypatch, *specs):
    monkeypatch.setattr(cli, "SCREENS", {s.id: s for s in specs})


def test_check_fails_a_known_bad_screen_that_passes_now(monkeypatch, capsys):
    _only(monkeypatch, _spec("t.fixed", [[Button("OK", "ok")]], "dv-0000"))

    assert cli.main(["check"]) == 1
    assert "drop known_bad=dv-0000" in capsys.readouterr().out


def test_check_fails_a_builder_that_crashes(monkeypatch, capsys):
    async def build():
        raise RuntimeError("boom")

    _only(monkeypatch, registry.ScreenSpec("t.crash", "g", build))

    assert cli.main(["check"]) == 1
    assert "t.crash: RuntimeError: boom" in capsys.readouterr().out


def test_render_without_pillow_still_writes_the_report(monkeypatch, tmp_path):
    _only(monkeypatch, _spec("t.ok", [[Button("OK", "ok")]]))
    real_import = builtins.__import__

    def no_pil(name, *args, **kwargs):
        if name == "PIL" or name.startswith("PIL."):
            raise ImportError("no Pillow")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pil)

    assert cli.main(["render", "--out", str(tmp_path)]) == 2
    report = json.loads((tmp_path / "report.json").read_text(encoding="utf-8"))
    assert "Pillow" in report["error"]
    assert report["screens"][0]["id"] == "t.ok"
    assert not (tmp_path / "after").exists()


def test_base_without_the_renderer_reports_no_before(monkeypatch, tmp_path):
    # df11ac2 predates scripts/screens: no before, and it says why (in a
    # checkout without that commit, git's own error).
    before = cli._render_base("df11ac2", tmp_path)

    assert before["error"]
    assert not (tmp_path / "before").exists()


def test_layout_flags_every_button_of_a_too_wide_row():
    from src.bot.screens import render

    screen = Screen("t.row", "x", None, [[Button(c, c) for c in "ABCDE"]])

    drawn = render.layout(screen)
    assert all(d.flagged and "row_width" in d.codes for d in drawn[0])
