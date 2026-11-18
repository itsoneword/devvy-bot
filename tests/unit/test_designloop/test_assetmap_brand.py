"""assetmap: the Brand and Templates & references sections of MAP.md."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from typing import Any

from src.designloop import assetmap

from .test_assetmap import PNG, _board, _text


def _brand_board(tmp: Path, **extra: Any) -> SimpleNamespace:
    b = _board(tmp)
    repo = b.repo
    for name in ("2026-11-07", "2026-11-07-r2"):
        d = repo / ".screens" / "brand" / name
        d.mkdir(parents=True)
        (d / "a.png").write_bytes(PNG)
        (d / "b.svg").write_text("<svg/>", encoding="utf-8")
    (repo / ".screens" / "brand" / "2026-11-07" / "c.png").write_bytes(PNG)
    (repo / "webapp" / "public").mkdir(parents=True)
    (repo / "webapp" / "public" / "logo.svg").write_text("<svg/>", encoding="utf-8")
    refs = repo / "docs" / "references"
    (refs / "gen-logo-1-symbol.jpg").write_bytes(b"\xff\xd8")
    (refs / "kit-1.png").write_bytes(PNG)
    (refs / "README.md").write_text(
        "# refs\n\n### `gen-logo-1-symbol.jpg`\n\n"
        "- likes: the owner's favourite of the five\n- avoid: text\n",
        encoding="utf-8",
    )
    (repo / "art").mkdir()
    (repo / "art" / "x.png").write_bytes(PNG)
    (repo / "art" / "SPEC.md").write_text("spec", encoding="utf-8")
    b.design.update(
        brand={
            "rounds": [".screens/brand"],
            "current": ["webapp/public/logo.svg", "webapp/public/nope.png"],
        },
        templates=["art", "docs/references/owner-mockups"],
        **extra,
    )
    return b


def test_C1_brand_section_rows(tmp_path: Path) -> None:
    text = _text(tmp_path, _brand_board(tmp_path))
    assert "## Brand" in text
    sec = text.split("## Brand", 1)[1].split("\n## ", 1)[0]
    r1 = next(
        ln for ln in sec.splitlines() if "2026-11-07 " in ln + " " and "r2" not in ln
    )
    assert "3" in r1.replace("2026-11-07", "") or "| 3 |" in r1  # 2 png + ... counted
    assert any("2026-11-07-r2" in ln for ln in sec.splitlines())
    # logo reference matched by name
    assert "gen-logo-1-symbol.jpg" in sec or "gen-logo" in sec


def test_C1_no_brand_sources_placeholder(tmp_path: Path) -> None:
    text = _text(tmp_path, _board(tmp_path))
    assert "## Brand" in text
    assert "## Templates & references" in text


def test_C2_current_mark_and_missing(tmp_path: Path) -> None:
    text = _text(tmp_path, _brand_board(tmp_path))
    sec = text.split("## Brand", 1)[1].split("\n## ", 1)[0]
    assert "webapp/public/logo.svg" in sec
    line = next(ln for ln in sec.splitlines() if "nope.png" in ln)
    assert "missing" in line
    ok = next(ln for ln in sec.splitlines() if "logo.svg" in ln)
    assert "missing" not in ok


def test_C3_templates_section(tmp_path: Path) -> None:
    text = _text(tmp_path, _brand_board(tmp_path))
    assert "## Templates & references" in text
    sec = text.split("## Templates & references", 1)[1].split("\n## ", 1)[0]
    line = next(ln for ln in sec.splitlines() if "art" in ln and "|" in ln)
    assert "SPEC.md" in line
    assert "owner-mockups" in sec


def test_C4_sources_from_config_and_assets_brand(tmp_path: Path) -> None:
    b = _board(tmp_path)
    ext = tmp_path / "elsewhere" / "logos"
    ext.mkdir(parents=True)
    (ext / "l.png").write_bytes(PNG)
    b.design["brand"] = {"rounds": [str(ext)]}
    ab = tmp_path / "assets" / "brand" / "round1"
    ab.mkdir(parents=True)
    (ab / "m.png").write_bytes(PNG)
    text = _text(tmp_path, b)
    sec = text.split("## Brand", 1)[1].split("\n## ", 1)[0]
    assert str(ext) in sec  # absolute path as is
    assert "brand" in sec and "round1" in sec  # <assets>/brand always scanned
    src = Path(assetmap.__file__).read_text(encoding="utf-8")
    assert "gymbro" not in src.lower().replace("gymbro-less", "")


def test_C6_owner_pick_from_readme(tmp_path: Path) -> None:
    text = _text(tmp_path, _brand_board(tmp_path))
    sec = text.split("## Brand", 1)[1].split("\n## ", 1)[0]
    assert "favourite of the five" in sec


def test_C7_unreadable_paths_do_not_raise(tmp_path: Path) -> None:
    b = _brand_board(tmp_path)
    b.design["brand"]["rounds"].append(str(tmp_path / "does-not-exist"))
    b.design["templates"].append(str(tmp_path / "also-missing"))
    b.design["brand"]["current"].append(123)  # garbage entry
    text = _text(tmp_path, b)
    assert "## Screens" in text or "feed" in text
    assert assetmap.write_map(b, tmp_path).is_file()
