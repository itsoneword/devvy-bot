import stat
from types import SimpleNamespace
from unittest.mock import AsyncMock

from src.agent_backends.codex_usage import CodexUsageClient, format_codex_limits
from src.bot.orchestration.commands.info import InfoCommandsMixin


def _fake_app_server(tmp_path):
    script = tmp_path / "codex"
    script.write_text(
        "#!/usr/bin/env bash\n"
        "while IFS= read -r line; do\n"
        '  case "$line" in\n'
        '    *\'"id":1\'*) echo \'{"id":1,"result":{}}\' ;;\n'
        '    *\'"id":2\'*) echo \'{"method":"remoteControl/status/changed","params":{}}\'; '
        'echo \'{"id":2,"result":{"rateLimitsByLimitId":{"codex":{"planType":"free","primary":{"usedPercent":76,"windowDurationMins":43200,"resetsAt":1790695102},"secondary":null,"credits":{"hasCredits":false,"unlimited":false}}}}}\' ;;\n'
        '    *\'"id":3\'*) echo \'{"id":3,"result":{"summary":{"lifetimeTokens":1234567,"peakDailyTokens":45678},"dailyUsageBuckets":[]}}\' ;;\n'
        "  esac\n"
        "done\n"
    )
    script.chmod(script.stat().st_mode | stat.S_IEXEC)
    return script


async def test_client_reads_limits_without_an_llm_turn(tmp_path):
    client = CodexUsageClient(codex_bin=str(_fake_app_server(tmp_path)))

    snapshot = await client.read()

    rate = snapshot["rate_limits"]["rateLimitsByLimitId"]["codex"]
    assert rate["planType"] == "free"
    assert rate["primary"]["usedPercent"] == 76
    assert snapshot["usage"]["summary"]["lifetimeTokens"] == 1234567


def test_format_codex_limits_is_compact_and_does_not_dump_daily_buckets():
    text = format_codex_limits(
        {
            "rate_limits": {
                "rateLimitsByLimitId": {
                    "codex": {
                        "planType": "free",
                        "primary": {
                            "usedPercent": 76,
                            "windowDurationMins": 43200,
                            "resetsAt": 1790695102,
                        },
                        "credits": {
                            "hasCredits": False,
                            "unlimited": False,
                        },
                    }
                }
            },
            "usage": {
                "summary": {
                    "lifetimeTokens": 1234567,
                    "peakDailyTokens": 45678,
                },
                "dailyUsageBuckets": [{"startDate": "2026-09-20", "tokens": 999999999}],
            },
        }
    )

    assert "Plan: free" in text
    assert "30 дн. · использовано 76%" in text
    assert "2026-09-29 15:18 UTC" in text
    assert "Токены за всю историю аккаунта: 1,234,567" in text
    assert "999,999,999" not in text


async def test_limits_command_reads_provider_state_without_a_model_turn(monkeypatch):
    # dv-f6af: Codex limits show only when Codex ran in the last 7 days.
    from src.storage import model_calls

    async def _used(db, harness, days):
        return harness == "openai_codex"

    async def _no_spend(db):
        return []

    monkeypatch.setattr(model_calls, "harness_used_since", _used)
    monkeypatch.setattr(model_calls, "paid_spend", _no_spend)
    backend = SimpleNamespace(
        read_account_limits=AsyncMock(
            return_value={
                "rate_limits": {
                    "rateLimitsByLimitId": {
                        "codex": {
                            "planType": "plus",
                            "primary": {"usedPercent": 12},
                        }
                    }
                },
                "usage": {"summary": {"lifetimeTokens": 123}},
            }
        )
    )
    update = SimpleNamespace(message=SimpleNamespace(reply_text=AsyncMock()))
    context = SimpleNamespace(
        bot_data={
            "backends": {"openai_codex": backend},
            "storage": SimpleNamespace(db_manager=object()),
        },
        user_data={},
    )

    await InfoCommandsMixin.agentic_limits(object(), update, context)

    backend.read_account_limits.assert_awaited_once_with()
    sent = update.message.reply_text.await_args.args[0]
    assert "Plan: plus" in sent
    assert "использовано 12%" in sent
