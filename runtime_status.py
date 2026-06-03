#!/usr/bin/env python3
import json
import os
import signal
import tempfile
from datetime import datetime
from pathlib import Path


APP_DIR = Path(__file__).resolve().parent
RUNTIME_DIR = APP_DIR / "runtime"
LOG_DIR = APP_DIR / "logs"
STATUS_PATH = RUNTIME_DIR / "status.json"
MONITOR_LOG_PATH = LOG_DIR / "monitor.log"
VERIFICATION_EVENTS_PATH = LOG_DIR / "verification_events.jsonl"


def now_iso():
    return datetime.now().isoformat(timespec="seconds")


def is_pid_running(pid):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def sanitize_no_proxy_value(value):
    """Return an httpx-safe NO_PROXY value.

    Bare IPv6 entries such as ::1 and ::1/128 can be interpreted by httpx as
    malformed URL patterns. Keep IPv4/hostname localhost exclusions and drop
    the problematic IPv6 forms.
    """
    if not value:
        return value
    safe_parts = []
    for part in str(value).split(","):
        item = part.strip()
        if not item:
            continue
        if item in ("::1", "::1/128"):
            continue
        safe_parts.append(item)
    return ",".join(safe_parts)


def sanitize_network_env(env=None):
    target = os.environ if env is None else env
    for key in ("NO_PROXY", "no_proxy"):
        if key in target:
            target[key] = sanitize_no_proxy_value(target.get(key, ""))
    return target


def default_status():
    return {
        "state": "stopped",
        "updated_at": now_iso(),
        "monitor_pid": None,
        "monitor_pgid": None,
        "main_pid": None,
        "started_by_web": False,
        "can_stop_process_group": False,
        "last_start_at": None,
        "last_restart_at": None,
        "last_stop_at": None,
        "last_cookie_refresh_at": None,
        "verification_required": False,
        "verification_reason": "",
        "last_verification_event": None,
        "last_error": "",
        "next_restart_at": None,
    }


def atomic_write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def read_status():
    status = default_status()
    if STATUS_PATH.exists():
        try:
            payload = json.loads(STATUS_PATH.read_text(encoding="utf-8"))
            if isinstance(payload, dict):
                status.update(payload)
        except json.JSONDecodeError:
            status["last_error"] = "runtime/status.json 格式损坏"

    monitor_running = is_pid_running(status.get("monitor_pid"))
    main_running = is_pid_running(status.get("main_pid"))
    status["monitor_running"] = monitor_running
    status["main_running"] = main_running
    if not monitor_running and status.get("state") in ("running", "restarting", "waiting_verification"):
        status["state"] = "stopped"
        status["verification_required"] = False
    return status


def update_status(**fields):
    status = read_status()
    status.update(fields)
    status["updated_at"] = now_iso()
    atomic_write_json(STATUS_PATH, status)
    return status


def reset_status(state="stopped", **fields):
    status = default_status()
    status.update(fields)
    status["state"] = state
    status["updated_at"] = now_iso()
    atomic_write_json(STATUS_PATH, status)
    return status


def append_jsonl(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def append_monitor_log(message, level="info", event="monitor", **extra):
    payload = {
        "time": now_iso(),
        "level": level,
        "event": event,
        "message": str(message),
    }
    payload.update(extra)
    append_jsonl(MONITOR_LOG_PATH, payload)
    return payload


def append_verification_event(action, result="", reason="", message="", **extra):
    payload = {
        "time": now_iso(),
        "action": action,
        "result": result,
        "reason": reason,
        "message": message,
    }
    payload.update(extra)
    append_jsonl(VERIFICATION_EVENTS_PATH, payload)
    update_status(
        last_verification_event=payload,
        verification_required=action in ("required", "stale_cookie", "open_browser"),
        verification_reason=reason or message,
    )
    return payload


def tail_jsonl(path, limit=100):
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 100
    if not path.exists():
        return []
    lines = path.read_text(encoding="utf-8", errors="replace").splitlines()[-limit:]
    entries = []
    for line in lines:
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except json.JSONDecodeError:
            entries.append({"time": "", "level": "raw", "event": "raw", "message": line})
    return entries


def terminate_pid(pid, sig=signal.SIGTERM):
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, sig)
        return True
    except OSError:
        return False
