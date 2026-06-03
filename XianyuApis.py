import time
import os
import re
import sys
import hashlib

import requests
from loguru import logger
from utils.xianyu_utils import generate_sign


class XianyuApis:
    def __init__(self):
        self.url = 'https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.login.token/1.0/'
        self.session = requests.Session()
        self.session.headers.update({
            'accept': 'application/json',
            'accept-language': 'zh-CN,zh;q=0.9',
            'cache-control': 'no-cache',
            'origin': 'https://www.goofish.com',
            'pragma': 'no-cache',
            'priority': 'u=1, i',
            'referer': 'https://www.goofish.com/',
            'sec-ch-ua': '"Not(A:Brand";v="99", "Google Chrome";v="133", "Chromium";v="133"',
            'sec-ch-ua-mobile': '?0',
            'sec-ch-ua-platform': '"Windows"',
            'sec-fetch-dest': 'empty',
            'sec-fetch-mode': 'cors',
            'sec-fetch-site': 'same-site',
            'user-agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36',
        })
        self.browser_cookie_hashes = set()

    def auto_fetch_cookie_from_browser(self):
        """Try to fetch a fresh Cookie from the local dedicated browser session."""
        enabled = os.getenv("AUTO_COOKIE_FROM_BROWSER", "True").strip().lower()
        if enabled in ("0", "false", "no", "off", ""):
            return ""

        try:
            from browser_cookie_fetcher import fetch_cookie_from_browser

            timeout = float(os.getenv("BROWSER_COOKIE_PROMPT_TIMEOUT", "180"))
            logger.info("尝试从本机专用浏览器会话自动获取Cookie...")
            cookie = fetch_cookie_from_browser(
                os.getcwd(),
                timeout_seconds=timeout,
                open_login=True,
                log=lambda message: logger.info(f"[browser-cookie] {message}"),
            )
            if cookie:
                cookie_hash = hashlib.sha256(cookie.encode("utf-8")).hexdigest()
                if cookie_hash in self.browser_cookie_hashes:
                    logger.warning("浏览器Cookie尚未变化，请在专用Chrome消息页完成验证后再试")
                    return ""
                self.browser_cookie_hashes.add(cookie_hash)
                logger.success("已从浏览器会话获取Cookie")
                return cookie
        except Exception as e:
            logger.warning(f"浏览器Cookie自动获取失败，退回手动输入: {e}")

        return ""

    def prompt_new_cookie(self):
        """
        读取新的Cookie字符串。

        支持三种输入方式：
        1. 直接粘贴完整Cookie后回车，TTY下使用raw模式读取，避免超长单行被截断。
        2. 输入 BEGIN 后多行粘贴，最后单独输入 END。
        3. 输入 @文件路径，从文件读取Cookie。
        """
        browser_cookie = self.auto_fetch_cookie_from_browser()
        if browser_cookie:
            return browser_cookie

        print("\n" + "=" * 50)
        print("请输入新的Cookie字符串。")
        print("提示: 直接粘贴完整Cookie并回车；粘贴内容不会回显，会显示读取到的字符数。")
        print("如粘贴内容本身包含换行，输入 BEGIN 后多行粘贴，最后输入 END。")
        print("也可以输入 @/path/to/cookie.txt 从文件读取；直接回车则退出程序。")

        first_line = self.read_cookie_line("Cookie> ").strip()
        if not first_line:
            print("=" * 50 + "\n")
            return ""

        if first_line.startswith("@"):
            file_path = os.path.expanduser(first_line[1:].strip())
            with open(file_path, "r", encoding="utf-8") as f:
                cookie_text = f.read()
            print("=" * 50 + "\n")
            return self.normalize_cookie_input(cookie_text)

        if first_line.upper() == "BEGIN":
            lines = []
            while True:
                line = input()
                if line.strip().upper() == "END":
                    break
                lines.append(line)
            print("=" * 50 + "\n")
            return self.normalize_cookie_input("\n".join(lines))

        print("=" * 50 + "\n")
        return self.normalize_cookie_input(first_line)

    def read_cookie_line(self, prompt):
        """
        读取一行Cookie输入。

        普通input在TTY canonical模式下容易受超长单行限制影响；这里在交互式
        终端中改用raw模式逐字符读取到回车，保证直接粘贴长Cookie也能完整接收。
        """
        if not sys.stdin.isatty():
            return input(prompt)

        try:
            import termios
            import tty
        except ImportError:
            return input(prompt)

        fd = sys.stdin.fileno()
        old_settings = termios.tcgetattr(fd)
        chars = []

        sys.stdout.write(prompt)
        sys.stdout.flush()

        try:
            tty.setraw(fd)
            while True:
                ch = sys.stdin.read(1)
                if ch in ("\r", "\n", "\x04"):
                    break
                if ch == "\x03":
                    raise KeyboardInterrupt
                if ch in ("\x7f", "\b"):
                    if chars:
                        chars.pop()
                    continue
                chars.append(ch)
        finally:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)

        sys.stdout.write(f"\n已读取 {len(chars)} 个字符\n")
        sys.stdout.flush()
        return "".join(chars)

    def normalize_cookie_input(self, cookie_text):
        """清理浏览器复制出来的Cookie文本，兼容Cookie:前缀和多行粘贴。"""
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
        
    def clear_duplicate_cookies(self):
        """清理重复的cookies"""
        # 创建一个新的CookieJar
        new_jar = requests.cookies.RequestsCookieJar()
        
        # 记录已经添加过的cookie名称
        added_cookies = set()
        
        # 按照cookies列表的逆序遍历（最新的通常在后面）
        cookie_list = list(self.session.cookies)
        cookie_list.reverse()
        
        for cookie in cookie_list:
            # 如果这个cookie名称还没有添加过，就添加到新jar中
            if cookie.name not in added_cookies:
                new_jar.set_cookie(cookie)
                added_cookies.add(cookie.name)
                
        # 替换session的cookies
        self.session.cookies = new_jar
        
        # 更新完cookies后，更新.env文件
        self.update_env_cookies()
        
    def update_env_cookies(self):
        """更新.env文件中的COOKIES_STR"""
        try:
            # 获取当前cookies的字符串形式
            cookie_str = '; '.join([f"{cookie.name}={cookie.value}" for cookie in self.session.cookies])
            
            # 读取.env文件
            env_path = os.path.join(os.getcwd(), '.env')
            if not os.path.exists(env_path):
                logger.warning(".env文件不存在，无法更新COOKIES_STR")
                return
                
            with open(env_path, 'r', encoding='utf-8') as f:
                env_content = f.read()
                
            # 使用正则表达式替换COOKIES_STR的值
            if 'COOKIES_STR=' in env_content:
                new_env_content = re.sub(
                    r'COOKIES_STR=.*', 
                    f'COOKIES_STR={cookie_str}',
                    env_content
                )
                
                # 写回.env文件
                with open(env_path, 'w', encoding='utf-8') as f:
                    f.write(new_env_content)
                    
                logger.debug("已更新.env文件中的COOKIES_STR")
            else:
                logger.warning(".env文件中未找到COOKIES_STR配置项")
        except Exception as e:
            logger.warning(f"更新.env文件失败: {str(e)}")
        
    def hasLogin(self, retry_count=0):
        """调用hasLogin.do接口进行登录状态检查"""
        if retry_count >= 2:
            logger.error("Login检查失败，重试次数过多")
            return False
            
        try:
            url = 'https://passport.goofish.com/newlogin/hasLogin.do'
            params = {
                'appName': 'xianyu',
                'fromSite': '77'
            }
            data = {
                'hid': self.session.cookies.get('unb', ''),
                'ltl': 'true',
                'appName': 'xianyu',
                'appEntrance': 'web',
                '_csrf_token': self.session.cookies.get('XSRF-TOKEN', ''),
                'umidToken': '',
                'hsiz': self.session.cookies.get('cookie2', ''),
                'bizParams': 'taobaoBizLoginFrom=web',
                'mainPage': 'false',
                'isMobile': 'false',
                'lang': 'zh_CN',
                'returnUrl': '',
                'fromSite': '77',
                'isIframe': 'true',
                'documentReferer': 'https://www.goofish.com/',
                'defaultView': 'hasLogin',
                'umidTag': 'SERVER',
                'deviceId': self.session.cookies.get('cna', '')
            }
            
            response = self.session.post(url, params=params, data=data)
            res_json = response.json()
            
            if res_json.get('content', {}).get('success'):
                logger.debug("Login成功")
                # 清理和更新cookies
                self.clear_duplicate_cookies()
                return True
            else:
                logger.warning(f"Login失败: {res_json}")
                time.sleep(0.5)
                return self.hasLogin(retry_count + 1)
                
        except Exception as e:
            logger.error(f"Login请求异常: {str(e)}")
            time.sleep(0.5)
            return self.hasLogin(retry_count + 1)

    def get_token(self, device_id, retry_count=0, relogin_count=0):
        max_relogin_count = int(os.getenv("TOKEN_MAX_RELOGIN_COUNT", "2"))
        if retry_count >= 2:  # 最多重试3次
            if relogin_count >= max_relogin_count:
                logger.error("Token获取失败，重新登录次数已达上限")
                logger.error("🔴 Cookie登录态已失效，请更新.env文件中的COOKIES_STR后重新启动")
                sys.exit(1)

            logger.warning("获取token失败，尝试重新登陆")
            # 尝试通过hasLogin重新登录
            if self.hasLogin():
                logger.info("重新登录成功，重新尝试获取token")
                return self.get_token(device_id, 0, relogin_count + 1)  # 重置单轮重试次数
            else:
                logger.error("重新登录失败，Cookie已失效")
                logger.error("🔴 程序即将退出，请更新.env文件中的COOKIES_STR后重新启动")
                sys.exit(1)  # 直接退出程序
            
        params = {
            'jsv': '2.7.2',
            'appKey': '34839810',
            't': str(int(time.time()) * 1000),
            'sign': '',
            'v': '1.0',
            'type': 'originaljson',
            'accountSite': 'xianyu',
            'dataType': 'json',
            'timeout': '20000',
            'api': 'mtop.taobao.idlemessage.pc.login.token',
            'sessionOption': 'AutoLoginOnly',
            'spm_cnt': 'a21ybx.im.0.0',
        }
        data_val = '{"appKey":"444e9908a51d1cb236a27862abc769c9","deviceId":"' + device_id + '"}'
        data = {
            'data': data_val,
        }
        
        # 简单获取token，信任cookies已清理干净
        token = self.session.cookies.get('_m_h5_tk', '').split('_')[0]
        
        sign = generate_sign(params['t'], token, data_val)
        params['sign'] = sign
        
        try:
            response = self.session.post(
                'https://h5api.m.goofish.com/h5/mtop.taobao.idlemessage.pc.login.token/1.0/',
                params=params,
                data=data,
                timeout=20
            )
            res_json = response.json()
            
            if isinstance(res_json, dict):
                ret_value = res_json.get('ret', [])
                # 检查ret是否包含成功信息
                if not any('SUCCESS::调用成功' in ret for ret in ret_value):
                    # 检测风控/限流错误
                    error_msg = str(ret_value)
                    if 'RGV587_ERROR' in error_msg or '被挤爆啦' in error_msg:
                        logger.error(f"❌ 触发风控: {ret_value}")
                        logger.error("🔴 系统目前无法自动解决，请进入专用Chrome的闲鱼消息页并完成滑块/验证")
                        if os.getenv("XIANYU_MONITOR_MODE", "").strip() == "1":
                            logger.error("XIANYU_VERIFICATION_REQUIRED: Goofish token API triggered slider verification")
                            sys.exit(1)

                        if relogin_count >= max_relogin_count:
                            logger.error("🔴 Cookie登录态已失效或消息页验证仍未完成，请完成验证后等待监控脚本自动重试")
                            sys.exit(1)
                        
                        new_cookie_str = self.prompt_new_cookie()
                        
                        if new_cookie_str:
                            try:
                                # 解析cookie字符串并更新session
                                from http.cookies import SimpleCookie
                                cookie = SimpleCookie()
                                cookie.load(new_cookie_str)
                                
                                # 清空旧cookie并设置新cookie
                                self.session.cookies.clear()
                                for key, morsel in cookie.items():
                                    self.session.cookies.set(key, morsel.value, domain='.goofish.com')
                                
                                logger.success("✅ Cookie已更新，正在尝试重连...")
                                # 同步更新到.env文件
                                self.update_env_cookies()
                                
                                # 立即重试
                                return self.get_token(device_id, 0, relogin_count + 1)
                            except Exception as e:
                                logger.error(f"Cookie解析失败: {e}")
                                sys.exit(1)
                        else:
                            logger.info("用户取消输入，程序退出")
                            sys.exit(1)

                    logger.warning(f"Token API调用失败，错误信息: {ret_value}")
                    # 处理响应中的Set-Cookie
                    if 'Set-Cookie' in response.headers:
                        logger.debug("检测到Set-Cookie，更新cookie")  # 降级为DEBUG并简化
                        self.clear_duplicate_cookies()
                    time.sleep(0.5)
                    return self.get_token(device_id, retry_count + 1, relogin_count)
                else:
                    logger.info("Token获取成功")
                    return res_json
            else:
                logger.error(f"Token API返回格式异常: {res_json}")
                return self.get_token(device_id, retry_count + 1, relogin_count)
                
        except Exception as e:
            logger.error(f"Token API请求异常: {str(e)}")
            time.sleep(0.5)
            return self.get_token(device_id, retry_count + 1, relogin_count)

    def get_item_info(self, item_id, retry_count=0):
        """获取商品信息，自动处理token失效的情况"""
        if retry_count >= 3:  # 最多重试3次
            logger.error("获取商品信息失败，重试次数过多")
            return {"error": "获取商品信息失败，重试次数过多"}
            
        params = {
            'jsv': '2.7.2',
            'appKey': '34839810',
            't': str(int(time.time()) * 1000),
            'sign': '',
            'v': '1.0',
            'type': 'originaljson',
            'accountSite': 'xianyu',
            'dataType': 'json',
            'timeout': '20000',
            'api': 'mtop.taobao.idle.pc.detail',
            'sessionOption': 'AutoLoginOnly',
            'spm_cnt': 'a21ybx.im.0.0',
        }
        
        data_val = '{"itemId":"' + item_id + '"}'
        data = {
            'data': data_val,
        }
        
        # 简单获取token，信任cookies已清理干净
        token = self.session.cookies.get('_m_h5_tk', '').split('_')[0]
        
        sign = generate_sign(params['t'], token, data_val)
        params['sign'] = sign
        
        try:
            response = self.session.post(
                'https://h5api.m.goofish.com/h5/mtop.taobao.idle.pc.detail/1.0/', 
                params=params, 
                data=data
            )
            
            res_json = response.json()
            # 检查返回状态
            if isinstance(res_json, dict):
                ret_value = res_json.get('ret', [])
                # 检查ret是否包含成功信息
                if not any('SUCCESS::调用成功' in ret for ret in ret_value):
                    logger.warning(f"商品信息API调用失败，错误信息: {ret_value}")
                    # 处理响应中的Set-Cookie
                    if 'Set-Cookie' in response.headers:
                        logger.debug("检测到Set-Cookie，更新cookie")
                        self.clear_duplicate_cookies()
                    time.sleep(0.5)
                    return self.get_item_info(item_id, retry_count + 1)
                else:
                    logger.debug(f"商品信息获取成功: {item_id}")
                    return res_json
            else:
                logger.error(f"商品信息API返回格式异常: {res_json}")
                return self.get_item_info(item_id, retry_count + 1)
                
        except Exception as e:
            logger.error(f"商品信息API请求异常: {str(e)}")
            time.sleep(0.5)
            return self.get_item_info(item_id, retry_count + 1)
