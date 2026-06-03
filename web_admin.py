#!/usr/bin/env python3
import argparse
import hashlib
import json
import os
import re
import signal
import sqlite3
import subprocess
import tempfile
import time
from datetime import datetime
from http import HTTPStatus
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

from runtime_status import (
    MONITOR_LOG_PATH,
    VERIFICATION_EVENTS_PATH,
    append_monitor_log,
    append_verification_event,
    is_pid_running,
    read_status,
    reset_status,
    sanitize_network_env,
    tail_jsonl,
    terminate_pid,
    update_status,
)


BASE_DIR = Path(__file__).resolve().parent
DB_PATH = BASE_DIR / "data" / "chat_history.db"
IMAGE_REPLIES_PATH = BASE_DIR / "image_replies.json"
CAPTURE_PATH = BASE_DIR / "logs" / "message_capture.jsonl"
SLIDER_LOG_PATH = BASE_DIR / "logs" / "slider_notify.log"
STATIC_DIR = BASE_DIR / "web_admin"
MONITOR_STDOUT_PATH = BASE_DIR / "logs" / "monitor_stdout.log"
DATE_RE = re.compile(r"^\[(\d{4}-\d{2}-\d{2})[ T]\d{2}:\d{2}:\d{2}\]")
IMAGE_URL_RE = re.compile(r"\.(?:jpg|jpeg|png|gif|webp|heic|heif)(?:[?#].*)?$", re.IGNORECASE)


def json_response(handler, payload, status=HTTPStatus.OK):
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(body)))
    handler.end_headers()
    handler.wfile.write(body)


def error_response(handler, message, status=HTTPStatus.BAD_REQUEST):
    json_response(handler, {"error": message}, status)


def parse_date(value):
    value = str(value or "").strip()
    if not value:
        return ""
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        raise ValueError(f"日期格式不正确: {value}")
    return value


def date_in_range(day, start="", end=""):
    if start and day < start:
        return False
    if end and day > end:
        return False
    return True


def walk_json(value):
    if isinstance(value, dict):
        yield value
        for child in value.values():
            yield from walk_json(child)
    elif isinstance(value, list):
        for child in value:
            yield from walk_json(child)
    elif isinstance(value, str):
        text = value.strip()
        if text.startswith("{") or text.startswith("["):
            try:
                yield from walk_json(json.loads(text))
            except json.JSONDecodeError:
                return


def extract_item_id(message):
    for obj in walk_json(message):
        reminder_url = str(obj.get("reminderUrl", ""))
        if "itemId=" in reminder_url:
            return reminder_url.split("itemId=", 1)[1].split("&", 1)[0]
    return ""


def extract_sender_title(message):
    for obj in walk_json(message):
        title = str(obj.get("reminderTitle", "")).strip()
        if title:
            return title
    return ""


def is_image_url_object(obj, url):
    if not isinstance(obj, dict):
        return False
    if "width" in obj and "height" in obj:
        return True
    lowered = url.lower()
    if IMAGE_URL_RE.search(lowered):
        return True
    return any(marker in lowered for marker in ("/img/", "/image/", "alicdn.com/img", "xy_chat"))


def captured_image_urls(record):
    urls = set()
    for obj in walk_json(record.get("message")):
        url = str(obj.get("url", "")).strip()
        if url.startswith(("http://", "https://")) and is_image_url_object(obj, url):
            urls.add(url)
    return urls


def connect_db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def get_query(handler):
    return {key: values[-1] for key, values in parse_qs(urlparse(handler.path).query).items()}


def cookie_hash(cookie):
    return hashlib.sha256(str(cookie or "").encode("utf-8")).hexdigest()


def read_env_cookie():
    env_path = BASE_DIR / ".env"
    if not env_path.exists():
        return ""
    for line in env_path.read_text(encoding="utf-8").splitlines():
        if line.startswith("COOKIES_STR="):
            return line.split("=", 1)[1].strip()
    return ""


def monitor_process_running():
    return is_pid_running(read_status().get("monitor_pid"))


def start_monitor_process():
    status = read_status()
    if status.get("monitor_running"):
        return {"started": False, "message": "monitor 已在运行", "status": status}

    env = os.environ.copy()
    sanitize_network_env(env)
    env["XIANYU_STARTED_BY_WEB"] = "1"
    MONITOR_STDOUT_PATH.parent.mkdir(parents=True, exist_ok=True)
    stdout = MONITOR_STDOUT_PATH.open("a", encoding="utf-8")
    process = subprocess.Popen(
        [os.sys.executable, "run_with_cookie_monitor.py"],
        cwd=BASE_DIR,
        stdout=stdout,
        stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL,
        env=env,
        start_new_session=True,
        close_fds=True,
    )
    append_monitor_log(f"Web admin started monitor PID {process.pid}.", event="web_start_monitor", pid=process.pid)
    status = update_status(
        state="running",
        monitor_pid=process.pid,
        monitor_pgid=process.pid,
        started_by_web=True,
        can_stop_process_group=True,
        last_start_at=append_monitor_log("Monitor launch requested from Web admin.", event="monitor_start_request")["time"],
        last_error="",
    )
    return {"started": True, "message": "monitor 已启动", "status": status}


def stop_monitor_process():
    status = read_status()
    monitor_pid = status.get("monitor_pid")
    main_pid = status.get("main_pid")
    monitor_pgid = status.get("monitor_pgid")
    stopped = False

    if status.get("can_stop_process_group") and monitor_pgid:
        try:
            os.killpg(int(monitor_pgid), signal.SIGTERM)
            stopped = True
        except OSError:
            stopped = False

    if not stopped:
        stopped = terminate_pid(main_pid, signal.SIGTERM) or stopped
        stopped = terminate_pid(monitor_pid, signal.SIGTERM) or stopped

    time.sleep(0.5)
    status = reset_status(
        "stopped",
        last_stop_at=append_monitor_log("Monitor stop requested from Web admin.", event="monitor_stop_request")["time"],
        verification_required=False,
        verification_reason="",
    )
    return {"stopped": stopped, "message": "停止请求已发送" if stopped else "没有检测到运行中的 monitor", "status": status}


def restart_monitor_process():
    stop_monitor_process()
    time.sleep(0.8)
    return start_monitor_process()


def open_verification_browser():
    try:
        from browser_cookie_fetcher import fetch_cookie_from_browser

        fetch_cookie_from_browser(BASE_DIR, timeout_seconds=1, open_login=True, log=append_monitor_log)
    except Exception as exc:
        append_verification_event("open_browser", "error", reason=str(exc))
        return {"ok": False, "message": f"打开/聚焦验证页面失败: {exc}", "status": read_status()}

    append_verification_event("open_browser", "requested", reason="用户从后台打开验证页面")
    update_status(state="waiting_verification", verification_required=True, verification_reason="已打开/聚焦闲鱼验证页面")
    return {"ok": True, "message": "已打开或聚焦专用 Chrome 的闲鱼消息页", "status": read_status()}


def refresh_cookie_from_browser(timeout_seconds=10):
    old_cookie = read_env_cookie()
    old_hash = cookie_hash(old_cookie) if old_cookie else ""
    try:
        from browser_cookie_fetcher import close_dedicated_browser, fetch_cookie_from_browser, update_env_cookie

        cookie = fetch_cookie_from_browser(
            BASE_DIR,
            timeout_seconds=timeout_seconds,
            open_login=True,
            log=append_monitor_log,
        )
        if not cookie:
            append_verification_event("refresh_cookie", "not_found", reason="No valid browser Cookie was found.")
            update_status(state="waiting_verification", verification_required=True, verification_reason="未读取到有效 Cookie")
            return {"ok": False, "message": "未读取到有效 Cookie，请先在专用 Chrome 完成登录/验证", "status": read_status()}
        if old_hash and cookie_hash(cookie) == old_hash:
            append_verification_event("refresh_cookie", "unchanged", reason="Browser Cookie unchanged.")
            update_status(state="waiting_verification", verification_required=True, verification_reason="Cookie 未变化，需要先完成验证")
            return {"ok": False, "message": "Cookie 未变化，需要先完成验证", "status": read_status()}

        update_env_cookie(BASE_DIR / ".env", cookie)
        append_verification_event("refresh_cookie", "success", message="Browser Cookie refreshed from Web admin.")
        update_status(
            state="restarting" if monitor_process_running() else "stopped",
            verification_required=False,
            verification_reason="",
            last_cookie_refresh_at=append_monitor_log("Cookie refreshed from Web admin.", event="cookie_refresh")["time"],
            last_error="",
        )
        try:
            close_dedicated_browser(BASE_DIR, log=append_monitor_log)
        except Exception as exc:
            append_monitor_log(f"Dedicated Chrome close failed: {exc}", level="warning", event="browser_close")
        if monitor_process_running():
            restart_monitor_process()
        return {"ok": True, "message": "Cookie 已刷新，monitor 已重启" if monitor_process_running() else "Cookie 已刷新", "status": read_status()}
    except Exception as exc:
        append_verification_event("refresh_cookie", "error", reason=str(exc))
        update_status(state="error", last_error=f"刷新 Cookie 失败: {exc}")
        return {"ok": False, "message": f"刷新 Cookie 失败: {exc}", "status": read_status()}


def query_between(column, start, end):
    where = []
    params = []
    if start:
        where.append(f"date({column}) >= ?")
        params.append(start)
    if end:
        where.append(f"date({column}) <= ?")
        params.append(end)
    return (" WHERE " + " AND ".join(where) if where else ""), params


def read_items(query=""):
    if not DB_PATH.exists():
        return []
    items = []
    with connect_db() as conn:
        rows = conn.execute(
            "SELECT item_id, data, price, description, last_updated FROM items ORDER BY last_updated DESC"
        ).fetchall()

    needle = query.strip().lower()
    for row in rows:
        try:
            data = json.loads(row["data"])
        except Exception:
            data = {}
        title = str(data.get("title") or "").strip()
        description = str(row["description"] or data.get("desc") or "").strip()
        price = row["price"]
        cover = ""
        default_picture = data.get("defaultPicture")
        if isinstance(default_picture, dict):
            cover = str(default_picture.get("url") or default_picture.get("picUrl") or "")
        if not cover:
            image_infos = data.get("imageInfos")
            if isinstance(image_infos, list) and image_infos:
                first = image_infos[0]
                if isinstance(first, dict):
                    cover = str(first.get("url") or first.get("picUrl") or "")
        item = {
            "item_id": str(row["item_id"]),
            "title": title or str(row["item_id"]),
            "price": price,
            "description": description,
            "description_short": description[:180],
            "last_updated": row["last_updated"],
            "cover": cover,
            "product_type": classify_product_type(title, description),
        }
        haystack = " ".join([item["item_id"], item["title"], item["description"]]).lower()
        if needle and needle not in haystack:
            continue
        items.append(item)
    return items


def classify_product_type(title, description=""):
    title_text = str(title or "").lower()
    fallback_text = f"{title} {description}".lower()
    rules = [
        ("酷态科10号mini", ("10号mini", "10 号mini", "10号 mini")),
        ("酷态科10号plus", ("10号plus", "10 号plus", "10号 plus", "10号超级", "10号电能棒")),
        ("酷态科15号AIR", ("15号air", "15号 air", "15 号air")),
        ("酷态科6号", ("6号", "6 号", "电能卡片", "饼干")),
        ("磁吸/模块化配件", ("magsafe", "磁吸", "模块化", "强磁")),
        ("雕刻/定制服务", ("雕刻", "定制服务", "个性化")),
        ("手机/数码设备", ("iphone", "ipad", "手机", "平板")),
    ]
    for label, keywords in rules:
        if any(keyword in title_text for keyword in keywords):
            return label
    for label, keywords in rules:
        if any(keyword in fallback_text for keyword in keywords):
            return label
    return "其他商品"


def load_config():
    if not IMAGE_REPLIES_PATH.exists():
        return {"rules": []}
    try:
        payload = json.loads(IMAGE_REPLIES_PATH.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise ValueError(f"image_replies.json 格式损坏: {exc}")
    if isinstance(payload, list):
        payload = {"rules": payload}
    if not isinstance(payload, dict):
        raise ValueError("image_replies.json 顶层必须是对象或规则数组")
    rules = payload.setdefault("rules", [])
    if not isinstance(rules, list):
        raise ValueError("image_replies.json 中 rules 必须是数组")
    return payload


def atomic_write_json(path, payload):
    path.parent.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, indent=2) + "\n"
    with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=path.parent, delete=False) as tmp:
        tmp.write(data)
        tmp_path = Path(tmp.name)
    os.replace(tmp_path, path)


def normalize_list(value, field_name):
    if isinstance(value, str):
        value = [part.strip() for part in value.split(",")]
    if not isinstance(value, list):
        raise ValueError(f"{field_name} 必须是数组或逗号分隔字符串")
    return [str(item).strip() for item in value if str(item).strip()]


def normalize_images(value):
    if isinstance(value, str):
        value = [{"url": part.strip()} for part in value.split(",") if part.strip()]
    if isinstance(value, dict):
        value = [value]
    if not isinstance(value, list):
        raise ValueError("images 必须是数组、对象或逗号分隔 URL")

    images = []
    for image in value:
        if isinstance(image, str):
            image = {"url": image.strip()}
        if not isinstance(image, dict):
            raise ValueError("图片配置必须是对象或 URL 字符串")
        url = str(image.get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            raise ValueError("图片 URL 必须以 http:// 或 https:// 开头")
        normalized = dict(image)
        normalized["url"] = url
        for key in ("width", "height", "type"):
            if key in normalized and normalized[key] not in ("", None):
                try:
                    normalized[key] = int(normalized[key])
                except (TypeError, ValueError):
                    raise ValueError(f"{key} 必须是数字")
        images.append(normalized)
    return images


def normalize_rule(payload, existing=None):
    existing = dict(existing or {})
    rule = dict(existing)
    rule["enabled"] = bool(payload.get("enabled", existing.get("enabled", True)))
    rule["name"] = str(payload.get("name", existing.get("name", "图片自动回复规则"))).strip()
    rule["keywords"] = normalize_list(payload.get("keywords", existing.get("keywords", [])), "keywords")
    rule["match"] = str(payload.get("match", existing.get("match", "contains")) or "contains").strip().lower()
    rule["text"] = str(payload.get("text", existing.get("text", "")) or "")
    rule["default"] = bool(payload.get("default", existing.get("default", False)))
    existing_item_ids = existing.get("item_ids", existing.get("item_id", []))
    existing_images = existing.get("images", existing.get("image", []))
    rule["item_ids"] = normalize_list(payload.get("item_ids", existing_item_ids), "item_ids")
    rule["images"] = normalize_images(payload.get("images", existing_images))

    if not rule["name"]:
        raise ValueError("规则名称不能为空")
    if not rule["keywords"]:
        raise ValueError("关键词不能为空")
    if rule["match"] not in ("contains", "exact"):
        raise ValueError("match 只能是 contains 或 exact")
    if not rule["default"] and not rule["item_ids"]:
        raise ValueError("非默认规则必须绑定至少一个商品 ID")
    if not rule["images"] and not rule["text"].strip():
        raise ValueError("规则至少需要回复文案或图片")
    return rule


def list_rules(status="", item_id="", query=""):
    config = load_config()
    rules = []
    assigned_urls = set()
    needle = query.strip().lower()
    for index, raw_rule in enumerate(config.get("rules", [])):
        if not isinstance(raw_rule, dict):
            continue
        enabled = raw_rule.get("enabled", True) is not False
        item_ids = normalize_list(raw_rule.get("item_ids") or raw_rule.get("item_id") or [], "item_ids")
        images = raw_rule.get("images") or raw_rule.get("image") or []
        try:
            normalized_images = normalize_images(images)
        except ValueError:
            normalized_images = []
        for image in normalized_images:
            assigned_urls.add(image["url"])
        if status == "enabled" and not enabled:
            continue
        if status == "disabled" and enabled:
            continue
        if item_id and item_id not in item_ids:
            continue
        haystack = " ".join(
            [
                str(raw_rule.get("name", "")),
                " ".join(item_ids),
                " ".join(normalize_list(raw_rule.get("keywords", []), "keywords")),
                str(raw_rule.get("text", "")),
                " ".join(image["url"] for image in normalized_images),
            ]
        ).lower()
        if needle and needle not in haystack:
            continue
        rules.append(
            {
                "id": str(index),
                "enabled": enabled,
                "name": raw_rule.get("name", ""),
                "item_ids": item_ids,
                "keywords": normalize_list(raw_rule.get("keywords", []), "keywords"),
                "match": raw_rule.get("match", "contains"),
                "text": raw_rule.get("text", ""),
                "default": bool(raw_rule.get("default", False)),
                "images": normalized_images,
            }
        )
    return rules, assigned_urls


def read_captured_images(start="", end="", item_id="", status="", query=""):
    _, assigned_urls = list_rules()
    if not CAPTURE_PATH.exists():
        return []
    images_by_url = {}
    needle = query.strip().lower()
    for line in CAPTURE_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            continue
        captured_at = str(record.get("captured_at", ""))
        day = captured_at[:10]
        if day and not date_in_range(day, start, end):
            continue
        message = record.get("message")
        record_item_id = extract_item_id(message)
        if item_id and record_item_id != item_id:
            continue
        sender_title = extract_sender_title(message)
        for obj in walk_json(message):
            url = str(obj.get("url", "")).strip()
            if not url.startswith(("http://", "https://")):
                continue
            if not is_image_url_object(obj, url):
                continue
            assigned = url in assigned_urls
            if status == "assigned" and not assigned:
                continue
            if status == "unassigned" and assigned:
                continue
            haystack = " ".join([url, record_item_id, sender_title, str(record.get("reason", ""))]).lower()
            if needle and needle not in haystack:
                continue
            images_by_url[url] = {
                "url": url,
                "captured_at": captured_at,
                "reason": record.get("reason", ""),
                "item_id": record_item_id,
                "sender_title": sender_title,
                "width": obj.get("width", ""),
                "height": obj.get("height", ""),
                "type": obj.get("type", 0),
                "assigned": assigned,
            }
    return sorted(images_by_url.values(), key=lambda item: item.get("captured_at", ""), reverse=True)


def delete_captured_image(url):
    url = str(url or "").strip()
    if not url.startswith(("http://", "https://")):
        raise ValueError("图片 URL 不正确")
    if not CAPTURE_PATH.exists():
        return 0

    kept_lines = []
    removed = 0
    for line in CAPTURE_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            record = json.loads(line)
        except json.JSONDecodeError:
            kept_lines.append(line)
            continue
        if url in captured_image_urls(record):
            removed += 1
            continue
        kept_lines.append(json.dumps(record, ensure_ascii=False))

    if removed:
        CAPTURE_PATH.parent.mkdir(parents=True, exist_ok=True)
        data = "\n".join(kept_lines)
        if data:
            data += "\n"
        with tempfile.NamedTemporaryFile("w", encoding="utf-8", dir=CAPTURE_PATH.parent, delete=False) as tmp:
            tmp.write(data)
            tmp_path = Path(tmp.name)
        os.replace(tmp_path, CAPTURE_PATH)
    return removed


def parse_verification_counts(start="", end=""):
    totals = {}
    if VERIFICATION_EVENTS_PATH.exists():
        for event in tail_jsonl(VERIFICATION_EVENTS_PATH, limit=500):
            day = str(event.get("time", ""))[:10]
            if not day or not date_in_range(day, start, end):
                continue
            if event.get("action") == "required":
                totals[day] = totals.get(day, 0) + 1
        if totals:
            return totals

    if not SLIDER_LOG_PATH.exists():
        return totals
    for line in SLIDER_LOG_PATH.read_text(encoding="utf-8").splitlines():
        match = DATE_RE.match(line)
        if not match:
            continue
        if "通知已发送" not in line:
            continue
        if "滑块" not in line and "验证" not in line:
            continue
        day = match.group(1)
        if not date_in_range(day, start, end):
            continue
        totals[day] = totals.get(day, 0) + 1
    return totals


def get_summary(start="", end=""):
    messages_by_day = {}
    processed_by_day = {}
    total_assistant = 0
    total_user = 0
    total_processed = 0
    total_chats = 0
    total_items = 0

    if DB_PATH.exists():
        with connect_db() as conn:
            where, params = query_between("timestamp", start, end)
            row = conn.execute(
                f"""
                SELECT
                  SUM(CASE WHEN role='assistant' THEN 1 ELSE 0 END) AS assistant_count,
                  SUM(CASE WHEN role='user' THEN 1 ELSE 0 END) AS user_count,
                  COUNT(DISTINCT chat_id) AS chat_count,
                  COUNT(DISTINCT item_id) AS item_count
                FROM messages{where}
                """,
                params,
            ).fetchone()
            total_assistant = int(row["assistant_count"] or 0)
            total_user = int(row["user_count"] or 0)
            total_chats = int(row["chat_count"] or 0)
            total_items = int(row["item_count"] or 0)

            for row in conn.execute(
                f"""
                SELECT date(timestamp) AS day,
                  SUM(CASE WHEN role='assistant' THEN 1 ELSE 0 END) AS assistant_count,
                  SUM(CASE WHEN role='user' THEN 1 ELSE 0 END) AS user_count,
                  COUNT(DISTINCT chat_id) AS chat_count,
                  COUNT(DISTINCT item_id) AS item_count
                FROM messages{where}
                GROUP BY day ORDER BY day
                """,
                params,
            ):
                messages_by_day[row["day"]] = {
                    "day": row["day"],
                    "assistant_replies": int(row["assistant_count"] or 0),
                    "user_messages": int(row["user_count"] or 0),
                    "chats": int(row["chat_count"] or 0),
                    "items": int(row["item_count"] or 0),
                    "processed_messages": 0,
                    "verifications": 0,
                }

            where, params = query_between("processed_at", start, end)
            total_processed = int(
                conn.execute(f"SELECT COUNT(*) AS count FROM processed_messages{where}", params).fetchone()["count"]
                or 0
            )
            for row in conn.execute(
                f"SELECT date(processed_at) AS day, COUNT(*) AS count FROM processed_messages{where} GROUP BY day",
                params,
            ):
                processed_by_day[row["day"]] = int(row["count"] or 0)

    verification_by_day = parse_verification_counts(start, end)
    days = sorted(set(messages_by_day) | set(processed_by_day) | set(verification_by_day))
    daily = []
    for day in days:
        entry = messages_by_day.get(
            day,
            {
                "day": day,
                "assistant_replies": 0,
                "user_messages": 0,
                "chats": 0,
                "items": 0,
                "processed_messages": 0,
                "verifications": 0,
            },
        )
        entry["processed_messages"] = processed_by_day.get(day, 0)
        entry["verifications"] = verification_by_day.get(day, 0)
        daily.append(entry)

    return {
        "totals": {
            "assistant_replies": total_assistant,
            "user_messages": total_user,
            "processed_messages": total_processed,
            "verifications": sum(verification_by_day.values()),
            "chats": total_chats,
            "items": total_items,
            "configured_rules": len(load_config().get("rules", [])) if IMAGE_REPLIES_PATH.exists() else 0,
            "captured_images": len(read_captured_images(start, end)),
            "unassigned_images": len(read_captured_images(start, end, status="unassigned")),
        },
        "daily": daily,
    }


class AdminHandler(SimpleHTTPRequestHandler):
    server_version = "XianyuWebAdmin/1.0"

    def translate_path(self, path):
        parsed = urlparse(path)
        request_path = unquote(parsed.path)
        if request_path == "/":
            request_path = "/index.html"
        return str(STATIC_DIR / request_path.lstrip("/"))

    def log_message(self, format, *args):
        print(f"[web-admin] {self.address_string()} - {format % args}")

    def do_GET(self):
        parsed = urlparse(self.path)
        if not parsed.path.startswith("/api/"):
            return super().do_GET()

        try:
            query = get_query(self)
            start = parse_date(query.get("start", ""))
            end = parse_date(query.get("end", ""))
            if parsed.path == "/api/summary":
                return json_response(self, get_summary(start, end))
            if parsed.path == "/api/runtime/status":
                return json_response(self, {"status": read_status()})
            if parsed.path == "/api/runtime/logs":
                limit = query.get("limit", "100")
                return json_response(
                    self,
                    {
                        "logs": tail_jsonl(MONITOR_LOG_PATH, limit=limit),
                        "verification_events": tail_jsonl(VERIFICATION_EVENTS_PATH, limit=limit),
                    },
                )
            if parsed.path == "/api/items":
                return json_response(self, {"items": read_items(query.get("query", ""))})
            if parsed.path == "/api/image-rules":
                rules, _ = list_rules(
                    status=query.get("status", ""),
                    item_id=query.get("item_id", ""),
                    query=query.get("query", ""),
                )
                return json_response(self, {"rules": rules})
            if parsed.path == "/api/captured-images":
                images = read_captured_images(
                    start=start,
                    end=end,
                    item_id=query.get("item_id", ""),
                    status=query.get("status", ""),
                    query=query.get("query", ""),
                )
                return json_response(self, {"images": images})
            return error_response(self, "接口不存在", HTTPStatus.NOT_FOUND)
        except ValueError as exc:
            return error_response(self, str(exc), HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            return error_response(self, f"服务器错误: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR)

    def read_json_body(self):
        length = int(self.headers.get("Content-Length", "0") or 0)
        if length <= 0:
            return {}
        body = self.rfile.read(length).decode("utf-8")
        try:
            return json.loads(body)
        except json.JSONDecodeError as exc:
            raise ValueError(f"请求 JSON 格式不正确: {exc}")

    def do_POST(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/runtime/start":
            try:
                return json_response(self, start_monitor_process())
            except Exception as exc:
                return error_response(self, f"启动 monitor 失败: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR)
        if parsed.path == "/api/runtime/stop":
            try:
                return json_response(self, stop_monitor_process())
            except Exception as exc:
                return error_response(self, f"停止 monitor 失败: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR)
        if parsed.path == "/api/runtime/restart":
            try:
                return json_response(self, restart_monitor_process())
            except Exception as exc:
                return error_response(self, f"重启 monitor 失败: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR)
        if parsed.path == "/api/verification/open-browser":
            return json_response(self, open_verification_browser())
        if parsed.path == "/api/verification/refresh-cookie":
            try:
                payload = self.read_json_body()
                timeout_seconds = float(payload.get("timeout_seconds", 10))
            except Exception:
                timeout_seconds = 10
            return json_response(self, refresh_cookie_from_browser(timeout_seconds=timeout_seconds))

        if parsed.path != "/api/image-rules":
            return error_response(self, "接口不存在", HTTPStatus.NOT_FOUND)
        try:
            payload = self.read_json_body()
            config = load_config()
            rule = normalize_rule(payload)
            config.setdefault("rules", []).append(rule)
            atomic_write_json(IMAGE_REPLIES_PATH, config)
            rule_id = str(len(config["rules"]) - 1)
            return json_response(self, {"rule": {"id": rule_id, **rule}}, HTTPStatus.CREATED)
        except ValueError as exc:
            return error_response(self, str(exc), HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            return error_response(self, f"服务器错误: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_PUT(self):
        parsed = urlparse(self.path)
        match = re.fullmatch(r"/api/image-rules/(\d+)", parsed.path)
        if not match:
            return error_response(self, "接口不存在", HTTPStatus.NOT_FOUND)
        try:
            rule_index = int(match.group(1))
            payload = self.read_json_body()
            config = load_config()
            rules = config.setdefault("rules", [])
            if rule_index < 0 or rule_index >= len(rules) or not isinstance(rules[rule_index], dict):
                return error_response(self, "规则不存在", HTTPStatus.NOT_FOUND)
            rule = normalize_rule(payload, rules[rule_index])
            rules[rule_index] = rule
            atomic_write_json(IMAGE_REPLIES_PATH, config)
            return json_response(self, {"rule": {"id": str(rule_index), **rule}})
        except ValueError as exc:
            return error_response(self, str(exc), HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            return error_response(self, f"服务器错误: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_PATCH(self):
        parsed = urlparse(self.path)
        match = re.fullmatch(r"/api/image-rules/(\d+)/enabled", parsed.path)
        if not match:
            return error_response(self, "接口不存在", HTTPStatus.NOT_FOUND)
        try:
            rule_index = int(match.group(1))
            payload = self.read_json_body()
            config = load_config()
            rules = config.setdefault("rules", [])
            if rule_index < 0 or rule_index >= len(rules) or not isinstance(rules[rule_index], dict):
                return error_response(self, "规则不存在", HTTPStatus.NOT_FOUND)
            rules[rule_index]["enabled"] = bool(payload.get("enabled", True))
            atomic_write_json(IMAGE_REPLIES_PATH, config)
            return json_response(self, {"id": str(rule_index), "enabled": rules[rule_index]["enabled"]})
        except ValueError as exc:
            return error_response(self, str(exc), HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            return error_response(self, f"服务器错误: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR)

    def do_DELETE(self):
        parsed = urlparse(self.path)
        if parsed.path == "/api/captured-images":
            try:
                payload = self.read_json_body()
                removed = delete_captured_image(payload.get("url", ""))
                if not removed:
                    return error_response(self, "未找到这张捕获图片", HTTPStatus.NOT_FOUND)
                return json_response(self, {"removed": removed})
            except ValueError as exc:
                return error_response(self, str(exc), HTTPStatus.BAD_REQUEST)
            except Exception as exc:
                return error_response(self, f"服务器错误: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR)

        match = re.fullmatch(r"/api/image-rules/(\d+)", parsed.path)
        if not match:
            return error_response(self, "接口不存在", HTTPStatus.NOT_FOUND)
        try:
            rule_index = int(match.group(1))
            config = load_config()
            rules = config.setdefault("rules", [])
            if rule_index < 0 or rule_index >= len(rules):
                return error_response(self, "规则不存在", HTTPStatus.NOT_FOUND)
            removed = rules.pop(rule_index)
            atomic_write_json(IMAGE_REPLIES_PATH, config)
            return json_response(self, {"removed": {"id": str(rule_index), **removed}})
        except ValueError as exc:
            return error_response(self, str(exc), HTTPStatus.BAD_REQUEST)
        except Exception as exc:
            return error_response(self, f"服务器错误: {exc}", HTTPStatus.INTERNAL_SERVER_ERROR)


def run_server(host="127.0.0.1", port=8766):
    if not STATIC_DIR.exists():
        raise SystemExit(f"静态资源目录不存在: {STATIC_DIR}")
    server = ThreadingHTTPServer((host, port), AdminHandler)
    print(f"Xianyu Web 管理后台已启动: http://{host}:{port}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n正在关闭 Web 管理后台")
    finally:
        server.server_close()


def main():
    parser = argparse.ArgumentParser(description="Xianyu Auto Reply local web admin")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8766)
    args = parser.parse_args()
    run_server(args.host, args.port)


if __name__ == "__main__":
    main()
