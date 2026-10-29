"""``scripts/screens``: check and draw every registered bot screen (dv-4cd8).

    scripts/screens check
        Run the checks on every registry screen. Exit 0 when each passes or
        is ``known_bad`` and still fails; 1 otherwise (a violation, a build
        error, or a ``known_bad`` screen that passes now: drop the marker).
    scripts/screens render --out DIR [--base REV]
        The same, plus ``DIR/after/<screen id>.png`` and ``DIR/report.json``.
        ``--base REV`` also draws REV's screens into ``DIR/before/`` with REV's
        own renderer (``git archive`` into a temp dir, read-only git), so
        review sees what a change did to each screen. Same exit codes.

The frozen fixtures (``fixtures/``) are shipped mistakes kept to fail; they
are not part of the registry and never fail this command.
"""

import argparse
import asyncio
import io
import json
import shutil
import subprocess
import sys
import tarfile
import tempfile
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from .checks import check
from .model import Screen
from .registry import SCREENS

REPO = Path(__file__).resolve().parents[3]
#: What a base revision needs to draw its screens.
BASE_PATHS = ("src", "scripts/screens")
BASE_TIMEOUT_S = 600


async def _build_all() -> List[Dict[str, Any]]:
    """One entry per registry screen spec: its screens or its build error."""
    out: List[Dict[str, Any]] = []
    for spec_id in sorted(SCREENS):
        spec = SCREENS[spec_id]
        entry: Dict[str, Any] = {"spec": spec, "screens": [], "error": None}
        try:
            entry["screens"] = list(await spec.build())
            if not entry["screens"]:
                entry["error"] = "built no screen"
        except Exception as exc:  # a crashing builder is a failed screen
            entry["error"] = f"{type(exc).__name__}: {exc}"
        out.append(entry)
    return out


def _verdict(entry: Dict[str, Any]) -> Dict[str, Any]:
    """The report record of one spec, and whether it fails the command."""
    spec = entry["spec"]
    screens: List[Screen] = entry["screens"]
    record: Dict[str, Any] = {
        "id": spec.id,
        "goal": spec.goal,
        "known_bad": spec.known_bad,
        "error": entry["error"],
        "screens": [],
    }
    found = 0
    for screen in screens:
        violations = check(screen)
        found += len(violations)
        record["screens"].append(
            {
                "id": screen.id,
                "render_ms": screen.render_ms,
                "violations": [
                    {"code": v.code, "where": v.where, "detail": v.detail}
                    for v in violations
                ],
            }
        )
    if entry["error"]:
        record["failed"] = True
    elif spec.known_bad:
        # Strict, like the gate test: a fixed screen must drop its marker.
        record["failed"] = found == 0
    else:
        record["failed"] = found > 0
    return record


def _print(record: Dict[str, Any]) -> None:
    sid = record["id"]
    if record["error"]:
        print(f"FAIL {sid}: {record['error']}")
        return
    violations = [v for s in record["screens"] for v in s["violations"]]
    if record["known_bad"]:
        if violations:
            codes = ", ".join(sorted({v["code"] for v in violations}))
            print(f"known_bad {sid} ({record['known_bad']}): {codes}")
        else:
            print(f"FAIL {sid}: passes now, drop known_bad={record['known_bad']}")
        return
    if not violations:
        print(f"ok   {sid}")
        return
    print(f"FAIL {sid}")
    for v in violations:
        print(f"     {v['code']}: {v['where']} — {v['detail']}")


def _check(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    records = [_verdict(e) for e in entries]
    for record in records:
        _print(record)
    return records


def cmd_check() -> int:
    records = _check(asyncio.run(_build_all()))
    return 1 if any(r["failed"] for r in records) else 0


def _render_after(
    entries: List[Dict[str, Any]], records: List[Dict[str, Any]], out: Path
) -> None:
    from .render import render

    after = out / "after"
    after.mkdir(parents=True, exist_ok=True)
    for entry, record in zip(entries, records):
        for screen, srec in zip(entry["screens"], record["screens"]):
            try:
                png = render(screen)
            except Exception as exc:  # a screen that cannot be drawn fails
                record["failed"] = True
                srec["render_error"] = f"{type(exc).__name__}: {exc}"
                print(f"FAIL {screen.id}: render: {srec['render_error']}")
                continue
            path = after / f"{screen.id}.png"
            path.write_bytes(png)
            srec["png"] = str(path.relative_to(out))


def _git(*args: str) -> "subprocess.CompletedProcess[bytes]":
    return subprocess.run(
        ["git", "-C", str(REPO), *args], capture_output=True, timeout=120
    )


def _render_base(rev: str, out: Path) -> Dict[str, Any]:
    """Draw ``rev``'s screens with ``rev``'s code into ``out/before``."""
    listed = _git("ls-tree", "--name-only", rev, "--", *BASE_PATHS)
    if listed.returncode != 0:
        return {"rev": rev, "error": listed.stderr.decode(errors="replace").strip()}
    present = [p for p in listed.stdout.decode().split("\n") if p]
    if "scripts/screens" not in present:
        return {"rev": rev, "error": f"{rev} has no scripts/screens: no before"}
    archive = _git("archive", "--format=tar", rev, "--", *present)
    if archive.returncode != 0:
        return {"rev": rev, "error": archive.stderr.decode(errors="replace").strip()}

    with tempfile.TemporaryDirectory(prefix="screens-base-") as tmp:
        tree = Path(tmp) / "tree"
        with tarfile.open(fileobj=io.BytesIO(archive.stdout)) as tar:
            tar.extractall(tree, filter="data")
        base_out = Path(tmp) / "out"
        try:
            proc = subprocess.run(
                [sys.executable, "scripts/screens", "render", "--out", str(base_out)],
                cwd=tree,
                capture_output=True,
                text=True,
                timeout=BASE_TIMEOUT_S,
            )
        except subprocess.TimeoutExpired:
            return {"rev": rev, "error": f"timed out after {BASE_TIMEOUT_S} s"}
        report_path = base_out / "report.json"
        if not report_path.exists():
            tail = (proc.stdout + proc.stderr)[-2000:]
            return {"rev": rev, "error": f"exit {proc.returncode}: {tail}"}
        report: Dict[str, Any] = json.loads(report_path.read_text(encoding="utf-8"))
        before = out / "before"
        before.mkdir(parents=True, exist_ok=True)
        for png in sorted((base_out / "after").glob("*.png")):
            shutil.copy2(png, before / png.name)
    report.pop("before", None)
    report["rev"] = rev
    return report


def cmd_render(out: Path, base: Optional[str]) -> int:
    out.mkdir(parents=True, exist_ok=True)
    entries = asyncio.run(_build_all())
    records = _check(entries)
    report: Dict[str, Any] = {"base": base, "screens": records, "error": None}
    try:
        import PIL  # noqa: F401
    except ImportError:
        report["error"] = "Pillow is not installed: no PNGs drawn"
        print(report["error"])
    else:
        _render_after(entries, records, out)
        if base:
            report["before"] = _render_base(base, out)
            if report["before"].get("error"):
                print(f"before ({base}): {report['before']['error']}")
    report["failed"] = sorted(r["id"] for r in records if r["failed"])
    (out / "report.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(f"report: {out / 'report.json'}")
    if report["error"]:
        return 2
    return 1 if report["failed"] else 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="screens", description=__doc__)
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="run the checks on every registry screen")
    render = sub.add_parser("render", help="check and draw every registry screen")
    render.add_argument("--out", required=True, type=Path, help="output directory")
    render.add_argument("--base", help="also draw this revision's screens (before/)")
    args = parser.parse_args(argv)
    if args.cmd == "check":
        return cmd_check()
    return cmd_render(args.out, args.base)
