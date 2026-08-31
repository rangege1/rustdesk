from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
import time
import ctypes
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen


def load_config() -> dict:
    candidates = [Path(sys.executable).with_name("worker-config.json"), Path(__file__).with_name("worker-config.json")]
    for path in candidates:
        if path.exists():
            with path.open(encoding="utf-8") as stream:
                return json.load(stream)
    return {}


CONFIG = load_config()
API_BASE = os.environ.get("OPS_API", CONFIG.get("api_base", "http://127.0.0.1:8788")).rstrip("/")
WORKER_TOKEN = os.environ.get("OPS_WORKER_TOKEN", CONFIG.get("worker_token", "local-worker"))
CONNECT_FLAG = os.environ.get("RUSTDESK_CONNECT_FLAG", CONFIG.get("rustdesk_connect_flag", "--connect"))
CONNECT_TIMEOUT_SECONDS = int(os.environ.get("RUSTDESK_CONNECT_TIMEOUT", CONFIG.get("rustdesk_connect_timeout", 45)))
POLL_SECONDS = float(os.environ.get("OPS_WORKER_POLL_SECONDS", CONFIG.get("poll_seconds", 1)))
SESSION_CHECK_SECONDS = 0.25
LOG_FILE = Path(os.environ.get("REMOTE_INSTALL_WORKER_LOG", Path(sys.executable).with_name("rustdesk-worker.log")))
WORKER_VERSION = "1.1.0"
WORKER_ID = int(CONFIG.get("worker_id", 0) or 0)
HEARTBEAT_SECONDS = 15


def log(message: str) -> None:
    line = f"{datetime.now():%Y-%m-%d %H:%M:%S} {message}"
    try:
        LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a", encoding="utf-8") as stream:
            stream.write(f"{line}\n")
    except OSError:
        pass
    print(line, flush=True)


def find_rustdesk() -> str:
    configured = os.environ.get("RUSTDESK_EXE", CONFIG.get("rustdesk_exe"))
    if configured:
        if Path(configured).exists():
            role_file = Path(configured).with_name("ops-client-role.txt")
            if role_file.exists() and role_file.read_text(encoding="utf-8").strip() != "staff":
                raise RuntimeError(f"配置的 RustDesk 不是客服版: {configured}")
            return configured
        raise FileNotFoundError(f"未找到配置的客服端 RustDesk: {configured}")
    candidates = [
        r"C:\Program Files\RemoteInstallStaff\rustdesk.exe",
        r"D:\code\bishebao\代码生成测试\day2\customer-rustdesk-windows-x64 (1)\remote-install-staff-x86_64.exe",
        r"C:\Program Files\RustDesk\rustdesk.exe",
        r"C:\Program Files (x86)\RustDesk\rustdesk.exe",
        os.path.expandvars(r"%LOCALAPPDATA%\RustDesk\rustdesk.exe"),
    ]
    for candidate in candidates:
        if candidate and (Path(candidate).exists() or candidate == "rustdesk.exe"):
            return candidate
    raise FileNotFoundError("未找到 rustdesk.exe，请设置 RUSTDESK_EXE")


def worker_machine_id() -> str:
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_LOCAL_MACHINE, r"SOFTWARE\Microsoft\Cryptography") as key:
            machine_guid = winreg.QueryValueEx(key, "MachineGuid")[0]
        if machine_guid:
            return hashlib.sha256(f"RemoteInstallStaff:{machine_guid}".encode()).hexdigest()
    except OSError:
        pass
    return hashlib.sha256(f"RemoteInstallStaff:{os.environ.get('COMPUTERNAME', 'unknown')}".encode()).hexdigest()


def close_dashboard_windows() -> None:
    """Close only RustDesk's idle dashboard so the CLI startup path owns the connection."""
    user32 = ctypes.windll.user32
    kernel32 = ctypes.windll.kernel32
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    closed = False

    def visit(hwnd, _):
        nonlocal closed
        if not user32.IsWindowVisible(hwnd):
            return True
        size = user32.GetWindowTextLengthW(hwnd)
        if not size:
            return True
        title = ctypes.create_unicode_buffer(size + 1)
        user32.GetWindowTextW(hwnd, title, len(title))
        if title.value not in {"RustDesk", "远程安装客户端", "远程安装客服端"}:
            return True
        pid = ctypes.c_ulong()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        handle = kernel32.OpenProcess(1, False, pid.value)
        if handle:
            kernel32.TerminateProcess(handle, 0)
            kernel32.CloseHandle(handle)
            closed = True
        return True

    user32.EnumWindows(callback_type(visit), 0)
    if closed:
        time.sleep(0.5)


def session_window_handles(rustdesk_id: str) -> set[int]:
    user32 = ctypes.windll.user32
    expected = rustdesk_id.replace(" ", "")
    handles: set[int] = set()
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)

    def visit(hwnd, _):
        if not user32.IsWindowVisible(hwnd):
            return True
        size = user32.GetWindowTextLengthW(hwnd)
        if not size:
            return True
        title = ctypes.create_unicode_buffer(size + 1)
        user32.GetWindowTextW(hwnd, title, len(title))
        normalized = title.value.replace(" ", "")
        # The custom Windows client uses the peer ID as its tab/window title;
        # the upstream English title is not guaranteed to be present.
        if expected in normalized and title.value not in {"RustDesk", "远程安装客服端", "远程安装客户端"}:
            handles.add(int(hwnd))
        return True

    user32.EnumWindows(callback_type(visit), 0)
    return handles


def session_window_open(rustdesk_id: str) -> bool:
    """RustDesk can reuse an existing peer window for a new CLI connection."""
    return bool(session_window_handles(rustdesk_id))


def connection_error_detail() -> str:
    user32 = ctypes.windll.user32
    callback_type = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.c_void_p, ctypes.c_void_p)
    markers = (
        "Incoming only mode",
        "Key mismatch",
        "Connection Error",
        "连接错误",
        "密钥不匹配",
        "传入模式",
        "会话已结束",
        "Disconnected",
    )
    found = ""

    def read_text(hwnd: int) -> str:
        size = user32.GetWindowTextLengthW(hwnd)
        if not size:
            return ""
        text = ctypes.create_unicode_buffer(size + 1)
        user32.GetWindowTextW(hwnd, text, len(text))
        return text.value

    def visit_child(hwnd, _):
        nonlocal found
        text = read_text(hwnd)
        if any(marker.lower() in text.lower() for marker in markers):
            found = text
            return False
        return True

    def visit(hwnd, _):
        nonlocal found
        if not user32.IsWindowVisible(hwnd):
            return True
        text = read_text(hwnd)
        if any(marker.lower() in text.lower() for marker in markers):
            found = text
            return False
        user32.EnumChildWindows(hwnd, callback_type(visit_child), 0)
        if found:
            return False
        return True

    user32.EnumWindows(callback_type(visit), 0)
    return found


def connection_error_open() -> bool:
    return bool(connection_error_detail())


def connect_rustdesk(rustdesk_id: str, password: str, on_started, should_cancel, mode: str = "remote") -> bool | None:
    close_dashboard_windows()
    executable = find_rustdesk()
    if Path(executable).name.lower().startswith("remote-install-"):
        raise RuntimeError("RUSTDESK_EXE 指向了安装启动器，请改为安装目录中的 rustdesk.exe")
    connect_flag = "--file-transfer" if mode == "file_transfer" else CONNECT_FLAG
    log(f"connect_start id={rustdesk_id} executable={executable} flag={connect_flag}")
    process = subprocess.Popen([executable, connect_flag, rustdesk_id, "--password", password], close_fds=True)
    on_started()
    deadline = time.monotonic() + CONNECT_TIMEOUT_SECONDS
    next_cancel_check = 0.0
    while time.monotonic() < deadline:
        if time.monotonic() >= next_cancel_check:
            next_cancel_check = time.monotonic() + 1
            if should_cancel():
                process.terminate()
                log(f"connect_cancelled id={rustdesk_id}")
                return None
        if connection_error_open():
            log(f"connect_error id={rustdesk_id} detail={connection_error_detail()}")
            return False
        if session_window_open(rustdesk_id):
            log(f"connect_success id={rustdesk_id}")
            return True
        time.sleep(SESSION_CHECK_SECONDS)
    log(f"connect_timeout id={rustdesk_id} timeout={CONNECT_TIMEOUT_SECONDS}")
    return False


def request(path: str, method: str = "GET", payload: dict | None = None):
    data = json.dumps(payload).encode() if payload is not None else None
    headers = {"Content-Type": "application/json", "X-Worker-Token": WORKER_TOKEN}
    if WORKER_ID:
        headers["X-Worker-Id"] = str(WORKER_ID)
    req = Request(f"{API_BASE}{path}", data=data, method=method, headers=headers)
    with urlopen(req, timeout=15) as response:
        return json.loads(response.read())


def register_worker() -> None:
    global WORKER_ID
    worker = request(
        "/api/worker/register",
        "POST",
        {
            "machine_id": worker_machine_id(),
            "computer_name": CONFIG.get("staff_name") or os.environ.get("COMPUTERNAME", "客服电脑"),
            "worker_version": WORKER_VERSION,
        },
    )
    WORKER_ID = int(worker["id"])
    log(f"worker_registered worker_id={WORKER_ID}")


def heartbeat_worker() -> None:
    request("/api/worker/heartbeat", "POST")


def run_once() -> None:
    connection = request("/api/worker/connection-tasks")
    if not connection:
        return
    mode = connection.get("mode", "remote")
    action = "文件传输" if mode == "file_transfer" else "远程连接"
    log(f"task_received task_id={connection['id']} customer_id={connection['customer_id']} mode={mode}")
    try:
        def mark_rustdesk_started() -> None:
            request(
                f"/api/worker/connection-tasks/{connection['id']}",
                "PATCH",
                {"status": "running", "phase": "rustdesk_started", "log": f"RustDesk 已启动，正在建立{action}会话"},
            )

        def connection_cancelled() -> bool:
            try:
                response = request(f"/api/worker/connection-tasks/{connection['id']}/cancelled")
                return bool((response or {}).get("cancelled"))
            except Exception as exc:
                log(f"cancel_check_failed task_id={connection['id']} type={type(exc).__name__}")
                return False

        connected = connect_rustdesk(
            str(connection["rustdesk_id"]),
            connection["rustdesk_password"],
            mark_rustdesk_started,
            connection_cancelled,
            mode,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        log(f"connect_start_failed task_id={connection['id']} type={type(exc).__name__}")
        request(
            f"/api/worker/connection-tasks/{connection['id']}",
            "PATCH",
            {"status": "failed", "log": f"无法启动客服电脑上的 RustDesk: {exc}"},
        )
        return
    if connected is None:
        log(f"task_cancelled task_id={connection['id']}")
    elif connected:
        log(f"task_success task_id={connection['id']}")
        request(
            f"/api/worker/connection-tasks/{connection['id']}",
            "PATCH",
            {"status": "success", "phase": "session_established", "log": f"客服电脑已建立 RustDesk {action}会话。"},
        )
    else:
        detail = connection_error_detail()
        log(f"task_failed task_id={connection['id']} detail={detail or 'timeout'}")
        request(
            f"/api/worker/connection-tasks/{connection['id']}",
            "PATCH",
            {"status": "failed", "log": detail or f"RustDesk 已启动，但 {CONNECT_TIMEOUT_SECONDS} 秒内未检测到远程会话。请检查客户是否在线、ID 和临时密码是否正确。"},
        )


if __name__ == "__main__":
    log(f"worker_start api={API_BASE} executable={os.environ.get('RUSTDESK_EXE', 'auto')} timeout={CONNECT_TIMEOUT_SECONDS}")
    next_heartbeat = 0.0
    while True:
        try:
            if not WORKER_ID:
                register_worker()
            if time.monotonic() >= next_heartbeat:
                heartbeat_worker()
                next_heartbeat = time.monotonic() + HEARTBEAT_SECONDS
            run_once()
        except Exception as exc:
            log(f"worker_error type={type(exc).__name__}")
        time.sleep(POLL_SECONDS)
