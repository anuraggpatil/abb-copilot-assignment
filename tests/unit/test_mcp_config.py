"""Settings resolution for the MCP server.

Worth its own module because of one live-run failure that no in-process test could see: the
tests construct `MCPServer` objects directly and never read the serving fields, so a field
resolving from the wrong environment variable stays invisible until the server is started as
a real process — where it bound the shell's `PATH` as its HTTP endpoint.

The tests below therefore assert on where each value *comes from*, not just its default.
"""

from __future__ import annotations

import pytest
from alarm_management.config import McpSettings


def _settings(env: dict[str, str], monkeypatch: pytest.MonkeyPatch) -> McpSettings:
    """Build settings from a controlled environment, with `.env` taken out of the picture."""
    for key in list(env) + ["PATH", "MCP_SERVER_PATH", "MCP_TRANSPORT", "MCP_MAX_ROWS"]:
        monkeypatch.delenv(key, raising=False)
    for key, value in env.items():
        monkeypatch.setenv(key, value)
    # Otherwise a developer's own `.env` decides whether these tests pass.
    return McpSettings(_env_file=None)


def test_the_serving_path_is_not_taken_from_the_shells_path_variable(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The regression test for the live-run bug.

    `PATH` is set in every environment there is, so an un-aliased `path` field resolved to
    `/usr/local/bin:/usr/bin:…` and the server advertised that as its endpoint. It started
    cleanly and served nothing reachable.
    """
    settings = _settings({"PATH": "/usr/local/bin:/usr/bin:/bin"}, monkeypatch)
    assert settings.path == "/mcp"


def test_the_serving_path_is_configurable_under_its_own_name(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Fixing the collision must not cost the ability to override the value."""
    settings = _settings({"MCP_SERVER_PATH": "/alarm-mcp"}, monkeypatch)
    assert settings.path == "/alarm-mcp"


def test_every_field_resolves_from_a_prefixed_variable_only(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A field named for a common shell variable is the bug above waiting to recur.

    Asserting the *absence* of un-prefixed resolution is what keeps a later field addition
    from reintroducing it.
    """
    generic = {
        "PATH": "/usr/bin",
        "HOST": "malicious.internal",
        "PORT": "1",
        "TRANSPORT": "stdio",
        "CLIENT_ID": "someone-else",
        "MAX_ROWS": "99999",
    }
    settings = _settings(generic, monkeypatch)

    assert settings.path == "/mcp"
    assert settings.host == "0.0.0.0"
    assert settings.port == 9100
    assert settings.transport == "streamable-http"
    assert settings.client_id == "alarm-copilot"
    assert settings.max_rows == 100


def test_the_upstream_address_and_token_come_from_the_shared_alarm_api_names(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Shared with the simulator and the connector on purpose: one address, one token."""
    settings = _settings(
        {
            "ALARM_API_BASE_URL": "http://alarm-api:8000",
            "ALARM_API_TOKEN": "a-different-token",
            "ALARM_API_MAX_RETRIES": "5",
            "ALARM_API_TIMEOUT_SECONDS": "2.5",
        },
        monkeypatch,
    )
    assert settings.alarm_api_base_url == "http://alarm-api:8000"
    assert settings.alarm_api_token == "a-different-token"
    assert settings.alarm_api_max_retries == 5
    assert settings.alarm_api_timeout_seconds == 2.5


def test_an_unknown_transport_is_rejected_at_startup(monkeypatch: pytest.MonkeyPatch) -> None:
    """Better a refusal to boot than a process that runs and serves the wrong protocol.

    The message must name `MCP_TRANSPORT` rather than the field, because the variable is what
    whoever is reading the crash has to go and change.
    """
    with pytest.raises(ValueError, match="MCP_TRANSPORT") as raised:
        _settings({"MCP_TRANSPORT": "websocket"}, monkeypatch)
    assert "'stdio' or 'streamable-http'" in str(raised.value), "and list the valid choices"


def test_unrelated_settings_in_the_shared_env_file_are_ignored(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The whole point of a separate settings class: LLM credentials cannot land here.

    `extra="ignore"` is what lets one `.env` serve every process without this one gaining the
    ability to read a token it has no business holding.
    """
    settings = _settings({"GEMINI_API_KEY": "should-not-be-readable"}, monkeypatch)
    assert not hasattr(settings, "gemini_api_key")
    assert "should-not-be-readable" not in settings.model_dump_json()
