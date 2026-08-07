"""Executor Agent - 网页自动化任务的执行 Agent。

Executor 负责使用浏览器工具执行 Planner 给出的当前步骤。
它复用 BaseReActAgent 的工具调用与解析能力，但运行的是"单步骤"循环：
针对一个步骤指令，最多执行若干轮工具调用，直到 finish 或达到步骤上限。

Executor 不负责判断整个用户任务是否完成，只关注当前步骤是否执行完毕。
"""

import sys
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:
    from agents.base_agent import BaseReActAgent
except ImportError:
    # 直接运行脚本时（python agents/executor_agent.py），将项目根目录加入 sys.path
    _root = str(Path(__file__).resolve().parent.parent)
    if _root not in sys.path:
        sys.path.insert(0, _root)
    from agents.base_agent import BaseReActAgent


class ExecutorAgent(BaseReActAgent):
    """执行 Agent：针对单个步骤指令运行工具循环。

    继承 BaseReActAgent，复用其 prompt 构建、响应解析、工具调用与 trace 能力，
    但重写 run() 为"单步骤执行"模式：不关心全局任务，只执行传入的 current_step。

    Args:
        task: 用户原始任务（作为上下文）。
        current_step: Planner 给出的当前步骤指令。
        tools: 浏览器工具映射。
        tool_metadata: 工具元数据。
        llm_call: LLM 调用函数。
        config: 多 Agent 配置中 executor 部分的字典。
        max_steps: 单个步骤内 Executor 最多执行的工具轮数。
        trace_file: trace 文件路径。
    """

    def __init__(
        self,
        task: str,
        current_step: str,
        tools: Dict[str, Callable[..., Any]],
        tool_metadata: Dict[str, Dict[str, Any]],
        llm_call: Callable[[str, str, int], str],
        config: Dict[str, Any],
        max_steps: int = 4,
        trace_file: Optional[str] = None,
    ):
        # 构造一个兼容 BaseReActAgent 的 config：用 executor 的 prompt 覆盖
        base_config: Dict[str, Any] = {
            "agent": {
                "max_turns": max_steps,
                "history_window": config.get("history_window", 6),
                "trace_file": trace_file or "web_traces/executor_trace.jsonl",
            },
            "llm": {
                "system_prompt": config.get("system_prompt", "你是一个执行 Agent。"),
                "max_tokens": config.get("max_tokens", 256),
            },
            "prompt": config.get("prompt", {}),
            "tools": tool_metadata,
        }
        super().__init__(
            task=task,
            tools=tools,
            tool_metadata=tool_metadata,
            llm_call=llm_call,
            config=base_config,
        )
        self.current_step = current_step
        # 覆盖 trace_file 为绝对路径，避免依赖运行目录
        if trace_file:
            self.trace_file = str(Path(trace_file).resolve())
        self.max_steps = max_steps
        # 浏览器当前状态上下文（由协调器在每步开始前注入），避免 LLM 盲目重新打开页面
        self.browser_context: Optional[str] = None

    def build_react_prompt(self) -> str:
        """重写 prompt 构建：以 current_step 为核心，而非全局任务。"""
        tool_lines = []
        for name, meta in self.tool_metadata.items():
            tool_lines.append(
                f"- {name}: {meta.get('description', '')} 参数: {list(meta.get('params', {}).keys())}"
            )
        tools_description = "\n".join(tool_lines)

        # 只保留当前步骤执行过程中的历史（self.trace 已在 BaseReActAgent 中维护）
        history_text = ""
        for step in self.trace[-self.history_window:]:
            history_text += (
                f"Step {step['step']} | Thought: {step['thought_summary']} | "
                f"Tool: {step['tool']} | Args: {step['args']} | Observation: {step['observation']}\n"
            )
        current_history = history_text or "当前步骤尚无工具调用历史。"
        last_obs_text = self.last_observation or "当前尚无工具观察结果。"
        # 浏览器当前状态上下文（由协调器注入），让 LLM 知道页面是否已打开
        browser_ctx_text = self.browser_context or "未知（可能尚未打开任何页面）"

        prompt_lines = [
            self.config.get("prompt", {}).get("intro", "你是网页自动化任务的执行 Agent（Executor）。"),
            "",
            f"用户原始任务（仅作上下文参考）: {self.task}",
            f"当前需要执行的步骤: {self.current_step}",
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
            f"浏览器当前页面: {browser_ctx_text}",
            f"最近一次观察: {last_obs_text}",
            "工具历史：",
            current_history,
            "",
            *self.config.get("prompt", {}).get("closing", []),
        ]
        return "\n".join(prompt_lines)

    def run(self) -> str:
        """执行当前步骤：最多 max_steps 轮工具调用，直到 finish 或达到上限。

        返回当前步骤的执行结果摘要（即 finish 的 action_input 或最后一次 observation）。
        """
        # 不在 Executor 内部清空 trace 文件（由协调器统一管理）
        try:
            while self.step_count < self.max_steps:
                prompt_text = self.build_react_prompt()
                try:
                    response = self.llm_call(
                        system_prompt=self.system_prompt,
                        user_prompt=prompt_text,
                        max_tokens=self.max_tokens,
                    )
                except Exception as exc:
                    error_message = f"LLM 调用失败，终止当前步骤执行：{exc}"
                    self.error = error_message
                    self.add_trace(
                        thought_summary="LLM 调用失败，提前退出当前步骤。",
                        tool=None,
                        args={},
                        observation=error_message,
                        cost_estimate="none",
                    )
                    return error_message

                parsed = self.parse_llm_response(response)
                action = parsed["action"] or (next(iter(self.tools)) if self.tools else "finish")
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

                # 安全网：browser_open 超时后自动观察页面状态，避免盲目重试
                # 页面可能已导航到目标 URL，只是仍在加载资源（转圈）
                if action == "browser_open" and isinstance(observation, str) and observation.startswith("ERROR[TIMEOUT]"):
                    auto_obs = self.perform_action("browser_observe", "")
                    self.add_trace(
                        thought_summary="browser_open 超时后自动观察页面状态（安全网机制）",
                        tool="browser_observe",
                        args={"action_input": ""},
                        observation=auto_obs,
                        cost_estimate="low",
                    )
                    # 如果观察结果显示页面已加载（非 ERROR），说明页面其实已打开
                    if isinstance(auto_obs, str) and not auto_obs.startswith("ERROR["):
                        # 页面已加载，当前步骤（打开页面）视为成功
                        return f"页面已打开（browser_open 超时但页面已加载）。观察结果：{auto_obs[:500]}"

            # 达到单步骤上限，返回最后一次观察作为执行结果
            self.error = f"当前步骤达到最大工具调用轮数（{self.max_steps}），以最后一次观察作为结果。"
            self.add_trace(
                thought_summary="当前步骤达到最大工具调用轮数，停止执行。",
                tool=None,
                args={},
                observation=self.error,
                cost_estimate="none",
            )
            return self.last_observation or self.error
        except Exception as exc:
            error_message = f"当前步骤执行失败：{exc}"
            self.add_trace(
                thought_summary="当前步骤执行过程中出现异常。",
                tool=None,
                args={},
                observation=error_message,
                cost_estimate="none",
            )
            return error_message

    def reset_for_new_step(self, current_step: str) -> None:
        """为执行新的步骤重置内部状态（复用同一个 Executor 实例时调用）。"""
        self.current_step = current_step
        self.trace = []
        self.step_count = 0
        self.output = None
        self.last_observation = None
        self.last_summary = None
        self.error = None