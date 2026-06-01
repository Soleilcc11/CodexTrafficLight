#!/usr/bin/env python3
"""
Codex menu-bar traffic light for macOS.

- Green: a Codex session is active.
- Yellow: Codex is waiting for a permission approval.
- Red: the selected session/project is idle or ended.
"""
import atexit
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path

import rumps

if getattr(sys, "frozen", False):
    os.chdir(os.path.dirname(sys.executable))


# ---------- Config ----------
CODEX_HOME = Path(os.environ.get("CODEX_HOME", "~/.codex")).expanduser()
BASE_DIR = CODEX_HOME / "traffic_light"
STATE_DIR = BASE_DIR / "state"
CONFIG_PATH = CODEX_HOME / "config.toml"
BACKUP_PATH = BASE_DIR / "config_backup.toml"
SELECTED_FILE = BASE_DIR / "selected_project"
NOTIFY_BRIDGE = BASE_DIR / "codex_notify_bridge.py"
TURN_ENDED_FILE = STATE_DIR / "turn_ended.json"
SESSIONS_DIR = CODEX_HOME / "sessions"
SESSION_INDEX = CODEX_HOME / "session_index.jsonl"
HARDWARE_LOG = BASE_DIR / "hardware.log"

POLL_INTERVAL = 0.5
BLINK_INTERVAL = 0.5
MENU_REFRESH_INTERVAL = 2
ACTIVE_GRACE_SECONDS = 20
BLE_SEND_DEBOUNCE_SECONDS = 2.5
BLE_SCAN_TIMEOUT_SECONDS = 6.0

BLE_DEVICE_NAME = "CursorLight"
BLE_MODE_CHAR_UUID = "b8b7e002-7a6b-4f4f-9a8b-11c0ffee0001"

HARDWARE_MODE_BY_STATE = {
    "thinking": "green",
    "generating": "thinking",
    "review_request": "busy",
    "success": "red_blink_5",
    "error": "error",
    "idle": "traffic",
    "off": "off",
}

TRAFFIC_MARKER = "codex_traffic_light_app"

LIGHT_ON = {"red": "🔴", "yellow": "🟡", "green": "🟢"}
LIGHT_OFF = "⚫"


@dataclass
class ProjectStatus:
    name: str
    cwd: str
    session_id: str
    session_file: Path
    state: str
    hardware_state: str
    hardware_mode: str
    updated_at: float
    model: str = "未知"
    thread_name: str = ""


def _now() -> float:
    return time.time()


def _safe_project_name(cwd: str, session_id: str = "") -> str:
    if not cwd:
        return session_id or "default"
    name = Path(cwd).name or cwd.strip("/").replace("/", "-")
    return name or session_id or "default"


def _read_jsonl_tail(path: Path, max_lines: int = 240):
    try:
        lines = path.read_text(errors="ignore").splitlines()
    except Exception:
        return []
    events = []
    for line in lines[-max_lines:]:
        try:
            events.append(json.loads(line))
        except Exception:
            continue
    return events


def _iter_session_files():
    try:
        files = list(SESSIONS_DIR.glob("**/*.jsonl"))
    except Exception:
        return []
    return sorted(files, key=lambda p: p.stat().st_mtime if p.exists() else 0, reverse=True)


def _thread_names():
    names = {}
    if not SESSION_INDEX.exists():
        return names
    for event in _read_jsonl_tail(SESSION_INDEX, max_lines=1000):
        sid = event.get("id")
        if sid:
            names[sid] = event.get("thread_name", "")
    return names


def _parse_tool_args(raw):
    if not raw:
        return {}
    if isinstance(raw, dict):
        return raw
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _is_permission_call(payload):
    args = _parse_tool_args(payload.get("arguments"))
    if args.get("sandbox_permissions") == "require_escalated":
        return True
    text = json.dumps(args, ensure_ascii=False).lower()
    return any(token in text for token in ("require_escalated", "approval", "permission"))


def _tool_output_failed(output: str) -> bool:
    if not output:
        return False
    patterns = (
        r"Process exited with code [1-9]\d*",
        r"exec_command failed",
        r"Traceback \(most recent call last\)",
        r"PermissionError",
        r"Rejected\(",
        r"rejected by user",
        r"fatal:",
        r"ERROR:",
    )
    return any(re.search(pattern, output, re.IGNORECASE) for pattern in patterns)


def _menu_state_for_hardware_state(hardware_state: str) -> str:
    if hardware_state in ("thinking", "generating"):
        return "green"
    if hardware_state == "review_request":
        return "yellow"
    return "red"


def _session_status(path: Path, thread_names: dict) -> ProjectStatus | None:
    events = _read_jsonl_tail(path)
    if not events:
        return None

    meta = None
    pending_tool_calls = set()
    pending_permission_calls = set()
    last_phase = None
    last_ts = path.stat().st_mtime
    latest_user_ts = 0.0
    latest_final_ts = 0.0
    latest_assistant_ts = 0.0
    recent_tool_failed = False

    for event in events:
        payload = event.get("payload", {})
        event_ts = _parse_timestamp(event.get("timestamp")) or last_ts
        last_ts = max(last_ts, event_ts)

        if event.get("type") == "session_meta" and not meta:
            meta = payload
            continue

        if event.get("type") != "response_item":
            if payload.get("type") == "user_message":
                latest_user_ts = max(latest_user_ts, event_ts)
                recent_tool_failed = False
            continue

        payload_type = payload.get("type")
        if payload_type == "function_call":
            call_id = payload.get("call_id")
            pending_tool_calls.add(call_id)
            if _is_permission_call(payload):
                pending_permission_calls.add(call_id)
        elif payload_type == "function_call_output":
            call_id = payload.get("call_id")
            pending_tool_calls.discard(call_id)
            pending_permission_calls.discard(call_id)
            if event_ts >= latest_user_ts and _tool_output_failed(payload.get("output", "")):
                recent_tool_failed = True
        elif payload_type == "message":
            role = payload.get("role")
            phase = payload.get("phase")
            if role == "user":
                latest_user_ts = max(latest_user_ts, event_ts)
                recent_tool_failed = False
            elif role == "assistant":
                latest_assistant_ts = max(latest_assistant_ts, event_ts)
                last_phase = phase
                if phase == "final":
                    latest_final_ts = max(latest_final_ts, event_ts)

    meta = meta or {}
    session_id = meta.get("id") or path.stem.rsplit("-", 1)[-1]
    cwd = meta.get("cwd") or ""
    name = _safe_project_name(cwd, session_id)

    if pending_permission_calls:
        hardware_state = "review_request"
    elif pending_tool_calls:
        hardware_state = "generating"
    elif latest_final_ts and latest_final_ts >= latest_user_ts and last_phase == "final":
        hardware_state = "error" if recent_tool_failed else "success"
    elif _now() - last_ts <= ACTIVE_GRACE_SECONDS and latest_assistant_ts > latest_user_ts:
        hardware_state = "generating"
    elif _now() - last_ts <= ACTIVE_GRACE_SECONDS:
        hardware_state = "thinking"
    else:
        hardware_state = "idle"

    state = _menu_state_for_hardware_state(hardware_state)
    hardware_mode = HARDWARE_MODE_BY_STATE[hardware_state]

    return ProjectStatus(
        name=name,
        cwd=cwd,
        session_id=session_id,
        session_file=path,
        state=state,
        hardware_state=hardware_state,
        hardware_mode=hardware_mode,
        updated_at=last_ts,
        model=meta.get("model") or meta.get("model_provider") or "未知",
        thread_name=thread_names.get(session_id, ""),
    )


def _parse_timestamp(value):
    if not value:
        return None
    try:
        from datetime import datetime

        return datetime.fromisoformat(value.replace("Z", "+00:00")).timestamp()
    except Exception:
        return None


def list_active_projects():
    """Return the freshest known Codex session per project cwd."""
    thread_names = _thread_names()
    by_key = {}
    for path in _iter_session_files()[:80]:
        status = _session_status(path, thread_names)
        if not status:
            continue
        key = status.cwd or status.session_id
        current = by_key.get(key)
        if not current or status.updated_at > current.updated_at:
            by_key[key] = status
    return sorted(by_key.values(), key=lambda s: s.updated_at, reverse=True)


def get_selected_project():
    try:
        if SELECTED_FILE.exists():
            selected = SELECTED_FILE.read_text().strip()
            if selected:
                return selected
    except Exception:
        pass
    projects = list_active_projects()
    return projects[0].name if projects else "default"


def set_selected_project(project_name):
    try:
        SELECTED_FILE.parent.mkdir(parents=True, exist_ok=True)
        SELECTED_FILE.write_text(project_name)
    except Exception:
        pass


def selected_status(project_name):
    projects = list_active_projects()
    if not projects:
        return None
    for project in projects:
        if project.name == project_name:
            return project
    return projects[0]


def backup_config():
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    if CONFIG_PATH.exists() and not BACKUP_PATH.exists():
        shutil.copy2(CONFIG_PATH, BACKUP_PATH)
        print(f"已备份 Codex 配置: {BACKUP_PATH}")


def restore_config():
    if BACKUP_PATH.exists():
        try:
            shutil.copy2(BACKUP_PATH, CONFIG_PATH)
            BACKUP_PATH.unlink()
            print(f"已还原 Codex 配置: {CONFIG_PATH}")
        except Exception as exc:
            print(f"还原 Codex 配置失败: {exc}")


def _write_notify_bridge():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    bridge = f"""#!/usr/bin/env python3
import json
import os
import subprocess
import time
from pathlib import Path

BASE_DIR = Path({str(BASE_DIR)!r})
BACKUP_PATH = Path({str(BACKUP_PATH)!r})
TURN_ENDED_FILE = Path({str(TURN_ENDED_FILE)!r})

def original_notify():
    if not BACKUP_PATH.exists():
        return []
    try:
        import tomllib

        with BACKUP_PATH.open("rb") as fh:
            notify = tomllib.load(fh).get("notify", [])
        return notify if isinstance(notify, list) else []
    except Exception:
        return []

TURN_ENDED_FILE.parent.mkdir(parents=True, exist_ok=True)
TURN_ENDED_FILE.write_text(json.dumps({{"timestamp": time.time(), "marker": {TRAFFIC_MARKER!r}}}))
cmd = original_notify()
if cmd:
    try:
        subprocess.Popen(cmd)
    except Exception:
        pass
"""
    NOTIFY_BRIDGE.write_text(bridge)
    mode = NOTIFY_BRIDGE.stat().st_mode
    NOTIFY_BRIDGE.chmod(mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)


def configure_codex_notify():
    """Install a reversible Codex notify bridge.

    Codex currently exposes a turn-ended notify hook rather than Claude Code's
    full hook matrix. Live/yellow states are inferred from session JSONL files;
    the notify bridge gives the app a reliable red-light signal after a turn.
    """
    BASE_DIR.mkdir(parents=True, exist_ok=True)
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    backup_config()
    _write_notify_bridge()

    config_text = CONFIG_PATH.read_text(errors="ignore") if CONFIG_PATH.exists() else ""
    desired = f'notify = ["{NOTIFY_BRIDGE}"] # {TRAFFIC_MARKER}'
    if TRAFFIC_MARKER in config_text:
        return
    if re.search(r"(?m)^notify\s*=", config_text):
        config_text = re.sub(r"(?m)^notify\s*=.*$", desired, config_text, count=1)
    else:
        config_text = desired + "\n" + config_text
    CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
    CONFIG_PATH.write_text(config_text)
    print(f"Codex notify 已配置: {CONFIG_PATH}")


class HardwareLightController:
    """Best-effort BLE output for ESP32-C3 CursorLight firmware."""

    def __init__(self):
        self.last_mode = None
        self.last_sent_at = 0.0
        self.inflight = False
        self.lock = threading.Lock()

    def request_mode(self, mode: str):
        if mode not in {"red", "yellow", "green", "busy", "error", "thinking", "ai", "success", "traffic", "alarm", "demo", "off", "red_blink_5"}:
            return

        now = time.time()
        with self.lock:
            if self.inflight:
                return
            if mode == self.last_mode:
                return
            if (now - self.last_sent_at) < BLE_SEND_DEBOUNCE_SECONDS:
                return
            self.inflight = True

        thread = threading.Thread(target=self._send_mode_worker, args=(mode,), daemon=True)
        thread.start()

    def _send_mode_worker(self, mode: str):
        try:
            self._send_mode(mode)
            with self.lock:
                self.last_mode = mode
                self.last_sent_at = time.time()
            self._log(f"sent mode={mode}")
        except Exception as exc:
            self._log(f"skip mode={mode}: {exc}")
        finally:
            with self.lock:
                self.inflight = False

    def _send_mode(self, mode: str):
        import asyncio

        try:
            from bleak import BleakClient, BleakScanner
        except Exception as exc:
            raise RuntimeError(f"bleak unavailable ({exc})") from exc

        async def write_mode():
            device = await BleakScanner.find_device_by_name(BLE_DEVICE_NAME, timeout=BLE_SCAN_TIMEOUT_SECONDS)
            if device is None:
                raise RuntimeError(f"BLE device not found: {BLE_DEVICE_NAME}")
            async with BleakClient(device) as client:
                if not client.is_connected:
                    raise RuntimeError("BLE connection failed")
                if mode == "red_blink_5":
                    for _ in range(5):
                        await client.write_gatt_char(BLE_MODE_CHAR_UUID, b"red", response=True)
                        await asyncio.sleep(0.22)
                        await client.write_gatt_char(BLE_MODE_CHAR_UUID, b"off", response=True)
                        await asyncio.sleep(0.18)
                    await client.write_gatt_char(BLE_MODE_CHAR_UUID, b"traffic", response=True)
                else:
                    await client.write_gatt_char(BLE_MODE_CHAR_UUID, mode.encode("utf-8"), response=True)

        asyncio.run(write_mode())

    def _log(self, message: str):
        try:
            BASE_DIR.mkdir(parents=True, exist_ok=True)
            stamp = time.strftime("%Y-%m-%d %H:%M:%S")
            HARDWARE_LOG.open("a", encoding="utf-8").write(f"[{stamp}] {message}\n")
        except Exception:
            pass


class TrafficLightApp(rumps.App):
    def __init__(self):
        super().__init__("", quit_button="退出")
        self.state = "red"
        self.blink_on = True
        self.selected_project = get_selected_project()
        self.last_projects = []
        self.last_menu_build_time = 0
        self.current_status = None
        self.hardware = HardwareLightController()
        self.hardware.request_mode("demo")

        rumps.Timer(self.check_state, POLL_INTERVAL).start()
        rumps.Timer(self.blink, BLINK_INTERVAL).start()

        self._build_menu()
        self.update_display()

    def _build_menu(self):
        self.menu.clear()

        projects = list_active_projects()
        project_menu = rumps.MenuItem("📁 选择项目")
        if not projects:
            item = rumps.MenuItem("  (无 Codex 会话)")
            item.set_callback(None)
            project_menu.add(item)
        else:
            for project in projects:
                label = f"  {project.name}"
                if project.thread_name:
                    label += f" · {project.thread_name}"
                item = rumps.MenuItem(label)
                item._project_name = project.name
                item.set_callback(self._on_select_project)
                if project.name == self.selected_project:
                    item.state = True
                project_menu.add(item)
        self.menu.add(project_menu)

        status = selected_status(self.selected_project)
        self.current_status = status
        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("📊 当前 Codex 项目", callback=None))
        self.menu.add(rumps.MenuItem(f"  项目: {self.selected_project}"))
        self.menu.add(rumps.MenuItem(f"  模型: {status.model if status else '未知'}"))
        self.menu.add(rumps.MenuItem(f"  硬件状态: {status.hardware_state if status else 'off'}"))
        self.menu.add(rumps.MenuItem(f"  硬件模式: {status.hardware_mode if status else HARDWARE_MODE_BY_STATE['off']}"))
        if status and status.cwd:
            self.menu.add(rumps.MenuItem(f"  路径: {status.cwd}"))

        self.menu.add(rumps.separator)
        self.menu.add(rumps.MenuItem("状态说明", callback=None))
        self.menu.add(rumps.MenuItem("🟢 绿灯 - 思考状态"))
        self.menu.add(rumps.MenuItem("🟡 黄灯闪烁 - 请求审查"))
        self.menu.add(rumps.MenuItem("🔴 红灯 - 完成闪烁 / 失败 / 空闲"))
        self.menu.add(rumps.MenuItem("硬件: green/thinking/busy/red_blink_5/traffic"))

        self.last_projects = [(p.name, p.session_id, p.state, p.hardware_state, p.hardware_mode) for p in projects]
        self.last_menu_build_time = time.time()

    def _on_select_project(self, sender):
        self.selected_project = getattr(sender, "_project_name", sender.title.strip().split(" · ", 1)[0])
        set_selected_project(self.selected_project)
        self.state = "red"
        self.blink_on = True
        self._build_menu()
        self.update_display()

    def check_state(self, _):
        status = selected_status(self.selected_project)
        if status:
            if status.name != self.selected_project:
                self.selected_project = status.name
                set_selected_project(self.selected_project)
            self.current_status = status
            self._set_state(status.state)
            self.hardware.request_mode(status.hardware_mode)
        else:
            self._set_state("red")
            self.hardware.request_mode(HARDWARE_MODE_BY_STATE["off"])

        now = time.time()
        if now - self.last_menu_build_time > MENU_REFRESH_INTERVAL:
            projects = list_active_projects()
            snapshot = [(p.name, p.session_id, p.state, p.hardware_state, p.hardware_mode) for p in projects]
            if snapshot != self.last_projects:
                self._build_menu()

    def _set_state(self, new_state):
        if self.state != new_state:
            self.state = new_state
            self.blink_on = True
            self.update_display()

    def blink(self, _):
        self.blink_on = not self.blink_on
        self.update_display()

    def update_display(self):
        lights = [LIGHT_OFF, LIGHT_OFF, LIGHT_OFF]
        if self.state == "green":
            lights[2] = LIGHT_ON["green"]
        elif self.state == "yellow":
            lights[1] = LIGHT_ON["yellow"] if self.blink_on else LIGHT_OFF
        else:
            lights[0] = LIGHT_ON["red"]
        self.title = " ".join(lights)


def main():
    print("正在配置 Codex 状态监视器...")
    configure_codex_notify()
    atexit.register(restore_config)

    def signal_handler(_sig, _frame):
        restore_config()
        sys.exit(0)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    print("启动 Codex 红绿灯监视器...")
    TrafficLightApp().run()


if __name__ == "__main__":
    main()
