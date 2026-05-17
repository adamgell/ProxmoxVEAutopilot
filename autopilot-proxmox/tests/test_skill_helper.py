from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
SKILL = ROOT / "skill.sh"


def test_skill_helper_token_lookup_checks_controller_root_env_without_printing_token():
    text = SKILL.read_text(encoding="utf-8")

    assert "AUTOPILOT_MCP_REMOTE_ENV_FILE" in text
    assert '"${REMOTE_APP_DIR}/.env"' in text
    assert '"$(dirname "${REMOTE_APP_DIR}")/.env"' in text
    assert "docker inspect autopilot-mcp" in text
    assert "token_from_docker" in text
    assert "AUTOPILOT_MCP_TOKEN" in text
    assert "echo \"${AUTOPILOT_MCP_TOKEN}" not in text
    assert "cat .env" not in text
