"""WebAgent 的主 Agent 实现。

此文件提供 `WebAgent`，实现基于浏览器工具的 Web 自动化 Agent。
通过配置文件 `config/agent_config.json` 加载工具元数据与 prompt 模板，
使用 `openai_client` 封装的大模型调用。
"""

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from openai_client import call_openai_llm
    from web_tools import WebBrowser
    from base_agent import BaseReActAgent, load_json_config
except ImportError:
    from .openai_client import call_openai_llm
    from .web_tools import WebBrowser
    from .base_agent import BaseReActAgent, load_json_config


class WebAgent(BaseReActAgent):
    """基于浏览器工具和 OpenAI LLM 的 Web 自动化 Agent。

    该类负责加载配置、初始化浏览器工具并执行通用 REACT 循环。
    """

    def __init__(self, task: str):
        # 加载 Agent 配置文件并初始化浏览器工具映射
        config_path = Path(__file__).resolve().parent / "config" / "agent_config.json"
        config = load_json_config(config_path)
        self.browser = WebBrowser()
        # 如果任务中包含 URL，记下来以便启动时自动打开（减少 LLM 未主动打开页面的情况）
        m = re.search(r"(https?://[^\s,]+)", task)
        self.initial_url = m.group(1) if m else None
        tools = {
            "browser_open": self.browser.browser_open,
            "browser_observe": self.browser.browser_observe,
            "browser_click": self.browser.browser_click,
            "browser_type": self.browser.browser_type,
            "browser_select": self.browser_select,
            "browser_extract": self.browser.browser_extract,
            "browser_screenshot": self.browser.browser_screenshot,
        }
        tool_metadata = config.get("tools", {})
        super().__init__(task=task, tools=tools, tool_metadata=tool_metadata, llm_call=call_openai_llm, config=config)

    def browser_select(self, selector: str, value: str) -> Dict[str, Any]:
        """封装 browser_select 工具接口，保持工具映射一致。"""
        return self.browser.browser_select(selector=selector, value=value)

    def run(self) -> str:
        """执行父类 Agent 逻辑，并在结束后关闭浏览器。"""
        try:
            # 如果解析到初始 URL，先自动打开一次，作为环境准备
            if getattr(self, "initial_url", None):
                try:
                    res = self.browser.browser_open(self.initial_url)
                    if isinstance(res, dict) and res.get("status") == "ok":
                        obs = res.get("data")
                    elif isinstance(res, dict):
                        obs = f"ERROR[{res.get('error_code')}]: {res.get('error_msg')}"
                    else:
                        obs = str(res)
                except Exception as e:
                    obs = f"ERROR[OPEN_EXCEPTION]: {e}"
                self.step_count += 1
                self.add_trace(
                    thought_summary="自动打开初始 URL",
                    tool="browser_open",
                    args={"url": self.initial_url},
                    observation=obs,
                    cost_estimate="low",
                )
            return super().run()
        finally:
            try:
                self.browser.close()
            except Exception:
                pass


def main():
    if len(sys.argv) > 1:
        task = " ".join(sys.argv[1:]).strip()
    else:
        print("WebAgent: 使用 Playwright 操作网页。")
        task = input("请输入你的网页任务，例如：打开 https://example.com 并提取页面标题\n> ").strip()

    if not task:
        print("任务不能为空。")
        sys.exit(1)

    agent = WebAgent(task)
    result = agent.run()
    print("\n=== 结果 ===")
    print(result)
    print(f"追踪已写入：{agent.trace_file}")


if __name__ == "__main__":
    main()
