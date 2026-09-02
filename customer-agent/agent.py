from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import logging.handlers
import os
import platform
import psutil
import shutil
import socket
import subprocess
import sys
import time
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


AGENT_VERSION = "0.2.15"
POLL_SECONDS = 3
HEARTBEAT_SECONDS = 60
ARTIFACT_CLEANUP_INITIAL_DELAY_SECONDS = 15
ARTIFACT_CLEANUP_MAX_DELAY_SECONDS = 300
INSTALLER_COMPLETION_TIMEOUT_SECONDS = 30 * 60
RUNNERS = {"java", "python"}
RUSTDESK_ID_RE = re.compile(r"\d[\d\s-]{4,}\d")


def machine_id() -> str:
    """Return a stable, non-reversible identifier for this Windows install."""
    raw = ""
    if os.name == "nt":
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
                raw = str(winreg.QueryValueEx(key, "MachineGuid")[0])
        except (OSError, ImportError):
            pass
    if not raw:
        raw = socket.gethostname()
    return hashlib.sha256(f"RemoteInstall:{raw}".encode("utf-8")).hexdigest()


def executable_dir() -> Path:
    return Path(sys.executable if getattr(sys, "frozen", False) else __file__).resolve().parent


def rustdesk_id() -> str:
    """Read the numeric RustDesk ID from the colocated client, if it is ready."""
    executable = executable_dir() / "rustdesk.exe"
    if not executable.exists():
        return ""
    try:
        result = subprocess.run(
            [str(executable), "--get-id"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=10,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
    except (OSError, subprocess.SubprocessError):
        LOGGER.warning("rustdesk_id_unavailable")
        return ""
    match = RUSTDESK_ID_RE.search(result.stdout)
    if not match:
        return ""
    value = "".join(char for char in match.group(0) if char.isdigit())
    return value if 6 <= len(value) <= 20 else ""


DEFAULT_CONFIG = executable_dir() / "agent-config.json"
CONFIG_FILE = Path(os.environ.get("OPS_AGENT_CONFIG", DEFAULT_CONFIG))
WORK_DIR = Path(os.environ.get("PROGRAMDATA", os.environ.get("LOCALAPPDATA", str(executable_dir())))) / "RemoteInstall" / "agent"
LOG_DIR = WORK_DIR / "logs"
LOG_FILE = LOG_DIR / "customer-agent.log"
STARTUP_VALUE_NAME = "RemoteInstallCustomerAgent"
STARTUP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"


def configure_logging() -> logging.Logger:
    """Write a small rotating diagnostic log without recording credentials."""
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            LOG_FILE, maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
    except OSError:
        fallback = Path(os.environ.get("LOCALAPPDATA", str(executable_dir()))) / "RemoteInstall" / "agent" / "logs"
        fallback.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            fallback / "customer-agent.log", maxBytes=2 * 1024 * 1024, backupCount=3, encoding="utf-8"
        )
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%d %H:%M:%S")
    handler.setFormatter(formatter)
    stream = logging.StreamHandler()
    stream.setFormatter(formatter)
    logger = logging.getLogger("customer-agent")
    logger.setLevel(logging.INFO)
    logger.handlers.clear()
    logger.addHandler(handler)
    logger.addHandler(stream)
    logger.propagate = False
    return logger


LOGGER = configure_logging()


def ensure_agent_startup() -> bool:
    """Repair the current user's startup entry without requiring elevation."""
    if os.name != "nt":
        return False
    try:
        import winreg

        command = f'"{Path(sys.executable).resolve()}"'
        with winreg.CreateKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY) as key:
            winreg.SetValueEx(key, STARTUP_VALUE_NAME, 0, winreg.REG_SZ, command)
        LOGGER.info("agent_startup_ok scope=current_user command=%s", command)
        return True
    except (OSError, ImportError) as exc:
        LOGGER.warning("agent_startup_failed scope=current_user error=%s", exc)
        return False


@dataclass(frozen=True)
class AgentConfig:
    api_base: str
    customer_id: int
    agent_token: str


def load_config() -> AgentConfig:
    try:
        raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig")) if CONFIG_FILE.exists() else {}
    except Exception:
        LOGGER.exception("config_load_failed path=%s", CONFIG_FILE)
        raise
    api_base = str(raw.get("api_base", os.environ.get("OPS_API", "https://rmm.itadl.com:8443"))).rstrip("/")
    parsed = urlparse(api_base)
    if parsed.scheme not in {"https", "http"} or not parsed.netloc:
        raise ValueError("agent-config.json 的 api_base 必须是完整 HTTP(S) 地址")
    if parsed.scheme != "https" and parsed.hostname not in {"127.0.0.1", "localhost"}:
        raise ValueError("生产环境 api_base 必须使用 HTTPS")
    customer_id = int(raw.get("customer_id", os.environ.get("OPS_CUSTOMER_ID", "0")))
    agent_token = str(raw.get("agent_token", os.environ.get("OPS_AGENT_TOKEN", "")))
    if not customer_id or not agent_token:
        raise ValueError("请设置 customer_id 和 agent_token")
    LOGGER.info("config_loaded api_host=%s customer_id=%s config_path=%s", parsed.netloc, customer_id, CONFIG_FILE)
    return AgentConfig(api_base, customer_id, agent_token)


class CustomerAgent:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.active_tasks: dict[int, Path] = {}
        self.task_artifacts: dict[int, tuple[Path, Path, Path]] = {}
        self.task_install_checks: dict[int, list[list[Path]]] = {}
        self.active_task_started_at: dict[int, float] = {}
        self.artifact_cleanup_retries: dict[int, tuple[int, float]] = {}
        self.last_task_status: dict[int, tuple[str, str]] = {}
        self.last_heartbeat = 0.0
        self.last_empty_poll_log = 0.0
        (WORK_DIR / "installers").mkdir(parents=True, exist_ok=True)
        (WORK_DIR / "status").mkdir(parents=True, exist_ok=True)
        (WORK_DIR / "tasks").mkdir(parents=True, exist_ok=True)
        LOGGER.info("agent_initialized work_dir=%s", WORK_DIR)

    def request(self, path: str, method: str = "GET", payload: dict | None = None) -> dict | None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
        request = Request(
            f"{self.config.api_base}{path}",
            data=data,
            method=method,
            headers={"Content-Type": "application/json", "X-Agent-Token": self.config.agent_token},
        )
        try:
            with urlopen(request, timeout=20) as response:
                body = response.read()
                LOGGER.info("api_ok method=%s path=%s status=%s", method, path.split("?", 1)[0], response.status)
            return json.loads(body) if body else None
        except HTTPError as exc:
            LOGGER.error("api_http_error method=%s path=%s status=%s", method, path.split("?", 1)[0], exc.code)
            raise
        except (URLError, TimeoutError, OSError):
            LOGGER.exception("api_network_error method=%s path=%s", method, path.split("?", 1)[0])
            raise

    def heartbeat(self) -> None:
        free_disk = shutil.disk_usage(WORK_DIR.anchor or WORK_DIR).free
        computer_name = socket.gethostname()
        current_rustdesk_id = rustdesk_id()
        LOGGER.info(
            "heartbeat_start computer=%s free_disk_bytes=%s rustdesk_id_ready=%s",
            computer_name,
            free_disk,
            bool(current_rustdesk_id),
        )
        self.request(
            f"/api/agent/heartbeat?customer_id={self.config.customer_id}",
            "POST",
            {
                "agent_version": AGENT_VERSION,
                "computer_name": computer_name,
                "windows_version": platform.platform(),
                "free_disk_bytes": free_disk,
                "machine_id": machine_id(),
                "rustdesk_id": current_rustdesk_id,
            },
        )
        self.last_heartbeat = time.monotonic()
        LOGGER.info("heartbeat_ok")

    def report(self, task_id: int, status: str, log: str, actual_install_path: str = "") -> None:
        LOGGER.info("task_status task_id=%s status=%s message=%s", task_id, status, log[:160].replace("\n", " "))
        payload = {"status": status, "log": log}
        if actual_install_path:
            payload["actual_install_path"] = actual_install_path
        self.request(f"/api/agent/tasks/{task_id}/status", "PATCH", payload)

    def installer_password(self) -> str:
        response = self.request(f"/api/agent/installer-password?customer_id={self.config.customer_id}")
        password = str((response or {}).get("installer_password", ""))
        if not password:
            raise ValueError("服务器未配置安装密码")
        return password

    @staticmethod
    def installer_status_message(status: dict, message: str) -> str:
        parts = [message] if message else []
        install_path = str(status.get("actual_install_path", "")).strip()
        if install_path and install_path not in message:
            parts.append(f"实际安装路径：{install_path}")
        results = status.get("results")
        if isinstance(results, list):
            completed = []
            for result in results:
                if not isinstance(result, dict):
                    continue
                software = str(result.get("software", "")).strip()
                version = str(result.get("version", "")).strip()
                if software:
                    completed.append(f"{software} {version}".strip())
            if completed:
                parts.append(f"安装器确认：{', '.join(completed)}")
        return "；".join(parts)[:10_000]

    def cleanup_task_artifacts(self, task_id: int) -> None:
        artifacts = self.task_artifacts.get(task_id)
        if artifacts is None:
            return
        attempts, retry_at = self.artifact_cleanup_retries.get(task_id, (0, 0.0))
        if time.monotonic() < retry_at:
            return
        pending = False
        for path in artifacts:
            try:
                path.unlink(missing_ok=True)
            except OSError as exc:
                pending = True
                if attempts == 0:
                    LOGGER.warning("artifact_cleanup_deferred task_id=%s path=%s error=%s", task_id, path, exc)
        if not pending:
            self.task_artifacts.pop(task_id, None)
            self.artifact_cleanup_retries.pop(task_id, None)
            LOGGER.info("artifact_cleanup_ok task_id=%s", task_id)
            return
        attempts += 1
        delay = min(
            ARTIFACT_CLEANUP_INITIAL_DELAY_SECONDS * (2 ** min(attempts - 1, 5)),
            ARTIFACT_CLEANUP_MAX_DELAY_SECONDS,
        )
        self.artifact_cleanup_retries[task_id] = (attempts, time.monotonic() + delay)
        LOGGER.info("artifact_cleanup_scheduled task_id=%s retry_in_seconds=%s", task_id, delay)

    @staticmethod
    def stop_installer_process(process_id: object) -> None:
        try:
            process_id = int(process_id)
        except (TypeError, ValueError):
            return
        if process_id <= 0:
            return
        try:
            if os.name == "nt":
                subprocess.run(
                    ["taskkill", "/PID", str(process_id), "/T", "/F"],
                    capture_output=True,
                    timeout=15,
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            else:
                os.kill(process_id, 15)
            LOGGER.info("installer_stopped pid=%s", process_id)
        except (OSError, subprocess.SubprocessError) as exc:
            LOGGER.warning("installer_stop_deferred pid=%s error=%s", process_id, exc)

    @staticmethod
    def installation_checks(task: dict) -> list[list[Path]]:
        install_paths = CustomerAgent.resolve_install_paths(str(task.get("install_path", "")))
        versions = task.get("versions", {})
        if not isinstance(versions, dict):
            return []
        checks: list[list[Path]] = []

        def check(relative_path: str) -> list[Path]:
            return [path / relative_path for path in install_paths]

        for software in task.get("software", []):
            version = str(versions.get(software, ""))
            if software == "jdk":
                checks.append(check(f"jdk_{version}/bin/java.exe"))
            elif software == "node":
                checks.append(check(f"node_{version}/node.exe"))
            elif software == "idea":
                checks.append(check(f"idea_{version}/bin/idea64.exe"))
            elif software == "mysql":
                checks.append(check("mysql/bin/mysqld.exe"))
            elif software == "eclipse":
                checks.append(check(f"eclipse_{version}/eclipse.exe"))
            elif software == "android":
                checks.append(check(version))
            elif software == "navicat":
                checks.append(check(f"navicat_{version}/navicat.exe"))
            elif software == "tomcat":
                checks.append(check(f"tomcat_{version}/bin/catalina.bat"))
            elif software == "maven":
                checks.append(check(f"maven_{version}/bin/mvn.cmd"))
            elif software == "workbench":
                checks.append(check(f"mysql_{version}/MySQLWorkbench.exe"))
            elif software == "phpstudy":
                checks.append(check(f"phpstudy_{version}/COM/phpstudy_pro.exe"))
            elif software == "pycharm":
                checks.append(check(f"pycharm_{version}/bin/pycharm64.exe"))
            elif software == "python":
                checks.append(check(f"python_{version}/python.exe"))
            elif software == "anaconda":
                checks.append(check("anaconda/Scripts/conda.exe"))
            elif software in {"pytorch", "tensorflow"}:
                checks.append(check(f"anaconda/envs/{version}/python.exe"))
            elif software == "sqlserver":
                checks.append(check(version))
        return checks

    @staticmethod
    def missing_installation_checks(checks: list[list[Path]]) -> list[str]:
        return [str(candidates[0]) for candidates in checks if not any(path.is_file() for path in candidates)]

    @staticmethod
    def resolve_install_paths(value: str) -> list[Path]:
        """Check the requested path and installer drive fallbacks for verification."""
        candidate = Path(value.strip())
        paths: list[Path] = []
        if candidate.is_absolute() and candidate.drive:
            paths.append(candidate)
            suffix = candidate.relative_to(Path(candidate.anchor))
        else:
            suffix = candidate
        for drive in ("D:", "E:", "F:", "C:"):
            root = Path(drive + "\\")
            if root.exists():
                paths.append(root / suffix)
        paths.append(Path(os.environ.get("PROGRAMDATA", str(Path.home()))) / "RemoteInstall" / suffix)
        return list(dict.fromkeys(paths))

    @staticmethod
    def installer_destination(runner: str, task_id: int) -> Path:
        return WORK_DIR / "installers" / f"{runner}Main-task-{task_id}.exe"

    def download_installer(self, runner: str, task_id: int) -> Path:
        if runner not in RUNNERS:
            raise ValueError(f"不允许的安装器类型: {runner}")
        destination = self.installer_destination(runner, task_id)
        request = Request(
            f"{self.config.api_base}/api/agent/installers/{runner}?customer_id={self.config.customer_id}",
            headers={"X-Agent-Token": self.config.agent_token},
        )
        LOGGER.info("installer_download_start runner=%s destination=%s", runner, destination)
        try:
            with urlopen(request, timeout=120) as response:
                expected_hash = response.headers.get("X-Installer-SHA256", "").lower()
                if len(expected_hash) != 64 or any(char not in "0123456789abcdef" for char in expected_hash):
                    raise ValueError("服务器未提供有效的安装器 SHA-256")
                temporary = destination.with_suffix(".download")
                digest = hashlib.sha256()
                size = 0
                with temporary.open("wb") as output:
                    while chunk := response.read(1024 * 1024):
                        digest.update(chunk)
                        output.write(chunk)
                        size += len(chunk)
                actual_hash = digest.hexdigest().lower()
                if actual_hash != expected_hash:
                    temporary.unlink(missing_ok=True)
                    LOGGER.error("installer_hash_mismatch runner=%s size=%s", runner, size)
                    raise ValueError("安装器 SHA-256 校验失败")
                temporary.replace(destination)
                LOGGER.info("installer_download_ok runner=%s size=%s", runner, size)
        except Exception:
            LOGGER.exception("installer_download_failed runner=%s", runner)
            raise
        return destination

    def launch_installer(self, installer: Path, task_file: Path) -> None:
        args = ["--task-file", str(task_file)]
        LOGGER.info("installer_launch_start path=%s task_file=%s", installer, task_file)
        if os.name != "nt":
            subprocess.Popen([str(installer), *args], cwd=str(installer.parent))
            LOGGER.info("installer_launch_ok platform=non-windows")
            return
        result = ctypes.windll.shell32.ShellExecuteW(
            None,
            "runas",
            str(installer),
            subprocess.list2cmdline(args),
            str(installer.parent),
            1,
        )
        if result <= 32:
            LOGGER.error("installer_launch_failed shell_execute_result=%s", result)
            raise RuntimeError("客户未授权管理员权限或安装器无法启动")
        LOGGER.info("installer_launch_ok shell_execute_result=%s", result)

    def update_active_tasks(self) -> None:
        for task_id, status_file in list(self.active_tasks.items()):
            try:
                cancelled = self.request(f"/api/agent/tasks/{task_id}/cancelled?customer_id={self.config.customer_id}").get("cancelled", False)
            except Exception as exc:
                LOGGER.warning("task_cancel_check_failed task_id=%s error=%s", task_id, exc)
                cancelled = False
            if cancelled:
                try:
                    status = json.loads(status_file.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    status = {}
                self.stop_installer_process(status.get("pid"))
                self.report(task_id, "cancelled", "运营人员已取消任务，安装器已关闭")
                self.active_tasks.pop(task_id, None)
                self.last_task_status.pop(task_id, None)
                self.task_install_checks.pop(task_id, None)
                self.active_task_started_at.pop(task_id, None)
                self.cleanup_task_artifacts(task_id)
                LOGGER.info("task_cancelled_by_admin task_id=%s", task_id)
                continue
            try:
                status = json.loads(status_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            task_status = status.get("status")
            message = self.installer_status_message(status, str(status.get("message", "")))
            actual_install_path = str(status.get("actual_install_path", "")).strip()
            if task_status in {"started", "running", "success", "failed", "cancelled"}:
                current = (str(task_status), message)
                if self.last_task_status.get(task_id) != current:
                    self.report(task_id, task_status, message or "安装器状态已更新", actual_install_path)
                    self.last_task_status[task_id] = current
            if task_status in {"success", "failed", "cancelled"}:
                LOGGER.info("task_finished task_id=%s status=%s", task_id, task_status)
                self.active_tasks.pop(task_id, None)
                self.last_task_status.pop(task_id, None)
                self.task_install_checks.pop(task_id, None)
                self.active_task_started_at.pop(task_id, None)
                self.cleanup_task_artifacts(task_id)
                continue

            process_id = status.get("pid")
            if process_id and not self.process_exists(int(process_id)):
                checks = self.task_install_checks.get(task_id, [])
                missing = self.missing_installation_checks(checks)
                if checks and not missing:
                    message = "安装器已退出，已校验所有软件文件存在"
                    self.report(task_id, "success", message)
                    LOGGER.info("installer_exit_verified task_id=%s pid=%s", task_id, process_id)
                elif time.monotonic() - self.active_task_started_at.get(task_id, time.monotonic()) < INSTALLER_COMPLETION_TIMEOUT_SECONDS:
                    message = "安装器已移交提权进程，正在等待安装完成并校验文件"
                    current = ("running", message)
                    if self.last_task_status.get(task_id) != current:
                        self.report(task_id, *current)
                        self.last_task_status[task_id] = current
                    continue
                else:
                    message = "安装器进程已退出，未检测到完成状态"
                    if missing:
                        message += f"；缺少：{'；'.join(missing)}"
                    self.report(task_id, "failed", message)
                    LOGGER.error("installer_exited task_id=%s pid=%s missing=%s", task_id, process_id, missing)
                self.active_tasks.pop(task_id, None)
                self.last_task_status.pop(task_id, None)
                self.task_install_checks.pop(task_id, None)
                self.active_task_started_at.pop(task_id, None)
                self.cleanup_task_artifacts(task_id)

    @staticmethod
    def process_exists(process_id: int) -> bool:
        try:
            os.kill(process_id, 0)
            return True
        except PermissionError:
            # The installer is elevated while the Agent is not. Access denied
            # means the process is still alive, not that it has exited.
            return True
        except (OSError, ValueError):
            return False

    def start_task(self, task: dict) -> None:
        task_id = int(task["id"])
        if task.get("kind", "install") == "cleanup":
            self.cleanup_task(task_id, task.get("cleanup_targets", []))
            return
        runner = str(task.get("runner", ""))
        software = task.get("software", [])
        versions = task.get("versions", {})
        LOGGER.info("task_received task_id=%s runner=%s software=%s versions=%s", task_id, runner, software, versions)
        self.report(task_id, "running", "正在下载受控安装器")
        installer = self.download_installer(runner, task_id)
        self.report(task_id, "downloaded", "安装器下载并校验完成")

        if not isinstance(software, list) or not isinstance(versions, dict):
            raise ValueError("任务软件参数格式无效")
        task_file = WORK_DIR / "tasks" / f"task-{task_id}.json"
        status_file = WORK_DIR / "status" / f"task-{task_id}.json"
        status_file.unlink(missing_ok=True)
        task_file.write_text(
            json.dumps(
                {
                    "task_id": task_id,
                    "software": {name: versions.get(name) for name in software},
                    "install_path": task["install_path"],
                    "download_path": task["download_path"],
                    "status_file": str(status_file),
                    "installer_password": self.installer_password(),
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.task_artifacts[task_id] = (installer, task_file, status_file)
        self.task_install_checks[task_id] = self.installation_checks(task)
        self.launch_installer(installer, task_file)
        self.active_tasks[task_id] = status_file
        self.active_task_started_at[task_id] = time.monotonic()
        self.last_task_status.pop(task_id, None)
        self.report(task_id, "waiting_password", "安装器已启动，等待客户在本机确认并输入安装密码")
        LOGGER.info("task_waiting_customer task_id=%s", task_id)

    def cleanup_task(self, task_id: int, targets: object) -> None:
        if not isinstance(targets, list):
            raise ValueError("退款清理目标格式无效")
        self.report(task_id, "running", "正在永久删除后台记录的软件文件")
        removed: list[str] = []
        removed_installs: list[str] = []
        missing: list[str] = []
        failed: list[str] = []
        stopped: list[str] = []
        for target in targets:
            if not isinstance(target, dict):
                raise ValueError("退款清理目标格式无效")
            path = Path(str(target.get("path", ""))).resolve()
            root = Path(str(target.get("root", ""))).resolve()
            kind = str(target.get("kind", "install"))
            is_download_cache = kind == "download-cache"
            if (path == root and not is_download_cache) or (path != root and root not in path.parents):
                raise ValueError(f"拒绝删除非受控路径: {path}")
            label = str(target.get("label", target.get("software", path.name))).strip()
            if not path.exists():
                if kind == "install":
                    missing.append(f"{label}（{path}）")
                continue
            try:
                stopped.extend(self.stop_processes_under(path))
                if path.is_dir():
                    shutil.rmtree(path)
                else:
                    path.unlink()
                if path.exists():
                    raise OSError("删除后路径仍存在")
            except OSError as exc:
                failed.append(f"{label}（{path}）：{exc}")
                LOGGER.warning("cleanup_failed task_id=%s path=%s error=%s", task_id, path, exc)
                continue
            removed.append(f"{label}（{path}）")
            if kind == "install":
                removed_installs.append(f"{label}（{path}）")
            LOGGER.info("cleanup_removed task_id=%s path=%s", task_id, path)
        if failed:
            self.report(task_id, "failed", f"退款清理失败：{'；'.join(failed)}")
        elif not removed_installs and not missing:
            self.report(task_id, "failed", "退款清理未执行：未发现可清理的软件目录")
        else:
            details = []
            if stopped:
                details.append(f"已关闭占用进程：{'；'.join(stopped)}")
            if removed:
                details.append(f"已永久删除：{'；'.join(removed)}")
            if missing:
                details.append(f"以下目标已不存在：{'；'.join(missing)}")
            self.report(task_id, "success", f"退款清理完成；{'；'.join(details)}")

    @staticmethod
    def stop_processes_under(root: Path) -> list[str]:
        stopped: list[str] = []
        root = root.resolve()
        processes = []
        for process in psutil.process_iter(["pid", "name", "exe"]):
            try:
                executable = process.info.get("exe")
                if not executable:
                    continue
                executable_path = Path(executable).resolve()
                if executable_path != root and root not in executable_path.parents:
                    continue
                processes.append(process)
            except (OSError, psutil.Error):
                continue
        for process in processes:
            try:
                process.terminate()
                stopped.append(f"{process.info.get('name') or '未知进程'} (PID {process.pid})")
            except (OSError, psutil.Error) as exc:
                LOGGER.warning("cleanup_process_terminate_failed pid=%s error=%s", process.pid, exc)
        gone, alive = psutil.wait_procs(processes, timeout=5)
        for process in alive:
            try:
                process.kill()
                LOGGER.info("cleanup_process_killed pid=%s", process.pid)
            except (OSError, psutil.Error) as exc:
                LOGGER.warning("cleanup_process_kill_failed pid=%s error=%s", process.pid, exc)
        return stopped

    def run_once(self) -> None:
        for task_id in list(self.task_artifacts):
            if task_id not in self.active_tasks:
                self.cleanup_task_artifacts(task_id)
        if time.monotonic() - self.last_heartbeat >= HEARTBEAT_SECONDS:
            self.heartbeat()
        self.update_active_tasks()
        if self.active_tasks:
            return
        task = self.request(f"/api/agent/tasks?customer_id={self.config.customer_id}")
        if task:
            try:
                self.start_task(task)
            except Exception as exc:
                task_id = int(task.get("id", 0))
                message = f"客户 Agent 执行失败: {type(exc).__name__}: {exc}"
                LOGGER.exception("task_start_failed task_id=%s", task_id)
                if task_id:
                    self.report(task_id, "failed", message)
                raise
        elif time.monotonic() - self.last_empty_poll_log >= 60:
            LOGGER.info("task_poll_empty")
            self.last_empty_poll_log = time.monotonic()


def main() -> None:
    LOGGER.info("agent_start version=%s pid=%s", AGENT_VERSION, os.getpid())
    ensure_agent_startup()
    agent = CustomerAgent(load_config())
    print(f"Customer Agent connected to {agent.config.api_base}")
    while True:
        try:
            agent.run_once()
        except Exception as exc:
            LOGGER.exception("agent_loop_error type=%s", type(exc).__name__)
            print(f"agent error: {exc}")
        time.sleep(POLL_SECONDS)


if __name__ == "__main__":
    main()
