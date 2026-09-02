"""Tests for project registry without approved_directory constraint."""

from pathlib import Path

import pytest
import yaml

from src.projects.registry import load_project_registry


class TestRegistryNoBoundary:
    """Project registry works without approved_directory."""

    def test_loads_projects_with_absolute_paths(self, tmp_path: Path) -> None:
        """Projects can use absolute paths when approved_directory is None."""
        project_dir = tmp_path / "my-project"
        project_dir.mkdir()

        config = {
            "projects": [
                {
                    "slug": "my-project",
                    "name": "My Project",
                    "path": str(project_dir),
                    "enabled": True,
                }
            ]
        }
        config_path = tmp_path / "projects.yaml"
        config_path.write_text(yaml.dump(config))

        registry = load_project_registry(config_path, approved_directory=None)
        projects = registry.list_enabled()
        assert len(projects) == 1
        assert projects[0].absolute_path == project_dir

    def test_loads_projects_with_relative_paths_and_approved_dir(
        self, tmp_path: Path
    ) -> None:
        """Backward compat: relative paths resolved against approved_directory."""
        project_dir = tmp_path / "sub"
        project_dir.mkdir()

        config = {
            "projects": [
                {
                    "slug": "sub",
                    "name": "Sub Project",
                    "path": "sub",
                    "enabled": True,
                }
            ]
        }
        config_path = tmp_path / "projects.yaml"
        config_path.write_text(yaml.dump(config))

        registry = load_project_registry(config_path, approved_directory=tmp_path)
        assert registry.list_enabled()[0].absolute_path == project_dir

    def test_rejects_relative_path_without_approved_dir(self, tmp_path: Path) -> None:
        """Relative paths need approved_directory to resolve against."""
        config = {
            "projects": [
                {
                    "slug": "proj",
                    "name": "Project",
                    "path": "relative/path",
                    "enabled": True,
                }
            ]
        }
        config_path = tmp_path / "projects.yaml"
        config_path.write_text(yaml.dump(config))

        with pytest.raises(ValueError, match="absolute"):
            load_project_registry(config_path, approved_directory=None)
