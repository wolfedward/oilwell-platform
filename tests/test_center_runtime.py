"""Production launcher contracts for enabling the central telemetry read path."""

from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import server as server_module


class CenterRuntimeTests(unittest.TestCase):
    def test_launcher_allows_database_validation_to_take_eighteen_seconds(self):
        elapsed = [0.0]
        child = Mock(pid=123)
        child.poll.return_value = None

        def runtime(_path):
            return {"instance_id": "launch-token", "pid": 123} if elapsed[0] >= 18 else None

        def advance(seconds):
            elapsed[0] += seconds

        with tempfile.TemporaryDirectory() as temporary, \
                patch.object(server_module.Config, "LOG_PATH", Path(temporary)), \
                patch.object(server_module.secrets, "token_urlsafe", return_value="launch-token"), \
                patch.object(server_module.subprocess, "Popen", return_value=child), \
                patch.object(server_module.time, "monotonic", side_effect=lambda: elapsed[0]), \
                patch.object(server_module.time, "sleep", side_effect=advance), \
                patch.object(server_module, "_read_runtime", side_effect=runtime), \
                patch.object(server_module, "_health_is_ready", return_value=True):
            result = server_module.launch_server("127.0.0.1", 8765, False)

        self.assertEqual(result, 0, "validating a populated database must not be reported as failed startup")
        child.terminate.assert_not_called()

    def test_hidden_child_command_preserves_absolute_center_config_path(self):
        builder = getattr(server_module, "_launch_command", None)
        self.assertIsNotNone(
            builder,
            "the launcher needs one command builder so center configuration cannot be dropped",
        )
        config_path = Path("config/center.local.toml").resolve()

        command = builder("127.0.0.1", 8765, "instance-token", config_path)

        self.assertEqual(command[0], sys.executable)
        self.assertIn("--child", command)
        self.assertIn("--instance-id=instance-token", command)
        self.assertIn(f"--center-config={config_path}", command)

    def test_hidden_child_command_omits_center_option_in_compatibility_mode(self):
        builder = getattr(server_module, "_launch_command", None)
        self.assertIsNotNone(builder)

        command = builder("127.0.0.1", 8765, "instance-token", None)

        self.assertFalse(any(part.startswith("--center-config") for part in command))


if __name__ == "__main__":
    unittest.main()
