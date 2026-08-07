"""Planner Agent - 网页自动化任务的规划 Agent。

Planner 负责将用户任务分解为可执行的原子步骤，每次只输出下一个要执行的步骤，
并根据执行历史和验证反馈动态调整计划。Planner 不直接调用任何浏览器工具。
"""

import json
import re
from typing import Any, Callable, Dict, List, Optional


class PlannerAgent:
    """规划 Agent：分解任务、输出单步指令、响应验证反馈。

    Planner 不持有任何浏览器工具，只通过 LLM 生成结构化的"下一步指令"。

    Args:
        task: 用户任务描述。
        llm_call: LLM 调用函数，签名 (system_prompt, user_prompt, max_tokens) -> str。
        config: 多 Agent 配置中 planner 部分的字典。
    """

    def __init__(self, task: str, llm_call: Callable[[str, str, int], str], config: Dict[str, Any]):
        self.task = task
        self.llm_call = llm_call
        self.config = config
        self.system_prompt = config.get("system_prompt", "你是一个规划 Agent。")
        self.max_tokens = config.get("max_tokens", 384)
        self.prompt_cfg = config.get("prompt", {})
        # 执行历史：每条记录包含 step、current_step、expected_result、execution_result、verify_status、verify_feedback
        self.history: List[Dict[str, Any]] = []

    def add_history(self, record: Dict[str, Any]) -> None:
        """追加一条执行历史记录。"""
        self.history.append(record)

    def build_prompt(self, verify_feedback: Optional[str] = None) -> str:
        """构建发给 LLM 的 prompt，包含任务、历史和验证反馈。"""
        history_text = ""
        for idx, rec in enumerate(self.history, start=1):
            history_text += (
                f"步骤 {idx}:\n"
                f"  指令: {rec.get('current_step', '')}\n"
                f"  预期: {rec.get('expected_result', '')}\n"
                f"  执行结果: {rec.get('execution_result', '')}\n"
                f"  验证状态: {rec.get('verify_status', '')}\n"
                f"  验证反馈: {rec.get('verify_feedback', '')}\n"
            )
        current_history = history_text or "尚无执行历史。"

        feedback_text = verify_feedback or "无验证反馈。"

        prompt_lines = [
            self.prompt_cfg.get("intro", "你是网页自动化任务的规划 Agent（Planner）。当前用户任务："),
            self.task,
            "",
            "执行历史：",
            current_history,
            "",
            "验证反馈：",
            feedback_text,
            "",
            *self.prompt_cfg.get("instructions", []),
            "",
            "示例：",
            *self.prompt_cfg.get("examples", []),
            "",
            *self.prompt_cfg.get("closing", []),
        ]
        return "\n".join(prompt_lines)

    def parse_response(self, text: str) -> Dict[str, Any]:
        """解析 LLM 输出，提取 thought/current_step/expected_result/is_final_step。

        优先尝试 JSON 解析；失败时回退到行解析模式。
        """
        cleaned = text.strip()
        try:
            parsed = json.loads(cleaned)
            return {
                "thought": parsed.get("thought", "") or parsed.get("思考", ""),
                "current_step": parsed.get("current_step", "") or parsed.get("当前步骤", ""),
                "expected_result": parsed.get("expected_result", "") or parsed.get("预期结果", ""),
                "is_final_step": bool(parsed.get("is_final_step", False) or parsed.get("是否最后一步", False)),
            }
        except Exception:
            pass

        # 行解析回退
        thought = ""
        current_step = ""
        expected_result = ""
        is_final_step = False
        patterns = {
            "thought": re.compile(r"^(thought|思考)\s*[:：]\s*(.*)$", re.IGNORECASE),
            "current_step": re.compile(r"^(current_step|当前步骤)\s*[:：]\s*(.*)$", re.IGNORECASE),
            "expected_result": re.compile(r"^(expected_result|预期结果)\s*[:：]\s*(.*)$", re.IGNORECASE),
            "is_final_step": re.compile(r"^(is_final_step|是否最后一步)\s*[:：]\s*(.*)$", re.IGNORECASE),
        }
        for line in cleaned.splitlines():
            stripped = line.strip()
            if not stripped:
                continue
            for key, pattern in patterns.items():
                match = pattern.match(stripped)
                if match:
                    value = match.group(2).strip()
                    if key == "thought":
                        thought = value
                    elif key == "current_step":
                        current_step = value
                    elif key == "expected_result":
                        expected_result = value
                    elif key == "is_final_step":
                        is_final_step = value.lower() in ("true", "是", "yes", "1")
                    break

        return {
            "thought": thought,
            "current_step": current_step,
            "expected_result": expected_result,
            "is_final_step": is_final_step,
        }

    def plan_next_step(self, verify_feedback: Optional[str] = None) -> Dict[str, Any]:
        """调用 LLM 生成下一个步骤指令。

        Args:
            verify_feedback: 来自 Verifier 的反馈（可选），用于调整计划。

        Returns:
            包含 thought/current_step/expected_result/is_final_step 的字典。
        """
        prompt_text = self.build_prompt(verify_feedback=verify_feedback)
        response = self.llm_call(
            system_prompt=self.system_prompt,
            user_prompt=prompt_text,
            max_tokens=self.max_tokens,
        )
        return self.parse_response(response)