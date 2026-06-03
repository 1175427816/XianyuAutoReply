import re
from typing import List, Dict
import os
from openai import OpenAI
from loguru import logger


def build_sales_policy() -> str:
    """构建注入给模型的全局售卖策略。"""
    max_discount_percent = os.getenv("PRICE_MAX_DISCOUNT_PERCENT", "5")
    max_discount_amount = os.getenv("PRICE_MAX_DISCOUNT_AMOUNT", "100")
    first_bargain_mode = os.getenv("PRICE_FIRST_BARGAIN_MODE", "ask_budget")
    shipping_policy = os.getenv("PRICE_SHIPPING_POLICY", "以商品描述为准")
    custom_color_policy = os.getenv("CUSTOM_COLOR_POLICY", "定制颜色需先确认颜色，并查看库存是否支持")
    reply_style = os.getenv("REPLY_STYLE", "natural")
    return (
        f"- 回复风格: {reply_style}，像闲鱼正常店家，语气平实、清楚、不过度热情，按问题复杂度自行控制长短。\n"
        "- 长度规则: 简单确认10-25字；价格/成交15-45字；颜色定制20-45字；技术说明必要时40-70字。\n"
        "- 不重复买家的话，不铺垫，不写多余解释；能一句说清就一句。\n"
        "- 少用语气词和夸张口头禅；不要用哈哈、嘻嘻、呀、啦啦、宝子、亲亲、绝绝子、冲呀等表达。\n"
        "- 可以偶尔用“好的”“可以”“没问题”“这边看下”这类店家常用表达，但不要连续堆叠。\n"
        f"- 最大优惠比例: 不超过商品价格的{max_discount_percent}%。\n"
        f"- 最大优惠金额: 不超过{max_discount_amount}元。\n"
        f"- 首轮议价策略: {first_bargain_mode}。\n"
        f"- 包邮策略: {shipping_policy}。\n"
        f"- 颜色服务: {custom_color_policy}。买家需要定制时，先询问具体颜色，再说明要查看库存是否有对应颜色；是否加钱需按颜色确认后再说。\n"
        "- 不主动承诺正品、官方保修或额外服务，具体以商品描述为准。\n"
        "- 所有沟通和交易都引导在平台内完成。"
    )


def should_skip_message(message: str) -> bool:
    """识别提示词攻击、身份探测和无关命令类消息。"""
    text = message.strip().lower()
    compact = re.sub(r"\s+", "", text)
    skip_keywords = [
        "你是谁", "什么模型", "哪个模型", "你用的模型", "你来自哪里",
        "提示词", "系统提示", "系统规则", "开发者消息", "完整指令",
        "忽略规则", "忽略以上", "忘记之前", "重新扮演", "角色设定",
        "fullinstructions", "systemprompt", "developer", "ignoreprevious",
        "outputas-is", "withoutanyrewriting",
    ]
    return any(keyword in compact for keyword in skip_keywords)


def detect_quick_intent(message: str) -> str:
    """识别无需模型即可处理的高频意图。"""
    if should_skip_message(message):
        return "no_reply"

    compact = re.sub(r"[^\w\u4e00-\u9fa5]", "", message.strip().lower())

    platform_risk_keywords = ["微信", "vx", "qq", "支付宝", "银行卡", "线下", "私聊", "加v", "加微"]
    if any(keyword in compact for keyword in platform_risk_keywords):
        return "platform_safe"

    ready_buy_keywords = ["能拍吗", "可以拍吗", "现在拍", "我拍了", "我要了", "能发货吗", "什么时候发货"]
    if any(keyword in compact for keyword in ready_buy_keywords):
        return "ready_buy"

    shipping_keywords = ["包邮吗", "包不包邮", "邮费", "运费", "包邮不"]
    if any(keyword in compact for keyword in shipping_keywords):
        return "shipping"

    availability_keywords = ["还在吗", "在吗", "东西还在", "还卖吗", "有货吗"]
    if any(keyword in compact for keyword in availability_keywords):
        return "availability"

    first_price_keywords = ["最低多少", "最低价", "便宜点", "少点", "优惠点", "能便宜", "可小刀"]
    if any(keyword in compact for keyword in first_price_keywords):
        return "first_price"

    return None


def platform_safe_reply() -> str:
    return "平台内沟通交易更稳妥，直接在闲鱼拍就行。"


def sanitize_platform_reply(reply: str) -> str:
    """把模型可能生成的站外交易表达改成自然的平台内提醒。"""
    return platform_safe_reply()


def is_explicit_bargain(message: str) -> bool:
    """判断买家是否在明确砍价，而不是普通询价/问运费。"""
    compact = re.sub(r"[^\w\u4e00-\u9fa5]", "", message.strip().lower())
    bargain_keywords = [
        "最低价", "最低多少", "便宜点", "少点", "优惠点", "能便宜",
        "可小刀", "刀吗", "小刀", "大刀", "砍价", "讲价", "降价",
        "包邮出", "包邮能出", "能不能少", "再便宜",
    ]
    if any(keyword in compact for keyword in bargain_keywords):
        return True

    price_offer_patterns = [
        r"\d+(?:元|块)?(?:行吗|行不行|可以吗|能出吗|卖不卖|出不出)",
        r"(?:出|卖)\d+(?:元|块)?",
        r"\d+(?:元|块)?包邮",
    ]
    return any(re.search(pattern, compact) for pattern in price_offer_patterns)


class XianyuReplyBot:
    def __init__(self):
        # 初始化OpenAI客户端
        self.client = OpenAI(
            api_key=os.getenv("API_KEY"),
            base_url=os.getenv("MODEL_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1"),
        )
        self._init_system_prompts()
        self._init_agents()
        self.router = IntentRouter(self.agents['classify'])
        self.last_intent = None  # 记录最后一次意图


    def _init_agents(self):
        """初始化各领域Agent"""
        self.agents = {
            'classify':ClassifyAgent(self.client, self.classify_prompt, self._safe_filter),
            'price': PriceAgent(self.client, self.price_prompt, self._safe_filter),
            'tech': TechAgent(self.client, self.tech_prompt, self._safe_filter),
            'default': DefaultAgent(self.client, self.default_prompt, self._safe_filter),
        }

    def _init_system_prompts(self):
        """初始化各Agent专用提示词，优先加载用户自定义文件，否则使用Example默认文件"""
        prompt_dir = "prompts"
        
        def load_prompt_content(name: str) -> str:
            """尝试加载提示词文件"""
            # 优先尝试加载 target.txt
            target_path = os.path.join(prompt_dir, f"{name}.txt")
            if os.path.exists(target_path):
                file_path = target_path
            else:
                # 尝试默认提示词 target_example.txt
                file_path = os.path.join(prompt_dir, f"{name}_example.txt")

            with open(file_path, "r", encoding="utf-8") as f:
                content = f.read()
                logger.debug(f"已加载 {name} 提示词，路径: {file_path}, 长度: {len(content)} 字符")
                return content

        try:
            # 加载分类提示词
            self.classify_prompt = load_prompt_content("classify_prompt")
            # 加载价格提示词
            self.price_prompt = load_prompt_content("price_prompt")
            # 加载技术提示词
            self.tech_prompt = load_prompt_content("tech_prompt")
            # 加载默认提示词
            self.default_prompt = load_prompt_content("default_prompt")
                
            logger.info("成功加载所有提示词")
        except Exception as e:
            logger.error(f"加载提示词时出错: {e}")
            raise

    def _safe_filter(self, text: str) -> str:
        """安全过滤模块"""
        compact = re.sub(r"\s+", "", text.lower())
        safe_negations = [
            "不加微信", "不用微信", "不走微信", "不能微信", "别加微信",
            "不加qq", "不用qq", "不走qq", "不能qq", "别加qq",
            "不走支付宝", "不用支付宝", "不能支付宝",
            "不线下", "不走线下", "不用线下", "不能线下", "不支持线下", "不私聊",
        ]
        if any(phrase in compact for phrase in safe_negations):
            return text

        risky_patterns = [
            r"(加|留|发|给|走|用|换到|转到)?(微信|vx|v信|qq|支付宝|银行卡|线下|私聊)",
        ]
        return sanitize_platform_reply(text) if any(re.search(pattern, compact) for pattern in risky_patterns) else text

    def format_history(self, context: List[Dict]) -> str:
        """格式化对话历史，返回完整的对话记录"""
        # 过滤掉系统消息，只保留用户和助手的对话
        user_assistant_msgs = [msg for msg in context if msg['role'] in ['user', 'assistant']]
        return "\n".join([f"{msg['role']}: {msg['content']}" for msg in user_assistant_msgs])

    def generate_reply(self, user_msg: str, item_desc: str, context: List[Dict]) -> str:
        """生成回复主流程"""
        # 记录用户消息
        # logger.debug(f'用户所发消息: {user_msg}')
        
        formatted_context = self.format_history(context)
        # logger.debug(f'对话历史: {formatted_context}')
        
        # 1. 本地高置信规则优先处理，减少模型误判和无效调用
        bargain_count = self._extract_bargain_count(context)
        quick_reply = self._quick_reply(user_msg, bargain_count)
        if quick_reply is not None:
            return quick_reply

        # 2. 路由决策
        detected_intent = self.router.detect(user_msg, item_desc, formatted_context)



        # 3. 获取对应Agent

        internal_intents = {'classify'}  # 定义不对外开放的Agent

        if detected_intent == 'no_reply':
            # 无需回复的情况
            logger.info(f'意图识别完成: no_reply - 无需回复')
            self.last_intent = 'no_reply'
            return "-"  # 返回特殊标记，表示无需回复
        elif detected_intent in self.agents and detected_intent not in internal_intents:
            agent = self.agents[detected_intent]
            logger.info(f'意图识别完成: {detected_intent}')
            self.last_intent = detected_intent  # 保存当前意图
        else:
            agent = self.agents['default']
            logger.info(f'意图识别完成: default')
            self.last_intent = 'default'  # 保存当前意图
        
        # 4. 获取议价次数
        logger.info(f'议价次数: {bargain_count}')

        # 5. 生成回复
        return agent.generate(
            user_msg=user_msg,
            item_desc=item_desc,
            context=formatted_context,
            bargain_count=bargain_count
        )

    def _quick_reply(self, user_msg: str, bargain_count: int) -> str:
        """本地快速回复；返回None表示继续走模型。"""
        quick_intent = detect_quick_intent(user_msg)
        if quick_intent == "no_reply":
            logger.info("本地规则识别为无需回复")
            self.last_intent = "no_reply"
            return "-"
        if quick_intent == "platform_safe":
            self.last_intent = "default"
            return platform_safe_reply()
        return None
    
    def _extract_bargain_count(self, context: List[Dict]) -> int:
        """
        从上下文中提取议价次数信息
        
        Args:
            context: 对话历史
            
        Returns:
            int: 议价次数，如果没有找到则返回0
        """
        # 查找系统消息中的议价次数信息
        for msg in context:
            if msg['role'] == 'system' and '议价次数' in msg['content']:
                try:
                    # 提取议价次数
                    match = re.search(r'议价次数[:：]\s*(\d+)', msg['content'])
                    if match:
                        return int(match.group(1))
                except Exception:
                    pass
        return 0

    def should_count_bargain(self, user_msg: str) -> bool:
        """只有明确砍价才累计议价轮次。"""
        return is_explicit_bargain(user_msg)

    def reload_prompts(self):
        """重新加载所有提示词"""
        logger.info("正在重新加载提示词...")
        self._init_system_prompts()
        self._init_agents()
        logger.info("提示词重新加载完成")


class IntentRouter:
    """意图路由决策器"""

    def __init__(self, classify_agent):
        self.rules = {
            'tech': {  # 技术类优先判定
                'keywords': ['参数', '规格', '型号', '连接', '对比'],
                'patterns': [
                    r'和.+比'             
                ]
            },
            'price': {
                'keywords': ['便宜', '价', '砍价', '少点'],
                'patterns': [r'\d+元', r'能少\d+']
            }
        }
        self.classify_agent = classify_agent

    def detect(self, user_msg: str, item_desc, context) -> str:
        """本地规则优先，最后用大模型兜底分类"""
        quick_intent = detect_quick_intent(user_msg)
        if quick_intent == "no_reply":
            return "no_reply"
        if quick_intent == "first_price":
            return "price"

        text_clean = re.sub(r'[^\w\u4e00-\u9fa5]', '', user_msg)
        
        # 1. 技术类关键词优先检查
        if any(kw in text_clean for kw in self.rules['tech']['keywords']):
            # logger.debug(f"技术类关键词匹配: {[kw for kw in self.rules['tech']['keywords'] if kw in text_clean]}")
            return 'tech'
            
        # 2. 技术类正则优先检查
        for pattern in self.rules['tech']['patterns']:
            if re.search(pattern, text_clean):
                # logger.debug(f"技术类正则匹配: {pattern}")
                return 'tech'

        # 3. 价格类检查
        for intent in ['price']:
            if any(kw in text_clean for kw in self.rules[intent]['keywords']):
                # logger.debug(f"价格类关键词匹配: {[kw for kw in self.rules[intent]['keywords'] if kw in text_clean]}")
                return intent
            
            for pattern in self.rules[intent]['patterns']:
                if re.search(pattern, text_clean):
                    # logger.debug(f"价格类正则匹配: {pattern}")
                    return intent
        
        if os.getenv("CLASSIFIER_LLM_ENABLED", "False").lower() == "true":
            return self.classify_agent.generate(
                user_msg=user_msg,
                item_desc=item_desc,
                context=context
            )

        return "default"


class BaseAgent:
    """Agent基类"""

    def __init__(self, client, system_prompt, safety_filter):
        self.client = client
        self.system_prompt = system_prompt
        self.safety_filter = safety_filter

    def generate(self, user_msg: str, item_desc: str, context: str, bargain_count: int = 0) -> str:
        """生成回复模板方法"""
        try:
            messages = self._build_messages(user_msg, item_desc, context)
            response = self._call_llm(messages)
            return self.safety_filter(response)
        except Exception as e:
            logger.exception(f"{self.__class__.__name__} 调用大模型失败: {e}")
            return self.fallback_response()

    def _build_messages(self, user_msg: str, item_desc: str, context: str) -> List[Dict]:
        """构建消息链"""
        return [
            {"role": "system", "content": f"【商品信息】{item_desc}\n【你与客户对话历史】{context}\n【全局售卖策略】\n{build_sales_policy()}\n{self.system_prompt}"},
            {"role": "user", "content": user_msg}
        ]

    def _call_llm(self, messages: List[Dict], temperature: float = 0.4, extra_body: Dict = None) -> str:
        """调用大模型"""
        request_params = {
            "model": os.getenv("MODEL_NAME", "qwen-max"),
            "messages": messages,
            "temperature": temperature,
            "max_tokens": 500,
            "top_p": 0.8,
            "timeout": float(os.getenv("MODEL_TIMEOUT", "8")),
        }
        if extra_body:
            request_params["extra_body"] = extra_body

        response = self.client.chat.completions.create(**request_params)
        return response.choices[0].message.content

    def _dashscope_search_body(self) -> Dict:
        base_url = os.getenv("MODEL_BASE_URL", "https://dashscope.aliyuncs.com/compatible-mode/v1").lower()
        if "dashscope.aliyuncs.com" not in base_url:
            return None
        return {"enable_search": True}

    def fallback_response(self) -> str:
        return "不好意思，我这边刚才响应有点慢。您可以再发一下问题，我看到后马上回复。"


class PriceAgent(BaseAgent):
    """议价处理Agent"""

    def generate(self, user_msg: str, item_desc: str, context: str, bargain_count: int=0) -> str:
        """重写生成逻辑"""
        try:
            dynamic_temp = self._calc_temperature(bargain_count)
            messages = self._build_messages(user_msg, item_desc, context)
            messages[0]['content'] += f"\n▲当前议价轮次：{bargain_count}"
            response = self._call_llm(messages, temperature=dynamic_temp)
            return self.safety_filter(response)
        except Exception as e:
            logger.exception(f"{self.__class__.__name__} 调用大模型失败: {e}")
            return self.fallback_response()

    def _calc_temperature(self, bargain_count: int) -> float:
        """动态温度策略"""
        return min(0.3 + bargain_count * 0.15, 0.9)


class TechAgent(BaseAgent):
    """技术咨询Agent"""
    def generate(self, user_msg: str, item_desc: str, context: str, bargain_count: int=0) -> str:
        """重写生成逻辑"""
        try:
            messages = self._build_messages(user_msg, item_desc, context)
            # messages[0]['content'] += "\n▲知识库：\n" + self._fetch_tech_specs()
            response = self._call_llm(
                messages,
                temperature=0.4,
                extra_body=self._dashscope_search_body()
            )
            return self.safety_filter(response)
        except Exception as e:
            logger.exception(f"{self.__class__.__name__} 调用大模型失败: {e}")
            return self.fallback_response()


    # def _fetch_tech_specs(self) -> str:
    #     """模拟获取技术参数（可连接数据库）"""
    #     return "功率：200W@8Ω\n接口：XLR+RCA\n频响：20Hz-20kHz"


class ClassifyAgent(BaseAgent):
    """意图识别Agent"""

    def _build_messages(self, user_msg: str, item_desc: str, context: str) -> List[Dict]:
        return [
            {"role": "system", "content": f"【商品信息】{item_desc}\n【你与客户对话历史】{context}\n{self.system_prompt}"},
            {"role": "user", "content": user_msg}
        ]

    def generate(self, **args) -> str:
        response = super().generate(**args)
        return response

    def fallback_response(self) -> str:
        return "default"


class DefaultAgent(BaseAgent):
    """默认处理Agent"""

    def _call_llm(self, messages: List[Dict], *args) -> str:
        """限制默认回复长度"""
        response = super()._call_llm(messages, temperature=0.7)
        return response
