"""Child process that drives one form in headless Chromium (dv-5a25).

``python -m src.webforms.runner <job.json>``; prints one JSON result line on
stdout. Started by ``src/claude/webform_tools.py`` with a hard timeout, so a
browser crash or hang never touches the bot's event loop.

Job: ``{"form": id, "data": {...}, "submit": bool, "out_dir": path}``.

Only main-frame navigations and form posts are pinned to the form's domain
(an off-domain one is aborted and fails the run); sub-resources (consent
banner, fonts, CDN) load as they would in a normal browser.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import CHECK, FILE, FORMS, RADIO, SELECT, FormDef
from .engine import run_form, url_allowed

NAV_TIMEOUT_MS = 60_000
STEP_TIMEOUT_MS = 30_000
SUBMIT_TIMEOUT_MS = 60_000


class PlaywrightDriver:
    """The page driver ``engine.run_form`` expects, over a sync Playwright page."""

    def __init__(self, form: FormDef, page: Any) -> None:
        self.form = form
        self.pg = page
        self.page_no = 1
        self.blocked: Optional[str] = None
        self.submitted = False
        self._parts: Dict[str, List[str]] = {}
        self._files: List[str] = []
        page.route("**/*", self._route)

    # -- network pin ------------------------------------------------------
    def _route(self, route: Any, request: Any) -> None:
        main_nav = (
            request.is_navigation_request() and request.frame == self.pg.main_frame
        )
        form_post = request.method == "POST" and request.resource_type == "document"
        if (main_nav or form_post) and not url_allowed(self.form, request.url):
            self.blocked = self.blocked or request.url
            route.abort()
            return
        route.continue_()

    def url(self) -> str:
        return self.blocked or self.pg.url

    # -- page flow -------------------------------------------------------------
    def open(self, url: str) -> None:
        # "load", not "networkidle": on 13.11 a request on the site that never
        # settled failed four runs in a row at goto (60 s), while the form was up.
        self.pg.goto(url, wait_until="load", timeout=NAV_TIMEOUT_MS)
        try:
            self.pg.wait_for_load_state("networkidle", timeout=STEP_TIMEOUT_MS)
        except Exception:  # noqa: BLE001 - the form is usable without idle
            pass
        self.pg.wait_for_timeout(1500)
        for sel in ("button.cky-btn-accept", "text=Accept All"):
            try:
                self.pg.click(sel, timeout=2000)
                break
            except Exception:  # noqa: BLE001 - no banner is fine
                pass
        self._clear_overlays()

    def _clear_overlays(self) -> None:
        if self.form.overlay_selectors:
            self.pg.evaluate(
                "(s)=>document.querySelectorAll(s).forEach(e=>e.remove())",
                ",".join(self.form.overlay_selectors),
            )

    def _page_sel(self, n: int) -> str:
        return next(p.selector for p in self.form.pages if p.number == n)

    def next_page(self) -> None:
        cur = self._page_sel(self.page_no)
        nxt = self._page_sel(self.page_no + 1)
        self.pg.click(f"{cur} {self.form.next_selector}")
        self.pg.wait_for_function(
            "([n, v]) => { const e = document.querySelector(n);"
            " if (e && e.offsetParent !== null) return true;"
            " return [...document.querySelectorAll(v)]"
            ".some(x => x.offsetParent !== null && x.innerText.trim()); }",
            arg=[nxt, self.form.validation_selector],
            timeout=STEP_TIMEOUT_MS,
        )
        self.pg.wait_for_timeout(800)
        if self.pg.locator(nxt).is_visible():
            self.page_no += 1
        self._clear_overlays()

    def validation_errors(self) -> List[str]:
        return list(
            self.pg.evaluate(
                "(s)=>[...document.querySelectorAll(s)]"
                ".filter(e=>e.offsetParent!==null).map(e=>e.innerText.trim())"
                ".filter(Boolean)",
                self.form.validation_selector,
            )
        )

    # -- fields -----------------------------------------------------------------
    def fill_field(self, page: int, key: str, value: Any) -> None:
        f = self.form.field(page, key)
        if f.kind == RADIO:
            self.pg.check(f.options[str(value)], force=True)
        elif f.kind == SELECT:
            self.pg.select_option(
                f.selector, label=f.options.get(str(value), str(value))
            )
        elif f.kind == CHECK:
            if value:
                self.pg.check(f.selector, force=True)
            else:
                self.pg.uncheck(f.selector, force=True)
        else:
            text = str(value)
            if f.part:
                parts = self._parts.setdefault(f.selector, [""] * f.part[1])
                parts[f.part[0]] = text
                text = " ".join(p for p in parts if p)
            self.pg.fill(f.selector, text)
            if f.escape:
                self.pg.keyboard.press("Escape")
        self.pg.wait_for_timeout(150)

    def read_field(self, page: int, key: str) -> Any:
        f = self.form.field(page, key)
        if f.kind == RADIO:
            for label, sel in f.options.items():
                if self.pg.is_checked(sel):
                    return label
            return ""
        if f.kind == CHECK:
            return bool(self.pg.is_checked(f.selector))
        if f.kind == SELECT:
            label = self.pg.eval_on_selector(
                f.selector, "e => e.selectedOptions[0] ? e.selectedOptions[0].text : ''"
            )
            back = {v: k for k, v in f.options.items()}
            return back.get(str(label).strip(), str(label).strip())
        value = str(self.pg.input_value(f.selector))
        if f.part:
            pieces = value.split(" ", f.part[1] - 1)
            return pieces[f.part[0]] if f.part[0] < len(pieces) else ""
        return value

    def upload(self, page: int, key: str, paths: List[str]) -> None:
        f = self.form.field(page, key)
        assert f.kind == FILE
        self._files = list(paths)
        self.pg.locator(f.selector).set_input_files(paths)

    def upload_pending(self) -> bool:
        """True until Gravity Forms lists every file as uploaded."""
        self.pg.wait_for_timeout(500)
        state = self.pg.evaluate(
            "()=>{const h=[...document.querySelectorAll('input[name=gform_uploaded_files]')]"
            ".map(e=>e.value).join(' ');"
            "const busy=[...document.querySelectorAll('.ginput_preview')]"
            ".some(e=>/\\d+%/.test(e.innerText)&&!/100%/.test(e.innerText));"
            "return {h, busy};}"
        )
        if state.get("busy"):
            return True
        uploaded = str(state.get("h") or "")
        return not all(Path(p).name in uploaded for p in self._files)

    # -- output -----------------------------------------------------------------
    def screenshot(self, path: str) -> None:
        wrapper = (
            self.pg.locator(self.form.form_selector)
            if self.form.form_selector
            else None
        )
        if not self.submitted and wrapper is not None and wrapper.count():
            wrapper.first.screenshot(path=path)
        else:
            self.pg.screenshot(path=path, full_page=True)

    def click_submit(self) -> None:
        self.submitted = True
        self.pg.click(self.form.submit_selector)
        try:
            self.pg.wait_for_selector(
                f"{self.form.confirmation_selector}, .gform_validation_errors",
                timeout=SUBMIT_TIMEOUT_MS,
            )
        except Exception:  # noqa: BLE001 - a redirect has neither; engine decides
            pass
        try:
            self.pg.wait_for_load_state("networkidle", timeout=STEP_TIMEOUT_MS)
        except Exception:  # noqa: BLE001
            pass
        self.pg.wait_for_timeout(1500)

    def confirmation(self) -> Optional[str]:
        loc = self.pg.locator(self.form.confirmation_selector)
        if loc.count() and loc.first.is_visible():
            return str(loc.first.inner_text()).strip() or "confirmed"
        return None


def run_job(job: Dict[str, Any]) -> Dict[str, Any]:
    form = FORMS[job["form"]]
    from playwright.sync_api import sync_playwright  # type: ignore[import-not-found]

    with sync_playwright() as p:
        browser = p.chromium.launch()
        try:
            page = browser.new_page(viewport={"width": 1100, "height": 1400})
            driver = PlaywrightDriver(form, page)
            return run_form(
                form,
                job["data"],
                driver,
                Path(job["out_dir"]),
                submit=bool(job.get("submit")),
            )
        finally:
            browser.close()


def main(argv: List[str]) -> int:
    job = json.loads(Path(argv[1]).read_text(encoding="utf-8"))
    try:
        result = run_job(job)
    except Exception as exc:  # noqa: BLE001 - report, never a traceback
        result = {
            "status": "failed",
            "message": f"browser did not start or crashed: {type(exc).__name__}: {exc}",
            "screenshots": [],
            "warnings": [],
            "failure": None,
            "url": "",
        }
    out = Path(job["out_dir"]) / "result.json"
    out.write_text(json.dumps(result, indent=2), encoding="utf-8")
    print(json.dumps(result))
    return 0


if __name__ == "__main__":  # pragma: no cover - the child process
    sys.exit(main(sys.argv))
