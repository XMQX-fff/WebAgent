"""WebAgent 包的顶层导出。

本包导出两种架构的入口与核心组件：
  - 单 Agent 架构：`WebAgent`、`BaseReActAgent`
  - 三 Agent 架构：`MultiAgentCoordinator`、`PlannerAgent`、`ExecutorAgent`、`VerifierAgent`
以及浏览器工具类与 LLM 调用函数，便于在其他脚本中直接导入使用。
"""

from .agents.web_agent import WebAgent, main
from .core.openai_client import call_openai_llm
from .core.web_tools import WebBrowser, ToolResult
from .agents.base_agent import BaseReActAgent, load_json_config
from .agents.multi_agent import MultiAgentCoordinator
from .agents.planner_agent import PlannerAgent
from .agents.executor_agent import ExecutorAgent
from .agents.verifier_agent import VerifierAgent

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