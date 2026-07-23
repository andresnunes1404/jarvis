"""Tests for the opt-in Obsidian auto-launch check that runs before MCP
discovery at daemon startup. See mcp_runtime.spec.md "Obsidian
auto-launch" and src/jarvis/tools/external/mcp_runtime.py.
"""

from unittest.mock import patch

from jarvis.tools.external import mcp_runtime


class TestObsidianAutoLaunch:
    def test_disabled_by_default_never_checks_or_launches(self, mock_config):
        mock_config.obsidian_auto_launch = False

        with patch.object(mcp_runtime, "_is_port_listening") as mock_check, \
             patch.object(mcp_runtime, "_launch_obsidian") as mock_launch:
            mcp_runtime.ensure_obsidian_running(mock_config)

        mock_check.assert_not_called()
        mock_launch.assert_not_called()

    def test_enabled_and_port_already_listening_does_not_launch(self, mock_config):
        mock_config.obsidian_auto_launch = True

        with patch.object(mcp_runtime, "_is_port_listening", return_value=True), \
             patch.object(mcp_runtime, "_launch_obsidian") as mock_launch:
            mcp_runtime.ensure_obsidian_running(mock_config)

        mock_launch.assert_not_called()

    def test_enabled_and_port_not_listening_attempts_launch(self, mock_config):
        mock_config.obsidian_auto_launch = True
        mock_config.obsidian_executable_path = "C:/Obsidian/Obsidian.exe"

        with patch.object(mcp_runtime, "_is_port_listening", return_value=False), \
             patch.object(mcp_runtime, "_launch_obsidian", return_value=True) as mock_launch, \
             patch.object(mcp_runtime.time, "sleep") as mock_sleep:
            mcp_runtime.ensure_obsidian_running(mock_config)

        mock_launch.assert_called_once_with("C:/Obsidian/Obsidian.exe")
        mock_sleep.assert_called_once()

    def test_port_check_failure_fails_open_without_raising(self, mock_config):
        mock_config.obsidian_auto_launch = True

        with patch.object(mcp_runtime, "_is_port_listening", side_effect=RuntimeError("boom")):
            mcp_runtime.ensure_obsidian_running(mock_config)  # must not raise

    def test_launch_attempt_failure_fails_open_without_raising(self, mock_config):
        mock_config.obsidian_auto_launch = True

        with patch.object(mcp_runtime, "_is_port_listening", return_value=False), \
             patch.object(mcp_runtime, "_launch_obsidian", side_effect=RuntimeError("boom")):
            mcp_runtime.ensure_obsidian_running(mock_config)  # must not raise


class TestPortListeningCheck:
    def test_returns_false_when_nothing_listening(self):
        # An unused high port on loopback should refuse the connection.
        assert mcp_runtime._is_port_listening("127.0.0.1", 65432, 0.2) is False

    def test_returns_true_when_something_listening(self):
        import socket

        server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        port = server.getsockname()[1]
        try:
            assert mcp_runtime._is_port_listening("127.0.0.1", port, 0.5) is True
        finally:
            server.close()


class TestLaunchObsidian:
    def test_windows_uses_protocol_handler(self):
        with patch.object(mcp_runtime.sys, "platform", "win32"), \
             patch.object(mcp_runtime.os, "startfile") as mock_startfile:
            result = mcp_runtime._launch_obsidian(None)

        mock_startfile.assert_called_once_with("obsidian://open")
        assert result is True

    def test_protocol_handler_failure_falls_back_to_executable_path(self):
        with patch.object(mcp_runtime.sys, "platform", "win32"), \
             patch.object(mcp_runtime.os, "startfile", side_effect=OSError("no handler")), \
             patch.object(mcp_runtime.subprocess, "Popen") as mock_popen:
            result = mcp_runtime._launch_obsidian("C:/Obsidian/Obsidian.exe")

        mock_popen.assert_called_once_with(["C:/Obsidian/Obsidian.exe"])
        assert result is True

    def test_no_executable_path_and_no_protocol_handler_returns_false(self):
        with patch.object(mcp_runtime.sys, "platform", "win32"), \
             patch.object(mcp_runtime.os, "startfile", side_effect=OSError("no handler")):
            result = mcp_runtime._launch_obsidian(None)

        assert result is False
