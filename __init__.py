"""WebAgent 包的顶层导出。

本包将常用入口 `WebAgent`、`main`、`call_openai_llm` 以及浏览器工具类导出，便于在其他脚本中直接导入使用。
"""

from .web_agent import WebAgent, main
from .openai_client import call_openai_llm
from .web_tools import WebBrowser, ToolResult

__all__ = ["WebAgent", "main", "call_openai_llm", "WebBrowser", "ToolResult"]
