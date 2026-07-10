"""WebAgent 的主 Agent 实现（参考 test1 中的 ReActAgent）。

此文件提供 `BaseReActAgent` 和 `WebAgent`，实现与上层交互循环（build prompt -> 调用 LLM -> 解析 -> 执行工具 -> 记录 trace）。
通过配置文件 `config/agent_config.json` 加载工具元数据与 prompt 模板，使用 `openai_client` 封装的大模型调用。
"""

import json
import re
import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:
    from openai_client import call_openai_llm
    from web_tools import ToolResult, WebBrowser
except ImportError:
    from .openai_client import call_openai_llm
    from .web_tools import ToolResult, WebBrowser


def load_json_config(path: Path) -> Dict[str, Any]:
    """读取 JSON 配置文件并返回字典。

    该配置文件包含 Agent prompt、工具元数据和运行参数。
    """
    with path.open("r", encoding="utf-8") as handle:
        return json.load(handle)


class BaseReActAgent:
    """通用 REACT 风格 Agent 基类。

    通过工具列表、元数据和 LLM 调用实现 observe-think-act 循环。
    """
    def __init__(
        self,
        task: str,
        tools: Dict[str, Callable[..., Any]],
        tool_metadata: Dict[str, Dict[str, Any]],
        llm_call: Callable[[str, str, int], str],
        config: Dict[str, Any],
    ):
        self.task = task
        self.tools = tools
        self.tool_metadata = tool_metadata
        self.llm_call = llm_call
        self.config = config
        self.trace: List[Dict[str, Any]] = []
        self.step_count = 0
        self.output: Optional[str] = None
        self.last_observation: Optional[str] = None
        self.last_summary: Optional[str] = None
        self.error: Optional[str] = None

        # 读取 agent 配置项：循环次数、历史窗口、trace 文件、LLM 参数等
        agent_cfg = config.get("agent", {})
        self.max_turns = agent_cfg.get("max_turns", 8)
        self.history_window = agent_cfg.get("history_window", 10)
        self.trace_file = agent_cfg.get("trace_file", "web_agent_trace.jsonl")
        self.system_prompt = config.get("llm", {}).get("system_prompt", "你是一个 REACT Agent。")
        self.max_tokens = config.get("llm", {}).get("max_tokens", 256)

    def add_trace(self, thought_summary: str, tool: Optional[str], args: Dict[str, Any], observation: str, cost_estimate: str):
        """记录当前步骤 trace 并追加到文件。"""
        record = {
            "step": self.step_count,
            "thought_summary": thought_summary,
            "tool": tool,
            "args": args,
            "observation": observation,
            "cost_estimate": cost_estimate,
        }
        self.trace.append(record)
        self._append_trace_file(record)

    def _append_trace_file(self, record: Dict[str, Any]):
        # 追加写入 trace 文件，供调试与复盘使用
        with open(self.trace_file, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def build_react_prompt(self) -> str:
        tool_lines = []
        for name, meta in self.tool_metadata.items():
            tool_lines.append(
                f"- {name}: {meta.get('description', '')} 参数: {list(meta.get('params', {}).keys())}"
            )
        tools_description = "\n".join(tool_lines)

        history_text = ""
        for step in self.trace[-self.history_window:]:
            history_text += (
                f"Step {step['step']} | Thought: {step['thought_summary']} | "
                f"Tool: {step['tool']} | Args: {step['args']} | Observation: {step['observation']}\n"
            )
        current_history = history_text or "无历史操作。"
        last_obs_text = self.last_observation or "当前尚无工具观察结果。"

        # 根据配置与历史记录拼接最终 prompt，传给 LLM
        prompt_lines = [
            self.config.get("prompt", {}).get("intro", "你是一个 REACT 风格的 Agent。当前任务是："),
            self.task,
            "",
            "工具列表：",
            tools_description,
            "",
            *self.config.get("prompt", {}).get("instructions", []),
            "",
            "示例：",
            *self.config.get("prompt", {}).get("examples", []),
            "",
            "当前状态：",
            f"当前任务: {self.task}",
            f"最近一次观察: {last_obs_text}",
            "工具历史：",
            current_history,
            "",
            *self.config.get("prompt", {}).get("closing", []),
        ]
        return "\n".join(prompt_lines)

    def parse_llm_response(self, text: str) -> Dict[str, str]:
        """解析 LLM 输出，提取 thought/action/action_input。

        首先尝试 JSON 解析；若失败则回退到行解析模式。
        """
        cleaned = text.strip()
        try:
            parsed = json.loads(cleaned)
            action_input = parsed.get("action_input", "")
            if isinstance(action_input, dict):
                action_input = json.dumps(action_input, ensure_ascii=False)
            return {
                "thought": parsed.get("thought", "") or parsed.get("思考", ""),
                "action": parsed.get("action", "") or parsed.get("操作", ""),
                "action_input": action_input,
            }
        except Exception:
            pass

        thought = ""
        action = ""
        action_input = ""
        current_key = None
        patterns = {
            "thought": re.compile(r"^(thought|思考)\s*[:：]\s*(.*)$", re.IGNORECASE),
            "action": re.compile(r"^(action|操作)\s*[:：]\s*(.*)$", re.IGNORECASE),
            "action_input": re.compile(r"^(action input|action_input|输入)\s*[:：]\s*(.*)$", re.IGNORECASE),
        }

        for line in cleaned.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            matched = False
            for key, pattern in patterns.items():
                match = pattern.match(stripped)
                if match:
                    matched = True
                    value = match.group(2).strip()
                    current_key = key
                    if key == "thought":
                        thought = value
                    elif key == "action":
                        action = value
                    elif key == "action_input":
                        action_input = value
                    break
            if not matched and current_key == "action_input":
                # 支持 action_input 多行追加
                action_input = (action_input + "\n" + stripped).strip()

        if not action:
            lowered = cleaned.lower()
            # 如果没有明确输出 action，则尝试从文本里匹配工具名称或 finish 指令
            if "browser_open" in lowered:
                action = "browser_open"
            elif "browser_observe" in lowered:
                action = "browser_observe"
            elif "browser_click" in lowered:
                action = "browser_click"
            elif "browser_type" in lowered:
                action = "browser_type"
            elif "browser_extract" in lowered:
                action = "browser_extract"
            elif "browser_screenshot" in lowered:
                action = "browser_screenshot"
            elif "finish" in lowered or "完成" in lowered or "结束" in lowered:
                action = "finish"

        return {"thought": thought, "action": action, "action_input": action_input}

    def parse_action_input(self, tool_spec: Dict[str, Any], action_input: str) -> Dict[str, Any]:
        """解析工具输入参数，将 action_input 转为工具可调用的参数字典。"""
        action_input = action_input.strip()
        if not action_input:
            return {}
        if action_input.startswith("{") and action_input.endswith("}"):
            try:
                parsed = json.loads(action_input)
                if isinstance(parsed, dict):
                    return parsed
            except Exception:
                pass
        params: Dict[str, Any] = {}
        for line in action_input.splitlines():
            if ":" in line:
                key, value = line.split(":", 1)
                params[key.strip()] = value.strip()
        if params:
            return params
        input_param = tool_spec.get("input_param")
        if input_param:
            return {input_param: action_input}
        keys = list(tool_spec.get("params", {}).keys())
        if len(keys) == 1:
            return {keys[0]: action_input}
        # 当无法判断具体参数名时，用 text 回退，便于一些工具兼容单一文本输入
        return {"text": action_input}

    def perform_action(self, action: str, action_input: str) -> str:
        """执行指定工具动作，并返回 observation 文本。"""
        action = action.strip()
        if action == "finish":
            answer = action_input.strip() or self.last_summary or self.last_observation or "无法生成最终结果。"
            self.output = answer
            return answer

        tool_spec = self.tool_metadata.get(action)
        if not tool_spec:
            return f"未知动作：{action}。请使用 {list(self.tools.keys())}。"

        tool = self.tools.get(action)
        if not tool:
            return f"工具不可用：{action}。"

        params = self.parse_action_input(tool_spec, action_input)
        try:
            result = tool(**params)
        except Exception as exc:
            return f"ERROR[TOOL_EXCEPTION]: {exc}"

        if isinstance(result, dict):
            if result.get("status") == "ok":
                observation = result.get("data") or result.get("summary") or json.dumps(result, ensure_ascii=False)
            else:
                return f"ERROR[{result.get('error_code', 'UNKNOWN')}]: {result.get('error_msg', '')}. Suggestion: {result.get('suggestion', '')}"
        else:
            observation = str(result)

        self.last_observation = observation
        return observation

    def run(self) -> str:
        """Agent 主循环：调用 LLM、执行工具、记录 trace，直到 finish 或超出最大轮数。"""
        if Path(self.trace_file).exists():
            try:
                Path(self.trace_file).unlink()
            except OSError:
                pass

        try:
            while self.step_count < self.max_turns:
                prompt_text = self.build_react_prompt()
                response = self.llm_call(
                    system_prompt=self.system_prompt,
                    user_prompt=prompt_text,
                    max_tokens=self.max_tokens,
                )
                parsed = self.parse_llm_response(response)
                action = parsed["action"] or "browser_observe"
                action_input = parsed["action_input"]
                thought = parsed["thought"] or "模型未提供 thought。"
                self.step_count += 1

                observation = self.perform_action(action, action_input)
                self.add_trace(
                    thought_summary=thought,
                    tool=action if action in self.tools else None,
                    args={"action_input": action_input},
                    observation=observation,
                    cost_estimate="medium" if action in ["browser_extract", "finish"] else "low",
                )

                if action == "finish":
                    return self.output or observation

            self.error = "超过最大轮次，未完成任务。"
            self.add_trace(
                thought_summary="达到最大轮次，停止执行以避免无限循环。",
                tool=None,
                args={},
                observation=self.error,
                cost_estimate="none",
            )
            return self.error
        except Exception as exc:
            error_message = f"失败原因：{exc}"
            self.add_trace(
                thought_summary="处理过程中出现异常，记录失败原因。",
                tool=None,
                args={},
                observation=error_message,
                cost_estimate="none",
            )
            return error_message


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
