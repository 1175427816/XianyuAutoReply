#!/usr/bin/env python3
import errno
import hashlib
import json
import os
import pty
import select
import signal
import subprocess
import sys
import time
from pathlib import Path

from runtime_status import (
    append_monitor_log,
    append_verification_event,
    reset_status,
    sanitize_network_env,
    update_status,
)


APP_DIR = Path(__file__).resolve().parent
ENV_PATH = APP_DIR / ".env"
MAIN_COMMAND = [sys.executable, "main.py"]

RESTART_DELAY_SECONDS = int(os.getenv("MONITOR_RESTART_DELAY", "5"))
CLIPBOARD_POLL_SECONDS = float(os.getenv("MONITOR_CLIPBOARD_POLL_SECONDS", "2"))
BROWSER_COOKIE_RETRY_SECONDS = float(os.getenv("BROWSER_COOKIE_RETRY_SECONDS", "5"))
DEFAULT_BROWSER_COOKIE_RESUBMIT_SECONDS = 10

COOKIE_PROMPT_MARKERS = (
    "Cookie> ",
    "Cookie>",
)

FATAL_COOKIE_MARKERS = (
    "Cookie登录态已失效",
    "请更新.env文件中的COOKIES_STR",
    "重新登录失败",
    "Cookie解析失败",
)

VERIFICATION_REQUIRED_MARKERS = (
    "XIANYU_VERIFICATION_REQUIRED",
)

COOKIE_HINTS = (
    "_m_h5_tk=",
    "cookie2=",
    "unb=",
    "cna=",
    "sgcookie=",
    "x5sec=",
    "tracknick=",
    "_tb_token_=",
)

COOKIE_NAMES = (
    "cna",
    "t",
    "tracknick",
    "isg",
    "_hvn_lgc_",
    "xlly_s",
    "unb",
    "havana_lgc2_77",
    "havana_lgc_exp",
    "cookie2",
    "_samesite_flag_",
    "_tb_token_",
    "sgcookie",
    "csg",
    "sdkSilent",
    "mtop_partitioned_detect",
    "_m_h5_tk",
    "_m_h5_tk_enc",
    "tfstk",
)

stop_requested = False
attempted_cookie_hashes = set()
browser_cookie_resubmit_times = {}
invalid_cookie_browser_opened_once = False
pending_browser_close_after_token_success = False


def handle_signal(signum, frame):
    global stop_requested
    stop_requested = True


def monitor_log(message):
    print(f"\n[monitor] {message}", flush=True)
    append_monitor_log(message)


def take_invalid_cookie_browser_open_request():
    global invalid_cookie_browser_opened_once
    if invalid_cookie_browser_opened_once:
        return False
    invalid_cookie_browser_opened_once = True
    return True


def reset_invalid_cookie_browser_open_request():
    global invalid_cookie_browser_opened_once
    invalid_cookie_browser_opened_once = False


def read_env_value(name, default=None):
    value = os.getenv(name)
    if value is not None and value.strip() != "":
        return value.strip()

    if not ENV_PATH.exists():
        return default

    prefix = f"{name}="
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith(prefix):
            value = line.split("=", 1)[1].strip().strip('"').strip("'")
            return value if value else default
    return default


def env_flag(name, default=False):
    value = read_env_value(name)
    if value is None:
        return default
    return value.strip().lower() not in ("0", "false", "no", "off", "")


def env_float(name, default):
    try:
        return float(read_env_value(name, str(default)))
    except (TypeError, ValueError):
        return float(default)


def normalize_cookie_input(cookie_text):
    lines = []
    for line in cookie_text.splitlines():
        line = line.strip()
        if not line:
            continue
        if line.lower().startswith("cookie:"):
            line = line.split(":", 1)[1].strip()
        lines.append(line.rstrip(";").strip())
    cookie = "; ".join(lines).strip()
    return cookie.replace("\x1b[200~", "").replace("\x1b[201~", "").strip()


def cookie_from_json_capture(text):
    try:
        payload = json.loads(text)
    except json.JSONDecodeError:
        return None

    cookies = payload.get("cookies")
    if not isinstance(cookies, list):
        return None

    by_name = {}
    for item in cookies:
        if not isinstance(item, dict):
            continue
        name = str(item.get("name", "")).strip()
        value = str(item.get("value", "")).strip()
        if name and value:
            by_name[name] = value

    ordered_pairs = []
    for name in COOKIE_NAMES:
        if name in by_name:
            ordered_pairs.append(f"{name}={by_name.pop(name)}")

    for name in sorted(by_name):
        ordered_pairs.append(f"{name}={by_name[name]}")

    cookie = "; ".join(ordered_pairs)
    if looks_like_cookie(cookie):
        return cookie
    return None


def looks_like_cookie(cookie):
    if len(cookie) < 100:
        return False
    if "=" not in cookie:
        return False
    parts = [part.strip() for part in cookie.split(";") if "=" in part]
    if len(parts) < 3:
        return False
    return any(hint in cookie for hint in COOKIE_HINTS)


def cookie_hash(cookie):
    return hashlib.sha256(cookie.encode("utf-8")).hexdigest()


def read_env_cookie():
    if not ENV_PATH.exists():
        return None
    for line in ENV_PATH.read_text(encoding="utf-8").splitlines():
        if line.startswith("COOKIES_STR="):
            cookie = normalize_cookie_input(line.split("=", 1)[1])
            if looks_like_cookie(cookie):
                return cookie
    return None


def read_clipboard_text():
    try:
        result = subprocess.run(
            ["pbpaste"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        result = None

    if result and result.returncode == 0 and result.stdout.strip():
        return result.stdout

    try:
        result = subprocess.run(
            ["osascript", "-e", "the clipboard as text"],
            capture_output=True,
            text=True,
            timeout=2,
        )
    except Exception:
        return ""

    if result.returncode == 0:
        return result.stdout
    return ""


def read_clipboard_cookie():
    clipboard_text = read_clipboard_text()
    cookie = cookie_from_json_capture(clipboard_text)
    if cookie:
        return cookie

    cookie = normalize_cookie_input(clipboard_text)
    if looks_like_cookie(cookie):
        return cookie
    return None


def read_browser_cookie(timeout_seconds=None, open_login=True):
    if not env_flag("AUTO_COOKIE_FROM_BROWSER", True):
        return None

    try:
        from browser_cookie_fetcher import fetch_cookie_from_browser
    except Exception as exc:
        monitor_log(f"Browser Cookie fetcher is unavailable: {exc}")
        return None

    try:
        return fetch_cookie_from_browser(
            APP_DIR,
            timeout_seconds=timeout_seconds,
            open_login=open_login,
            log=monitor_log,
        )
    except Exception as exc:
        monitor_log(f"Browser Cookie fetch failed: {exc}")
        return None


def close_browser_after_cookie_refresh():
    if not env_flag("BROWSER_COOKIE_CLOSE_AFTER_REFRESH", True):
        return

    try:
        from browser_cookie_fetcher import close_dedicated_browser
    except Exception as exc:
        monitor_log(f"Browser close helper is unavailable: {exc}")
        return

    try:
        if close_dedicated_browser(APP_DIR, log=monitor_log):
            append_monitor_log("Dedicated Chrome closed after Cookie refresh.", event="browser_close")
    except Exception as exc:
        monitor_log(f"Dedicated Chrome close failed: {exc}")


def mark_browser_close_after_token_success():
    global pending_browser_close_after_token_success
    pending_browser_close_after_token_success = True


def close_browser_if_token_succeeded(recent_output):
    global pending_browser_close_after_token_success
    if not pending_browser_close_after_token_success:
        return
    if "Token获取成功" not in recent_output:
        return
    pending_browser_close_after_token_success = False
    close_browser_after_cookie_refresh()


def update_env_cookie(cookie):
    if ENV_PATH.exists():
        lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    else:
        lines = []

    replaced = False
    new_lines = []
    for line in lines:
        if line.startswith("COOKIES_STR="):
            new_lines.append(f"COOKIES_STR={cookie}")
            replaced = True
        else:
            new_lines.append(line)

    if not replaced:
        new_lines.append(f"COOKIES_STR={cookie}")

    ENV_PATH.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    try:
        os.chmod(ENV_PATH, 0o600)
    except OSError:
        pass


def submit_cookie(master_fd, cookie):
    attempted_cookie_hashes.add(cookie_hash(cookie))
    os.write(master_fd, (cookie + "\r").encode("utf-8"))


def browser_cookie_retry_allowed(cookie, now):
    digest = cookie_hash(cookie)
    last_retry = browser_cookie_resubmit_times.get(digest, 0.0)
    resubmit_seconds = env_float("BROWSER_COOKIE_RESUBMIT_SECONDS", DEFAULT_BROWSER_COOKIE_RESUBMIT_SECONDS)
    if now - last_retry < resubmit_seconds:
        return False
    browser_cookie_resubmit_times[digest] = now
    return True


def remember_browser_cookie_retry(cookie, now):
    browser_cookie_resubmit_times[cookie_hash(cookie)] = now


def refresh_env_cookie_from_browser(timeout_seconds, rejected_hashes=None, open_login=True):
    cookie = read_browser_cookie(timeout_seconds=timeout_seconds, open_login=open_login)
    if not cookie:
        return None

    if rejected_hashes and cookie_hash(cookie) in rejected_hashes:
        monitor_log("Browser Cookie matches a failed/current Cookie; waiting for a fresh login session.")
        return None

    update_env_cookie(cookie)
    attempted_cookie_hashes.add(cookie_hash(cookie))
    remember_browser_cookie_retry(cookie, time.monotonic())
    reset_invalid_cookie_browser_open_request()
    append_verification_event("refresh_cookie", "success", message="Valid browser Cookie detected.")
    update_status(
        state="running",
        verification_required=False,
        verification_reason="",
        last_cookie_refresh_at=append_monitor_log("Cookie refreshed from browser.", event="cookie_refresh")["time"],
        last_error="",
    )
    monitor_log("Valid Cookie detected from browser session; .env was updated.")
    if open_login:
        mark_browser_close_after_token_success()
    return cookie


def wait_for_fresh_cookie_and_update_env():
    monitor_log("main.py reported an invalid login cookie.")
    append_verification_event(
        "required",
        "waiting",
        reason="main.py reported an invalid login cookie.",
        message="Waiting for browser or clipboard Cookie refresh.",
    )
    update_status(
        state="waiting_verification",
        verification_required=True,
        verification_reason="Cookie 登录态失效或滑块验证未完成",
    )

    rejected_hashes = set(attempted_cookie_hashes)
    env_cookie = read_env_cookie()
    if env_cookie:
        rejected_hashes.add(cookie_hash(env_cookie))

    monitor_log("Watching the dedicated browser session and clipboard for a refreshed Cookie.")
    stale_notice_shown = False
    browser_notice_shown = False
    last_browser_check = 0.0
    last_clipboard_check = 0.0

    while not stop_requested:
        now = time.monotonic()

        if env_flag("AUTO_COOKIE_FROM_BROWSER", True) and now - last_browser_check >= BROWSER_COOKIE_RETRY_SECONDS:
            last_browser_check = now
            browser_timeout = env_float("BROWSER_COOKIE_PROMPT_TIMEOUT", 180)
            open_login = take_invalid_cookie_browser_open_request()
            cookie = read_browser_cookie(timeout_seconds=browser_timeout, open_login=open_login)
            if cookie:
                if cookie_hash(cookie) in rejected_hashes:
                    if not browser_notice_shown:
                        browser_notice_shown = True
                        append_verification_event(
                            "stale_cookie",
                            "waiting",
                            reason="Browser Cookie matches failed/current Cookie.",
                        )
                        update_status(
                            state="waiting_verification",
                            verification_required=True,
                            verification_reason="Cookie 未变化，需要先完成闲鱼验证",
                            last_error="Cookie 未变化，需要先完成闲鱼验证",
                        )
                        monitor_log("Browser Cookie is unchanged; finish verification before refreshing Cookie again.")
                else:
                    update_env_cookie(cookie)
                    attempted_cookie_hashes.add(cookie_hash(cookie))
                    remember_browser_cookie_retry(cookie, now)
                    reset_invalid_cookie_browser_open_request()
                    append_verification_event("refresh_cookie", "success", message="Valid browser Cookie detected.")
                    update_status(
                        state="restarting",
                        verification_required=False,
                        verification_reason="",
                        last_cookie_refresh_at=append_monitor_log("Cookie refreshed from browser.", event="cookie_refresh")["time"],
                        last_error="",
                    )
                    monitor_log("Valid Cookie detected from browser session; .env was updated. Restarting main.py.")
                    mark_browser_close_after_token_success()
                    return True

        if now - last_clipboard_check >= CLIPBOARD_POLL_SECONDS:
            last_clipboard_check = now
            cookie = read_clipboard_cookie()
            if cookie:
                if cookie_hash(cookie) in rejected_hashes:
                    if not stale_notice_shown:
                        stale_notice_shown = True
                        monitor_log("Clipboard Cookie matches a failed/current Cookie; waiting for a fresh one.")
                    continue
                update_env_cookie(cookie)
                attempted_cookie_hashes.add(cookie_hash(cookie))
                reset_invalid_cookie_browser_open_request()
                append_verification_event("refresh_cookie", "success", message="Valid clipboard Cookie detected.")
                update_status(
                    state="restarting",
                    verification_required=False,
                    verification_reason="",
                    last_cookie_refresh_at=append_monitor_log("Cookie refreshed from clipboard.", event="cookie_refresh")["time"],
                    last_error="",
                )
                monitor_log("Valid Cookie detected in clipboard; .env was updated. Restarting main.py.")
                return True

        time.sleep(0.5)

    return False


def run_once():
    master_fd, slave_fd = pty.openpty()
    child_env = sanitize_network_env(os.environ.copy())
    child_env["XIANYU_MONITOR_MODE"] = "1"
    process = subprocess.Popen(
        MAIN_COMMAND,
        cwd=APP_DIR,
        stdin=slave_fd,
        stdout=slave_fd,
        stderr=slave_fd,
        env=child_env,
        close_fds=True,
    )
    os.close(slave_fd)

    recent_output = ""
    cookie_prompt_seen = False
    cookie_submitted = False
    clipboard_notice_shown = False
    stale_notice_shown = False
    fatal_cookie_seen = False
    last_clipboard_check = 0.0
    last_browser_check = 0.0
    cookie_prompt_count = 0

    monitor_log(f"Started main.py with PID {process.pid}.")
    update_status(
        state="running",
        monitor_pid=os.getpid(),
        monitor_pgid=os.getpgrp(),
        main_pid=process.pid,
        started_by_web=os.getenv("XIANYU_STARTED_BY_WEB") == "1",
        can_stop_process_group=os.getenv("XIANYU_STARTED_BY_WEB") == "1",
        last_start_at=append_monitor_log(f"Started main.py with PID {process.pid}.", event="main_start", pid=process.pid)["time"],
        verification_required=False,
        verification_reason="",
        last_error="",
    )

    try:
        while not stop_requested:
            readable, _, _ = select.select([master_fd], [], [], 0.5)

            if readable:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        break
                    raise

                if not chunk:
                    break

                sys.stdout.buffer.write(chunk)
                sys.stdout.buffer.flush()

                text = chunk.decode("utf-8", errors="ignore")
                recent_output = (recent_output + text)[-8000:]
                close_browser_if_token_succeeded(recent_output)

                prompt_count = recent_output.count("Cookie>")
                if prompt_count > cookie_prompt_count:
                    cookie_prompt_count = prompt_count
                    cookie_prompt_seen = True
                    cookie_submitted = False
                    clipboard_notice_shown = False
                    stale_notice_shown = False
                    last_browser_check = 0.0
                    monitor_log("Cookie prompt detected; trying browser session first.")
                    append_verification_event(
                        "required",
                        "waiting",
                        reason="Cookie prompt detected.",
                        message="Trying browser session first.",
                    )
                    update_status(
                        state="waiting_verification",
                        verification_required=True,
                        verification_reason="Cookie 输入提示已出现，等待浏览器验证或新 Cookie",
                    )

                if any(marker in recent_output for marker in FATAL_COOKIE_MARKERS):
                    fatal_cookie_seen = True
                    update_status(
                        state="waiting_verification",
                        verification_required=True,
                        verification_reason="检测到 Cookie 登录态失效或验证未完成",
                        last_error="Cookie 登录态失效或验证未完成",
                    )

                if any(marker in recent_output for marker in VERIFICATION_REQUIRED_MARKERS):
                    fatal_cookie_seen = True
                    append_verification_event(
                        "required",
                        "waiting",
                        reason="闲鱼 token 接口触发滑块/登录验证",
                        message="Waiting for browser verification and Cookie refresh.",
                    )
                    update_status(
                        state="waiting_verification",
                        verification_required=True,
                        verification_reason="闲鱼 token 接口触发滑块/登录验证",
                        last_error="等待完成闲鱼验证",
                    )

            if cookie_prompt_seen and not cookie_submitted:
                now = time.monotonic()
                if env_flag("AUTO_COOKIE_FROM_BROWSER", True) and now - last_browser_check >= BROWSER_COOKIE_RETRY_SECONDS:
                    last_browser_check = now
                    browser_timeout = env_float("BROWSER_COOKIE_PROMPT_TIMEOUT", 180)
                    open_login = take_invalid_cookie_browser_open_request()
                    cookie = read_browser_cookie(timeout_seconds=browser_timeout, open_login=open_login)
                    if cookie:
                        if cookie_hash(cookie) in attempted_cookie_hashes:
                            if not stale_notice_shown:
                                stale_notice_shown = True
                                append_verification_event(
                                    "stale_cookie",
                                    "waiting",
                                    reason="Browser Cookie matches failed/current Cookie.",
                                )
                                update_status(
                                    state="waiting_verification",
                                    verification_required=True,
                                    verification_reason="Cookie 未变化，需要先完成闲鱼验证",
                                    last_error="Cookie 未变化，需要先完成闲鱼验证",
                                )
                                monitor_log("Browser Cookie is unchanged; finish verification before refreshing Cookie again.")
                        else:
                            update_env_cookie(cookie)
                            submit_cookie(master_fd, cookie)
                            remember_browser_cookie_retry(cookie, now)
                            reset_invalid_cookie_browser_open_request()
                            cookie_submitted = True
                            append_verification_event("refresh_cookie", "success", message="Valid browser Cookie submitted.")
                            update_status(
                                state="running",
                                verification_required=False,
                                verification_reason="",
                                last_cookie_refresh_at=append_monitor_log(
                                    "Cookie submitted to main.py from browser.", event="cookie_refresh"
                                )["time"],
                                last_error="",
                            )
                            monitor_log("Valid Cookie detected from browser session; submitted it to main.py and updated .env.")
                            mark_browser_close_after_token_success()
                    elif not clipboard_notice_shown:
                        clipboard_notice_shown = True
                        monitor_log("Browser Cookie is unavailable; still watching browser and clipboard.")
                    continue

                if now - last_clipboard_check >= CLIPBOARD_POLL_SECONDS:
                    last_clipboard_check = now
                    cookie = read_clipboard_cookie()
                    if cookie:
                        if cookie_hash(cookie) in attempted_cookie_hashes:
                            if not stale_notice_shown:
                                stale_notice_shown = True
                                monitor_log("Clipboard Cookie matches a failed/current Cookie; waiting for a fresh one.")
                            continue
                        submit_cookie(master_fd, cookie)
                        reset_invalid_cookie_browser_open_request()
                        cookie_submitted = True
                        append_verification_event("refresh_cookie", "success", message="Valid clipboard Cookie submitted.")
                        update_status(
                            state="running",
                            verification_required=False,
                            verification_reason="",
                            last_cookie_refresh_at=append_monitor_log(
                                "Cookie submitted to main.py from clipboard.", event="cookie_refresh"
                            )["time"],
                            last_error="",
                        )
                        monitor_log("Valid Cookie detected in clipboard; submitted it to main.py.")
                    elif not clipboard_notice_shown:
                        clipboard_notice_shown = True
                        monitor_log("Waiting for a valid Cookie in the clipboard.")

            if process.poll() is not None:
                break

        if stop_requested and process.poll() is None:
            process.terminate()
            try:
                process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait()

        exit_code = process.wait()
        update_status(main_pid=None)
        return exit_code, fatal_cookie_seen
    finally:
        os.close(master_fd)


def main():
    signal.signal(signal.SIGINT, handle_signal)
    signal.signal(signal.SIGTERM, handle_signal)

    monitor_log(f"Working directory: {APP_DIR}")
    monitor_log("Cookie automation mode: browser session first, clipboard fallback. Cookie values are not printed.")
    reset_status(
        "running",
        monitor_pid=os.getpid(),
        monitor_pgid=os.getpgrp(),
        started_by_web=os.getenv("XIANYU_STARTED_BY_WEB") == "1",
        can_stop_process_group=os.getenv("XIANYU_STARTED_BY_WEB") == "1",
        last_start_at=append_monitor_log("Monitor started.", event="monitor_start")["time"],
        verification_required=False,
        verification_reason="",
        last_error="",
    )

    if env_flag("BROWSER_COOKIE_STARTUP_REFRESH", True):
        startup_timeout = env_float("BROWSER_COOKIE_STARTUP_TIMEOUT", 5)
        startup_open_login = env_flag("BROWSER_COOKIE_STARTUP_OPEN_BROWSER", False)
        if startup_timeout > 0:
            refresh_env_cookie_from_browser(startup_timeout, open_login=startup_open_login)

    while not stop_requested:
        exit_code, fatal_cookie_seen = run_once()

        if stop_requested:
            break

        monitor_log(f"main.py exited with code {exit_code}.")
        update_status(
            state="waiting_verification" if fatal_cookie_seen else "restarting",
            main_pid=None,
            last_error="" if exit_code == 0 or fatal_cookie_seen else f"main.py exited with code {exit_code}",
        )

        if fatal_cookie_seen:
            if not wait_for_fresh_cookie_and_update_env():
                break

        monitor_log(f"Restarting main.py in {RESTART_DELAY_SECONDS} seconds.")
        update_status(
            state="restarting",
            next_restart_at=None,
            last_restart_at=append_monitor_log(
                f"Restarting main.py in {RESTART_DELAY_SECONDS} seconds.", event="main_restart"
            )["time"],
        )
        time.sleep(RESTART_DELAY_SECONDS)

    reset_status(
        "stopped",
        monitor_pid=None,
        monitor_pgid=None,
        main_pid=None,
        last_stop_at=append_monitor_log("Monitor stopped.", event="monitor_stop")["time"],
        verification_required=False,
        verification_reason="",
    )
    monitor_log("Monitor stopped.")


if __name__ == "__main__":
    main()
