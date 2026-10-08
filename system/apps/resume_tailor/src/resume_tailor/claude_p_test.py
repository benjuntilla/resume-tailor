"""The app's copy of the claude -p helper: the binary is swappable for a stand-in."""

from __future__ import annotations

from resume_tailor import claude_p


def test_completion_argv_uses_the_given_binary() -> None:
    argv = claude_p._completion_argv("hi", system="sys", model="m", binary="/opt/fake-claude")
    assert argv[:3] == ["/opt/fake-claude", "-p", "hi"]
    assert argv[argv.index("--tools") + 1] == ""
    assert "--no-session-persistence" in argv


def test_completion_argv_defaults_to_claude() -> None:
    assert claude_p._completion_argv("hi", system="sys", model="m")[0] == "claude"


def test_child_env_drops_the_session_and_agent_vars(monkeypatch) -> None:
    monkeypatch.setenv("MAIN_CLAUDE_SESSION_ID", "s")
    monkeypatch.setenv("MNGR_AGENT_NAME", "a")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", "/explicit")
    env = claude_p._child_env(strip_mngr_agent_vars=True)
    assert "MAIN_CLAUDE_SESSION_ID" not in env and "MNGR_AGENT_NAME" not in env
    assert env["CLAUDE_CONFIG_DIR"] == "/explicit"
