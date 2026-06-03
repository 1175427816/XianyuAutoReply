# Xianyu Auto Reply

> 闲鱼自动回复助手与本地 Web 管理后台。本项目初始代码来自 [shaxiu/XianyuAutoAgent](https://github.com/shaxiu/XianyuAutoAgent)，当前版本在其基础上加入了本地运行控制台、Cookie 自动恢复、状态持久化、图片回复规则和更贴近日常卖家沟通的回复策略。

[![Python](https://img.shields.io/badge/Python-3.8%2B-blue)](https://www.python.org/)
[![License](https://img.shields.io/badge/License-GPL--3.0-green)](./LICENSE)
[![LLM](https://img.shields.io/badge/LLM-OpenAI%20compatible-black)](https://platform.openai.com/docs)

## 功能概览

| 能力 | 说明 |
| --- | --- |
| LLM 自动回复 | 使用 OpenAI-compatible API 生成闲鱼买家回复 |
| 多场景意图路由 | 根据咨询内容切换默认客服、议价、技术说明等回复策略 |
| 本地会话记忆 | 使用 SQLite 保存会话历史，让多轮沟通更连贯 |
| 议价保护 | 通过环境变量限制最大优惠比例、最大优惠金额和首轮议价策略 |
| 平台安全提醒 | 对站外交易、私聊、转账等高风险表达进行收敛 |
| Cookie 自动恢复 | 登录态失效时优先从本机专用 Chrome 会话读取 Goofish Cookie |
| Web 管理后台 | 查看运行状态、启动/停止/重启监控、处理验证、查看日志 |
| 图片回复规则 | 按关键词、商品 ID 或默认规则发送图片和补充文案 |

## 目录结构

```text
.
├── main.py                         # 闲鱼 WebSocket 监听与消息处理主程序
├── run_with_cookie_monitor.py      # 推荐启动入口，负责监控与 Cookie 恢复
├── start_all.py                    # 同时启动监控和 Web 控制台
├── web_admin.py                    # 本地 Web 管理后台服务
├── XianyuAgent.py                  # LLM 回复、意图路由与安全过滤
├── XianyuApis.py                   # 闲鱼/Goofish 接口封装
├── context_manager.py              # 会话历史和幂等记录管理
├── browser_cookie_fetcher.py       # 专用 Chrome Cookie 读取
├── prompts/                        # 提示词模板
├── web_admin/                      # 控制台前端静态资源
├── image_replies.json              # 图片回复规则示例
└── .env.example                    # 环境变量示例
```

运行后会产生 `.env`、`data/`、`logs/`、`runtime/` 等本地文件。这些文件可能包含 Cookie、聊天记录、商品信息或运行状态，已在 `.gitignore` 中排除。

## 环境要求

- Python 3.8+
- 可访问的 OpenAI-compatible 模型服务
- 已登录闲鱼/Goofish 的浏览器会话或可手动复制的 Cookie

## 安装

```bash
git clone <your-repository-url>
cd xianyu-auto-reply

python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

cp .env.example .env
```

编辑 `.env`：

```dotenv
API_KEY=your_model_api_key
COOKIES_STR=your_goofish_cookie
MODEL_BASE_URL=https://dashscope.aliyuncs.com/compatible-mode/v1
MODEL_NAME=qwen-max
```

提示词默认读取 `prompts/*_prompt_example.txt`。如需自定义，可复制为不带 `_example` 的文件名：

```bash
cp prompts/default_prompt_example.txt prompts/default_prompt.txt
cp prompts/classify_prompt_example.txt prompts/classify_prompt.txt
cp prompts/price_prompt_example.txt prompts/price_prompt.txt
cp prompts/tech_prompt_example.txt prompts/tech_prompt.txt
```

自定义提示词文件会被 `.gitignore` 排除，避免把个人业务策略误提交。

## 启动方式

推荐启动本地控制台和自动回复监控：

```bash
python3 start_all.py
```

默认访问：

```text
http://127.0.0.1:8766
```

仅启动自动回复监控：

```bash
python3 run_with_cookie_monitor.py
```

仅启动 Web 管理后台：

```bash
python3 web_admin.py
```

直接启动主程序：

```bash
python3 main.py
```

日常更推荐 `start_all.py` 或 `run_with_cookie_monitor.py`，因为它们能在 Cookie 失效、滑块验证或主程序退出时给出更明确的恢复流程。

## Cookie 自动恢复

当 `AUTO_COOKIE_FROM_BROWSER=True` 时，程序会优先使用本机专用 Chrome 会话读取 Goofish Cookie，并在成功后写回 `.env`。默认配置：

```dotenv
BROWSER_COOKIE_CDP_PORT=9223
BROWSER_COOKIE_PROFILE_DIR=
BROWSER_COOKIE_PROMPT_TIMEOUT=180
BROWSER_COOKIE_LOGIN_URL=https://www.goofish.com/im
```

使用建议：

- 不要把 `.env`、浏览器 profile、日志或数据库提交到仓库。
- 不要在 Issue、PR、截图或日志中展示 Cookie。
- 如果自动读取失败，可在本地手动更新 `.env` 中的 `COOKIES_STR`。

## 图片回复规则

`image_replies.json` 提供了禁用状态的示例规则。你可以按商品 ID、关键词和匹配方式配置图片回复：

```json
{
  "enabled": false,
  "name": "商品A-颜色图",
  "item_ids": ["这里填商品ID"],
  "keywords": ["颜色", "色卡"],
  "match": "contains",
  "text": "颜色可以参考这张图。",
  "images": [
    {
      "url": "这里填该商品对应图片URL",
      "width": 1440,
      "height": 1920,
      "type": 0
    }
  ]
}
```

公开仓库中建议只保留示例规则，不提交真实商品图片 URL、商品 ID 或买家相关信息。

## 发布到 GitHub 前

检查待提交文件：

```bash
git status --short
```

确认以下内容没有进入提交列表：

- `.env`
- `data/`
- `logs/`
- `runtime/`
- `*.db`
- `*.log`
- `*.jsonl`
- `*_prompt.txt`
- `__pycache__/`
- `.DS_Store`

如果准备从更大的工作区根目录发布，也不要提交 `exports/`、`archive/` 或其他个人上下文文件。

## 与上游项目的关系

本项目初始代码来自 [shaxiu/XianyuAutoAgent](https://github.com/shaxiu/XianyuAutoAgent)。当前仓库是基于该项目的二次修改版本，主要改动包括：

- 增加本地 Web 管理后台与运行状态面板。
- 增加 Cookie 监控、专用 Chrome 自动读取和恢复流程。
- 增加运行状态、验证事件和日志查看能力。
- 增加图片自动回复规则。
- 调整回复风格、议价策略和平台内交易安全过滤。
- 重写公开 README，使其更适合作为个人二次开发仓库的项目页。

请保留上游来源说明，并遵守 GPL-3.0 许可要求。

## 免责声明

本项目仅用于学习、研究与个人自动化实验。请遵守闲鱼/Goofish 平台规则、模型服务条款和所在地法律法规。使用者需要自行承担账号风控、数据安全和平台合规风险。
