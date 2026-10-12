"""Memory bootstrap -- copies templates to runtime paths on first run."""

import shutil
from pathlib import Path
from typing import Optional

import structlog

logger = structlog.get_logger()


async def bootstrap_memory(
    memory_path: Path,
    soul_md_path: Path,
    templates_dir: Path,
) -> None:
    """Bootstrap agent.md and memory files from templates.

    Copies template files to their runtime locations if they do not already
    exist. Existing files are never overwritten.

    Args:
        memory_path: Runtime directory for memory files (e.g. data/memory).
        soul_md_path: Anchor path; agent.md is created in its parent directory.
            soul.md itself is no longer seeded (dv-c6be: it was never read).
        templates_dir: Directory containing committed templates.
    """
    if not templates_dir.is_dir():
        logger.warning(
            "Templates directory not found, skipping bootstrap",
            templates_dir=str(templates_dir),
        )
        return

    # Bootstrap agent.md
    agent_template = templates_dir / "agent.md"
    agent_target = soul_md_path.parent / "agent.md"
    if not agent_template.is_file():
        logger.warning(
            "Agent template not found, skipping agent.md bootstrap",
            expected=str(agent_template),
        )
    elif agent_target.exists():
        logger.info(
            "agent.md already exists, skipping",
            path=str(agent_target),
        )
    else:
        agent_target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(agent_template, agent_target)
        logger.info(
            "Created agent.md from template",
            path=str(agent_target),
        )

    # Bootstrap memory directory
    memory_templates = templates_dir / "memory"
    if not memory_templates.is_dir():
        logger.warning(
            "Memory templates directory not found, skipping memory bootstrap",
            expected=str(memory_templates),
        )
        return

    for template_file in memory_templates.rglob("*"):
        if not template_file.is_file():
            continue

        relative = template_file.relative_to(memory_templates)
        target = memory_path / relative

        if target.exists():
            logger.info(
                "Memory file already exists, skipping",
                path=str(target),
            )
            continue

        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(template_file, target)
        logger.info(
            "Created memory file from template",
            path=str(target),
        )


async def bootstrap_project_stubs(
    memory_path: Path,
    projects_config_path: Optional[Path] = None,
) -> None:
    """Generate per-project memory stubs from existing CLAUDE.md files.

    For each project in the project registry, checks if a memory stub
    exists at memory_path/projects/<slug>.md. If not, reads the project's
    CLAUDE.md (if it exists) and creates a stub with the project overview.

    Skips projects whose directories don't exist or have no CLAUDE.md.
    Never overwrites existing stubs.
    """
    if not projects_config_path or not projects_config_path.exists():
        logger.info("No projects config, skipping project stub generation")
        return

    projects_dir = memory_path / "projects"
    projects_dir.mkdir(parents=True, exist_ok=True)

    try:
        import yaml

        with open(projects_config_path, "r", encoding="utf-8") as f:
            config = yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning("Failed to load projects config", error=str(e))
        return

    projects = config.get("projects", [])
    for project in projects:
        slug = project.get("slug") or project.get("name", "").lower().replace(" ", "-")
        directory = project.get("directory")
        if not slug or not directory:
            continue

        stub_path = projects_dir / f"{slug}.md"
        if stub_path.exists():
            logger.info("Project stub already exists, skipping", slug=slug)
            continue

        # Try to read project's CLAUDE.md
        project_dir = Path(directory)
        claude_md = project_dir / "CLAUDE.md"
        overview = ""
        if claude_md.exists():
            try:
                content = claude_md.read_text(encoding="utf-8")
                # Extract first section (up to 2000 chars) as overview
                lines = content.split("\n")
                excerpt_lines = []
                char_count = 0
                for line in lines:
                    if char_count + len(line) > 2000:
                        break
                    excerpt_lines.append(line)
                    char_count += len(line)
                overview = "\n".join(excerpt_lines)
            except OSError:
                logger.warning("Failed to read CLAUDE.md", path=str(claude_md))

        stub_content = f"# {slug}\n\n"
        stub_content += f"**Directory:** {directory}\n\n"
        if overview:
            stub_content += f"## From CLAUDE.md\n\n{overview}\n\n"
        stub_content += (
            "## Notes\n\n<!-- Project-specific notes, decisions, context -->\n"
        )

        stub_path.write_text(stub_content, encoding="utf-8")
        logger.info("Created project memory stub", slug=slug, path=str(stub_path))
