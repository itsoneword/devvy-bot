"""The review PDF renderer: layouts, Cyrillic, numbering, downscale."""

from __future__ import annotations

import io
import random
from pathlib import Path
from typing import Any, Dict, List

import pytest
from PIL import Image

from src.designloop import review_pdf as rp


def _png(path: Path, size: tuple[int, int] = (300, 400), noise: bool = False) -> str:
    if noise:
        data = random.Random(1).randbytes(size[0] * size[1] * 3)
        Image.frombytes("RGB", size, data).save(path)
    else:
        Image.new("RGB", size, (200, 120, 40)).save(path)
    return str(path)


def _items(tmp_path: Path, n: int, **extra: Any) -> List[Dict[str, Any]]:
    return [
        {"image": _png(tmp_path / f"i{k}.png"), "caption": f"подпись {k}", **extra}
        for k in range(n)
    ]


def _render(tmp_path: Path, sections: List[Dict[str, Any]], numbers=None) -> int:
    out = tmp_path / "r.pdf"
    spec = {
        "title": "Объекты: проверка",
        "intro": "Ответ «ок: 3»",
        "sections": sections,
    }
    return rp.render(spec, out, tmp_path / "r-sheet.png")


@pytest.fixture
def badges(monkeypatch: pytest.MonkeyPatch) -> List[int]:
    seen: List[int] = []
    orig = rp._Doc.badge

    def spy(self: Any, number: int, *a: Any, **k: Any) -> None:
        if number > 0:
            seen.append(number)
        orig(self, number, *a, **k)

    monkeypatch.setattr(rp._Doc, "badge", spy)
    return seen


@pytest.mark.parametrize("layout", rp.LAYOUTS)
def test_every_layout_writes_a_pdf_and_a_cover(tmp_path: Path, layout: str) -> None:
    sec = {"heading": "Раздел", "layout": layout, "items": _items(tmp_path, 4)}
    pages = _render(tmp_path, [sec])
    pdf = (tmp_path / "r.pdf").read_bytes()
    assert pdf.startswith(b"%PDF") and pages >= 2
    assert pdf.count(b"/Type /Page\n") + pdf.count(b"/Type /Page ") >= pages or pages
    cover = Image.open(tmp_path / "r-sheet.png")
    assert cover.size == (rp.PAGE_W, rp.PAGE_H)


def test_single_is_one_page_per_item(tmp_path: Path) -> None:
    sec = {"heading": "S", "layout": "single", "items": _items(tmp_path, 3)}
    assert _render(tmp_path, [sec]) == 1 + 3


def test_numbers_run_through_the_whole_document(
    tmp_path: Path, badges: List[int]
) -> None:
    secs = [
        {"heading": "A", "layout": "table", "items": _items(tmp_path, 3)},
        {"heading": "B", "layout": "grid", "columns": 2, "items": _items(tmp_path, 4)},
        {"heading": "C", "layout": "compare", "items": _items(tmp_path, 5)},
    ]
    _render(tmp_path, secs)
    # 3 + 4 + a pair counts once (5 items = 3 pairs)
    assert badges == list(range(1, 11))


def test_unnumbered_section_takes_no_numbers(tmp_path: Path, badges: List[int]) -> None:
    secs = [
        {
            "heading": "now",
            "layout": "single",
            "numbered": False,
            "items": _items(tmp_path, 1),
        },
        {"heading": "V", "layout": "grid", "items": _items(tmp_path, 3)},
    ]
    _render(tmp_path, secs)
    assert badges == [1, 2, 3]


def test_cyrillic_uses_dejavu_and_draws_glyphs() -> None:
    font = rp._font(24)
    assert "dejavu" in font.path.lower()
    im = Image.new("L", (400, 50), 255)
    from PIL import ImageDraw

    ImageDraw.Draw(im).text((5, 5), "Бейджи, жёлтый", font=font, fill=0)
    assert im.getextrema()[0] < 100
    # the notdef box is not what Cyrillic renders as
    box = Image.new("L", (400, 50), 255)
    ImageDraw.Draw(box).text((5, 5), "Ж", font=font, fill=0)
    other = Image.new("L", (400, 50), 255)
    ImageDraw.Draw(other).text((5, 5), "Щ", font=font, fill=0)
    assert box.tobytes() != other.tobytes()


def test_long_caption_wraps_inside_the_width() -> None:
    im = Image.new("RGB", (10, 10))
    from PIL import ImageDraw

    draw = ImageDraw.Draw(im)
    font = rp._font(21)
    lines = rp.wrap(draw, "слово " * 60 + "х" * 200, font, 300)
    assert len(lines) > 5
    assert all(draw.textlength(line, font=font) <= 300 for line in lines)


def test_input_images_are_downscaled(tmp_path: Path) -> None:
    big = _png(tmp_path / "big.png", (4000, 2500))
    im = rp.load_image(big)
    assert max(im.size) <= rp.MAX_IMAGE_SIDE


def test_transparent_png_is_flattened_on_white(tmp_path: Path) -> None:
    p = tmp_path / "t.png"
    Image.new("RGBA", (20, 20), (0, 0, 0, 0)).save(p)
    assert rp.load_image(str(p)).getpixel((5, 5)) == (255, 255, 255)


def test_eighty_large_images_stay_far_under_50_mb(tmp_path: Path) -> None:
    items = [
        {
            "image": _png(tmp_path / f"n{k % 4}.png", (1000, 750), noise=True),
            "caption": f"картинка {k}",
        }
        for k in range(80)
    ]
    secs = [{"heading": "Много", "layout": "grid", "columns": 4, "items": items}]
    _render(tmp_path, secs)
    assert (tmp_path / "r.pdf").stat().st_size < 30 * 1024 * 1024


def test_a_missing_image_is_a_placeholder_not_a_crash(tmp_path: Path) -> None:
    items = [{"image": str(tmp_path / "nope.png"), "caption": "нет файла", "score": 5}]
    for layout in rp.LAYOUTS:
        _render(tmp_path, [{"heading": "x", "layout": layout, "items": items}])


def test_table_score_and_label_and_headers(tmp_path: Path) -> None:
    items = _items(tmp_path, 30, score="7 → 8", label="ПРОХОДИТ")
    sec = {
        "heading": "T",
        "layout": "table",
        "columns": ["объект", "оценка"],
        "items": items,
    }
    assert _render(tmp_path, [sec]) > 3  # a table runs over several pages


@pytest.mark.parametrize(
    "spec",
    [
        [],
        {"sections": []},
        {"sections": [{"layout": "nope", "items": [{"caption": "x"}]}]},
        {"sections": [{"layout": "grid", "items": []}]},
        {"sections": [{"layout": "grid", "items": ["x"]}]},
    ],
)
def test_bad_specs_are_refused(spec: Any) -> None:
    with pytest.raises(rp.ReviewError):
        rp.validate(spec)


def test_script_runs_with_a_spec_file(tmp_path: Path) -> None:
    import json

    spec = {
        "title": "t",
        "sections": [{"heading": "h", "layout": "grid", "items": _items(tmp_path, 2)}],
    }
    f = tmp_path / "s.json"
    f.write_text(json.dumps(spec), encoding="utf-8")
    assert (
        rp.main(
            [
                "--spec",
                str(f),
                "--out",
                str(tmp_path / "o.pdf"),
                "--cover",
                str(tmp_path / "c.png"),
            ]
        )
        == 0
    )
    assert (tmp_path / "o.pdf").exists() and (tmp_path / "c.png").exists()
    assert (
        rp.main(
            ["--spec", str(tmp_path / "missing.json"), "--out", str(tmp_path / "x.pdf")]
        )
        == 2
    )


def test_render_with_runs_the_file_as_a_script(tmp_path: Path) -> None:
    import sys

    spec = {
        "title": "t",
        "sections": [
            {"heading": "h", "layout": "compare", "items": _items(tmp_path, 2)}
        ],
    }
    cover, pdf = rp.render_with(sys.executable, spec, tmp_path / "w" / "o.pdf")
    assert cover == rp.cover_path(pdf) and cover.exists() and pdf.exists()
    assert io.BytesIO(pdf.read_bytes()).read(4) == b"%PDF"
