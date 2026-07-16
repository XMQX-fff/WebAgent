"""OpenAI 风格 LLM 调用封装。

本模块负责读取环境变量中的 `OPENAI_API_KEY` 并创建 SDK 客户端，
提供 `call_openai_llm` 供 Agent 发送 system + user 消息并取得文本回复。

注意：运行前需要安装 `openai` 包并在环境中设置 `OPENAI_API_KEY`。
"""

import os

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None

# 可通过环境变量覆盖模型与 base_url
OPENAI_MODEL = "deepseek-ai/DeepSeek-V4-Flash"
OPENAI_DEFAULT_BASE_URL = "https://api.siliconflow.cn/v1"



def create_openai_client():
    """创建并返回 OpenAI 客户端实例。

    如果缺少 SDK 或 API Key，会抛出 RuntimeError，便于上层捕获并提示。
    """
    if OpenAI is None:
        raise RuntimeError("缺少 openai 库，请安装 openai 包以调用大模型。")
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        raise ValueError("未配置 OPENAI_API_KEY 系统环境变量")
    if not api_key:
        raise RuntimeError("请通过环境变量 OPENAI_API_KEY 提供 API Key。")
    return OpenAI(api_key=api_key, base_url=OPENAI_DEFAULT_BASE_URL)


def call_openai_llm(system_prompt, user_prompt, max_tokens=256):
    """调用 OpenAI 风格的 chat completion 接口并返回文本结果。

    返回值为模型生成的纯文本；在出现异常时返回带前缀的错误字符串（上层 Agent 会以此判断并降级处理）。
    """
    try:
        client = create_openai_client()
        response = client.chat.completions.create(
            model=OPENAI_MODEL,
            messages=[
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ],
            temperature=0.2,
            max_tokens=max_tokens,
        )
        return response.choices[0].message.content.strip()
    except Exception as exc:
        return f"LLM 调用失败：{exc}"
