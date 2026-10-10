"""Info/status command handlers moved verbatim from src/bot/orchestrator.py."""

import asyncio
from pathlib import Path
from typing import Any, Dict, List, Optional

import structlog
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from ....agent_backends.codex_usage import CodexUsageError, format_codex_limits
from ....agent_backends.protocol import BackendSession
from ....profiles.registry import family_agent_id_for_agent, guest_agent_id
from ....projects import PrivateTopicsUnavailableError
from ...utils.html_format import escape_html
from ..agents_menu import build_agents_picker
from ..base import OrchestratorBase, policy_for
from ..formatting import format_diag, format_sessions_summary, format_usage_table
from ..windowing import apply_window_tokens, reset_window, window_conversation_text

logger = structlog.get_logger()


class InfoCommandsMixin(OrchestratorBase):
    """Session/status/info commands and the model picker flow."""

    # --- Agentic handlers ---

    async def agentic_start(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Brief welcome, no buttons."""
        user = update.effective_user
        sync_line = ""
        if (
            self.settings.enable_project_threads
            and self.settings.project_threads_mode == "private"
        ):
            if (
                not update.effective_chat
                or getattr(update.effective_chat, "type", "") != "private"
            ):
                await update.message.reply_text(
                    "🚫 <b>Private Topics Mode</b>\n\n"
                    "Use this bot in a private chat and run <code>/start</code> there.",
                    parse_mode="HTML",
                )
                return
            manager = context.bot_data.get("project_threads_manager")
            if manager:
                try:
                    result = await manager.sync_topics(
                        context.bot,
                        chat_id=update.effective_chat.id,
                    )
                    sync_line = (
                        "\n\n🧵 Topics synced"
                        f" (created {result.created}, reused {result.reused})."
                    )
                except PrivateTopicsUnavailableError:
                    await update.message.reply_text(
                        manager.private_topics_unavailable_message(),
                        parse_mode="HTML",
                    )
                    return
                except Exception:
                    sync_line = "\n\n🧵 Topic sync failed. Run /sync_threads to retry."
        # Routed like text turns (dv-c07a): the greeting must name the
        # workspace the next message will actually run in. Reading
        # current_directory showed the home-dir fallback on a cold process.
        current_dir = self._routed_working_dir(context, user.id if user else None)
        dir_display = f"<code>{current_dir}/</code>"

        safe_name = escape_html(user.first_name)
        await update.message.reply_text(
            f"Hi {safe_name}! I'm your AI coding assistant.\n"
            f"Just tell me what you need — I can read, write, and run code.\n\n"
            f"Working in: {dir_display}\n"
            f"Commands: /new (reset) · /status"
            f"{sync_line}",
            parse_mode="HTML",
        )

    async def agentic_new(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Reset session, one-line confirmation."""
        context.user_data["claude_session_id"] = None
        context.user_data["codex_session_id"] = None
        context.user_data["session_started"] = True
        context.user_data["force_new_session"] = True

        # BPC: /new is a clean start — no continuity seed — but data care
        # still applies (dv-d9e2 path 3): final extraction over the window's
        # un-extracted tail, then roll it (fresh bundle next turn) and arm a
        # durable seedless force-new so a restart before the next message
        # cannot cancel the reset, and any previously armed *seeded*
        # transition cannot leak old conversation into the clean session.
        window_repo = context.bot_data.get("window_repo")
        if window_repo:
            user_id = update.effective_user.id
            current_dir = self._routed_working_dir(context, user_id)
            backend = self._selected_backend(context)
            state = await window_repo.get_or_create(user_id, current_dir)
            await reset_window(
                window_repo,
                context.bot_data.get("evaluator_service"),
                context.user_data,
                user_id=user_id,
                working_dir=current_dir,
                backend=backend,
                conversation_text=await window_conversation_text(
                    context.bot_data.get("storage"), user_id, current_dir, state
                ),
                agent_id=self._agent_id_for(context, current_dir, user_id),
                family_id=self._family_id_for(context, user_id, current_dir),
            )
        context.user_data.pop("pending_seed_window", None)

        await update.message.reply_text("Session reset. What's next?")

    async def agentic_status(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Compact one-line status, no buttons."""
        # Routed like text turns — /status must describe the workspace the
        # user is actually talking in, not the legacy current_directory.
        current_dir = self._routed_working_dir(context, update.effective_user.id)
        dir_display = str(current_dir)

        backend = self._selected_backend(context)
        session_id = context.user_data.get(self._session_key(backend))
        session_status = "active" if session_id else "none"
        model = self._selected_model(context)

        # Cost info
        cost_str = ""
        rate_limiter = context.bot_data.get("rate_limiter")
        if rate_limiter and update.effective_user:
            status_user_id = update.effective_user.id
            try:
                user_status = rate_limiter.get_user_status(
                    status_user_id,
                    role=self._resolve_role(status_user_id, context),
                )
                cost_usage = user_status.get("cost_usage", {})
                current_cost = cost_usage.get("current", 0.0)
                cost_str = f" · Cost: ${current_cost:.2f}"
                # Unpriced-backend tokens (dv-0722): a Codex day is not $0.
                token_usage = user_status.get("token_usage") or {}
                used_tokens = int(token_usage.get("current", 0) or 0)
                if used_tokens:
                    cost_str += f" + {used_tokens:,} tok"
            except Exception:
                pass

        # Conversation window fill (bot's own 150k rotation/summarization
        # threshold — see storage/window_repository.py), not the raw model
        # context limit.
        window_str = ""
        window_repo = context.bot_data.get("window_repo")
        if window_repo and update.effective_user:
            try:
                w_state = await window_repo.get_or_create(
                    update.effective_user.id, current_dir
                )
                window_str = (
                    f" · Win: {w_state.last_context_tokens // 1000}k/"
                    f"{window_repo.refresh_threshold_tokens // 1000}k"
                )
            except Exception:
                pass

        await update.message.reply_text(
            f"📂 {dir_display} · {backend}/{model} · Session: {session_status}"
            f"{cost_str}{window_str}"
        )

    async def agentic_usage(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show spend and caps: today / 30 days (SQLite) + budget window state.

        Until now the caps surfaced exactly once — as a hard refusal when the
        rolling-24h budget ran out. This makes them visible beforehand.
        """
        from datetime import UTC, datetime

        user_id = update.effective_user.id
        role = self._resolve_role(user_id, context)
        lines = [f"💸 Usage ({role})"]

        storage = context.bot_data.get("storage")
        if storage:
            try:
                rows = await storage.costs.get_user_daily_costs(user_id, days=30)
                today = datetime.now(UTC).strftime("%Y-%m-%d")
                today_row = next((r for r in rows if r.date == today), None)
                if today_row:
                    lines.append(
                        f"Today: ${today_row.daily_cost:.2f}"
                        f" ({today_row.request_count} requests)"
                    )
                else:
                    lines.append("Today: $0.00")
                total = sum(r.daily_cost for r in rows)
                requests = sum(r.request_count for r in rows)
                lines.append(f"Last 30 days: ${total:.2f} ({requests} requests)")
            except Exception as e:
                logger.warning("usage: cost history unavailable", error=str(e))

        # Owners are exempt from both the daily rolling cap and the
        # per-request SDK cap (see rate_limiter._check_cost_limit and
        # sdk_integration.execute_command) — show "no cap" instead of a
        # number that would misleadingly imply enforcement.
        rate_limiter = context.bot_data.get("rate_limiter")
        if rate_limiter:
            try:
                status = rate_limiter.get_user_status(user_id, role=role)
                cost = status["cost_usage"]
                # Ask the limiter what it will actually enforce rather than
                # re-deriving it from the role: a per-user override outranks
                # the owner exemption, so "owner" alone doesn't mean uncapped.
                cap = rate_limiter.effective_daily_cap(user_id, role)
                # The budget is a rolling 24h window held in memory, so it can
                # show less than today's SQLite total after a bot restart.
                if cap is None:
                    lines.append(
                        f"Budget window (24h): ${cost['current']:.2f} (no cap)"
                    )
                else:
                    lines.append(
                        f"Budget window (24h): ${cost['current']:.2f}"
                        f" / ${cap:.2f}"
                        f" — ${max(0.0, cap - cost['current']):.2f} left"
                    )
                # Unpriced backends (Codex on a subscription, dv-0722) are
                # budgeted in tokens; without this line a day on Codex reads
                # as $0.00, i.e. free. Shown whenever any were spent or a cap
                # is enforced, so a capped guest sees their allowance early.
                tokens = status.get("token_usage") or {}
                used_tokens = int(tokens.get("current", 0) or 0)
                token_cap = rate_limiter.effective_daily_token_cap(user_id, role)
                if token_cap is None:
                    if used_tokens:
                        lines.append(
                            f"Unmetered tokens (24h): {used_tokens:,} (no cap)"
                        )
                else:
                    lines.append(
                        f"Unmetered tokens (24h): {used_tokens:,} / {token_cap:,}"
                        f" — {max(0, token_cap - used_tokens):,} left"
                    )
            except Exception as e:
                logger.warning("usage: rate limiter status unavailable", error=str(e))

        # Per-request cap for the profile the user is currently in — a profile
        # may set max_cost_per_request, which applies to owners too.
        per_request: Optional[float] = (
            None if role == "owner" else self.settings.claude_max_cost_per_request
        )
        registry = context.bot_data.get("profile_registry")
        # Routed, not current_directory (dv-c07a): unset on a cold process,
        # which silently reported "no per-request cap" for a capped profile.
        current_dir = self._routed_working_dir(context, user_id) if registry else None
        if registry and current_dir:
            profile = registry.get_by_working_dir(Path(str(current_dir)))
            if profile is not None and profile.max_cost_per_request is not None:
                per_request = profile.max_cost_per_request or None
        if per_request is None:
            lines.append("Per-request cap: none")
        else:
            lines.append(f"Per-request cap: ${per_request:.2f}")

        if storage:
            await storage.log_bot_event(user_id, "command_usage", {})
        await update.message.reply_text("\n".join(lines))

    async def agentic_limits(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show provider-side Codex limits without invoking an LLM."""
        backends = context.bot_data.get("backends") or {}
        backend = backends.get("openai_codex") or context.bot_data.get(
            "codex_integration"
        )
        reader = getattr(backend, "read_account_limits", None)
        if reader is None:
            await update.message.reply_text("Codex backend не подключён.")
            return
        try:
            snapshot = await reader()
        except CodexUsageError as exc:
            await update.message.reply_text(f"Не удалось получить лимиты Codex: {exc}")
            return
        except Exception:
            logger.warning("limits: Codex account query failed", exc_info=True)
            await update.message.reply_text("Не удалось получить лимиты Codex.")
            return
        await update.message.reply_text(format_codex_limits(snapshot))

    def _stop_notice(self, error_type: Optional[str]) -> Optional[str]:
        """User-facing note for a run that stopped before producing a result.

        Without this the partial narration reads as a finished answer that
        trails off mid-sentence — the failure mode that hid the per-request
        budget cap for days (see ClaudeSDKManager._ABORT_SUBTYPES).
        """
        if error_type == "max_turns":
            return (
                "⚠️ Stopped at the agent turn limit before finishing — the "
                "text above is progress notes, not a final result. Send "
                '"continue" to resume from where it stopped.'
            )
        if error_type == "max_budget":
            return (
                "⚠️ Stopped at the per-request cost cap before finishing — the "
                "text above is progress notes, not a final result. Raise "
                "CLAUDE_MAX_COST_PER_REQUEST (or the profile's "
                'max_cost_per_request), then send "continue" to resume.'
            )
        if error_type == "agent_error":
            return (
                "⚠️ The run ended with an error before finishing — the text "
                'above is progress notes, not a final result. Send "continue" '
                "to retry from where it stopped."
            )
        return None

    async def agentic_verbose(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Set output verbosity: /verbose [0|1|2]."""
        args = update.message.text.split()[1:] if update.message.text else []
        if not args:
            current = self._get_verbose_level(context)
            labels = {0: "quiet", 1: "normal", 2: "detailed"}
            await update.message.reply_text(
                f"Verbosity: <b>{current}</b> ({labels.get(current, '?')})\n\n"
                "Usage: <code>/verbose 0|1|2</code>\n"
                "  0 = quiet (final response only)\n"
                "  1 = normal (tools + reasoning)\n"
                "  2 = detailed (tools with inputs + reasoning)",
                parse_mode="HTML",
            )
            return

        try:
            level = int(args[0])
            if level not in (0, 1, 2):
                raise ValueError
        except ValueError:
            await update.message.reply_text(
                "Please use: /verbose 0, /verbose 1, or /verbose 2"
            )
            return

        context.user_data["verbose_level"] = level
        labels = {0: "quiet", 1: "normal", 2: "detailed"}
        await update.message.reply_text(
            f"Verbosity set to <b>{level}</b> ({labels[level]})",
            parse_mode="HTML",
        )

    async def _codex_model_rows(self) -> List[List[InlineKeyboardButton]]:
        """Keyboard rows for the Codex backend, built from the live catalog.

        `codex debug models` is the source of truth (dv-0c01): the entitled
        set is a property of the subscription and the vendor renames slugs,
        so the previous two hardcoded buttons drifted into offering models the
        CLI would reject. Cached per process with a TTL and invalidated on a
        successful `/auth` Codex login; a failed read falls back to the two
        historical constants rather than rendering an empty section.
        """
        from ....agent_backends.codex_catalog import CODEX_CATALOG

        # Entitlements can change immediately after a ChatGPT plan upgrade.
        # `/model` is explicitly the point where the user asks to see them,
        # so refresh rather than leaving a recently upgraded account on the
        # hour-long catalog cache.
        models = await CODEX_CATALOG.get(force=True)
        rows: List[List[InlineKeyboardButton]] = []
        for i in range(0, len(models), 2):
            rows.append(
                [
                    InlineKeyboardButton(
                        f"Codex {m.display_name}",
                        callback_data=f"model:codex:{m.slug}",
                    )
                    for m in models[i : i + 2]
                ]
            )
        return rows

    async def agentic_model(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Choose backend/model and start OpenAI Codex auth."""
        backend = self._selected_backend(context)
        model = self._selected_model(context)
        codex_status = (
            "authenticated"
            if (Path.home() / ".codex" / "auth.json").exists()
            else "not authenticated"
        )
        rows = [
            [
                InlineKeyboardButton(
                    "Claude Sonnet", callback_data="model:claude:sonnet"
                ),
                InlineKeyboardButton("Claude Opus", callback_data="model:claude:opus"),
            ]
        ]
        rows += await self._codex_model_rows()
        rows.append(
            [
                InlineKeyboardButton(
                    "Login OpenAI Codex", callback_data="model:codex_login"
                ),
                InlineKeyboardButton(
                    "Logout Codex", callback_data="model:codex_logout"
                ),
            ]
        )
        keyboard = InlineKeyboardMarkup(rows)
        await update.message.reply_text(
            "<b>Model backend</b>\n\n"
            f"Current: <code>{escape_html(backend)}</code> "
            f"/ <code>{escape_html(model)}</code>\n"
            f"OpenAI Codex: {escape_html(codex_status)}",
            parse_mode="HTML",
            reply_markup=keyboard,
        )

    async def agentic_memory(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show current memory state: /memory."""
        composer = context.bot_data.get("prompt_composer")
        if not composer:
            await update.message.reply_text("Memory system not available.")
            return

        sizes = composer.get_section_sizes()
        total_chars = sum(sizes.values())
        total_tokens_est = total_chars // 4  # rough estimate

        # dv-5e2a / owner D18: this is the live (next-roll) view. The running
        # window keeps its frozen bundle until the next rotation or /new.
        lines = [
            "Live view — takes effect at the next window roll or /new; the "
            "current window uses its frozen bundle.",
            f"Context: ~{total_tokens_est / 1000:.1f}k tokens",
        ]
        for name, char_count in sizes.items():
            token_est = char_count // 4
            lines.append(f"  - {name}: ~{token_est / 1000:.1f}k")

        # DB-backed agent memory (pinned + general facts for the active agent).
        # Guests see their private per-guest scope, never the owner-tier facts.
        registry = context.bot_data.get("profile_registry")
        user = update.effective_user
        user_id = user.id if user else None
        profile = None
        if registry:
            role = self._resolve_role(user_id, context)
            slug = context.user_data.get("active_profile")
            profile = registry.get_by_slug(slug) if slug else None
            if role != "owner":
                if profile is None or not profile.allows_role(role):
                    profile = registry.get_default_for_role(role)
            elif profile is None:
                profile = registry.get_default()
            if profile is None:
                agent_id = "devops"
            elif role != "owner" and user_id is not None:
                agent_id = guest_agent_id(profile.slug, user_id)
            elif profile.personal_memory and user_id is not None:
                agent_id = guest_agent_id(profile.slug, user_id)
            else:
                agent_id = profile.slug
        else:
            agent_id = "devops"
        storage = context.bot_data.get("storage")
        repo = getattr(storage, "memory_repository", None) if storage else None
        if repo is not None:
            pinned = await repo.get_facts(agent_id, tier="pinned")
            general = await repo.get_facts(agent_id, tier="general")
            lines.append("")
            lines.append(f"🧠 Memory for {agent_id} (private)")
            lines.append(f"📌 Pinned: {len(pinned)}  📝 General: {len(general)}")
            for f in pinned[:10]:
                lines.append(f"  • [{f.label or 'pinned'}] {f.content}")
            # Family scope, clearly separated: shared facts other family
            # members contributed. Only shown for users in a family.
            family_id = self._family_id_for(
                context, user_id, profile.working_dir if profile else None
            )
            if family_id is not None:
                fam_scope = family_agent_id_for_agent(agent_id, family_id)
                fam_pinned = await repo.get_facts(fam_scope, tier="pinned")
                fam_general = await repo.get_facts(fam_scope, tier="general")
                lines.append("")
                lines.append(f"👪 Family memory for {fam_scope} (shared)")
                lines.append(
                    f"📌 Pinned: {len(fam_pinned)}  📝 General: {len(fam_general)}"
                )
                for f in fam_pinned[:10]:
                    lines.append(f"  • [{f.label or 'pinned'}] {f.content}")

        # Session token tracking. Read from the conversation window, which is
        # the number the thresholds actually gate on — ClaudeSession's own
        # cumulative counter sums uncached input only and reads ~2.5x low on a
        # cache-hitting deployment (dv-9f31). The evaluator lookup below keys
        # on the selected backend's session id straight from user_data
        # (dv-5368): no reach into a backend's session manager, so a
        # Codex-only deployment answers the same way.
        session_id = context.user_data.get(
            self._session_key(self._selected_backend(context))
        )
        window_repo = context.bot_data.get("window_repo")
        # The window the conversation actually accounts against (dv-c07a):
        # profile.working_dir is the profile ROOT, but personal_memory profiles
        # run in <root>/users/<id>, so reading the root here reported the wrong
        # window and get_or_create() minted a phantom row for it.
        window_dir = (
            self._routed_working_dir(context, user_id)
            if registry
            else context.user_data.get("current_directory")
        )
        w_state = None
        if window_repo and user_id is not None and window_dir:
            try:
                w_state = await window_repo.get_or_create(user_id, window_dir)
            except Exception:
                w_state = None

        if w_state is not None:
            lines.append("")
            lines.append(
                f"Conversation window {w_state.window_id}: "
                f"{w_state.last_context_tokens / 1000:.1f}k"
                f" / {window_repo.refresh_threshold_tokens // 1000}k"
            )
            if window_repo.eval_interval_tokens > 0:
                next_threshold = (
                    w_state.last_eval_checkpoint + 1
                ) * window_repo.eval_interval_tokens
            else:
                next_threshold = window_repo.refresh_threshold_tokens
            lines.append(f"Next evaluator: ~{next_threshold // 1000}k tokens")

        # Evaluator status via token_tracker
        token_tracker = context.bot_data.get("token_tracker")
        if token_tracker is not None and session_id:
            try:
                extraction = await token_tracker.get_last_extraction(session_id)
                if extraction and extraction.timestamp:
                    lines.append(f"Last evaluator: {extraction.timestamp}")
                else:
                    lines.append("Last evaluator: not yet")
            except Exception:
                lines.append("Last evaluator: error")
        else:
            lines.append("Last evaluator: not available")

        await update.message.reply_text("\n".join(lines))

    async def agentic_agents(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show an inline keyboard to pick the active profile."""
        registry = context.bot_data.get("profile_registry")
        if not registry:
            await update.message.reply_text("Profiles are not configured.")
            return
        active = context.user_data.get("active_profile") or registry.get_default().slug
        role = self._resolve_role(update.effective_user.id, context)
        text, markup = build_agents_picker(registry, active=active, role=role)
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=markup)

    async def agentic_tasks(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show the task board (open tasks)."""
        storage = context.bot_data.get("storage")
        if not storage:
            await update.message.reply_text("Storage is not available.")
            return
        tasks = await storage.tasks.list_tasks(status="open")
        await storage.log_bot_event(
            update.effective_user.id, "command_tasks", {"count": len(tasks)}
        )
        if not tasks:
            await update.message.reply_text("Task board is empty. 🎉")
            return
        lines = ["📋 Open tasks:"]
        for t in tasks:
            due = f" — due {t.due_at.strftime('%b %d %H:%M')}" if t.due_at else ""
            who = f" (by {t.created_by})" if t.created_by != "user" else ""
            assignee = f" → {t.assignee}" if t.assignee else ""
            status = f" [{t.status}]" if t.status != "todo" else ""
            lines.append(f"{t.id} {t.title}{status}{assignee}{due}{who}")
        await update.message.reply_text("\n".join(lines))

    async def agentic_stop(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Killswitch: interrupt in-flight runs, pause scheduler, refuse new runs."""
        claude_integration = context.bot_data.get("claude_integration")
        storage = context.bot_data.get("storage")
        user_id = update.effective_user.id
        sdk = getattr(claude_integration, "sdk_manager", None)
        if not sdk or not hasattr(sdk, "stop_all"):
            await update.message.reply_text("Killswitch unavailable.")
            return
        interrupted = await sdk.stop_all()
        if storage:
            await storage.log_bot_event(
                user_id, "command_stop", {"interrupted": interrupted}
            )
        await update.message.reply_text(
            f"🛑 <b>Stopped.</b> Interrupted {interrupted} run(s); scheduler paused.\n"
            "New runs are refused until /resume.",
            parse_mode="HTML",
        )

    async def agentic_resume(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Clear the killswitch: resume scheduler and allow new runs."""
        claude_integration = context.bot_data.get("claude_integration")
        storage = context.bot_data.get("storage")
        user_id = update.effective_user.id
        sdk = getattr(claude_integration, "sdk_manager", None)
        if not sdk or not hasattr(sdk, "resume_all"):
            await update.message.reply_text("Killswitch unavailable.")
            return
        sdk.resume_all()
        if storage:
            await storage.log_bot_event(user_id, "command_resume", {})
        await update.message.reply_text(
            "▶️ <b>Resumed.</b> Scheduler running; new runs allowed.",
            parse_mode="HTML",
        )

    async def agentic_schedules(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List active scheduled jobs (from the scheduled_jobs table)."""
        storage = context.bot_data.get("storage")
        if not storage:
            await update.message.reply_text("Storage is not available.")
            return
        if not self.settings.enable_scheduler:
            await update.message.reply_text(
                "Scheduler is disabled (ENABLE_SCHEDULER=false)."
            )
            return
        from ....storage.scheduler_repository import SchedulerRepository

        all_rows = await SchedulerRepository(storage.db_manager).list_active_jobs()
        # Same authorization rule as the agent tools (dv-8c19) — created it,
        # or it targets you, or (legacy unattributed rows) you're an owner.
        from ....scheduler.scheduler import job_visible_to

        user_id = update.effective_user.id
        role = self._resolve_role(user_id, context)
        chat_id = update.effective_chat.id if update.effective_chat else None
        rows = [r for r in all_rows if job_visible_to(r, user_id, role, chat_id)]
        await storage.log_bot_event(
            update.effective_user.id, "command_schedules", {"count": len(rows)}
        )
        if not rows:
            await update.message.reply_text("No scheduled jobs.")
            return
        registry = context.bot_data.get("profile_registry")
        lines = ["⏰ Scheduled jobs (times in UTC):"]
        for r in rows:
            expr = r["cron_expression"]
            when = f"once at {expr[4:]}" if expr.startswith("@at:") else f"cron {expr}"
            profile = ""
            if registry:
                p = registry.get_by_working_dir(Path(r["working_directory"]))
                profile = f" [{p.slug}]" if p else ""
            lines.append(f"{r['job_name']}{profile} — {when}\n  id: {r['job_id']}")
        await update.message.reply_text("\n".join(lines))

    async def agentic_sessions(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show each profile's last session activity."""
        registry = context.bot_data.get("profile_registry")
        if not registry:
            await update.message.reply_text("Profiles are not configured.")
            return
        user_id = update.effective_user.id
        last_by_dir: Dict[Path, Optional[Dict[str, Any]]] = {
            p.working_dir: None for p in registry.profiles
        }
        # Registry order decides who reports a directory (dv-5368): the first
        # backend with a session there wins, later ones only fill gaps. With
        # the default wiring that is Claude first, Codex as overlay — the
        # same precedence as the pre-registry code.
        for backend in self._backends(context).values():
            try:
                sessions = await backend.sessions_for_user(user_id)
            except Exception:
                logger.warning(
                    "sessions_backend_lookup_failed",
                    backend=getattr(backend, "backend_id", "?"),
                    exc_info=True,
                )
                continue
            newest_by_dir: Dict[Path, BackendSession] = {}
            for s in sessions:
                cur = newest_by_dir.get(s.working_dir)
                if cur is None or s.last_used > cur.last_used:
                    newest_by_dir[s.working_dir] = s
            for p in registry.profiles:
                newest = newest_by_dir.get(p.working_dir)
                if newest is not None and last_by_dir.get(p.working_dir) is None:
                    last_by_dir[p.working_dir] = {
                        "cumulative_tokens": newest.cumulative_tokens,
                        "updated_at": newest.last_used.isoformat(),
                        "backend": policy_for(newest.backend_id).label,
                    }

        # Overlay authoritative token counts from conversation_windows: session
        # records stay at 0 for Claude sessions; WindowRepository holds the
        # real running counter (same source /status reads).
        window_repo = context.bot_data.get("window_repo")
        if window_repo:
            try:
                windows = await window_repo.list_for_user(user_id)
            except Exception:
                logger.warning("sessions_window_overlay_failed", exc_info=True)
                windows = []
            apply_window_tokens(registry, last_by_dir, windows)

        active = context.user_data.get("active_profile") or registry.get_default().slug
        reply = format_sessions_summary(registry, last_by_dir, active)

        # 24h/30d token+cost usage per profile — "last session" above tells
        # you recency, not how much you've actually spent; this answers that.
        token_tracker = context.bot_data.get("token_tracker")
        if token_tracker:
            try:
                from datetime import UTC, datetime, timedelta

                now = datetime.now(UTC)
                usage_24h, usage_30d = await asyncio.gather(
                    token_tracker.get_usage_by_project(
                        user_id, since=now - timedelta(hours=24)
                    ),
                    token_tracker.get_usage_by_project(
                        user_id, since=now - timedelta(days=30)
                    ),
                )
                reply += "\n\n" + format_usage_table(registry, usage_24h, usage_30d)
            except Exception:
                logger.warning("sessions_usage_table_failed", exc_info=True)

        await update.message.reply_text(reply, parse_mode="HTML")

    async def agentic_diag(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show recent lifecycle diagnostics for a profile."""
        registry = context.bot_data.get("profile_registry")
        lifecycle = context.bot_data.get("lifecycle_logger")
        if not registry or not lifecycle:
            await update.message.reply_text("Observability is not configured.")
            return
        args = context.args or []
        active = context.user_data.get("active_profile") or registry.get_default().slug
        slug = args[0] if args else active
        events = await lifecycle.recent(profile=slug, limit=50)
        await update.message.reply_text(format_diag(slug, events), parse_mode="HTML")

    async def agentic_identity(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """Show the composed identity prompt for a profile (persona + shared caps).

        Usage: /identity [slug]. Defaults to the active profile. Reconstructs the
        same layered prompt the SDK builds, minus the memory-fact layer, so the
        persona differences between agents are visible from the bot interface.
        Project CLAUDE.md is not shown: it is loaded by the CLI from the
        "project" setting source, not composed into this prompt.
        """
        from ....claude.prompt_composer import PromptComposer

        registry = context.bot_data.get("profile_registry")
        if not registry:
            await update.message.reply_text("Profiles are not configured.")
            return
        args = context.args or []
        active = context.user_data.get("active_profile") or registry.get_default().slug
        slug = args[0] if args else active
        profile = registry.get_by_slug(slug)
        if not profile:
            known = ", ".join(p.slug for p in registry.profiles)
            await update.message.reply_text(
                f"Unknown profile `{slug}`. Known: {known}", parse_mode="Markdown"
            )
            return

        composer = PromptComposer(
            memory_path=profile.memory_dir, agent_md_path=profile.persona
        )
        prompt = composer.compose(working_directory=profile.working_dir)

        await update.message.reply_text(
            f"Identity prompt for `{slug}` ({profile.name}).\n"
            "Excludes the runtime memory-fact layer. Project CLAUDE.md is "
            "loaded by the CLI, not composed here.\n"
            "Live (next-roll) content — the current window keeps its frozen "
            "bundle until the next rotation or /new.",
            parse_mode="Markdown",
        )
        header = f"===== {slug} identity prompt =====\n"
        body = header + prompt
        # Telegram caps messages at 4096 chars; chunk plain text to stay safe.
        limit = 3900
        for i in range(0, len(body), limit):
            await update.message.reply_text(body[i : i + limit])

        storage = context.bot_data.get("storage")
        if storage:
            await storage.log_bot_event(
                update.effective_user.id,
                "command_identity",
                {"profile": slug, "chars": len(prompt)},
            )

    async def agentic_repo(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> None:
        """List repos in workspace or switch to one.

        /repo          — list subdirectories with git indicators
        /repo <name>   — switch to that directory, resume session if available
        """
        args = update.message.text.split()[1:] if update.message.text else []
        base = self._workspace_base(context)
        current_dir = context.user_data.get("current_directory", base)

        if args:
            # Switch to named repo
            target_name = args[0]
            target_path = base / target_name
            if not target_path.is_dir():
                await update.message.reply_text(
                    f"Directory not found: <code>{escape_html(target_name)}</code>",
                    parse_mode="HTML",
                )
                return

            context.user_data["current_directory"] = target_path

            # Try to find a resumable session on every registered backend
            session_id = await self._refresh_resumable_sessions(
                context, update.effective_user.id, target_path
            )

            is_git = (target_path / ".git").is_dir()
            git_badge = " (git)" if is_git else ""
            session_badge = " · session resumed" if session_id else ""

            await update.message.reply_text(
                f"Switched to <code>{escape_html(target_name)}/</code>"
                f"{git_badge}{session_badge}",
                parse_mode="HTML",
            )
            return

        # No args — list repos
        try:
            entries = sorted(
                [
                    d
                    for d in base.iterdir()
                    if d.is_dir() and not d.name.startswith(".")
                ],
                key=lambda d: d.name,
            )
        except OSError as e:
            await update.message.reply_text(f"Error reading workspace: {e}")
            return

        if not entries:
            await update.message.reply_text(
                f"No repos in <code>{escape_html(str(base))}</code>.\n"
                'Clone one by telling me, e.g. <i>"clone org/repo"</i>.',
                parse_mode="HTML",
            )
            return

        lines: List[str] = []
        keyboard_rows: List[list] = []  # type: ignore[type-arg]
        current_name = current_dir.name if current_dir != base else None

        for d in entries:
            is_git = (d / ".git").is_dir()
            icon = "\U0001f4e6" if is_git else "\U0001f4c1"
            marker = " \u25c0" if d.name == current_name else ""
            lines.append(f"{icon} <code>{escape_html(d.name)}/</code>{marker}")

        # Build inline keyboard (2 per row)
        for i in range(0, len(entries), 2):
            row = []
            for j in range(2):
                if i + j < len(entries):
                    name = entries[i + j].name
                    row.append(InlineKeyboardButton(name, callback_data=f"cd:{name}"))
            keyboard_rows.append(row)

        reply_markup = InlineKeyboardMarkup(keyboard_rows)

        await update.message.reply_text(
            "<b>Repos</b>\n\n" + "\n".join(lines),
            parse_mode="HTML",
            reply_markup=reply_markup,
        )
