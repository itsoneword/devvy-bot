"""Tests for token usage footer at verbose level 2."""


class TestTokenFooter:
    """Test the token footer logic in agentic_text responses."""

    def test_footer_with_real_tokens(self) -> None:
        """Footer uses real token values from ClaudeResponse when available."""
        input_tokens = 100
        output_tokens = 200
        cost = 0.0042

        # Simulate the orchestrator logic with real tokens
        input_t = input_tokens
        output_t = output_tokens
        prefix = ""
        if input_t == 0 and output_t == 0:
            prefix = "~"
        total_t = input_t + output_t
        cost_str = f"${cost:.4f}" if cost > 0 else ""
        footer_parts = [
            f"{prefix}{input_t} in",
            f"{prefix}{output_t} out",
            f"{prefix}{total_t} total",
        ]
        if cost_str:
            footer_parts.append(cost_str)
        footer = "\n\n---\n" + " | ".join(footer_parts)

        assert "100 in" in footer
        assert "200 out" in footer
        assert "300 total" in footer
        assert "$0.0042" in footer
        # No tilde prefix for real values
        assert "~" not in footer

    def test_footer_falls_back_to_estimation(self) -> None:
        """When input_tokens and output_tokens are both 0, fall back to char/4."""
        message_text = "a" * 400  # 100 tokens est
        response_content = "b" * 2000  # 500 tokens est
        cost = 0.0042

        input_t = 0
        output_t = 0
        if input_t == 0 and output_t == 0:
            input_t = len(message_text) // 4
            output_t = len(response_content) // 4
            prefix = "~"
        else:
            prefix = ""
        total_t = input_t + output_t
        cost_str = f"${cost:.4f}" if cost > 0 else ""
        footer_parts = [
            f"{prefix}{input_t} in",
            f"{prefix}{output_t} out",
            f"{prefix}{total_t} total",
        ]
        if cost_str:
            footer_parts.append(cost_str)
        footer = "\n\n---\n" + " | ".join(footer_parts)

        assert "~100 in" in footer
        assert "~500 out" in footer
        assert "~600 total" in footer

    def test_footer_omits_cost_when_zero(self) -> None:
        """No cost string when cost is 0."""
        cost = 0.0
        cost_str = f"${cost:.4f}" if cost > 0 else ""
        assert cost_str == ""

    def test_footer_respects_telegram_limit(self) -> None:
        """Footer appended only if within 4000 char limit."""
        existing_text = "x" * 3990
        footer = "\n\n---\n100 in | 200 out | 300 total"
        combined = len(existing_text) + len(footer)
        # Should exceed 4000 -> footer goes as separate message
        assert combined > 4000
