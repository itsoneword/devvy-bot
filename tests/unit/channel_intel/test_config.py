from src.channel_intel.config import ServerConfig


def test_config_from_env(monkeypatch):
    monkeypatch.setenv("CHANNEL_INTEL_PORT", "8765")
    monkeypatch.setenv("CHANNEL_INTEL_TOKEN", "secret")
    c = ServerConfig.from_env()
    assert c.port == 8765 and c.token == "secret"
    assert c.host == "127.0.0.1"  # default
