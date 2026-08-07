"""WebAgent 的主 Agent 实现与 CLI 入口。

本文件提供两种架构的入口：
  1. `WebAgent` — 单 Agent（REACT）架构，基于 `BaseReActAgent`，向后兼容。
  2. `MultiAgentCoordinator` — Planner + Executor + Verifier 三 Agent 架构（推荐）。

CLI 入口 `main()` 默认使用三 Agent 架构，可通过 `--single` 参数回退到单 Agent 架构。

支持浏览器状态持久化：Agent 启动前通过 LLM 判断任务是否需要复用已保存的浏览器状态
（cookies/localStorage），并在运行结束后自动保存状态。
"""

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, Optional

try:
    from core.openai_client import call_openai_llm
    from core.web_tools import WebBrowser, decide_state_from_task
    from agents.base_agent import BaseReActAgent, load_json_config
    from agents.multi_agent import MultiAgentCoordinator
except ImportError:
    # 直接运行脚本时（python agents/web_agent.py），将项目根目录加入 sys.path
    _root = str(Path(__file__).resolve().parent.parent)
    if _root not in sys.path:
        sys.path.insert(0, _root)
    from core.openai_client import call_openai_llm
    from core.web_tools import WebBrowser, decide_state_from_task
    from agents.base_agent import BaseReActAgent, load_json_config
    from agents.multi_agent import MultiAgentCoordinator


# 默认浏览器状态文件路径
DEFAULT_STATE_FILE = "web_traces/browser_state.json"


class WebAgent(BaseReActAgent):
    """基于浏览器工具和 OpenAI LLM 的 Web 自动化 Agent。

    支持浏览器状态持久化：Agent 启动前通过 LLM 判断任务是否需要复用已保存的浏览器状态，
    并在运行结束后自动保存状态。

    Args:
        task: 用户任务描述。
        state_file: 浏览器状态持久化文件路径。为 None 时不持久化状态。
    """

    def __init__(self, task: str, state_file: Optional[str] = DEFAULT_STATE_FILE):
        # 加载 Agent 配置文件并初始化浏览器工具映射
        config_path = Path(__file__).resolve().parent.parent / "config" / "agent_config.json"
        config = load_json_config(config_path)
        self.browser = WebBrowser(state_file=state_file)
        # 如果任务中包含 URL，记下来以便启动时自动打开（减少 LLM 未主动打开页面的情况）
        m = re.search(r"(https?://[^\s,]+)", task)
        self.initial_url = m.group(1) if m else None
        tools = {
            "browser_open": self.browser.browser_open,
            "browser_observe": self.browser.browser_observe,
            "browser_click": self.browser.browser_click,
            "browser_type": self.browser.browser_type,
            "browser_select": self.browser.browser_select,
            "browser_extract": self.browser.browser_extract,
            "browser_screenshot": self.browser.browser_screenshot,
            "browser_clear_state": self.browser.browser_clear_state,
        }
        tool_metadata = config.get("tools", {})
        super().__init__(task=task, tools=tools, tool_metadata=tool_metadata, llm_call=call_openai_llm, config=config)

    def run(self) -> str:
        """执行 Agent 逻辑：先通过 LLM 决策状态复用，再执行 REACT 循环，最后保存状态。"""
        try:
            # 步骤 1：通过 LLM 判断是否需要加载已保存的浏览器状态
            use_saved_state = decide_state_from_task(
                task=self.task,
                llm_call=self.llm_call,
                state_file=self.browser.state_file,
            )
            if use_saved_state:
                # 以 load_state=True 启动浏览器，自动加载已保存的状态
                self.browser.start(load_state=True)
                self.step_count += 1
                self.add_trace(
                    thought_summary="LLM 决策：复用已保存的浏览器状态",
                    tool=None,
                    args={},
                    observation="已加载已保存的浏览器状态（cookies/localStorage）。",
                    cost_estimate="low",
                )
            else:
                # 以 load_state=False 启动浏览器，全新环境
                self.browser.start(load_state=False)
                if self.browser.state_file and Path(self.browser.state_file).exists():
                    self.step_count += 1
                    self.add_trace(
                        thought_summary="LLM 决策：不使用已保存的浏览器状态，以全新环境启动",
                        tool=None,
                        args={},
                        observation="已跳过状态加载，以全新浏览器环境启动。",
                        cost_estimate="low",
                    )

            # 步骤 2：如果解析到初始 URL，先自动打开一次
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

            # 步骤 3：执行父类 REACT 主循环
            return super().run()
        finally:
            # 步骤 4：关闭浏览器（自动保存状态）
            try:
                self.browser.close()
            except Exception:
                pass


def main():
    # 解析命令行参数：支持 --single 回退到单 Agent 架构，其余参数拼成任务
    use_single = False
    args = sys.argv[1:]
    if "--single" in args:
        use_single = True
        args = [a for a in args if a != "--single"]

    if args:
        task = " ".join(args).strip()
    else:
        arch_label = "单 Agent (REACT)" if use_single else "三 Agent (Planner + Executor + Verifier)"
        print(f"WebAgent: 使用 Playwright 操作网页。当前架构：{arch_label}")
        task = input("请输入你的网页任务，例如：打开 https://example.com 并提取页面标题\n> ").strip()

    if not task:
        print("任务不能为空。")
        sys.exit(1)

    if use_single:
        agent = WebAgent(task)
        result = agent.run()
        trace_file = agent.trace_file
    else:
        coordinator = MultiAgentCoordinator(task)
        result = coordinator.run()
        trace_file = coordinator.trace_file

    print("\n=== 结果 ===")
    print(result)
    print(f"追踪已写入：{trace_file}")


if __name__ == "__main__":
    main()
