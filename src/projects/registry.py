"""YAML-backed project registry for thread mode."""

from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

import yaml


@dataclass(frozen=True)
class ProjectDefinition:
    """Project entry from YAML configuration."""

    slug: str
    name: str
    relative_path: Path
    absolute_path: Path
    enabled: bool = True


class ProjectRegistry:
    """In-memory validated project registry."""

    def __init__(self, projects: List[ProjectDefinition]) -> None:
        self._projects = projects
        self._by_slug: Dict[str, ProjectDefinition] = {p.slug: p for p in projects}

    @property
    def projects(self) -> List[ProjectDefinition]:
        """Return all projects."""
        return list(self._projects)

    def list_enabled(self) -> List[ProjectDefinition]:
        """Return enabled projects only."""
        return [p for p in self._projects if p.enabled]

    def get_by_slug(self, slug: str) -> Optional[ProjectDefinition]:
        """Get project by slug."""
        return self._by_slug.get(slug)


def load_project_registry(
    config_path: Path, approved_directory: Optional[Path] = None
) -> ProjectRegistry:
    """Load and validate project definitions from YAML.

    If approved_directory is set, relative paths are resolved against it and
    boundary validation is enforced. If None, paths must be absolute.
    """
    if not config_path.exists():
        raise ValueError(f"Projects config file does not exist: {config_path}")

    with open(config_path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}

    if not isinstance(data, dict):
        raise ValueError("Projects config must be a YAML object")

    raw_projects = data.get("projects")
    if not isinstance(raw_projects, list) or not raw_projects:
        raise ValueError("Projects config must contain a non-empty 'projects' list")

    resolved_approved = approved_directory.resolve() if approved_directory else None
    seen_slugs: set[str] = set()
    seen_names: set[str] = set()
    seen_paths: set[Path] = set()
    projects: List[ProjectDefinition] = []

    for idx, raw in enumerate(raw_projects):
        if not isinstance(raw, dict):
            raise ValueError(f"Project entry at index {idx} must be an object")

        slug = str(raw.get("slug", "")).strip()
        name = str(raw.get("name", "")).strip()
        path_str = str(raw.get("path", "")).strip()
        enabled = bool(raw.get("enabled", True))

        if not slug:
            raise ValueError(f"Project entry at index {idx} is missing 'slug'")
        if not name:
            raise ValueError(f"Project '{slug}' is missing 'name'")
        if not path_str:
            raise ValueError(f"Project '{slug}' is missing 'path'")

        raw_path = Path(path_str)

        if raw_path.is_absolute():
            absolute_path = raw_path.resolve()
            relative_path = raw_path
        else:
            if resolved_approved is None:
                raise ValueError(
                    f"Project '{slug}' uses relative path '{path_str}' but no "
                    f"APPROVED_DIRECTORY is set. Use absolute paths in "
                    f"denylist mode."
                )
            absolute_path = (resolved_approved / raw_path).resolve()
            relative_path = raw_path

            # Boundary check only in allowlist mode
            try:
                absolute_path.relative_to(resolved_approved)
            except ValueError as e:
                raise ValueError(
                    f"Project '{slug}' path outside approved " f"directory: {path_str}"
                ) from e

        if not absolute_path.exists() or not absolute_path.is_dir():
            raise ValueError(
                f"Project '{slug}' path does not exist or "
                f"is not a directory: {absolute_path}"
            )

        if slug in seen_slugs:
            raise ValueError(f"Duplicate project slug: {slug}")
        if name in seen_names:
            raise ValueError(f"Duplicate project name: {name}")
        if absolute_path in seen_paths:
            raise ValueError(f"Duplicate project path: {absolute_path}")

        seen_slugs.add(slug)
        seen_names.add(name)
        seen_paths.add(absolute_path)

        projects.append(
            ProjectDefinition(
                slug=slug,
                name=name,
                relative_path=relative_path,
                absolute_path=absolute_path,
                enabled=enabled,
            )
        )

    return ProjectRegistry(projects)
