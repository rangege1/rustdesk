from __future__ import annotations

import ctypes
import hashlib
import json
import logging
import logging.handlers
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen


AGENT_VERSION = "0.2.0"
POLL_SECONDS = 3
HEARTBEAT_SECONDS = 60
RUNNERS = {"java", "python"}


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


DEFAULT_CONFIG = executable_dir() / "agent-config.json"
CONFIG_FILE = Path(os.environ.get("OPS_AGENT_CONFIG", DEFAULT_CONFIG))
WORK_DIR = Path(os.environ.get("PROGRAMDATA", os.environ.get("LOCALAPPDATA", str(executable_dir())))) / "RemoteInstall" / "agent"
LOG_DIR = WORK_DIR / "logs"
LOG_FILE = LOG_DIR / "customer-agent.log"


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


@dataclass(frozen=True)
class AgentConfig:
    api_base: str
    customer_id: int
    agent_token: str
    installer_password: str = ""


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
    installer_password = str(raw.get("installer_password", os.environ.get("OPS_INSTALLER_PASSWORD", "")))
    return AgentConfig(api_base, customer_id, agent_token, installer_password)


class CustomerAgent:
    def __init__(self, config: AgentConfig) -> None:
        self.config = config
        self.active_tasks: dict[int, Path] = {}
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
        LOGGER.info("heartbeat_start computer=%s free_disk_bytes=%s", computer_name, free_disk)
        self.request(
            f"/api/agent/heartbeat?customer_id={self.config.customer_id}",
            "POST",
            {
                "agent_version": AGENT_VERSION,
                "computer_name": computer_name,
                "windows_version": platform.platform(),
                "free_disk_bytes": free_disk,
                "machine_id": machine_id(),
            },
        )
        self.last_heartbeat = time.monotonic()
        LOGGER.info("heartbeat_ok")

    def report(self, task_id: int, status: str, log: str) -> None:
        LOGGER.info("task_status task_id=%s status=%s message=%s", task_id, status, log[:160].replace("\n", " "))
        self.request(f"/api/agent/tasks/{task_id}/status", "PATCH", {"status": status, "log": log})

    def download_installer(self, runner: str) -> Path:
        if runner not in RUNNERS:
            raise ValueError(f"不允许的安装器类型: {runner}")
        destination = WORK_DIR / "installers" / f"{runner}Main.exe"
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
                status = json.loads(status_file.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                continue
            task_status = status.get("status")
            message = str(status.get("message", ""))[:10_000]
            if task_status in {"started", "running", "success", "failed"}:
                self.report(task_id, task_status, message or "安装器状态已更新")
            if task_status in {"success", "failed"}:
                LOGGER.info("task_finished task_id=%s status=%s", task_id, task_status)
                self.active_tasks.pop(task_id, None)

    def start_task(self, task: dict) -> None:
        task_id = int(task["id"])
        runner = str(task.get("runner", ""))
        software = task.get("software", [])
        versions = task.get("versions", {})
        LOGGER.info("task_received task_id=%s runner=%s software=%s versions=%s", task_id, runner, software, versions)
        self.report(task_id, "running", "正在下载受控安装器")
        installer = self.download_installer(runner)
        self.report(task_id, "downloaded", "安装器下载并校验完成")

        if not isinstance(software, list) or not isinstance(versions, dict):
            raise ValueError("任务软件参数格式无效")
        task_file = WORK_DIR / "tasks" / f"task-{task_id}.json"
        status_file = WORK_DIR / "status" / f"task-{task_id}.json"
        status_file.unlink(missing_ok=True)
        task_file.write_text(
            json.dumps(
                {
                    "software": {name: versions.get(name) for name in software},
                    "install_path": task["install_path"],
                    "download_path": task["download_path"],
                    "status_file": str(status_file),
                    "installer_password": self.config.installer_password,
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        self.launch_installer(installer, task_file)
        self.active_tasks[task_id] = status_file
        self.report(task_id, "waiting_password", "安装器已启动，等待客户在本机确认并输入安装密码")
        LOGGER.info("task_waiting_customer task_id=%s", task_id)

    def run_once(self) -> None:
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
