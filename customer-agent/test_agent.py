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

    def test_success_keeps_installer_alive_until_it_closes_itself(self):
        with tempfile.TemporaryDirectory() as directory:
            status_file = Path(directory) / "task.json"
            status_file.write_text(json.dumps({"status": "success", "message": "done", "pid": 1234}), encoding="utf-8")
            instance = agent.CustomerAgent(agent.AgentConfig("https://example.test", 1, "token"))
            instance.active_tasks[1] = status_file
            instance.task_artifacts[1] = (Path(directory) / "javaMain.exe", Path(directory) / "request.json", status_file)
            with patch.object(instance, "report"), patch.object(instance, "stop_installer_process") as stop, patch.object(instance, "cleanup_task_artifacts") as cleanup:
                instance.update_active_tasks()
            stop.assert_not_called()
            cleanup.assert_called_once_with(1)

    def test_installer_exit_rechecks_all_install_paths_before_marking_success(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            requested = root / "D-soft" / "jdk_1.8.0_241" / "bin" / "java.exe"
            installed = root / "C-soft" / "jdk_1.8.0_241" / "bin" / "java.exe"
            installed.parent.mkdir(parents=True)
            installed.touch()
            status_file = root / "task.json"
            status_file.write_text(json.dumps({"status": "running", "pid": 1234}), encoding="utf-8")
            instance = agent.CustomerAgent(agent.AgentConfig("https://example.test", 1, "token"))
            instance.active_tasks[1] = status_file
            instance.task_install_checks[1] = [[requested, installed]]
            with patch.object(instance, "process_exists", return_value=False), patch.object(instance, "report") as report:
                instance.update_active_tasks()
            report.assert_any_call(1, "success", "安装器已退出，已校验所有软件文件存在")
            self.assertNotIn(1, instance.active_tasks)

    def test_installer_exit_waits_for_elevated_process_to_finish(self):
        with tempfile.TemporaryDirectory() as directory:
            status_file = Path(directory) / "task.json"
            status_file.write_text(json.dumps({"status": "running", "pid": 1234}), encoding="utf-8")
            instance = agent.CustomerAgent(agent.AgentConfig("https://example.test", 1, "token"))
            instance.active_tasks[1] = status_file
            instance.active_task_started_at[1] = agent.time.monotonic()
            instance.task_install_checks[1] = [[Path(directory) / "jdk" / "bin" / "java.exe"]]
            with patch.object(instance, "process_exists", return_value=False), patch.object(instance, "report") as report:
                instance.update_active_tasks()
            report.assert_any_call(1, "running", "安装器已移交提权进程，正在等待安装完成并校验文件")
            self.assertIn(1, instance.active_tasks)

    def test_installer_status_uses_reported_path_and_results(self):
        status = {
            "actual_install_path": r"C:\soft",
            "results": [{"software": "jdk", "version": "1.8.0_241"}],
        }
        message = agent.CustomerAgent.installer_status_message(status, "全部安装项已完成")
        self.assertEqual(message, "全部安装项已完成；实际安装路径：C:\\soft；安装器确认：jdk 1.8.0_241")

    def test_cleanup_fails_when_no_installed_directory_is_deleted(self):
        instance = agent.CustomerAgent(agent.AgentConfig("https://example.test", 1, "token"))
        target = {"path": r"C:\\missing\\maven_3.8.1", "root": r"C:\\missing", "kind": "install", "label": "Maven 3.8.1"}
        with patch.object(instance, "report") as report:
            instance.cleanup_task(7, [target])
        self.assertEqual(report.call_count, 2)
        self.assertEqual(report.call_args_list[-1].args[1], "failed")
        self.assertIn("Maven 3.8.1", report.call_args_list[-1].args[2])

    def test_cleanup_removes_download_cache_and_runtime_with_installation(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            install = root / "maven_3.8.1"
            cache = root / "package"
            runtime = root / "jre_1.8.0_241"
            for path in (install, cache, runtime):
                path.mkdir()
            instance = agent.CustomerAgent(agent.AgentConfig("https://example.test", 1, "token"))
            targets = [
                {"path": str(install), "root": str(root), "kind": "install", "label": "Maven 3.8.1"},
                {"path": str(cache), "root": str(root), "kind": "download-cache", "label": "下载缓存目录"},
                {"path": str(runtime), "root": str(root), "kind": "runtime", "label": "Java 运行环境 1.8.0_241"},
            ]
            with patch.object(instance, "report") as report:
                instance.cleanup_task(7, targets)
            self.assertFalse(install.exists())
            self.assertFalse(cache.exists())
            self.assertFalse(runtime.exists())
            self.assertEqual(report.call_args_list[-1].args[1], "success")
            self.assertIn("下载缓存目录", report.call_args_list[-1].args[2])


if __name__ == "__main__":
    unittest.main()
