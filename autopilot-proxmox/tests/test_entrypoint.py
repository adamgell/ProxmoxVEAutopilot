import sys
from unittest.mock import patch


def test_default_mode_is_web():
    from web import entrypoint
    with patch("web.entrypoint._run_web") as mock_web, \
         patch("web.entrypoint._run_builder") as mock_builder, \
         patch("web.entrypoint._run_monitor") as mock_monitor, \
         patch("web.entrypoint._run_mcp") as mock_mcp:
        entrypoint.main([])
        mock_web.assert_called_once()
        mock_builder.assert_not_called()
        mock_monitor.assert_not_called()
        mock_mcp.assert_not_called()


def test_builder_mode_dispatches():
    from web import entrypoint
    with patch("web.entrypoint._run_web") as mock_web, \
         patch("web.entrypoint._run_builder") as mock_builder, \
         patch("web.entrypoint._run_monitor") as mock_monitor, \
         patch("web.entrypoint._run_mcp") as mock_mcp:
        entrypoint.main(["builder"])
        mock_builder.assert_called_once()
        mock_web.assert_not_called()
        mock_mcp.assert_not_called()


def test_monitor_mode_dispatches():
    from web import entrypoint
    with patch("web.entrypoint._run_web") as mock_web, \
         patch("web.entrypoint._run_builder") as mock_builder, \
         patch("web.entrypoint._run_monitor") as mock_monitor, \
         patch("web.entrypoint._run_mcp") as mock_mcp:
        entrypoint.main(["monitor"])
        mock_monitor.assert_called_once()
        mock_mcp.assert_not_called()


def test_mcp_mode_dispatches():
    from web import entrypoint
    with patch("web.entrypoint._run_web") as mock_web, \
         patch("web.entrypoint._run_builder") as mock_builder, \
         patch("web.entrypoint._run_monitor") as mock_monitor, \
         patch("web.entrypoint._run_mcp") as mock_mcp:
        entrypoint.main(["mcp"])
        mock_mcp.assert_called_once()
        mock_web.assert_not_called()
        mock_builder.assert_not_called()
        mock_monitor.assert_not_called()


def test_unknown_mode_exits_nonzero(capsys):
    from web import entrypoint
    import pytest
    with pytest.raises(SystemExit) as exc:
        entrypoint.main(["bogus"])
    assert exc.value.code != 0
    captured = capsys.readouterr()
    assert "unknown mode" in captured.err.lower()
