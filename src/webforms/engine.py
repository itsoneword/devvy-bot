"""The deterministic form filler (dv-5a25).

``run_form`` walks a :class:`FormDef` page by page through a *page driver*
(the Playwright one lives in ``runner.py``; tests use a fake): fill every
value of the plan, wait for uploads, screenshot the step, read every field
back against the data, and stop at the first mismatch, validation message,
unfinished upload or off-domain navigation with a structured failure. Submit
is clicked only when ``submit`` is True and every page passed.

No judgement happens here: a failure is returned for the agent to read.

Driver interface: ``open(url)``, ``fill_field(page, key, value)``,
``read_field(page, key)``, ``upload(page, key, paths)``, ``upload_pending()``,
``validation_errors()``, ``next_page()``, ``screenshot(path)``,
``click_submit()``, ``url()``, ``confirmation()``.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional
from urllib.parse import urlsplit

from . import BadData, FillPlan, FormDef, Unsupported

# upload_pending() polls; the Playwright driver waits ~0.5 s per poll.
UPLOAD_POLLS = 240
UNCONFIRMED_MESSAGE = (
    "Submit was clicked but no confirmation was seen. Do not resubmit: check "
    "the William Russell email first (a retry files a duplicate claim)."
)


def url_allowed(form: FormDef, url: str) -> bool:
    """https on the form's domain or one of its subdomains, nothing else."""
    try:
        parts = urlsplit(url)
    except ValueError:
        return False
    host = (parts.hostname or "").lower().rstrip(".")
    domain = form.domain.lower()
    return parts.scheme == "https" and (host == domain or host.endswith("." + domain))


def _same(expected: Any, actual: Any) -> bool:
    if isinstance(expected, bool) or isinstance(actual, bool):
        return bool(expected) == bool(actual) and isinstance(actual, bool)
    norm = lambda v: " ".join(str(v if v is not None else "").split())  # noqa: E731
    return norm(expected) == norm(actual)


def _path_of(url: str) -> str:
    parts = urlsplit(url)
    return f"{(parts.hostname or '').lower()}{parts.path.rstrip('/')}"


def run_form(
    form: FormDef,
    data: Dict[str, Any],
    driver: Any,
    shots_dir: Any,
    submit: bool = False,
) -> Dict[str, Any]:
    shots = Path(shots_dir)
    shots.mkdir(parents=True, exist_ok=True)
    result: Dict[str, Any] = {
        "status": "failed",
        "form": form.id,
        "screenshots": [],
        "failure": None,
        "warnings": [],
        "url": "",
        "message": "",
    }

    def shot(name: str) -> str:
        path = str(shots / f"{name}.png")
        try:
            driver.screenshot(path)
        except Exception:  # noqa: BLE001 - a failed screenshot is not the failure
            return ""
        result["screenshots"].append(path)
        return path

    def current_url() -> str:
        try:
            return str(driver.url() or "")
        except Exception:  # noqa: BLE001
            return ""

    def fail(
        page: Optional[int],
        message: str,
        field: Optional[str] = None,
        expected: Any = None,
        actual: Any = None,
        validation: Optional[List[str]] = None,
        screenshot: str = "",
    ) -> Dict[str, Any]:
        result["status"] = "failed"
        result["url"] = current_url()
        result["message"] = message
        result["failure"] = {
            "page": page,
            "field": field,
            "expected": expected,
            "actual": actual,
            "validation": list(validation or []),
            "screenshot": screenshot,
            "message": message,
        }
        return result

    try:
        plan: FillPlan = form.plan(data)
    except Unsupported as exc:
        result.update(status="unsupported", message=str(exc))
        return result
    except BadData as exc:
        return fail(None, str(exc), field=exc.key)
    result["warnings"] = list(plan.warnings)

    def off_domain(page: Optional[int]) -> Optional[Dict[str, Any]]:
        url = current_url()
        if url_allowed(form, url):
            return None
        return fail(
            page,
            f"navigation left {form.domain}: {url} (run aborted)",
            screenshot="",
        )

    page_no: Optional[int] = None
    clicked = False
    try:
        driver.open(form.url)
        bad = off_domain(None)
        if bad:
            return bad
        last = form.pages[-1].number
        for page in form.pages:
            page_no = page.number
            values = plan.values.get(page_no, [])
            for key, value in values:
                driver.fill_field(page_no, key, value)
            if plan.upload and plan.upload[0] == page_no and plan.files:
                driver.upload(page_no, plan.upload[1], list(plan.files))
                pending = True
                for _ in range(UPLOAD_POLLS):
                    pending = bool(driver.upload_pending())
                    if not pending:
                        break
                if pending:
                    path = shot(f"step{page_no}")
                    return fail(
                        page_no,
                        "upload did not finish",
                        field=plan.upload[1],
                        expected=[Path(f).name for f in plan.files],
                        screenshot=path,
                    )
            path = shot(f"step{page_no}")
            for key, value in values:
                actual = driver.read_field(page_no, key)
                if not _same(value, actual):
                    return fail(
                        page_no,
                        f"field {key} reads back {actual!r}, expected {value!r}",
                        field=key,
                        expected=value,
                        actual=actual,
                        validation=driver.validation_errors(),
                        screenshot=path,
                    )
            errors = list(driver.validation_errors() or [])
            if errors:
                return fail(
                    page_no,
                    "the form shows validation errors",
                    validation=errors,
                    screenshot=path,
                )
            if page_no == last:
                break
            driver.next_page()
            bad = off_domain(page_no)
            if bad:
                return bad
            errors = list(driver.validation_errors() or [])
            if errors:
                return fail(
                    page_no,
                    "the form refused the page",
                    validation=errors,
                    screenshot=shot(f"step{page_no}-refused"),
                )

        if not submit:
            result.update(
                status="filled",
                url=current_url(),
                message=f"all {len(form.pages)} pages filled and read back; not submitted",
            )
            return result

        page_no = None
        clicked = True
        driver.click_submit()
        url = current_url()
        errors = list(driver.validation_errors() or [])
        confirmation = driver.confirmation()
        path = shot("confirmation")
        result["url"] = url
        if errors and not confirmation:
            return fail(
                last,
                "the form refused the submission",
                validation=errors,
                screenshot=path,
            )
        moved = url_allowed(form, url) and _path_of(url) != _path_of(form.url)
        if confirmation or moved:
            result.update(
                status="submitted",
                message=str(confirmation or f"submitted; the form moved to {url}"),
            )
            return result
        note = "" if url_allowed(form, url) else f" The page left {form.domain}: {url}."
        result.update(status="unconfirmed", message=UNCONFIRMED_MESSAGE + note)
        return result
    except Exception as exc:  # noqa: BLE001 - every driver error is a failure
        if clicked:
            result.update(
                status="unconfirmed",
                url=current_url(),
                message=f"{UNCONFIRMED_MESSAGE} ({type(exc).__name__}: {exc})",
            )
            shot("confirmation")
            return result
        return fail(
            page_no,
            f"{type(exc).__name__}: {exc}",
            screenshot=shot(f"step{page_no}-error") if page_no else "",
        )
