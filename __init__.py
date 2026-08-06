"""WebAgent 包的顶层导出。

本包导出两种架构的入口与核心组件：
  - 单 Agent 架构：`WebAgent`、`BaseReActAgent`
  - 三 Agent 架构：`MultiAgentCoordinator`、`PlannerAgent`、`ExecutorAgent`、`VerifierAgent`
以及浏览器工具类与 LLM 调用函数，便于在其他脚本中直接导入使用。
"""

from .web_agent import WebAgent, main
from .openai_client import call_openai_llm
from .web_tools import WebBrowser, ToolResult
from .base_agent import BaseReActAgent, load_json_config
from .multi_agent import MultiAgentCoordinator
from .planner_agent import PlannerAgent
from .executor_agent import ExecutorAgent
from .verifier_agent import VerifierAgent

__all__ = [
    "WebAgent",
    "main",
    "call_openai_llm",
    "WebBrowser",
    "ToolResult",
    "BaseReActAgent",
    "load_json_config",
    "MultiAgentCoordinator",
    "PlannerAgent",
    "ExecutorAgent",
    "VerifierAgent",
]
