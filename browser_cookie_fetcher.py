#!/usr/bin/env python3
import argparse
import asyncio
import json
import os
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import websockets


DEFAULT_CDP_PORT = 9223
DEFAULT_LOGIN_URL = "https://www.goofish.com/im?spm=a21ybx.home.sidebar.2.4c053da6i6W3Qz"
DEFAULT_COOKIE_URLS = (
    "https://www.goofish.com/",
    "https://passport.goofish.com/",
    "https://h5api.m.goofish.com/",
)
ALLOWED_COOKIE_DOMAIN_SUFFIXES = ("goofish.com",)
COOKIE_POLL_SECONDS = 2.0

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


class BrowserCookieFetchError(RuntimeError):
    pass


def _noop_log(message):
    return None


def _env_value(name, default=None):
    value = os.getenv(name)
    if value is None or value.strip() == "":
        return default
    return value.strip()


def _env_float(name, default):
    try:
        return float(_env_value(name, str(default)))
    except ValueError:
        return float(default)


def _env_int(name, default):
    try:
        return int(_env_value(name, str(default)))
    except ValueError:
        return int(default)


def _truthy(value, default=False):
    if value is None:
        return default
    return str(value).strip().lower() not in ("0", "false", "no", "off", "")


def _default_profile_dir():
    return Path.home() / "Library" / "Application Support" / "XianyuAutoReply" / "browser-profile"


def looks_like_cookie(cookie):
    if not cookie or len(cookie) < 100:
        return False
    if "=" not in cookie:
        return False
    parts = [part.strip() for part in cookie.split(";") if "=" in part]
    if len(parts) < 3:
        return False
    if "unb=" not in cookie:
        return False
    return any(hint in cookie for hint in COOKIE_HINTS)


def _format_cookie_header(cookies):
    now = time.time()
    by_name = {}

    for item in cookies:
        name = str(item.get("name", "")).strip()
        value = str(item.get("value", "")).strip()
        domain = str(item.get("domain", "")).strip().lstrip(".")
        expires = item.get("expires", -1)

        if not name or value == "":
            continue
        if domain and not any(domain == suffix or domain.endswith("." + suffix) for suffix in ALLOWED_COOKIE_DOMAIN_SUFFIXES):
            continue
        if isinstance(expires, (int, float)) and expires not in (-1, 0) and expires < now:
            continue

        by_name[name] = value

    ordered_pairs = []
    for name in COOKIE_NAMES:
        if name in by_name:
            ordered_pairs.append(f"{name}={by_name.pop(name)}")

    for name in sorted(by_name):
        ordered_pairs.append(f"{name}={by_name[name]}")

    return "; ".join(ordered_pairs)


def update_env_cookie(env_path, cookie):
    env_path = Path(env_path)
    if env_path.exists():
        lines = env_path.read_text(encoding="utf-8").splitlines()
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

    env_path.write_text("\n".join(new_lines) + "\n", encoding="utf-8")
    try:
        os.chmod(env_path, 0o600)
    except OSError:
        pass


class ChromeCookieFetcher:
    def __init__(self, app_dir, log=None):
        self.app_dir = Path(app_dir).resolve()
        self.log = log or _noop_log
        self.port = _env_int("BROWSER_COOKIE_CDP_PORT", DEFAULT_CDP_PORT)
        profile_dir = _env_value("BROWSER_COOKIE_PROFILE_DIR")
        self.profile_dir = Path(profile_dir).expanduser() if profile_dir else _default_profile_dir()
        self.login_url = _env_value("BROWSER_COOKIE_LOGIN_URL", DEFAULT_LOGIN_URL)
        self.cookie_urls = self._cookie_urls()
        self.chrome_path = _env_value("BROWSER_COOKIE_CHROME_PATH") or self._find_chrome_path()
        self._launched_process = None

    def _cookie_urls(self):
        value = _env_value("BROWSER_COOKIE_URLS")
        if not value:
            return list(DEFAULT_COOKIE_URLS)
        urls = [part.strip() for part in value.split(",") if part.strip()]
        return urls or list(DEFAULT_COOKIE_URLS)

    def fetch(self, timeout_seconds=None, open_login=True):
        timeout_seconds = _env_float("BROWSER_COOKIE_TIMEOUT_SECONDS", 180) if timeout_seconds is None else float(timeout_seconds)
        deadline = time.monotonic() + max(timeout_seconds, 0)

        if not self._cdp_ready():
            if not open_login:
                return None
            self._launch_chrome()

        if not self._wait_for_cdp(deadline):
            return None

        if open_login:
            self._ensure_goofish_tab()
            self._focus_chrome()

        login_notice_shown = False
        while True:
            cookie = self._read_cookie_header()
            if looks_like_cookie(cookie):
                return cookie

            if time.monotonic() >= deadline:
                return None

            if open_login and not login_notice_shown:
                login_notice_shown = True
                self.log("Dedicated Chrome window is ready. Log in to Goofish there; no manual Cookie copy is needed.")

            time.sleep(COOKIE_POLL_SECONDS)

    def _find_chrome_path(self):
        candidates = (
            "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
            "/Applications/Google Chrome Beta.app/Contents/MacOS/Google Chrome Beta",
            "/Applications/Chromium.app/Contents/MacOS/Chromium",
        )
        for candidate in candidates:
            if Path(candidate).exists():
                return candidate
        return "Google Chrome"

    def _launch_chrome(self):
        if not self.chrome_path:
            raise BrowserCookieFetchError("Chrome executable was not found.")

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        command = [
            self.chrome_path,
            f"--remote-debugging-port={self.port}",
            "--remote-debugging-address=127.0.0.1",
            "--remote-allow-origins=*",
            f"--user-data-dir={self.profile_dir}",
            "--profile-directory=Default",
            "--no-first-run",
            "--no-default-browser-check",
            self.login_url,
        ]

        self.log(f"Starting dedicated Chrome session on 127.0.0.1:{self.port}.")
        self._launched_process = subprocess.Popen(
            command,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
        )

    def _wait_for_cdp(self, deadline):
        while time.monotonic() <= deadline:
            if self._cdp_ready():
                return True
            time.sleep(0.5)
        return False

    def _cdp_ready(self):
        try:
            self._get_json("/json/version", timeout=1)
            return True
        except Exception:
            return False

    def _base_url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def _get_json(self, path, timeout=3, method="GET"):
        request = urllib.request.Request(self._base_url(path), method=method)
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return json.loads(response.read().decode("utf-8"))

    def _ensure_goofish_tab(self):
        login_parts = urllib.parse.urlparse(self.login_url)
        login_prefix = f"{login_parts.scheme}://{login_parts.netloc}{login_parts.path}"

        try:
            targets = self._get_json("/json/list", timeout=2)
            goofish_pages = [
                target
                for target in targets
                if target.get("type") == "page" and "goofish.com" in target.get("url", "")
            ]
            for target in targets:
                target_url = target.get("url", "")
                if target.get("type") == "page" and target_url.startswith(login_prefix):
                    self._activate_page(target.get("id"))
                    self._reload_page(target)
                    self._close_duplicate_goofish_tabs(target.get("id"), goofish_pages)
                    return

            if goofish_pages:
                self._navigate_page(goofish_pages[0], self.login_url)
                self._activate_page(goofish_pages[0].get("id"))
                self._reload_page(goofish_pages[0])
                self._close_duplicate_goofish_tabs(goofish_pages[0].get("id"), goofish_pages)
                return
        except Exception:
            pass

        quoted_url = urllib.parse.quote(self.login_url, safe="")
        try:
            target = self._get_json(f"/json/new?{quoted_url}", timeout=3, method="PUT")
        except urllib.error.HTTPError:
            target = self._get_json(f"/json/new?{quoted_url}", timeout=3, method="GET")
        if isinstance(target, dict):
            self._activate_page(target.get("id"))
            self._reload_page(target)

    def _activate_page(self, target_id):
        if not target_id:
            return
        try:
            self._get_json(f"/json/activate/{target_id}", timeout=2)
        except Exception:
            pass

    def _focus_chrome(self):
        script = '''
        tell application "Google Chrome"
            activate
        end tell
        '''
        try:
            subprocess.run(
                ["osascript", "-e", script],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=2,
            )
        except Exception:
            pass

    def _close_duplicate_goofish_tabs(self, keep_id, goofish_pages):
        if not keep_id:
            return

        for target in goofish_pages:
            target_id = target.get("id")
            if not target_id or target_id == keep_id:
                continue
            try:
                self._get_json(f"/json/close/{target_id}", timeout=2)
            except Exception:
                pass

    def _navigate_page(self, target, url):
        ws_url = target.get("webSocketDebuggerUrl")
        if not ws_url:
            return
        try:
            asyncio.run(self._navigate_page_async(ws_url, url))
        except RuntimeError:
            result = {}

            def target_runner():
                try:
                    asyncio.run(self._navigate_page_async(ws_url, url))
                except Exception as exc:
                    result["error"] = exc

            thread = threading.Thread(target=target_runner, daemon=True)
            thread.start()
            thread.join()
            if "error" in result:
                raise result["error"]

    async def _navigate_page_async(self, ws_url, url):
        async with websockets.connect(ws_url, max_size=8 * 1024 * 1024) as websocket:
            await self._cdp_call(websocket, "Page.navigate", {"url": url})

    def _reload_page(self, target):
        ws_url = target.get("webSocketDebuggerUrl") if isinstance(target, dict) else None
        if not ws_url:
            return
        try:
            asyncio.run(self._reload_page_async(ws_url))
        except RuntimeError:
            result = {}

            def target_runner():
                try:
                    asyncio.run(self._reload_page_async(ws_url))
                except Exception as exc:
                    result["error"] = exc

            thread = threading.Thread(target=target_runner, daemon=True)
            thread.start()
            thread.join()
            if "error" in result:
                raise result["error"]

    async def _reload_page_async(self, ws_url):
        async with websockets.connect(ws_url, max_size=8 * 1024 * 1024) as websocket:
            await self._cdp_call(websocket, "Page.reload", {"ignoreCache": True})

    def _page_websocket_url(self):
        targets = self._get_json("/json/list", timeout=3)
        page_targets = [target for target in targets if target.get("type") == "page"]

        for target in page_targets:
            if "goofish.com" in target.get("url", "") and target.get("webSocketDebuggerUrl"):
                return target["webSocketDebuggerUrl"]

        for target in page_targets:
            if target.get("webSocketDebuggerUrl"):
                return target["webSocketDebuggerUrl"]

        raise BrowserCookieFetchError("No Chrome page target is available for CDP.")

    def close_browser(self):
        if not self._cdp_ready():
            return False

        try:
            version = self._get_json("/json/version", timeout=2)
            ws_url = version.get("webSocketDebuggerUrl")
            if ws_url:
                self._browser_close(ws_url)
                self.log("Dedicated Chrome window was closed after Cookie refresh.")
                return True
        except Exception:
            pass

        closed_any = False
        try:
            targets = self._get_json("/json/list", timeout=2)
        except Exception:
            targets = []

        for target in targets:
            if target.get("type") != "page":
                continue
            target_id = target.get("id")
            if not target_id:
                continue
            try:
                self._get_json(f"/json/close/{target_id}", timeout=2)
                closed_any = True
            except Exception:
                pass

        if closed_any:
            self.log("Dedicated Chrome tabs were closed after Cookie refresh.")
        return closed_any

    def _browser_close(self, ws_url):
        try:
            asyncio.run(self._browser_close_async(ws_url))
        except RuntimeError:
            result = {}

            def target_runner():
                try:
                    asyncio.run(self._browser_close_async(ws_url))
                except Exception as exc:
                    result["error"] = exc

            thread = threading.Thread(target=target_runner, daemon=True)
            thread.start()
            thread.join()
            if "error" in result:
                raise result["error"]

    async def _browser_close_async(self, ws_url):
        async with websockets.connect(ws_url, max_size=8 * 1024 * 1024) as websocket:
            await self._cdp_call(websocket, "Browser.close")

    def _read_cookie_header(self):
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            cookies = asyncio.run(self._read_cookies())
        else:
            cookies = self._read_cookies_in_thread()
        return _format_cookie_header(cookies)

    def _read_cookies_in_thread(self):
        result = {}

        def target():
            try:
                result["cookies"] = asyncio.run(self._read_cookies())
            except Exception as exc:
                result["error"] = exc

        thread = threading.Thread(target=target, daemon=True)
        thread.start()
        thread.join()

        if "error" in result:
            raise result["error"]
        return result.get("cookies", [])

    async def _read_cookies(self):
        ws_url = self._page_websocket_url()
        async with websockets.connect(ws_url, max_size=8 * 1024 * 1024) as websocket:
            await self._cdp_call(websocket, "Network.enable")

            response = await self._cdp_call(
                websocket,
                "Network.getCookies",
                {"urls": self.cookie_urls},
            )
            cookies = response.get("result", {}).get("cookies", [])
            if cookies:
                return cookies

            response = await self._cdp_call(websocket, "Network.getAllCookies")
            return response.get("result", {}).get("cookies", [])

    async def _cdp_call(self, websocket, method, params=None):
        message_id = getattr(self, "_message_id", 0) + 1
        self._message_id = message_id
        await websocket.send(json.dumps({"id": message_id, "method": method, "params": params or {}}))

        while True:
            message = json.loads(await websocket.recv())
            if message.get("id") != message_id:
                continue
            if "error" in message:
                raise BrowserCookieFetchError(f"CDP {method} failed: {message['error']}")
            return message


def fetch_cookie_from_browser(app_dir, timeout_seconds=None, open_login=True, log=None):
    return ChromeCookieFetcher(app_dir, log=log).fetch(timeout_seconds=timeout_seconds, open_login=open_login)


def close_dedicated_browser(app_dir, log=None):
    return ChromeCookieFetcher(app_dir, log=log).close_browser()


def main():
    parser = argparse.ArgumentParser(description="Fetch Goofish Cookie from a dedicated local Chrome CDP session.")
    parser.add_argument("--timeout", type=float, default=_env_float("BROWSER_COOKIE_TIMEOUT_SECONDS", 180))
    parser.add_argument("--no-open", action="store_true", help="Do not launch Chrome or open a login page.")
    parser.add_argument("--update-env", action="store_true", help="Write the fetched Cookie into .env as COOKIES_STR.")
    parser.add_argument("--app-dir", default=Path(__file__).resolve().parent)
    args = parser.parse_args()

    def log(message):
        print(f"[browser-cookie] {message}", flush=True)

    cookie = fetch_cookie_from_browser(
        args.app_dir,
        timeout_seconds=args.timeout,
        open_login=not args.no_open,
        log=log,
    )

    if not cookie:
        print("[browser-cookie] No valid logged-in Cookie was found.", flush=True)
        raise SystemExit(1)

    if args.update_env:
        update_env_cookie(Path(args.app_dir) / ".env", cookie)
        print("[browser-cookie] Updated .env with the browser Cookie.", flush=True)
    else:
        print("[browser-cookie] Valid browser Cookie detected. Use --update-env to save it.", flush=True)


if __name__ == "__main__":
    main()
