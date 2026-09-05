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
    def test_startup_registration_is_defined_and_called_before_agent_loop(self):
        source = Path(__file__).with_name("agent.py").read_text(encoding="utf-8")
        self.assertIn("def ensure_agent_startup() -> bool:", source)
        self.assertIn('STARTUP_VALUE_NAME = "RemoteInstallCustomerAgent"', source)
        self.assertIn("ensure_agent_startup()", source)

    def test_agent_has_independent_windows_service_entrypoint(self):
        source = Path(__file__).with_name("agent.py").read_text(encoding="utf-8")
        self.assertIn('SERVICE_NAME = "RemoteInstallCustomerAgent"', source)
        self.assertIn("class CustomerAgentService", source)
        self.assertIn("win32serviceutil.HandleCommandLine(CustomerAgentService)", source)
        self.assertIn("run_agent(self.stop_event)", source)
        self.assertIn("import win32timezone", source)

    def test_agent_is_started_at_boot_as_system_with_restart(self):
        source = Path(__file__).resolve().parents[1] / "customer-installer" / "Program.cs"
        source = source.read_text(encoding="utf-8")
        self.assertIn("void ConfigureCustomerAgentStartup(string agent)", source)
        self.assertIn("<BootTrigger><Enabled>true</Enabled><Delay>PT30S</Delay></BootTrigger>", source)
        self.assertIn("<UserId>S-1-5-18</UserId><LogonType>ServiceAccount</LogonType>", source)
        self.assertIn('<Interval>PT1M</Interval><Count>999</Count>', source)
        self.assertIn('RunScheduledTaskCommand("/Run /TN \\"RemoteInstallCustomerAgent\\"", true);', source)

    def test_rustdesk_id_retries_until_client_is_ready(self):
        executable = Path(agent.executable_dir()) / "rustdesk.exe"
        first = type("Result", (), {"stdout": "", "stderr": "", "returncode": 0})()
        second = type("Result", (), {"stdout": "176854346\\n", "stderr": "", "returncode": 0})()
        with patch.object(agent, "_last_rustdesk_id", ""), patch.object(Path, "exists", return_value=True), patch.object(
            agent.subprocess, "run", side_effect=[first, second]
        ) as run, patch.object(agent.time, "sleep"):
            self.assertEqual(agent.rustdesk_id(), "176854346")
        self.assertEqual(run.call_count, 2)

    def test_rustdesk_id_reads_stderr_and_keeps_last_value(self):
        first = type("Result", (), {"stdout": "", "stderr": "RustDesk ID: 123456789", "returncode": 0})()
        with patch.object(agent, "_last_rustdesk_id", ""), patch.object(Path, "exists", return_value=True), patch.object(
            agent.subprocess, "run", return_value=first
        ), patch.object(agent.time, "sleep"):
            self.assertEqual(agent.rustdesk_id(), "123456789")

    def test_missing_rustdesk_id_retries_heartbeat_every_ten_seconds(self):
        instance = agent.CustomerAgent(agent.AgentConfig("https://example.test", 1, "token"))
        instance.last_heartbeat = 100.0
        self.assertFalse(instance.heartbeat_due(109.9))
        self.assertTrue(instance.heartbeat_due(110.0))
        instance.last_rustdesk_id = "123456789"
        self.assertFalse(instance.heartbeat_due(159.9))
        self.assertTrue(instance.heartbeat_due(160.0))

    def test_environment_inspection_reports_available_jdks(self):
        with patch.object(agent.CustomerAgent, "find_java_homes", return_value=[Path(r"D:\soft\jdk_21")]):
            result = json.loads(agent.CustomerAgent.execute_ai_action({"tool_name": "inspect_development_environment"}))
        self.assertEqual(result["java_homes"], [r"D:\soft\jdk_21"])

    def test_installer_artifacts_are_unique_per_task(self):
        first = agent.CustomerAgent.installer_destination("python", 27)
        second = agent.CustomerAgent.installer_destination("python", 28)
        self.assertNotEqual(first, second)
        self.assertTrue(str(first).endswith("pythonMain-task-27.exe"))

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

    def test_cancelled_task_stops_active_installer(self):
        with tempfile.TemporaryDirectory() as directory:
            status_file = Path(directory) / "task.json"
            status_file.write_text(json.dumps({"status": "running", "pid": 1234}), encoding="utf-8")
            instance = agent.CustomerAgent(agent.AgentConfig("https://example.test", 1, "token"))
            instance.active_tasks[1] = status_file
            instance.task_artifacts[1] = (Path(directory) / "javaMain.exe", Path(directory) / "request.json", status_file)
            with patch.object(instance, "request", return_value={"cancelled": True}), patch.object(instance, "report") as report, patch.object(instance, "stop_installer_process") as stop, patch.object(instance, "cleanup_task_artifacts") as cleanup:
                instance.update_active_tasks()
            stop.assert_called_once_with(1234)
            report.assert_called_once_with(1, "cancelled", "运营人员已取消任务，安装器已关闭")
            cleanup.assert_called_once_with(1)

    def test_cleanup_is_successful_when_installed_directory_is_already_missing(self):
        instance = agent.CustomerAgent(agent.AgentConfig("https://example.test", 1, "token"))
        target = {"path": r"C:\\missing\\maven_3.8.1", "root": r"C:\\missing", "kind": "install", "label": "Maven 3.8.1"}
        with patch.object(instance, "report") as report:
            instance.cleanup_task(7, [target])
        self.assertEqual(report.call_count, 2)
        self.assertEqual(report.call_args_list[-1].args[1], "success")
        self.assertIn("Maven 3.8.1", report.call_args_list[-1].args[2])
        self.assertIn("已不存在", report.call_args_list[-1].args[2])

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

    def test_cleanup_terminates_processes_running_from_install_directory(self):
        class FakeProcess:
            pid = 321
            info = {"pid": 321, "name": "python.exe", "exe": "C:/soft/anaconda/python.exe"}

            def terminate(self):
                self.terminated = True

            def kill(self):
                self.killed = True

        process = FakeProcess()
        with patch.object(agent.psutil, "process_iter", return_value=[process]), patch.object(agent.psutil, "wait_procs", return_value=([], [process])):
            stopped = agent.CustomerAgent.stop_processes_under(Path("C:/soft/anaconda"))
        self.assertEqual(stopped, ["python.exe (PID 321)"])
        self.assertTrue(process.terminated)
        self.assertTrue(process.killed)


if __name__ == "__main__":
    unittest.main()
