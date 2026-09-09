"""dv-9f31: ClaudeResponse must expose the conversation's real SIZE.

Two distinct mistakes are guarded here:

1. Reading `input_tokens` alone — with prompt caching on it is the uncached
   remainder, ~7 tokens a call on this deployment.
2. Reading the ResultMessage totals — every field there is a SUM over the
   turn's API calls, so a 20-round-trip agentic turn at 55k context reports
   over 1M and trips any window threshold instantly.

Only the per-call peak is a level.
"""

from src.claude.sdk_integration import ClaudeResponse, _peak_context_tokens


def _resp(**kw):
    return ClaudeResponse(
        content="", session_id="s", cost=0.0, duration_ms=1, num_turns=1, **kw
    )


class _Assistant:
    """Stands in for claude_agent_sdk AssistantMessage."""

    def __init__(self, usage=None, parent_tool_use_id=None):
        self.usage = usage
        self.parent_tool_use_id = parent_tool_use_id
        self.content = []


def _patch_assistant(monkeypatch):
    """Make isinstance(_Assistant(), AssistantMessage) true for the helper."""
    import src.claude.sdk_integration as sdk

    monkeypatch.setattr(sdk, "AssistantMessage", _Assistant)


def _usage(inp=0, read=0, create=0):
    return {
        "input_tokens": inp,
        "cache_read_input_tokens": read,
        "cache_creation_input_tokens": create,
    }


def test_peak_is_the_max_call_not_the_sum(monkeypatch):
    """The real shape of session fc737d2d (2026-08-04): 22 calls, each ~50-66k
    of context, summing to 1.22M. The turn's size is 66k, not 1.22M."""
    _patch_assistant(monkeypatch)
    msgs = [
        _Assistant(_usage(inp=3, read=50_000)),
        _Assistant(_usage(inp=5, read=58_000, create=1_200)),
        _Assistant(_usage(inp=2, read=66_704)),
    ]
    assert _peak_context_tokens(msgs) == 66_706
    # Not the sum, which is what rolled the window every turn.
    assert _peak_context_tokens(msgs) < 180_000


def test_peak_ignores_subagent_calls(monkeypatch):
    """A Task sub-agent carries its own context the parent never holds."""
    _patch_assistant(monkeypatch)
    msgs = [
        _Assistant(_usage(read=40_000)),
        _Assistant(_usage(read=900_000), parent_tool_use_id="toolu_123"),
    ]
    assert _peak_context_tokens(msgs) == 40_000


def test_peak_is_zero_without_usage(monkeypatch):
    """Aborted turn / CLI that emits no per-message usage."""
    _patch_assistant(monkeypatch)
    assert _peak_context_tokens([_Assistant(None), _Assistant({})]) == 0
    assert _peak_context_tokens([]) == 0


def test_context_tokens_prefers_the_peak_over_result_totals():
    """ResultMessage totals for that same turn are ~1.2M of flow; the level
    is 66.7k. context_tokens must report the level."""
    r = _resp(
        input_tokens=40,
        output_tokens=11_000,
        cache_read_tokens=1_180_000,
        cache_creation_tokens=38_638,
        peak_context_tokens=66_706,
    )
    assert r.context_tokens == 66_706


def test_context_tokens_never_falls_back_to_input_tokens():
    """Used to degrade to input_tokens "for uncached backends (Codex)". Codex
    is not uncached, and its input_tokens is the thread's running total --
    the flow, which rolled the window every turn (2026-09-05). Unknown is 0."""
    r = _resp(input_tokens=54_000, output_tokens=800)
    assert r.context_tokens == 0


def test_context_tokens_is_zero_when_cached_but_peak_unknown():
    """Caching was active but no per-call usage arrived. 0 means 'unknown' —
    callers key off the falsy value and keep the window's last known level.
    Falling back to the cache sum here is exactly the regression this fixes."""
    r = _resp(input_tokens=2, cache_read_tokens=900_000, cache_creation_tokens=40_000)
    assert r.context_tokens == 0


def test_context_tokens_is_zero_when_nothing_reported():
    """An aborted turn reports no usage at all."""
    assert _resp().context_tokens == 0
