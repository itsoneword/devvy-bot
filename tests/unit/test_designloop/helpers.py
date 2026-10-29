"""A trimmed copy of the gymbro docs the design loop reads (dv-9133 phase 2).

Never the live gymbro repo: the shapes below are cut from it
(docs/features.yaml, docs/SCREENS.md, docs/UI_GUIDE.md, docs/screens/) so a
test pins the format, not today's content.

- features.yaml: ``screens: <screen_id>: {passport, source, features:
  {<feature>: {action?, api?, scenarios}}}``, the YAML subset gymbro's
  ``scripts/harness/feature_map.py`` reads (quoted test ids with ``[..]``).
- SCREENS.md: ``## 1. Shared tokens`` + one ``###`` passport per screen; a
  screen's ``passport:`` is its heading up to `` (``.
- docs/screens/<screen_id>/<state>-<theme>.png: references, dark + light.
"""

from pathlib import Path
from typing import Any, Dict, List, Optional

FEATURES_YAML = """\
# GymBro feature map (gb-1a72): screen -> feature -> scenarios.
# Written in a YAML subset (feature_map.loads): quote anything with braces, '*' or '#'.

bug_rule_since: 2026-10-24

screens:
  feed:
    passport: TAB Feed
    source: webapp/src/Discover.tsx
    features:
      new_event_bell:
        scenarios:
          - id: feed.new_event_bell.wired
            test: tests/web/test_screen_invariants.py::test_new_event_alerts_screen_and_feed_bell_wired
            from: [gb-1aa8]
      discover_list:
        api: ["GET /api/events/discover"]
        scenarios:
          - id: feed.discover_list.baseline
            test: "tests/harness/test_journeys.py::test_baseline_diff_flags_an_extra_button[feed]"
      sort_remembered:
        scenarios:
          - id: feed.sort_remembered.per_user
            test: tests/web/test_profile_api.py::test_discover_sort_is_remembered_per_user
      saved_chip:
        action: Saved
        scenarios:
          - id: feed.saved_chip.text_chip_not_bookmark
            test: tests/web/test_screen_invariants.py::test_discover_saved_entry_is_a_text_chip_not_the_bookmark

  profile_tab:
    passport: TAB Profile
    source: webapp/src/Profile.tsx
    features:
      own_profile:
        api: ["GET /api/me/profile"]
        scenarios:
          - id: profile_tab.own_profile.baseline
            test: "tests/harness/test_journeys.py::test_baseline_diff_flags_an_extra_button[profile]"
      goals:
        action: Achieved
        scenarios:
          - id: profile_tab.goals.move_back
            test: tests/web/test_profile_api.py::test_achieved_goal_can_move_back_to_active
      bug_report:
        action: Report a bug
        api: ["POST /api/me/bug-context"]
        scenarios:
          - id: profile_tab.bug_report.context_persists
            test: tests/web/test_persist_api.py::test_bug_context_is_stored
            persist: ["POST /api/me/bug-context"]

  event_page:
    passport: EventPage
    source: webapp/src/EventView.tsx
    features:
      view:
        api: ["GET /api/events/{id}"]
        scenarios:
          - id: event_page.view.baseline
            test: "tests/harness/test_journeys.py::test_baseline_diff_flags_an_extra_button[event_page]"
      join_leave:
        action: I'm going
        api: ["POST /api/events/{id}/join", "POST /api/events/{id}/leave"]
        scenarios:
          - id: event_page.join_leave.persists
            test: tests/web/test_persist_api.py::test_join_and_leave_persist
            persist: ["POST /api/events/{id}/join", "POST /api/events/{id}/leave"]
      share:
        action: Share
        api: ["GET /api/events/{id}/share"]
        scenarios:
          - id: event_page.share.link_qr_start_param
            test: tests/web/test_create_api.py::test_share_link_qr_and_start_param_resolution
"""

SCREENS_MD = """\
# Screens

## 1. Shared tokens (as they are in code)

**Colors.** Only tgui theme vars for UI chrome: `--tgui--bg_color`, `secondary_bg_color`, `hint_color`.
**Type.** Sizes in use: 12 (hint) · 15 (body row) · 16 (inputs, card title) · 18 (sheet title).
**Spacing.** Content inset **16**. Card lists inset **12**. Touch targets 44 / 38 / 32.

## 2. Mini App: tabs

### TAB Feed (`Discover.tsx`, tab `feed`, default)
- Blocks: [view switch (carousel|map|list) + «Saved» text chip] · [3 filter buttons] · [bell chip] · body: Carousel.
- Primary: open an event (carousel "Open" / card tap). FEED_PASSPORT_MARKER

### TAB Profile (`Profile.tsx` `ProfileTab`, view mode)
- Blocks: avatar+name · stats · track record · active goals (ConfirmButton "Achieved") · cell: Report a bug.
- Primary: Edit profile. PROFILE_PASSPORT_MARKER

## 3. Mini App: pages and sheets

### EventPage (`App.tsx` `EventPage` + `EventView.tsx`)
- Blocks: Organizer · activity header (Save, Share, report menu) · going list · Comments.
- Primary: **BottomBar** "I'm going". EVENT_PASSPORT_MARKER
"""

UI_GUIDE_MD = """\
# UI guide — Mini App and bot screens

## Checklist
- One primary action per screen. UI_GUIDE_MARKER
"""

# screen -> states with dark + light references.
REFS = {
    "feed": ("main", "map_hint"),
    "event_page": ("other", "going"),
    "profile_tab": ("main", "about"),
}

FEED_FEATURES = ("new_event_bell", "discover_list", "sort_remembered", "saved_chip")
FEED_ACTIONS = ("Saved",)
FEED_API = ("GET /api/events/discover",)


def make_gymbro_repo(tmp_path: Path) -> Path:
    repo = tmp_path / "gymbro"
    docs = repo / "docs"
    docs.mkdir(parents=True)
    (docs / "features.yaml").write_text(FEATURES_YAML, encoding="utf-8")
    (docs / "SCREENS.md").write_text(SCREENS_MD, encoding="utf-8")
    (docs / "UI_GUIDE.md").write_text(UI_GUIDE_MD, encoding="utf-8")
    for screen, states in REFS.items():
        folder = docs / "screens" / screen
        folder.mkdir(parents=True)
        for state in states:
            for theme in ("dark", "light"):
                (folder / f"{state}-{theme}.png").write_bytes(
                    b"\x89PNG\r\n\x1a\n" + f"{screen}/{state}/{theme}".encode()
                )
    return repo


def ref(repo: Path, screen: str, state: str, theme: str) -> Path:
    return repo / "docs" / "screens" / screen / f"{state}-{theme}.png"


def conforming(
    schema: Dict[str, Any], note: str, path: Optional[List[str]] = None
) -> Any:
    """An instance of ``schema`` as a model would answer it: the first
    criterion gets ``fix: <note>``, the others ``ok``, the verdict
    ``revise``. Built from the schema the adapter received, so a builder's
    extra fields (a score, say) do not break the fake."""
    path = path or []
    if "enum" in schema:
        values = list(schema["enum"])
        return "revise" if "revise" in values else values[0]
    kind = schema.get("type")
    kinds = kind if isinstance(kind, list) else [kind]
    kinds = [k for k in kinds if k != "null"] or ["null"]
    first = kinds[0]
    if first == "object" or (first is None and "properties" in schema):
        props = schema.get("properties", {})
        out = {}
        for i, (key, sub) in enumerate(props.items()):
            if path and path[-1] == "criteria":
                out[key] = f"fix: {note} ({key})" if i == 0 else "ok"
            else:
                out[key] = conforming(sub, note, path + [key])
        return out
    if first == "array":
        return []
    if first == "string":
        return "x"
    if first in ("integer", "number"):
        low = schema.get("minimum", 1)
        high = schema.get("maximum", low)
        return max(low, min(high, low))
    if first == "boolean":
        return True
    return None
