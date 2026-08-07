"""OpenAI 风格 LLM 调用封装。

本模块负责读取环境变量中的 `OPENAI_API_KEY` 并创建 SDK 客户端，
提供 `call_openai_llm` 供 Agent 发送 system + user 消息并取得文本回复。

支持通过环境变量配置：
- OPENAI_API_KEY: API 密钥（必填）
- OPENAI_BASE_URL: API 基础 URL（可选，默认 SiliconFlow）
- OPENAI_MODEL: 模型名称（可选，默认 DeepSeek-V4-Flash）

注意：运行前需要安装 `openai` 包并在环境中设置 `OPENAI_API_KEY`。
"""

import os
import time

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

# 默认值（可通过环境变量覆盖）
OPENAI_DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"
OPENAI_DEFAULT_MODEL = "deepseek-ai/DeepSeek-V4-Flash"

# 模块级客户端单例，避免每次调用都创建新连接
_client_instance = None


def create_openai_client():
    """创建并返回 OpenAI 客户端实例。

    如果缺少 SDK 或 API Key，会抛出 RuntimeError，便于上层捕获并提示。
    """
    if OpenAI is None:
        raise RuntimeError("缺少 openai 库，请安装 openai 包以调用大模型。")
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise RuntimeError("请通过环境变量 OPENAI_API_KEY 提供 API Key。")
    base_url = os.getenv("OPENAI_BASE_URL", OPENAI_DEFAULT_BASE_URL)
    return OpenAI(api_key=api_key, base_url=base_url)


def _get_client():
    """获取或创建模块级客户端单例。"""
    global _client_instance
    if _client_instance is None:
        _client_instance = create_openai_client()
    return _client_instance


def call_openai_llm(system_prompt, user_prompt, max_tokens=256):
    """调用 OpenAI 风格的 chat completion 接口并返回文本结果。

    返回值为模型生成的纯文本。调用失败时抛出 RuntimeError，
    上层 Agent 可捕获异常并决定是否终止执行。

    内置简单的重试机制（最多 3 次，指数退避），应对网络抖动和速率限制。
    """
    model = os.getenv("OPENAI_MODEL", OPENAI_DEFAULT_MODEL)
    max_retries = 3

    for attempt in range(max_retries):
        try:
            client = _get_client()
            response = client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                temperature=0.2,
                max_tokens=max_tokens,
            )
            return response.choices[0].message.content.strip()
        except Exception as exc:
            if attempt < max_retries - 1:
                time.sleep(2 ** attempt)  # 指数退避：1s, 2s
                continue
            raise RuntimeError(f"LLM 调用失败（已重试 {max_retries} 次）：{exc}") from exc