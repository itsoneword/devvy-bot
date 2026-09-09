"""Profile bootstrap -- materialises workspaces and personas on first run.

Mirrors ``bootstrap_memory``: copy from a committed template to a runtime path,
never overwrite an existing file, log either way. The difference is that this
one is driven by the profile registry, so a fresh clone with the shipped
``profiles.yaml`` produces every profile's ``working_dir``, ``memory_dir`` and
``persona`` without a single hand-edit.

Runs *before* ``validate_registry_paths``: on a first run the persona files it
creates are exactly the ones validation would otherwise abort on.

Template lookup per profile, in order:

1. the profile's explicit ``persona_template:`` (already resolved by the
   registry through the same ``${VAR}``/relative rules as every other path);
2. ``<templates_dir>/local/<slug>/persona.md`` -- profiles the owner created
   through the new-agent interview (dv-0e1e). Same layout as 3, kept apart
   because these carry personal paths and never ship;
3. ``<templates_dir>/profiles/<slug>/persona.md`` -- the shipped
   profile-template layout (dv-4e29), where the persona sits next to the
   ``profile.yaml`` fragment the installer instantiated the profile from;
4. ``<templates_dir>/personas/<slug>.md`` -- the older flat layout, still
   honoured so an existing checkout that carries one keeps working.

A profile with neither a persona nor a template is *not* an error here -- it is
left alone and ``validate_registry_paths`` reports it, naming the profile and
the path it expected. Reporting it twice, in two different shapes, would only
make the real message harder to find.
"""

import shutil
from pathlib import Path
from typing import Optional

import structlog

from src.profiles import ProfileDefinition, ProfileRegistry

logger = structlog.get_logger()

PERSONAS_SUBDIR = "personas"
PROFILES_SUBDIR = "profiles"
LOCAL_SUBDIR = "local"


def _persona_candidates(slug: str, templates_dir: Path) -> list[Path]:
    """Conventional persona-template paths for ``slug``, best first."""
    return [
        templates_dir / LOCAL_SUBDIR / slug / "persona.md",
        templates_dir / PROFILES_SUBDIR / slug / "persona.md",
        templates_dir / PERSONAS_SUBDIR / f"{slug}.md",
    ]


def _persona_template_for(
    profile: ProfileDefinition, templates_dir: Path
) -> Optional[Path]:
    """The template that would seed this profile's persona, if any exists."""
    if profile.persona_template is not None:
        return profile.persona_template if profile.persona_template.is_file() else None
    for candidate in _persona_candidates(profile.slug, templates_dir):
        if candidate.is_file():
            return candidate
    return None


def _ensure_dir(path: Path, *, slug: str, field_name: str) -> bool:
    """Create a profile directory if absent; never touch an existing one.

    Returns whether the directory is usable afterwards. Every failure here --
    a file in the way, a read-only parent -- is deliberately swallowed: it is
    already an error case ``validate_registry_paths`` reports naming the
    profile, the field and the path, and a raw ``OSError`` traceback out of
    boot would only bury that message.
    """
    if path.is_dir():
        return True
    if path.exists():
        logger.warning(
            "Profile path exists but is not a directory, skipping",
            profile=slug,
            field=field_name,
            path=str(path),
        )
        return False
    try:
        path.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.warning(
            "Could not create profile directory",
            profile=slug,
            field=field_name,
            path=str(path),
            error=str(exc),
        )
        return False
    logger.info(
        "Created profile directory",
        profile=slug,
        field=field_name,
        path=str(path),
    )
    return True


async def bootstrap_profiles(
    registry: ProfileRegistry,
    templates_dir: Path,
) -> None:
    """Materialise every profile's workspace, memory dir and persona.

    Idempotent: an existing directory is left as-is and an existing persona is
    never overwritten, so re-running an install loses no data.

    Args:
        registry: The loaded profile registry (parsed, not yet path-validated).
        templates_dir: Committed templates root (``data/templates``).
    """
    if not templates_dir.is_dir():
        logger.warning(
            "Templates directory not found, personas will not be seeded",
            templates_dir=str(templates_dir),
        )

    for profile in registry.profiles:
        _ensure_dir(profile.working_dir, slug=profile.slug, field_name="working_dir")
        _ensure_dir(profile.memory_dir, slug=profile.slug, field_name="memory_dir")

        if profile.persona.exists():
            logger.debug(
                "Profile persona already exists, skipping",
                profile=profile.slug,
                path=str(profile.persona),
            )
            continue

        template = _persona_template_for(profile, templates_dir)
        if template is None:
            # Left for validate_registry_paths to report -- see module docstring.
            logger.warning(
                "No persona template for profile; expected one of"
                " persona_template:, <templates>/local/<slug>/persona.md,"
                " <templates>/profiles/<slug>/persona.md"
                " or <templates>/personas/<slug>.md",
                profile=profile.slug,
                persona=str(profile.persona),
                looked_in=[
                    str(p) for p in _persona_candidates(profile.slug, templates_dir)
                ],
            )
            continue

        try:
            profile.persona.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(template, profile.persona)
        except OSError as exc:
            logger.warning(
                "Could not seed profile persona from template",
                profile=profile.slug,
                path=str(profile.persona),
                template=str(template),
                error=str(exc),
            )
            continue
        logger.info(
            "Created profile persona from template",
            profile=profile.slug,
            path=str(profile.persona),
            template=str(template),
        )
