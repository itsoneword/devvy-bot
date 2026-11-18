#!/usr/bin/env python3
"""One structured review PDF for design pictures (replaces the PNG collage).

The owner reviews pictures in ONE document, not in many chat images: a cover
page (title, intro, contents, a mosaic of the first pictures; also saved as a
PNG for a chat preview) and one or more sections, each in a layout::

    {"title": "...", "intro": "...",
     "sections": [{"heading": "...", "layout": "table|grid|compare|single",
                   "note": "...", "columns": 3,
                   "items": [{"image": "/abs/a.png", "caption": "...",
                              "score": "7/10", "label": "A"}]}]}

* ``table``: a row per item: thumbnail, caption, label and score. ``columns``
  may be a list of up to two header strings (caption column, score column).
  A section with ``"numbered": false`` (a reference such as "current") takes
  no numbers.
* ``grid``: ``columns`` tiles per row (default 3), the tile number on the
  picture and in the caption.
* ``compare``: items come in pairs, left then right (mockup vs build, v1 vs
  v2); ``label`` is printed above each side.
* ``single``: one picture per page with its text.

Items are numbered 1, 2, 3 ... through the whole document (a compare pair is
one number), so the owner can answer "ok: 3, 7, 12".

Run by any python with Pillow (the board's, the bot's):

    python review_pdf.py --spec spec.json --out review.pdf [--cover cover.png]

Stdlib + Pillow only (Pillow is imported lazily): the bot imports the small
helpers at the top, the MCP server and the board python load this file by
path.
"""

from __future__ import annotations

import argparse
import io
import json
import subprocess
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

LAYOUTS = ("table", "grid", "compare", "single")

PAGE_W, PAGE_H = 1240, 1754  # A4 portrait at 150 dpi
MARGIN = 70
FOOTER_Y = PAGE_H - 56
BOTTOM = PAGE_H - 100  # the last content row
MAX_IMAGE_SIDE = 1600  # an input picture is downscaled to this before use
JPEG_QUALITY = 85
DPI = 150
MAX_ITEMS = 200

INK = (20, 20, 20)
GREY = (110, 110, 110)
RULE = (210, 210, 210)
ACCENT = (26, 90, 170)
PAPER = (255, 255, 255)
PLACEHOLDER = (240, 240, 240)

_FONT_DIRS = ("/usr/share/fonts/truetype/dejavu", "/usr/share/fonts/dejavu", "")


class ReviewError(ValueError):
    """The spec cannot be rendered (named in the message)."""


def cover_path(pdf: Path) -> Path:
    """Where the cover PNG of ``pdf`` is written: ``*-sheet.png``, a name the
    asset map already treats as an overview, not a picture."""
    return pdf.with_name(pdf.stem + "-sheet.png")


def load_script() -> Path:
    """This file, for a caller that runs it as a script."""
    return Path(__file__).resolve()


def render_with(python: str, spec: Dict[str, Any], pdf: Path) -> Tuple[Path, Path]:
    """Render ``spec`` to ``pdf`` (+ its cover PNG) with ``python`` (one that
    has Pillow); returns (cover, pdf). Raises :class:`ReviewError`."""
    pdf.parent.mkdir(parents=True, exist_ok=True)
    spec_file = pdf.with_name(pdf.stem + "-spec.json")
    spec_file.write_text(json.dumps(spec, ensure_ascii=False), encoding="utf-8")
    cover = cover_path(pdf)
    cmd = [python, str(load_script()), "--spec", str(spec_file)]
    cmd += ["--out", str(pdf), "--cover", str(cover)]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=600)
    except (OSError, subprocess.SubprocessError) as e:
        raise ReviewError(f"review pdf failed: {e}") from e
    if r.returncode != 0 or not pdf.exists() or not cover.exists():
        raise ReviewError(
            f"review pdf failed (rc {r.returncode}): {(r.stderr or '').strip()[-400:]}"
        )
    return cover, pdf


def item(
    image: Any, caption: str, score: Any = None, label: str = ""
) -> Dict[str, Any]:
    """One spec item (drops the empty keys)."""
    out: Dict[str, Any] = {"image": str(image), "caption": caption}
    if score not in (None, ""):
        out["score"] = str(score)
    if label:
        out["label"] = label
    return out


# --- Pillow side ---------------------------------------------------------


def _font(size: int, bold: bool = False):  # type: ignore[no-untyped-def]
    from PIL import ImageFont

    name = "DejaVuSans-Bold.ttf" if bold else "DejaVuSans.ttf"
    for d in _FONT_DIRS:
        try:
            return ImageFont.truetype(str(Path(d) / name) if d else name, size)
        except OSError:
            continue
    return ImageFont.load_default()


def wrap(draw: Any, text: str, font: Any, width: int) -> List[str]:
    """``text`` broken into lines no wider than ``width`` px; a word longer
    than a line is cut by characters."""
    lines: List[str] = []
    for para in str(text).split("\n"):
        line = ""
        for word in para.split():
            while draw.textlength(word, font=font) > width:  # one huge word
                cut = len(word)
                while cut > 1 and draw.textlength(word[:cut], font=font) > width:
                    cut -= 1
                if line:
                    lines.append(line)
                    line = ""
                lines.append(word[:cut])
                word = word[cut:]
            trial = f"{line} {word}".strip()
            if line and draw.textlength(trial, font=font) > width:
                lines.append(line)
                line = word
            else:
                line = trial
        lines.append(line)
    return lines


def load_image(path: str) -> Any:
    """The picture as RGB, at most :data:`MAX_IMAGE_SIDE` on a side; None
    when it cannot be read (the layout draws a placeholder)."""
    from PIL import Image

    try:
        with Image.open(path) as im:
            im.load()
            if im.mode in ("RGBA", "LA", "P"):
                rgba = im.convert("RGBA")
                base = Image.new("RGB", rgba.size, PAPER)
                base.paste(rgba, mask=rgba.getchannel("A"))
                out = base
            else:
                out = im.convert("RGB")
    except (OSError, ValueError, Image.DecompressionBombError):
        return None
    out.thumbnail((MAX_IMAGE_SIDE, MAX_IMAGE_SIDE), Image.Resampling.LANCZOS)
    return out


def fit(image: Any, width: int, height: int) -> Any:
    """``image`` scaled to sit inside width x height (up to 2x enlarged)."""
    from PIL import Image

    w, h = image.size
    scale = min(width / w, height / h, 2.0)
    size = (max(1, round(w * scale)), max(1, round(h * scale)))
    return image.resize(size, Image.Resampling.LANCZOS)


class _Doc:
    """Pages drawn top to bottom; a finished page is kept as JPEG bytes."""

    def __init__(self, title: str) -> None:
        self.title = title
        self.pages: List[Optional[bytes]] = [None]  # [0]: the cover, drawn last
        self.page: Any = None
        self.draw: Any = None
        self.y = MARGIN
        self.number = 0  # last item number
        self.fonts: Dict[Tuple[int, bool], Any] = {}

    def font(self, size: int, bold: bool = False) -> Any:
        key = (size, bold)
        if key not in self.fonts:
            self.fonts[key] = _font(size, bold)
        return self.fonts[key]

    def new_page(self) -> None:
        from PIL import Image, ImageDraw

        self.flush()
        self.page = Image.new("RGB", (PAGE_W, PAGE_H), PAPER)
        self.draw = ImageDraw.Draw(self.page)
        self.y = MARGIN

    def flush(self) -> None:
        if self.page is None:
            return
        self._footer(len(self.pages) + 1, self.draw, self.page)
        self.pages.append(_jpeg(self.page))
        self.page = None

    def _footer(self, number: int, draw: Any, page: Any) -> None:
        font = self.font(20)
        draw.line((MARGIN, FOOTER_Y - 12, PAGE_W - MARGIN, FOOTER_Y - 12), fill=RULE)
        draw.text((MARGIN, FOOTER_Y), self.title[:80], font=font, fill=GREY)
        text = f"стр. {number}"
        width = draw.textlength(text, font=font)
        draw.text((PAGE_W - MARGIN - width, FOOTER_Y), text, font=font, fill=GREY)

    @property
    def page_number(self) -> int:
        """Number of the page being drawn."""
        return len(self.pages) + 1

    def room(self, height: int) -> bool:
        return self.page is not None and self.y + height <= BOTTOM

    def ensure(self, height: int, heading: str = "") -> None:
        if not self.room(height):
            self.new_page()
            if heading:
                self.text(heading + " (продолжение)", 26, bold=True, gap=14)

    def text(
        self,
        text: str,
        size: int,
        bold: bool = False,
        colour: Tuple[int, int, int] = INK,
        width: int = PAGE_W - 2 * MARGIN,
        gap: int = 8,
        x: int = MARGIN,
        max_lines: int = 0,
    ) -> int:
        """Wrapped text at the cursor; returns its height."""
        font = self.font(size, bold)
        lines = wrap(self.draw, text, font, width)
        if max_lines and len(lines) > max_lines:
            lines = lines[:max_lines]
            lines[-1] = lines[-1].rstrip() + " …"
        step = size + 7
        for line in lines:
            self.draw.text(
                (x, self.y), line, font=font, fill=INK if not colour else colour
            )
            self.y += step
        self.y += gap
        return len(lines) * step

    def badge(self, number: int, x: int, y: int, size: int = 44) -> None:
        if number <= 0:  # an unnumbered section
            return
        self.draw.ellipse((x, y, x + size, y + size), fill=INK)
        font = self.font(24, True)
        label = str(number)
        w = self.draw.textlength(label, font=font)
        self.draw.text(
            (x + (size - w) / 2, y + size / 2 - 15), label, font=font, fill=PAPER
        )

    def picture(
        self, image: Any, x: int, y: int, w: int, h: int, note: str
    ) -> Tuple[int, int]:
        """``image`` fitted into the box and centred horizontally; a missing
        one is a grey box naming ``note``. Returns the drawn (w, h)."""
        if image is None:
            self.draw.rectangle((x, y, x + w, y + h), fill=PLACEHOLDER, outline=RULE)
            font = self.font(18)
            for i, line in enumerate(wrap(self.draw, note, font, w - 16)[:4]):
                self.draw.text((x + 8, y + 8 + i * 24), line, font=font, fill=GREY)
            return w, h
        shown = fit(image, w, h)
        self.page.paste(shown, (x + (w - shown.size[0]) // 2, y))
        self.draw.rectangle(
            (
                x + (w - shown.size[0]) // 2 - 1,
                y - 1,
                x + (w - shown.size[0]) // 2 + shown.size[0],
                y + shown.size[1],
            ),
            outline=RULE,
        )
        return int(shown.size[0]), int(shown.size[1])

    def caption_block(
        self,
        entry: Dict[str, Any],
        number: Optional[int],
        width: int,
        x: int,
        size: int = 21,
        max_lines: int = 10,
    ) -> int:
        """caption (numbered), then label and score in bold; the height."""
        start = self.y
        caption = str(entry.get("caption") or "")
        if number is not None:
            caption = f"{number}. {caption}"
        if caption:
            self.text(caption, size, width=width, x=x, gap=4, max_lines=max_lines)
        tag = " · ".join(
            s
            for s in (str(entry.get("label") or ""), str(entry.get("score") or ""))
            if s
        )
        if tag:
            self.text(
                tag,
                size,
                bold=True,
                colour=ACCENT,
                width=width,
                x=x,
                gap=4,
                max_lines=3,
            )
        return self.y - start


def _jpeg(page: Any) -> bytes:
    buf = io.BytesIO()
    page.save(buf, "JPEG", quality=JPEG_QUALITY)
    return buf.getvalue()


# --- layouts -------------------------------------------------------------


def _num(first: int, k: int) -> int:
    """Number of the k-th item of a section; 0 in an unnumbered one."""
    return first + k if first > 0 else 0


def _heading(doc: _Doc, section: Dict[str, Any]) -> str:
    heading = str(section.get("heading") or "")
    doc.new_page()
    if heading:
        doc.text(heading, 34, bold=True, gap=10)
    if section.get("note"):
        doc.text(str(section["note"]), 22, colour=GREY, gap=16)
    return heading


def _table(
    doc: _Doc, section: Dict[str, Any], items: List[Dict[str, Any]], first: int
) -> None:
    heading = _heading(doc, section)
    thumb = 190
    x_thumb = MARGIN + 70
    x_text = x_thumb + thumb + 24
    right = 270
    text_w = PAGE_W - MARGIN - x_text - right - 20
    heads = section.get("columns")
    if isinstance(heads, list) and heads:
        font = doc.font(20, True)
        doc.draw.text((x_text, doc.y), str(heads[0]), font=font, fill=GREY)
        if len(heads) > 1:
            doc.draw.text(
                (PAGE_W - MARGIN - right, doc.y), str(heads[1]), font=font, fill=GREY
            )
        doc.y += 34
    for i, entry in enumerate(items):
        number = _num(first, i)
        doc.ensure(thumb + 30, heading)
        top = doc.y
        image = (
            load_image(str(entry.get("image") or "")) if entry.get("image") else None
        )
        doc.badge(number, MARGIN, top + 6, 40)
        doc.picture(image, x_thumb, top, thumb, thumb, "нет картинки")
        doc.y = top
        doc.text(
            str(entry.get("caption") or ""),
            21,
            width=text_w,
            x=x_text,
            gap=0,
            max_lines=8,
        )
        text_bottom = doc.y
        doc.y = top
        tag = [
            s
            for s in (str(entry.get("label") or ""), str(entry.get("score") or ""))
            if s
        ]
        for s in tag:
            doc.text(
                s,
                22,
                bold=True,
                colour=ACCENT,
                width=right,
                x=PAGE_W - MARGIN - right,
                gap=2,
                max_lines=3,
            )
        doc.y = max(top + thumb, text_bottom, doc.y) + 14
        doc.draw.line((MARGIN, doc.y - 7, PAGE_W - MARGIN, doc.y - 7), fill=RULE)


def _grid(
    doc: _Doc, section: Dict[str, Any], items: List[Dict[str, Any]], first: int
) -> None:
    heading = _heading(doc, section)
    raw = section.get("columns")
    cols = raw if isinstance(raw, int) and raw > 0 else 3
    cols = min(cols, 6)
    gap = 28
    cell = (PAGE_W - 2 * MARGIN - gap * (cols - 1)) // cols
    images = [load_image(str(e["image"])) if e.get("image") else None for e in items]
    ratios = [im.size[1] / im.size[0] for im in images if im is not None]
    ratio = min(2.2, max(0.75, max(ratios) if ratios else 1.0))
    box_h = round(cell * ratio)
    cap = 8 if cols <= 3 else max(4, 12 - 2 * cols)
    for r in range(0, len(items), cols):
        row = list(range(r, min(r + cols, len(items))))
        # a caption block is at most `cap` lines + a tag line + the gap
        doc.ensure(box_h + (cap + 2) * 28 + 30, heading)
        top = doc.y
        bottom = top
        for k, idx in enumerate(row):
            x = MARGIN + k * (cell + gap)
            doc.y = top
            _, h = doc.picture(images[idx], x, top, cell, box_h, "нет картинки")
            doc.badge(_num(first, idx), x + 6, top + 6, 44)
            doc.y = top + h + 10
            doc.caption_block(items[idx], None, cell, x, size=20, max_lines=cap)
            bottom = max(bottom, doc.y)
        doc.y = bottom + 24


def _compare(
    doc: _Doc, section: Dict[str, Any], items: List[Dict[str, Any]], first: int
) -> None:
    heading = _heading(doc, section)
    gap = 36
    side = (PAGE_W - 2 * MARGIN - gap) // 2
    for p in range(0, len(items), 2):
        pair = items[p : p + 2]
        images = [load_image(str(e["image"])) if e.get("image") else None for e in pair]
        box_h = 880
        heights = [
            fit(im, side, box_h - 60).size[1] if im is not None else 200
            for im in images
        ]
        doc.ensure(56 + 40 + max(heights) + 190, heading)
        top = doc.y
        doc.badge(_num(first, p // 2), MARGIN, top, 44)
        doc.y = top + 56
        row = doc.y
        bottom = row
        for k, entry in enumerate(pair):
            x = MARGIN + k * (side + gap)
            doc.y = row
            if entry.get("label"):
                doc.text(
                    str(entry["label"]),
                    24,
                    bold=True,
                    colour=ACCENT,
                    width=side,
                    x=x,
                    gap=6,
                    max_lines=2,
                )
            img_top = doc.y
            _, h = doc.picture(images[k], x, img_top, side, box_h - 60, "нет картинки")
            doc.y = img_top + h + 10
            body = dict(entry, label="")
            doc.caption_block(body, None, side, x, size=20, max_lines=14)
            bottom = max(bottom, doc.y)
        doc.y = bottom + 26


def _single(
    doc: _Doc, section: Dict[str, Any], items: List[Dict[str, Any]], first: int
) -> None:
    for i, entry in enumerate(items):
        doc.new_page()
        head = str(section.get("heading") or "")
        doc.badge(_num(first, i), MARGIN, doc.y, 48)
        if head:
            doc.draw.text(
                (MARGIN + 66, doc.y + 6), head[:70], font=doc.font(28, True), fill=INK
            )
        doc.y += 70
        if i == 0 and section.get("note"):
            doc.text(str(section["note"]), 21, colour=GREY, gap=10, max_lines=3)
        image = load_image(str(entry["image"])) if entry.get("image") else None
        avail = BOTTOM - doc.y - 230
        w, h = doc.picture(
            image, MARGIN, doc.y, PAGE_W - 2 * MARGIN, avail, "нет картинки"
        )
        doc.y += h + 16
        doc.caption_block(
            entry, None, PAGE_W - 2 * MARGIN, MARGIN, size=24, max_lines=7
        )


_LAYOUT_FUNCS = {"table": _table, "grid": _grid, "compare": _compare, "single": _single}


def _numbers(section: Dict[str, Any]) -> int:
    """How many numbers a section uses: a compare pair is one."""
    if section.get("numbered") is False:
        return 0
    n = len(section["items"])
    return (n + 1) // 2 if section["layout"] == "compare" else n


def validate(spec: Any) -> Dict[str, Any]:
    """The spec checked and normalised; raises :class:`ReviewError`."""
    if not isinstance(spec, dict):
        raise ReviewError("spec must be an object")
    sections = spec.get("sections")
    if not isinstance(sections, list) or not sections:
        raise ReviewError("spec needs at least one section")
    total = 0
    clean: List[Dict[str, Any]] = []
    for i, sec in enumerate(sections, 1):
        if not isinstance(sec, dict):
            raise ReviewError(f"section {i} must be an object")
        layout = sec.get("layout")
        if layout not in LAYOUTS:
            raise ReviewError(f"section {i}: layout must be one of {list(LAYOUTS)}")
        items = sec.get("items")
        if not isinstance(items, list) or not items:
            raise ReviewError(f"section {i}: items must be a non-empty list")
        for j, it in enumerate(items, 1):
            if not isinstance(it, dict):
                raise ReviewError(f"section {i} item {j}: must be an object")
        total += len(items)
        clean.append(dict(sec, items=items))
    if total > MAX_ITEMS:
        raise ReviewError(f"{total} items; at most {MAX_ITEMS} in one document")
    return dict(spec, sections=clean)


def _cover(
    doc: _Doc, spec: Dict[str, Any], starts: List[int], counts: List[int]
) -> Tuple[bytes, Any]:
    """Page 1: title, intro, contents, a mosaic of the first pictures."""
    doc.new_page()
    doc.text(str(spec.get("title") or "Review"), 44, bold=True, gap=14)
    if spec.get("intro"):
        doc.text(str(spec["intro"]), 24, colour=GREY, gap=20, max_lines=14)
    doc.draw.line((MARGIN, doc.y, PAGE_W - MARGIN, doc.y), fill=RULE)
    doc.y += 16
    total = sum(counts)
    doc.text(f"Позиций: {total}. Ответ: «ок: 3, 7», номера на картинках.", 22, gap=14)
    for sec, start, count in zip(spec["sections"], starts, counts):
        line = f"{sec.get('heading') or sec['layout']}  ·  {count}  ·  стр. {start}"
        if doc.room(40):
            doc.text(line, 22, gap=2, max_lines=2)
    doc.y += 16
    images: List[str] = []
    for sec in spec["sections"]:
        for entry in sec["items"]:
            if entry.get("image") and len(images) < 12:
                images.append(entry["image"])
    cols = 4
    gap = 16
    cell = (PAGE_W - 2 * MARGIN - gap * (cols - 1)) // cols
    side = min(cell, (BOTTOM - doc.y) // 3 - gap)
    for i, path in enumerate(images):
        r, c = divmod(i, cols)
        top = doc.y + r * (side + gap)
        if top + side > BOTTOM:
            break
        im = load_image(str(path))
        x = MARGIN + c * (cell + gap)
        doc.picture(im, x, top, cell, side, "нет картинки")
    doc._footer(1, doc.draw, doc.page)
    page = doc.page
    doc.page = None
    return _jpeg(page), page


def render(
    spec: Dict[str, Any], out_pdf: Path, cover_png: Optional[Path] = None
) -> int:
    """Write the PDF (and the cover PNG); returns the page count."""
    from PIL import Image

    spec = validate(spec)
    doc = _Doc(str(spec.get("title") or "Review"))
    starts: List[int] = []
    counts: List[int] = []
    number = 1
    for sec in spec["sections"]:
        starts.append(doc.page_number)
        used = _numbers(sec)
        _LAYOUT_FUNCS[sec["layout"]](doc, sec, sec["items"], number if used else 0)
        number += used
        counts.append(used or len(sec["items"]))
        doc.flush()
    # The cover goes first; section pages were numbered from 2.
    cover_jpeg, cover_page = _cover(doc, spec, starts, counts)
    doc.pages[0] = cover_jpeg
    if cover_png is not None:
        cover_png.parent.mkdir(parents=True, exist_ok=True)
        cover_page.save(cover_png)
    out_pdf.parent.mkdir(parents=True, exist_ok=True)
    pages = [Image.open(io.BytesIO(b)) for b in doc.pages if b is not None]
    pages[0].save(
        out_pdf,
        "PDF",
        save_all=True,
        append_images=pages[1:],
        resolution=float(DPI),
        quality=JPEG_QUALITY,
        title=str(spec.get("title") or ""),
    )
    return len(pages)


def main(argv: Optional[Sequence[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--spec", type=Path, required=True, help="JSON spec file")
    ap.add_argument("--out", type=Path, required=True, help="the PDF to write")
    ap.add_argument("--cover", type=Path, default=None, help="the cover PNG")
    args = ap.parse_args(argv)
    try:
        spec = json.loads(args.spec.read_text(encoding="utf-8"))
        pages = render(spec, args.out, args.cover)
    except (OSError, ValueError) as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    print(f"{args.out}: {pages} pages")
    return 0


if __name__ == "__main__":
    sys.exit(main())
