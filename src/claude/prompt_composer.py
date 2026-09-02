"""Compose a structured system prompt from persona file + DB-backed facts."""

from pathlib import Path
from typing import Dict, Optional

import structlog

logger = structlog.get_logger()

# Capabilities shared by every profile (agent MCP tools: notify/schedule/tasks).
# Missing file = section silently skipped, so deployments without the
# scheduler stack lose nothing.
SHARED_CAPABILITIES_PATH = (
    Path(__file__).resolve().parent.parent.parent / "data" / "shared-capabilities.md"
)


class PromptComposer:
    """Assemble a layered system prompt from persona identity + passed-in facts.

    Reads the persona file fresh on every compose() call -- no caching.
    Memory facts (pinned/general) are selected by MemoryRetriever and passed in;
    this class is a pure formatter and does not read memory files.
    """

    def __init__(
        self,
        memory_path: Path,
        agent_md_path: Path,
    ) -> None:
        self._memory_path = memory_path
        self._agent_md_path = agent_md_path

    def compose(
        self,
        working_directory: Path,
        pinned_facts: Optional[list[str]] = None,
        general_facts: Optional[list[str]] = None,
        family_facts: Optional[list[str]] = None,
        result_pointers: Optional[list[str]] = None,
        conflict_pairs: Optional[list[tuple[str, str]]] = None,
        full_access: bool = False,
        window_started: Optional[str] = None,
    ) -> str:
        """Build the layered system prompt.

        Order: directory constraint -> window start (amnesia boundary)
               -> agent identity -> shared capabilities
               -> constraints (pinned) -> user facts (general)
               -> memory pointers -> family facts.

        ``window_started`` (dv-1106): the date this conversation window began.
        Injected so the agent knows where its conversational memory ends —
        without it, a model in a freshly rolled window still "feels" like it
        remembers prior days and confabulates cross-day claims instead of
        reading its files.
        Pinned facts are authoritative rules; general facts are soft.

        ``result_pointers`` are dated one-line stand-ins for
        ``conversation_result`` memory rows (research, diagnoses, decisions).
        Their full text is deliberately withheld: the agent must re-ground
        via recall()/Read instead of replaying a stale summary (dv-8254).

        ``conflict_pairs`` are safety conflicts (dv-8254 ``conflict_group``):
        both statements are still active and are rendered adjacently under an
        explicit flag inside ``<user_facts>`` so the agent clarifies with the
        user instead of silently picking one.

        Project instructions (CLAUDE.md) are NOT a layer here: the CLI loads
        them itself from the "project" setting source. Adding them back would
        put the file in context twice.

        Family facts (only present for users in a family) are shared by other
        family members and injected as a separate, clearly-labeled block so
        the agent can attribute them instead of assuming the current user
        said them.

        ``full_access`` (unsandboxed profiles like devops): drop the cwd
        confinement — the agent operates across the server. It stays bounded by
        the security denylist, not the prompt.
        """
        sections: list[str] = []

        if full_access:
            sections.append(
                f"Your working directory is {working_directory}. You have "
                "full-access to the server and may read and write outside it "
                "when the task requires (subject to the security denylist). "
                "Prefer the working directory for scratch/project files."
            )
        else:
            sections.append(
                f"All file operations must stay within {working_directory}. "
                "Use relative paths."
            )

        if window_started:
            sections.append(
                f"This conversation window started {window_started}. You have "
                "no conversational memory of anything before that date — "
                "earlier exchanges are gone from your context, even if it "
                "feels otherwise. The dated facts below and the files in your "
                "working directory are the only record of that time; read "
                "them before stating anything about days this window did not "
                "see."
            )

        agent_content = self._read_file(self._agent_md_path)
        if agent_content:
            sections.append(f"<agent_identity>\n{agent_content}\n</agent_identity>")

        shared = self._read_file(SHARED_CAPABILITIES_PATH)
        if shared:
            sections.append(f"<shared_capabilities>\n{shared}\n</shared_capabilities>")

        if pinned_facts:
            body = "\n".join(f"- {f}" for f in pinned_facts)
            sections.append(
                "<constraints>\n"
                "Standing constraints — a snapshot taken when this window "
                "started. Each line is dated [DD.MM.YY] = when it became "
                "true. Things change: if the user tells you a constraint no "
                "longer holds, the conversation wins — treat that line as "
                "superseded from that moment (the snapshot refreshes at the "
                "next window). Only the user's explicit statement relaxes a "
                "health or safety constraint; never retire one by your own "
                "inference.\n"
                f"{body}\n</constraints>"
            )

        if general_facts or conflict_pairs:
            parts: list[str] = []
            if general_facts:
                parts.append("\n".join(f"- {f}" for f in general_facts))
            for first, second in conflict_pairs or []:
                parts.append(
                    "⚠ CONFLICT (safety) — both statements are active and "
                    "unresolved; clarify with the user before acting on "
                    "either:\n"
                    f"- {first}\n- {second}"
                )
            body = "\n".join(parts)
            sections.append(
                "<user_facts>\n"
                "Facts about the user — a snapshot frozen when this window "
                "started; anything said in the conversation since is MORE "
                "current than anything here. Dated [DD.MM.YY] = when each "
                "became true. If a line conflicts with what the user says "
                "now, the user wins.\n"
                f"{body}\n</user_facts>"
            )

        if result_pointers:
            body = "\n".join(f"- {p}" for p in result_pointers)
            sections.append(
                "<memory_pointers>\n"
                "Work you produced earlier (research, diagnoses, decisions). "
                "Only these one-line pointers are injected — before relying "
                "on any of them, retrieve the full text with recall() or by "
                "reading the referenced file.\n"
                f"{body}\n</memory_pointers>"
            )

        if family_facts:
            body = "\n".join(f"- {f}" for f in family_facts)
            sections.append(
                "<family_facts>\n"
                "Shared by family members — attribute when relevant; do not "
                "assume the current user said these. Dated [DD.MM.YY] = when "
                "each became true.\n"
                f"{body}\n</family_facts>"
            )

        return "\n\n".join(sections)

    def get_section_sizes(self) -> Dict[str, int]:
        """Character counts for the file-backed sections (persona only now).

        DB-backed fact sizes are reported by /memory directly from the repo.
        """
        sizes: Dict[str, int] = {}
        agent_content = self._read_file(self._agent_md_path)
        if agent_content:
            sizes["agent.md"] = len(agent_content)
        return sizes

    def _read_file(self, path: Path) -> Optional[str]:
        """Read file content, returning None if missing or empty."""
        try:
            if path.exists():
                content = path.read_text(encoding="utf-8").strip()
                return content if content else None
        except OSError:
            logger.warning("Failed to read memory file", path=str(path))
        return None
