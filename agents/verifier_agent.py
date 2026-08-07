"""Verifier Agent - 网页自动化任务的验证 Agent。

Verifier 负责检查 Executor 的执行结果是否符合 Planner 的预期，
判断步骤是否成功完成以及整个任务是否完成。Verifier 不直接调用任何工具。

验证结果有四种状态：
- success: 当前步骤执行成功，可以继续下一步
- retry: 当前步骤执行失败，需要 Executor 重试
- adjust: 当前步骤方向有误，需要 Planner 调整计划
- done: 整个用户任务已完成，最终答案已获得
"""

import json
import re
from typing import Any, Callable, Dict


# 验证状态常量
STATUS_SUCCESS = "success"
STATUS_RETRY = "retry"
STATUS_ADJUST = "adjust"
STATUS_DONE = "done"
VALID_STATUSES = {STATUS_SUCCESS, STATUS_RETRY, STATUS_ADJUST, STATUS_DONE}


class VerifierAgent:
    """验证 Agent：校验执行结果，输出结构化判断。

    Verifier 不持有任何工具，只通过 LLM 生成结构化的验证结论。

    Args:
        task: 用户任务描述。
        llm_call: LLM 调用函数，签名 (system_prompt, user_prompt, max_tokens) -> str。
        config: 多 Agent 配置中 verifier 部分的字典。
    """

    def __init__(self, task: str, llm_call: Callable[[str, str, int], str], config: Dict[str, Any]):
        self.task = task
        self.llm_call = llm_call
        self.config = config
        self.system_prompt = config.get("system_prompt", "你是一个验证 Agent。")
        self.max_tokens = config.get("max_tokens", 256)
        self.prompt_cfg = config.get("prompt", {})

    def build_prompt(
        self,
        current_step: str,
        expected_result: str,
        execution_result: str,
        is_final_step: bool,
    ) -> str:
        """构建发给 LLM 的 prompt，包含步骤、预期、实际结果。"""
        final_flag = "是（这是最终步骤，执行结果应包含任务的最终答案）" if is_final_step else "否"
        prompt_lines = [
            self.prompt_cfg.get("intro", "你是网页自动化任务的验证 Agent（Verifier）。"),
            "",
            f"用户原始任务: {self.task}",
            f"当前步骤描述: {current_step}",
            f"预期结果: {expected_result}",
            f"实际执行结果: {execution_result}",
            f"是否为最终步骤: {final_flag}",
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
        """解析 LLM 输出，提取 thought/status/feedback/is_task_complete。

        优先尝试 JSON 解析；失败时回退到行解析模式。
        """
        cleaned = text.strip()
        try:
            parsed = json.loads(cleaned)
            status = (parsed.get("status", "") or parsed.get("状态", "")).strip().lower()
            if status not in VALID_STATUSES:
                status = STATUS_SUCCESS
            return {
                "thought": parsed.get("thought", "") or parsed.get("思考", ""),
                "status": status,
                "feedback": parsed.get("feedback", "") or parsed.get("反馈", ""),
                "is_task_complete": bool(parsed.get("is_task_complete", False) or parsed.get("任务是否完成", False)),
            }
        except Exception:
            pass

        # 行解析回退
        thought = ""
        status = STATUS_SUCCESS
        feedback = ""
        is_task_complete = False
        patterns = {
            "thought": re.compile(r"^(thought|思考)\s*[:：]\s*(.*)$", re.IGNORECASE),
            "status": re.compile(r"^(status|状态)\s*[:：]\s*(.*)$", re.IGNORECASE),
            "feedback": re.compile(r"^(feedback|反馈)\s*[:：]\s*(.*)$", re.IGNORECASE),
            "is_task_complete": re.compile(r"^(is_task_complete|任务是否完成)\s*[:：]\s*(.*)$", re.IGNORECASE),
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
                    elif key == "status":
                        lowered = value.lower()
                        status = lowered if lowered in VALID_STATUSES else STATUS_SUCCESS
                    elif key == "feedback":
                        feedback = value
                    elif key == "is_task_complete":
                        is_task_complete = value.lower() in ("true", "是", "yes", "1")
                    break

        return {
            "thought": thought,
            "status": status,
            "feedback": feedback,
            "is_task_complete": is_task_complete,
        }

    def verify(
        self,
        current_step: str,
        expected_result: str,
        execution_result: str,
        is_final_step: bool = False,
    ) -> Dict[str, Any]:
        """调用 LLM 验证执行结果。

        Args:
            current_step: 当前步骤描述。
            expected_result: 预期结果。
            execution_result: Executor 的实际执行结果。
            is_final_step: 是否为最终步骤。

        Returns:
            包含 thought/status/feedback/is_task_complete 的字典。
        """
        prompt_text = self.build_prompt(
            current_step=current_step,
            expected_result=expected_result,
            execution_result=execution_result,
            is_final_step=is_final_step,
        )
        response = self.llm_call(
            system_prompt=self.system_prompt,
            user_prompt=prompt_text,
            max_tokens=self.max_tokens,
        )
        result = self.parse_response(response)
        # 启发式补充：如果执行结果明显是错误，但 LLM 判断为 success/done，降级为 retry
        if isinstance(execution_result, str) and execution_result.startswith("ERROR[") and result["status"] not in (STATUS_RETRY, STATUS_ADJUST):
            result["status"] = STATUS_RETRY
            if not result["feedback"]:
                result["feedback"] = "执行结果包含错误，建议重试当前步骤。"
        return result