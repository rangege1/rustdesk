import importlib.util
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch


SPEC = importlib.util.spec_from_file_location("customer_agent", Path(__file__).with_name("agent.py"))
agent = importlib.util.module_from_spec(SPEC)
assert SPEC and SPEC.loader
sys.modules[SPEC.name] = agent
SPEC.loader.exec_module(agent)


class CustomerAgentTests(unittest.TestCase):
    def test_elevated_process_access_denied_means_still_running(self):
        with patch.object(agent.os, "kill", side_effect=PermissionError):
            self.assertTrue(agent.CustomerAgent.process_exists(1234))

    def test_success_stops_installer_before_artifact_cleanup(self):
        with tempfile.TemporaryDirectory() as directory:
            status_file = Path(directory) / "task.json"
            status_file.write_text(json.dumps({"status": "success", "message": "done", "pid": 1234}), encoding="utf-8")
            instance = agent.CustomerAgent(agent.AgentConfig("https://example.test", 1, "token"))
            instance.active_tasks[1] = status_file
            instance.task_artifacts[1] = (Path(directory) / "javaMain.exe", Path(directory) / "request.json", status_file)
            with patch.object(instance, "report"), patch.object(instance, "stop_installer_process") as stop, patch.object(instance, "cleanup_task_artifacts") as cleanup:
                instance.update_active_tasks()
            stop.assert_called_once_with(1234)
            cleanup.assert_called_once_with(1)


if __name__ == "__main__":
    unittest.main()
