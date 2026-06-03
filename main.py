import base64
import json
import asyncio
import time
import os
import websockets
import hashlib
import re
from loguru import logger
from dotenv import load_dotenv, set_key
from XianyuApis import XianyuApis
import sys
import random
from datetime import datetime
from runtime_status import sanitize_network_env


from utils.xianyu_utils import generate_mid, generate_uuid, trans_cookies, generate_device_id, decode_sync_payload
from XianyuAgent import XianyuReplyBot
from context_manager import ChatContextManager


class XianyuLive:
    def __init__(self, cookies_str, reply_bot=None):
        self.xianyu = XianyuApis()
        self.bot = reply_bot or XianyuReplyBot()
        self.base_url = 'wss://wss-goofish.dingtalk.com/'
        self.cookies_str = cookies_str
        self.cookies = trans_cookies(cookies_str)
        self.xianyu.session.cookies.update(self.cookies)  # 直接使用 session.cookies.update
        self.myid = self.cookies['unb']
        self.device_id = generate_device_id(self.myid)
        self.context_manager = ChatContextManager()
        
        # 心跳相关配置
        self.heartbeat_interval = int(os.getenv("HEARTBEAT_INTERVAL", "15"))  # 心跳间隔，默认15秒
        self.heartbeat_timeout = int(os.getenv("HEARTBEAT_TIMEOUT", "5"))     # 心跳超时，默认5秒
        self.last_heartbeat_time = 0
        self.last_heartbeat_response = 0
        self.heartbeat_task = None
        self.ws = None
        
        # Token刷新相关配置
        self.token_refresh_interval = int(os.getenv("TOKEN_REFRESH_INTERVAL", "3600"))  # Token刷新间隔，默认1小时
        self.token_retry_interval = int(os.getenv("TOKEN_RETRY_INTERVAL", "300"))       # Token重试间隔，默认5分钟
        self.last_token_refresh_time = 0
        self.current_token = None
        self.token_refresh_task = None
        self.connection_restart_flag = False  # 连接重启标志
        
        # 人工接管相关配置
        self.manual_mode_conversations = set()  # 存储处于人工接管模式的会话ID
        self.manual_mode_timeout = int(os.getenv("MANUAL_MODE_TIMEOUT", "3600"))  # 人工接管超时时间，默认1小时
        self.manual_mode_timestamps = {}  # 记录进入人工模式的时间
        
        # 消息过期时间配置
        self.message_expire_time = int(os.getenv("MESSAGE_EXPIRE_TIME", "300000"))  # 消息过期时间，默认5分钟
        self.processed_message_ttl = int(os.getenv("PROCESSED_MESSAGE_TTL", "86400"))  # 幂等记录保留时间，默认1天
        self.item_cache_ttl = int(os.getenv("ITEM_CACHE_TTL", "1800"))  # 商品缓存有效期，默认30分钟
        
        # 人工接管关键词，从环境变量读取
        self.toggle_keywords = os.getenv("TOGGLE_KEYWORDS", "。")
        
        # 模拟人工输入配置
        self.simulate_human_typing = os.getenv("SIMULATE_HUMAN_TYPING", "True").lower() == "true"
        self.image_reply_rules = self.load_image_reply_rules()

        self.context_manager.cleanup_processed_messages(self.processed_message_ttl)

    def sync_cookies_from_session(self):
        """将请求Session中的最新Cookie同步到WebSocket连接使用的Cookie字符串。"""
        cookie_str = '; '.join([f"{cookie.name}={cookie.value}" for cookie in self.xianyu.session.cookies])
        if not cookie_str:
            logger.warning("Session中没有可同步的Cookie")
            return

        self.cookies_str = cookie_str
        self.cookies = trans_cookies(cookie_str)
        if self.cookies.get('unb') and self.cookies['unb'] != self.myid:
            self.myid = self.cookies['unb']
            self.device_id = generate_device_id(self.myid)
        logger.debug("已同步最新Cookie到WebSocket连接配置")

    def build_message_key(self, chat_id, sender_id, create_time, content):
        """生成稳定的消息幂等键，用于避免重复自动回复。"""
        raw_key = f"{chat_id}|{sender_id}|{create_time}|{content}"
        return hashlib.sha256(raw_key.encode("utf-8")).hexdigest()

    def env_float(self, name, default):
        try:
            return float(os.getenv(name, str(default)))
        except (TypeError, ValueError):
            return float(default)

    def normalize_seller_reply(self, text):
        """把模型回复收敛成更像正常店家的语气。"""
        if not text:
            return text

        reply = str(text).replace("\r\n", "\n").replace("\r", "\n").strip()
        replacements = [
            (r"^(亲亲|亲爱的|宝子|宝宝|姐妹)[，,、\s]*", ""),
            (r"[，,、\s]*(亲亲|亲爱的|宝子|宝宝|姐妹)[。.!！?？]*$", ""),
            (r"(哈哈哈+|哈哈|hhh+|Hhh+)", ""),
            (r"(嘻嘻+|嘿嘿+|哇塞+|绝绝子|冲鸭|冲呀)", ""),
            (r"([呀啊哦哟啦呢嘛哈])\1+", r"\1"),
            (r"[~～]+", ""),
            (r"([!！]){2,}", "。"),
            (r"([?？]){2,}", "？"),
            (r"([。]){2,}", "。"),
        ]
        for pattern, replacement in replacements:
            reply = re.sub(pattern, replacement, reply, flags=re.IGNORECASE)

        expressive_emojis = (
            "捂脸哭", "笑哭", "偷笑", "奸笑", "嘿哈", "机智", "旺柴",
            "比心", "玫瑰", "鼓掌", "666", "裂开", "抱拳"
        )
        emoji_pattern = "|".join(re.escape(name) for name in expressive_emojis)
        reply = re.sub(rf"(?:\[(?:{emoji_pattern})\])+", "", reply)

        reply = re.sub(r"[ \t]+", " ", reply)
        reply = re.sub(r"\n{3,}", "\n\n", reply)
        reply = re.sub(r"\s+([，。！？,.!?])", r"\1", reply)
        reply = reply.strip(" ，,")

        return reply or text

    def calculate_human_reply_delay(self, reply):
        """根据回复长度估算更接近真人输入的发送延迟。"""
        compact = re.sub(r"\s+", "", reply or "")
        char_count = len(compact)

        min_delay = self.env_float("HUMAN_REPLY_DELAY_MIN", 1.2)
        max_delay = self.env_float("HUMAN_REPLY_DELAY_MAX", 12.0)
        base_min = self.env_float("HUMAN_REPLY_DELAY_BASE_MIN", 0.8)
        base_max = self.env_float("HUMAN_REPLY_DELAY_BASE_MAX", 2.0)
        chars_per_second = max(self.env_float("HUMAN_REPLY_CHARS_PER_SECOND", 7.0), 1.0)
        jitter = self.env_float("HUMAN_REPLY_DELAY_JITTER", 1.2)

        thinking_delay = random.uniform(base_min, base_max)
        typing_delay = char_count / chars_per_second
        total_delay = thinking_delay + typing_delay + random.uniform(0, jitter)

        if char_count <= 8:
            total_delay *= random.uniform(0.75, 0.95)
        elif char_count >= 60:
            total_delay *= random.uniform(1.05, 1.25)

        return max(min_delay, min(total_delay, max_delay))

    def load_image_reply_rules(self):
        """加载关键词自动发图规则。"""
        if os.getenv("AUTO_IMAGE_REPLY_ENABLED", "True").lower() not in ("1", "true", "yes", "on"):
            return []

        config_path = os.getenv("IMAGE_REPLY_CONFIG", "image_replies.json")
        if not os.path.isabs(config_path):
            config_path = os.path.join(os.getcwd(), config_path)

        if not os.path.exists(config_path):
            logger.info(f"未找到图片自动回复配置: {config_path}")
            return []

        try:
            with open(config_path, "r", encoding="utf-8") as f:
                payload = json.load(f)
        except Exception as e:
            logger.warning(f"读取图片自动回复配置失败: {e}")
            return []

        rules = payload.get("rules", []) if isinstance(payload, dict) else payload
        if not isinstance(rules, list):
            logger.warning("图片自动回复配置格式不正确，rules 应为列表")
            return []

        normalized_rules = []
        for rule in rules:
            if not isinstance(rule, dict) or rule.get("enabled", True) is False:
                continue
            keywords = rule.get("keywords", [])
            if isinstance(keywords, str):
                keywords = [keywords]
            keywords = [str(keyword).strip().lower() for keyword in keywords if str(keyword).strip()]
            if not keywords:
                continue

            images = rule.get("images") or rule.get("image") or []
            if isinstance(images, (str, dict)):
                images = [images]

            normalized_images = []
            for image in images:
                if isinstance(image, str):
                    normalized_images.append({"url": image})
                elif isinstance(image, dict):
                    normalized_images.append(image)

            if not normalized_images and not rule.get("text"):
                continue

            item_ids = rule.get("item_ids") or rule.get("item_id") or []
            if isinstance(item_ids, (str, int)):
                item_ids = [item_ids]
            item_ids = [str(item_id).strip() for item_id in item_ids if str(item_id).strip()]

            normalized_rules.append({
                "name": rule.get("name") or ",".join(keywords),
                "item_ids": item_ids,
                "default": bool(rule.get("default", False)),
                "keywords": keywords,
                "match": str(rule.get("match", "contains")).lower(),
                "text": rule.get("text"),
                "images": normalized_images,
            })

        logger.info(f"已加载图片自动回复规则 {len(normalized_rules)} 条")
        return normalized_rules

    def find_image_reply_rule(self, message, item_id=None):
        """按商品和关键词匹配需要自动回复图片的规则。"""
        text = str(message or "").strip().lower()
        if not text:
            return None

        item_id = str(item_id or "").strip()
        default_match = None
        for rule in self.image_reply_rules:
            keywords = rule["keywords"]
            if rule["match"] == "exact":
                matched = text in keywords
            else:
                matched = any(keyword in text for keyword in keywords)
            if not matched:
                continue

            item_ids = rule.get("item_ids", [])
            if item_ids and item_id in item_ids:
                return rule

            if not item_ids and rule.get("default"):
                default_match = default_match or rule

        if default_match:
            return default_match
        return None

    def build_image_content(self, image):
        """把配置中的图片信息转换成闲鱼 IM 自定义消息内容。"""
        url = str(image.get("url", "")).strip()
        if not url.startswith(("http://", "https://")):
            logger.warning(f"图片自动回复暂只支持公网 URL，已跳过: {url}")
            return None

        pic = {
            "height": int(image.get("height") or 0),
            "type": int(image.get("type") or 0),
            "url": url,
            "width": int(image.get("width") or 0),
        }
        return {
            "contentType": 2,
            "image": {
                "pics": [pic]
            }
        }

    async def refresh_token(self):
        """刷新token"""
        try:
            logger.info("开始刷新token...")
            
            # 获取新token（如果Cookie失效，get_token会直接退出程序）
            token_result = self.xianyu.get_token(self.device_id)
            if 'data' in token_result and 'accessToken' in token_result['data']:
                new_token = token_result['data']['accessToken']
                self.current_token = new_token
                self.last_token_refresh_time = time.time()
                self.sync_cookies_from_session()
                logger.info("Token刷新成功")
                return new_token
            else:
                logger.error(f"Token刷新失败: {token_result}")
                return None
                
        except Exception as e:
            logger.error(f"Token刷新异常: {str(e)}")
            return None

    async def token_refresh_loop(self):
        """Token刷新循环"""
        while True:
            try:
                current_time = time.time()
                
                # 检查是否需要刷新token
                if current_time - self.last_token_refresh_time >= self.token_refresh_interval:
                    logger.info("Token即将过期，准备刷新...")
                    
                    new_token = await self.refresh_token()
                    if new_token:
                        logger.info("Token刷新成功，准备重新建立连接...")
                        # 设置连接重启标志
                        self.connection_restart_flag = True
                        # 关闭当前WebSocket连接，触发重连
                        if self.ws:
                            await self.ws.close()
                        break
                    else:
                        logger.error("Token刷新失败，将在{}分钟后重试".format(self.token_retry_interval // 60))
                        await asyncio.sleep(self.token_retry_interval)  # 使用配置的重试间隔
                        continue
                
                # 每分钟检查一次
                await asyncio.sleep(60)
                
            except Exception as e:
                logger.error(f"Token刷新循环出错: {e}")
                await asyncio.sleep(60)

    async def send_custom_content(self, ws, cid, toid, content_payload):
        content_base64 = str(base64.b64encode(json.dumps(content_payload, ensure_ascii=False).encode('utf-8')), 'utf-8')
        msg = {
            "lwp": "/r/MessageSend/sendByReceiverScope",
            "headers": {
                "mid": generate_mid()
            },
            "body": [
                {
                    "uuid": generate_uuid(),
                    "cid": f"{cid}@goofish",
                    "conversationType": 1,
                    "content": {
                        "contentType": 101,
                        "custom": {
                            "type": 1,
                            "data": content_base64
                        }
                    },
                    "redPointPolicy": 0,
                    "extension": {
                        "extJson": "{}"
                    },
                    "ctx": {
                        "appVersion": "1.0",
                        "platform": "web"
                    },
                    "mtags": {},
                    "msgReadStatusSetting": 1
                },
                {
                    "actualReceivers": [
                        f"{toid}@goofish",
                        f"{self.myid}@goofish"
                    ]
                }
            ]
        }
        await ws.send(json.dumps(msg))

    async def send_msg(self, ws, cid, toid, text):
        payload = {
            "contentType": 1,
            "text": {
                "text": text
            }
        }
        await self.send_custom_content(ws, cid, toid, payload)

    async def send_image_msg(self, ws, cid, toid, image):
        payload = self.build_image_content(image)
        if not payload:
            return False
        await self.send_custom_content(ws, cid, toid, payload)
        return True

    async def init(self, ws):
        # 如果没有token或者token过期，获取新token
        if not self.current_token or (time.time() - self.last_token_refresh_time) >= self.token_refresh_interval:
            logger.info("获取初始token...")
            await self.refresh_token()
        
        if not self.current_token:
            logger.error("无法获取有效token，初始化失败")
            raise Exception("Token获取失败")
            
        msg = {
            "lwp": "/reg",
            "headers": {
                "cache-header": "app-key token ua wv",
                "app-key": "444e9908a51d1cb236a27862abc769c9",
                "token": self.current_token,
                "ua": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36 DingTalk(2.1.5) OS(Windows/10) Browser(Chrome/133.0.0.0) DingWeb/2.1.5 IMPaaS DingWeb/2.1.5",
                "dt": "j",
                "wv": "im:3,au:3,sy:6",
                "sync": "0,0;0;0;",
                "did": self.device_id,
                "mid": generate_mid()
            }
        }
        await ws.send(json.dumps(msg))
        # 等待一段时间，确保连接注册完成
        await asyncio.sleep(1)
        msg = {"lwp": "/r/SyncStatus/ackDiff", "headers": {"mid": "5701741704675979 0"}, "body": [
            {"pipeline": "sync", "tooLong2Tag": "PNM,1", "channel": "sync", "topic": "sync", "highPts": 0,
             "pts": int(time.time() * 1000) * 1000, "seq": 0, "timestamp": int(time.time() * 1000)}]}
        await ws.send(json.dumps(msg))
        logger.info('连接注册完成')

    def is_chat_message(self, message):
        """判断是否为用户聊天消息"""
        try:
            return (
                isinstance(message, dict) 
                and "1" in message 
                and isinstance(message["1"], dict)  # 确保是字典类型
                and "10" in message["1"]
                and isinstance(message["1"]["10"], dict)  # 确保是字典类型
                and "reminderContent" in message["1"]["10"]
            )
        except Exception:
            return False

    def is_sync_package(self, message_data):
        """判断是否为同步包消息"""
        try:
            return (
                isinstance(message_data, dict)
                and "body" in message_data
                and "syncPushPackage" in message_data["body"]
                and "data" in message_data["body"]["syncPushPackage"]
                and len(message_data["body"]["syncPushPackage"]["data"]) > 0
            )
        except Exception:
            return False

    def is_typing_status(self, message):
        """判断是否为用户正在输入状态消息"""
        try:
            return (
                isinstance(message, dict)
                and "1" in message
                and isinstance(message["1"], list)
                and len(message["1"]) > 0
                and isinstance(message["1"][0], dict)
                and "1" in message["1"][0]
                and isinstance(message["1"][0]["1"], str)
                and "@goofish" in message["1"][0]["1"]
            )
        except Exception:
            return False

    def is_system_message(self, message):
        """判断是否为系统消息"""
        try:
            return (
                isinstance(message, dict)
                and "3" in message
                and isinstance(message["3"], dict)
                and "needPush" in message["3"]
                and message["3"]["needPush"] == "false"
            )
        except Exception:
            return False

    def capture_message_payload(self, message, reason):
        """把非文本/疑似富媒体消息保存下来，用于反推发送协议。"""
        if os.getenv("CAPTURE_MESSAGE_PAYLOADS", "True").lower() not in ("1", "true", "yes", "on"):
            return

        try:
            os.makedirs("logs", exist_ok=True)
            record = {
                "captured_at": datetime.now().isoformat(),
                "reason": reason,
                "message": message,
            }
            with open("logs/message_capture.jsonl", "a", encoding="utf-8") as f:
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
            logger.info(f"已捕获消息结构用于分析: {reason}")
        except Exception as e:
            logger.warning(f"保存消息结构失败: {e}")

    def is_possible_media_message(self, message, content):
        """识别普通聊天通道里携带的图片/富媒体消息。"""
        content = str(content or "")
        if "图片" in content or "[图" in content:
            return True

        try:
            raw_message = json.dumps(message, ensure_ascii=False).lower()
        except Exception:
            raw_message = str(message).lower()

        media_markers = (
            "image",
            "img",
            "pic",
            "photo",
            "media",
            "url",
            "图片",
        )
        return any(marker in raw_message for marker in media_markers)
    
    def is_bracket_system_message(self, message):
        """检查是否为带中括号的系统消息"""
        try:
            if not message or not isinstance(message, str):
                return False
            
            clean_message = message.strip()
            # 检查是否以 [ 开头，以 ] 结尾
            if clean_message.startswith('[') and clean_message.endswith(']'):
                logger.debug(f"检测到系统消息: {clean_message}")
                return True
            return False
        except Exception as e:
            logger.error(f"检查系统消息失败: {e}")
            return False

    def check_toggle_keywords(self, message):
        """检查消息是否包含切换关键词"""
        message_stripped = message.strip()
        return message_stripped in self.toggle_keywords

    def is_manual_mode(self, chat_id):
        """检查特定会话是否处于人工接管模式"""
        if chat_id not in self.manual_mode_conversations:
            return False
        
        # 检查是否超时
        current_time = time.time()
        if chat_id in self.manual_mode_timestamps:
            if current_time - self.manual_mode_timestamps[chat_id] > self.manual_mode_timeout:
                # 超时，自动退出人工模式
                self.exit_manual_mode(chat_id)
                return False
        
        return True

    def enter_manual_mode(self, chat_id):
        """进入人工接管模式"""
        self.manual_mode_conversations.add(chat_id)
        self.manual_mode_timestamps[chat_id] = time.time()

    def exit_manual_mode(self, chat_id):
        """退出人工接管模式"""
        self.manual_mode_conversations.discard(chat_id)
        if chat_id in self.manual_mode_timestamps:
            del self.manual_mode_timestamps[chat_id]

    def toggle_manual_mode(self, chat_id):
        """切换人工接管模式"""
        if self.is_manual_mode(chat_id):
            self.exit_manual_mode(chat_id)
            self.context_manager.set_auto_reply_override(chat_id, True)
            return "auto"
        else:
            self.enter_manual_mode(chat_id)
            self.context_manager.set_auto_reply_override(chat_id, False)
            return "manual"
    
    def format_price(self, price):
        """
        处理逻辑：标准化价格（分转元）
        """
        try:
            return round(float(price) / 100, 2)
        except (ValueError, TypeError):
            # 遇到 None 或脏数据，默认返回 0
            return 0.0
    
    def build_item_description(self, item_info):
        """构建商品描述"""
        
        # 处理 SKU 列表
        clean_skus = []
        raw_sku_list = item_info.get('skuList', [])
        
        for sku in raw_sku_list:
            # 提取规格文本
            specs = [p['valueText'] for p in sku.get('propertyList', []) if p.get('valueText')]
            spec_text = " ".join(specs) if specs else "默认规格"
            
            clean_skus.append({
                "spec": spec_text,
                "price": self.format_price(sku.get('price', 0)),
                "stock": sku.get('quantity', 0)
            })

        # 获取价格
        valid_prices = [s['price'] for s in clean_skus if s['price'] > 0]
        
        if valid_prices:
            min_price = min(valid_prices)
            max_price = max(valid_prices)
            if min_price == max_price:
                price_display = f"¥{min_price}"
            else:
                price_display = f"¥{min_price} - ¥{max_price}" # 价格区间
        else:
            # 如果没有SKU价格，回退使用商品主价格
            main_price = round(float(item_info.get('soldPrice', 0)), 2)
            price_display = f"¥{main_price}"

        summary = {
            "title": item_info.get('title', ''),
            "desc": item_info.get('desc', ''),
            "price_range": price_display,
            "total_stock": item_info.get('quantity', 0),
            "sku_details": clean_skus
        }

        return json.dumps(summary, ensure_ascii=False)

    async def handle_message(self, message_data, websocket):
        """处理所有类型的消息"""
        try:

            # 如果不是同步包消息，直接返回
            if not self.is_sync_package(message_data):
                return

            # 获取并解密数据
            sync_data = message_data["body"]["syncPushPackage"]["data"][0]
            
            # 检查是否有必要的字段
            if "data" not in sync_data:
                logger.debug("同步包中无data字段")
                return

            # 解密数据
            try:
                data = sync_data["data"]
                try:
                    data = base64.b64decode(data).decode("utf-8")
                    data = json.loads(data)
                    # logger.info(f"无需解密 message: {data}")
                    return
                except Exception as e:
                    # logger.info(f'加密数据: {data}')
                    decrypted_data = decode_sync_payload(data)
                    message = json.loads(decrypted_data)
            except Exception as e:
                logger.error(f"消息解密失败: {e}")
                return

            try:
                # 判断是否为订单消息,需要自行编写付款后的逻辑
                if message['3']['redReminder'] == '等待买家付款':
                    user_id = message['1'].split('@')[0]
                    user_url = f'https://www.goofish.com/personal?userId={user_id}'
                    logger.info(f'等待买家 {user_url} 付款')
                    return
                elif message['3']['redReminder'] == '交易关闭':
                    user_id = message['1'].split('@')[0]
                    user_url = f'https://www.goofish.com/personal?userId={user_id}'
                    logger.info(f'买家 {user_url} 交易关闭')
                    return
                elif message['3']['redReminder'] == '等待卖家发货':
                    user_id = message['1'].split('@')[0]
                    user_url = f'https://www.goofish.com/personal?userId={user_id}'
                    logger.info(f'交易成功 {user_url} 等待卖家发货')
                    return

            except (KeyError, TypeError):
                logger.debug("非订单状态消息，继续按普通消息处理")

            # 判断消息类型
            if self.is_typing_status(message):
                logger.debug("用户正在输入")
                return
            elif not self.is_chat_message(message):
                logger.debug("其他非聊天消息")
                logger.debug(f"原始消息: {message}")
                self.capture_message_payload(message, "non_chat_message")
                return

            # 处理聊天消息
            create_time = int(message["1"]["5"])
            send_user_name = message["1"]["10"]["reminderTitle"]
            send_user_id = message["1"]["10"]["senderUserId"]
            send_message = message["1"]["10"]["reminderContent"]
            if self.is_possible_media_message(message, send_message):
                self.capture_message_payload(message, "possible_media_chat_message")
            
            # 时效性验证（过滤5分钟前消息）
            if (time.time() * 1000 - create_time) > self.message_expire_time:
                logger.debug("过期消息丢弃")
                return
                
            # 获取商品ID和会话ID
            url_info = message["1"]["10"]["reminderUrl"]
            item_id = url_info.split("itemId=")[1].split("&")[0] if "itemId=" in url_info else None
            chat_id = message["1"]["2"].split('@')[0]
            
            if not item_id:
                logger.warning("无法获取商品ID")
                return

            message_key = self.build_message_key(chat_id, send_user_id, create_time, send_message)
            if self.context_manager.is_message_processed(message_key):
                logger.info(f"检测到重复消息，跳过处理 (会话: {chat_id}, 商品: {item_id})")
                return

            # 检查是否为卖家（自己）发送的控制命令
            if send_user_id == self.myid:
                logger.debug("检测到卖家消息，检查是否为控制命令")
                
                # 检查切换命令
                if self.check_toggle_keywords(send_message):
                    mode = self.toggle_manual_mode(chat_id)
                    if mode == "manual":
                        logger.info(f"🔴 已接管会话 {chat_id} (商品: {item_id})")
                    else:
                        logger.info(f"🟢 已恢复会话 {chat_id} 的自动回复 (商品: {item_id})")
                    self.context_manager.mark_message_processed(message_key, chat_id, item_id)
                    return
                
                # 如果卖家先发起一个新会话，默认视为人工沟通，不启用自动回复。
                existing_context = self.context_manager.get_context_by_chat(chat_id)
                if not existing_context:
                    self.enter_manual_mode(chat_id)
                    logger.info(f"🔴 检测到卖家主动发起新会话 {chat_id}，已默认接管 (商品: {item_id})")

                # 记录卖家人工回复
                self.context_manager.add_message_by_chat(chat_id, self.myid, item_id, "assistant", send_message)
                self.context_manager.mark_message_processed(message_key, chat_id, item_id)
                logger.info(f"卖家人工回复 (会话: {chat_id}, 商品: {item_id}): {send_message}")
                return
            
            logger.info(f"用户: {send_user_name} (ID: {send_user_id}), 商品: {item_id}, 会话: {chat_id}, 消息: {send_message}")
            
            # 卖家主动发起的会话始终保持人工沟通，避免后续买家回复被自动接管。
            if (
                self.context_manager.is_chat_started_by_assistant(chat_id)
                and not self.context_manager.has_auto_reply_override(chat_id)
            ):
                if not self.is_manual_mode(chat_id):
                    self.enter_manual_mode(chat_id)
                logger.info(f"🔴 会话 {chat_id} 由卖家主动发起，跳过自动回复")
                self.context_manager.add_message_by_chat(chat_id, send_user_id, item_id, "user", send_message)
                self.context_manager.mark_message_processed(message_key, chat_id, item_id)
                return
            
            # 如果当前会话处于人工接管模式，不进行自动回复
            if self.is_manual_mode(chat_id):
                logger.info(f"🔴 会话 {chat_id} 处于人工接管模式，跳过自动回复")
                # 添加用户消息到上下文
                self.context_manager.add_message_by_chat(chat_id, send_user_id, item_id, "user", send_message)
                self.context_manager.mark_message_processed(message_key, chat_id, item_id)
                return
            # 检查是否为带中括号的系统消息
            if self.is_bracket_system_message(send_message):
                logger.info(f"检测到系统消息：'{send_message}'，跳过自动回复")
                self.context_manager.mark_message_processed(message_key, chat_id, item_id)
                return
            if self.is_system_message(message):
                logger.debug("系统消息，跳过处理")
                self.context_manager.mark_message_processed(message_key, chat_id, item_id)
                return

            image_reply_rule = self.find_image_reply_rule(send_message, item_id)
            if image_reply_rule:
                logger.info(f"命中图片自动回复规则: {image_reply_rule['name']} (商品: {item_id})")

                self.context_manager.add_message_by_chat(chat_id, send_user_id, item_id, "user", send_message)

                reply_text = str(image_reply_rule.get("text") or "").strip()
                sent_parts = []
                if reply_text:
                    await self.send_msg(websocket, chat_id, send_user_id, reply_text)
                    sent_parts.append(reply_text)

                sent_image_count = 0
                for image in image_reply_rule.get("images", []):
                    if await self.send_image_msg(websocket, chat_id, send_user_id, image):
                        sent_image_count += 1
                        sent_parts.append(f"[图片]{image.get('url', '')}")
                        await asyncio.sleep(float(os.getenv("IMAGE_REPLY_SEND_INTERVAL", "0.8")))

                if sent_image_count == 0 and not reply_text:
                    logger.warning(f"图片自动回复规则 {image_reply_rule['name']} 未发送任何内容")
                else:
                    assistant_record = "\n".join(sent_parts) or f"[图片自动回复 {sent_image_count} 张]"
                    self.context_manager.add_message_by_chat(chat_id, self.myid, item_id, "assistant", assistant_record)

                self.context_manager.mark_message_processed(message_key, chat_id, item_id)
                return

            # 从数据库中获取商品信息，如果不存在则从API获取并保存
            item_info = self.context_manager.get_item_info(item_id, max_age_seconds=self.item_cache_ttl)
            if not item_info:
                logger.info(f"从API获取商品信息: {item_id}")
                api_result = self.xianyu.get_item_info(item_id)
                if 'data' in api_result and 'itemDO' in api_result['data']:
                    item_info = api_result['data']['itemDO']
                    # 保存商品信息到数据库
                    self.context_manager.save_item_info(item_id, item_info)
                else:
                    logger.warning(f"获取商品信息失败: {api_result}")
                    return
            else:
                logger.info(f"从数据库获取商品信息: {item_id}")
                
            item_description=f"当前商品的信息如下：{self.build_item_description(item_info)}"
            
            # 获取完整的对话上下文
            context = self.context_manager.get_context_by_chat(chat_id)
            # 生成回复
            bot_reply = self.bot.generate_reply(
                send_message,
                item_description,
                context=context
            )
            
            # 检查是否需要回复
            if bot_reply == "-":
                logger.info(f"[无需回复] 用户 {send_user_name} 的消息被识别为无需回复类型")
                self.context_manager.mark_message_processed(message_key, chat_id, item_id)
                return

            normalized_reply = self.normalize_seller_reply(bot_reply)
            if normalized_reply != bot_reply:
                logger.info(f"已收敛回复语气: {bot_reply} -> {normalized_reply}")
                bot_reply = normalized_reply
            
            # 添加用户消息到上下文
            self.context_manager.add_message_by_chat(chat_id, send_user_id, item_id, "user", send_message)
            
            # 检查是否为价格意图，如果是则增加议价次数
            if self.bot.last_intent == "price" and self.bot.should_count_bargain(send_message):
                self.context_manager.increment_bargain_count_by_chat(chat_id)
                bargain_count = self.context_manager.get_bargain_count_by_chat(chat_id)
                logger.info(f"用户 {send_user_name} 对商品 {item_id} 的议价次数: {bargain_count}")
            
            # 添加机器人回复到上下文
            self.context_manager.add_message_by_chat(chat_id, self.myid, item_id, "assistant", bot_reply)
            
            logger.info(f"机器人回复: {bot_reply}")
            
            # 模拟人工输入延迟
            if self.simulate_human_typing:
                total_delay = self.calculate_human_reply_delay(bot_reply)
                logger.info(f"模拟真人回复节奏，文本长度 {len(bot_reply)}，延迟发送 {total_delay:.2f} 秒...")
                await asyncio.sleep(total_delay)
                
            await self.send_msg(websocket, chat_id, send_user_id, bot_reply)
            self.context_manager.mark_message_processed(message_key, chat_id, item_id)
            
        except Exception as e:
            logger.exception(f"处理消息时发生错误: {str(e)}")
            logger.debug(f"原始消息: {message_data}")

    async def send_heartbeat(self, ws):
        """发送心跳包并等待响应"""
        try:
            heartbeat_mid = generate_mid()
            heartbeat_msg = {
                "lwp": "/!",
                "headers": {
                    "mid": heartbeat_mid
                }
            }
            await ws.send(json.dumps(heartbeat_msg))
            self.last_heartbeat_time = time.time()
            logger.debug("心跳包已发送")
            return heartbeat_mid
        except Exception as e:
            logger.error(f"发送心跳包失败: {e}")
            raise

    async def heartbeat_loop(self, ws):
        """心跳维护循环"""
        while True:
            try:
                current_time = time.time()
                
                # 检查是否需要发送心跳
                if current_time - self.last_heartbeat_time >= self.heartbeat_interval:
                    await self.send_heartbeat(ws)
                
                # 检查上次心跳响应时间，如果超时则认为连接已断开
                if (current_time - self.last_heartbeat_response) > (self.heartbeat_interval + self.heartbeat_timeout):
                    logger.warning("心跳响应超时，可能连接已断开")
                    break
                
                await asyncio.sleep(1)
            except Exception as e:
                logger.error(f"心跳循环出错: {e}")
                break

    async def handle_heartbeat_response(self, message_data):
        """处理心跳响应"""
        try:
            if (
                isinstance(message_data, dict)
                and "headers" in message_data
                and "mid" in message_data["headers"]
                and "code" in message_data
                and message_data["code"] == 200
            ):
                self.last_heartbeat_response = time.time()
                logger.debug("收到心跳响应")
                return True
        except Exception as e:
            logger.error(f"处理心跳响应出错: {e}")
        return False

    async def main(self):
        while True:
            try:
                # 重置连接重启标志
                self.connection_restart_flag = False
                
                headers = {
                    "Cookie": self.cookies_str,
                    "Host": "wss-goofish.dingtalk.com",
                    "Connection": "Upgrade",
                    "Pragma": "no-cache",
                    "Cache-Control": "no-cache",
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/133.0.0.0 Safari/537.36",
                    "Origin": "https://www.goofish.com",
                    "Accept-Encoding": "gzip, deflate, br, zstd",
                    "Accept-Language": "zh-CN,zh;q=0.9",
                }

                async with websockets.connect(self.base_url, extra_headers=headers) as websocket:
                    self.ws = websocket
                    await self.init(websocket)
                    
                    # 初始化心跳时间
                    self.last_heartbeat_time = time.time()
                    self.last_heartbeat_response = time.time()
                    
                    # 启动心跳任务
                    self.heartbeat_task = asyncio.create_task(self.heartbeat_loop(websocket))
                    
                    # 启动token刷新任务
                    self.token_refresh_task = asyncio.create_task(self.token_refresh_loop())
                    
                    async for message in websocket:
                        try:
                            # 检查是否需要重启连接
                            if self.connection_restart_flag:
                                logger.info("检测到连接重启标志，准备重新建立连接...")
                                break
                                
                            message_data = json.loads(message)
                            
                            # 处理心跳响应
                            if await self.handle_heartbeat_response(message_data):
                                continue
                            
                            # 发送通用ACK响应
                            if "headers" in message_data and "mid" in message_data["headers"]:
                                ack = {
                                    "code": 200,
                                    "headers": {
                                        "mid": message_data["headers"]["mid"],
                                        "sid": message_data["headers"].get("sid", "")
                                    }
                                }
                                # 复制其他可能的header字段
                                for key in ["app-key", "ua", "dt"]:
                                    if key in message_data["headers"]:
                                        ack["headers"][key] = message_data["headers"][key]
                                await websocket.send(json.dumps(ack))
                            
                            # 处理其他消息
                            await self.handle_message(message_data, websocket)
                                
                        except json.JSONDecodeError:
                            logger.error("消息解析失败")
                        except Exception as e:
                            logger.error(f"处理消息时发生错误: {str(e)}")
                            logger.debug(f"原始消息: {message}")

            except websockets.exceptions.ConnectionClosed:
                logger.warning("WebSocket连接已关闭")
                
            except Exception as e:
                logger.error(f"连接发生错误: {e}")
                
            finally:
                # 清理任务
                if self.heartbeat_task:
                    self.heartbeat_task.cancel()
                    try:
                        await self.heartbeat_task
                    except asyncio.CancelledError:
                        pass
                        
                if self.token_refresh_task:
                    self.token_refresh_task.cancel()
                    try:
                        await self.token_refresh_task
                    except asyncio.CancelledError:
                        pass
                
                # 如果是主动重启，立即重连；否则等待5秒
                if self.connection_restart_flag:
                    logger.info("主动重启连接，立即重连...")
                else:
                    logger.info("等待5秒后重连...")
                    await asyncio.sleep(5)



def check_and_complete_env():
    """检查并补全关键环境变量"""
    # 定义关键变量及其默认无效值（占位符）
    critical_vars = {
        "API_KEY": "默认使用通义千问,apikey通过百炼模型平台获取",
        "COOKIES_STR": "your_cookies_here"
    }
    
    env_path = ".env"
    updated = False
    
    for key, placeholder in critical_vars.items():
        curr_val = os.getenv(key)
        
        # 如果变量未设置，或者值等于占位符
        if not curr_val or curr_val == placeholder:
            logger.warning(f"配置项 [{key}] 未设置或为默认值，请输入")
            while True:
                if key == "COOKIES_STR":
                    val = XianyuApis().prompt_new_cookie()
                else:
                    val = input(f"请输入 {key}: ").strip()
                if val:
                    # 更新当前环境
                    os.environ[key] = val
                    
                    # 尝试持久化到 .env
                    try:
                        # 如果没有.env文件，先创建
                        if not os.path.exists(env_path):
                            with open(env_path, 'w', encoding='utf-8') as f:
                                pass # Create empty file
                        
                        set_key(env_path, key, val)
                        updated = True
                    except Exception as e:
                        logger.warning(f"无法自动写入.env文件，请手动保存: {e}")
                    break
                else:
                    print(f"{key} 不能为空，请重新输入")
    
    if updated:
        logger.info("新的配置已保存/更新至 .env 文件中")


if __name__ == '__main__':
    sanitize_network_env()

    # 加载环境变量
    if os.path.exists(".env"):
        load_dotenv()
        logger.info("已加载 .env 配置")
    
    if os.path.exists(".env.example"):
        load_dotenv(".env.example")  # 不会覆盖已存在的变量
        logger.info("已加载 .env.example 默认配置")
    
    # 配置日志级别
    log_level = os.getenv("LOG_LEVEL", "DEBUG").upper()
    logger.remove()  # 移除默认handler
    logger.add(
        sys.stderr,
        level=log_level,
        format="<green>{time:YYYY-MM-DD HH:mm:ss.SSS}</green> | <level>{level: <8}</level> | <cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> - <level>{message}</level>"
    )
    logger.info(f"日志级别设置为: {log_level}")
    
    # 交互式检查并补全配置
    check_and_complete_env()
    
    cookies_str = os.getenv("COOKIES_STR")
    bot = XianyuReplyBot()
    xianyuLive = XianyuLive(cookies_str, reply_bot=bot)
    # 常驻进程
    asyncio.run(xianyuLive.main())
