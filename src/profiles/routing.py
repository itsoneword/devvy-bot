"""Pure profile-routing logic: parse switch/one-shot/plain from message text."""

from dataclasses import dataclass
from typing import Literal, Optional, Set

RouteKind = Literal["plain", "switch", "one_shot"]


@dataclass(frozen=True)
class RouteResult:
    target: str  # slug to run this message against
    prompt: str  # cleaned prompt (prefix stripped)
    new_active: Optional[str]  # set active profile to this slug, or None to leave it
    kind: RouteKind


def resolve_route(text: str, active: str, valid_slugs: Set[str]) -> RouteResult:
    """Decide routing for one inbound message.

    - "/slug [text]"  -> sticky switch to slug; run [text] if present.
    - "@slug text"    -> one-shot to slug; active unchanged.
    - anything else   -> plain message to the active profile.
    Unknown slugs fall through to a plain message (text untouched).
    """
    stripped = text.strip()

    if stripped.startswith("/"):
        head, _, rest = stripped[1:].partition(" ")
        if head in valid_slugs:
            return RouteResult(
                target=head, prompt=rest.strip(), new_active=head, kind="switch"
            )

    if stripped.startswith("@"):
        head, _, rest = stripped[1:].partition(" ")
        if head in valid_slugs:
            return RouteResult(
                target=head, prompt=rest.strip(), new_active=None, kind="one_shot"
            )

    return RouteResult(target=active, prompt=text, new_active=None, kind="plain")
