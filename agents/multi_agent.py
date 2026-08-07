"""MultiAgentCoordinator - Planner + Executor + Verifier 三 Agent 协调器。

协调器负责编排三 Agent 的主循环：
  1. Planner 分析任务，输出下一个步骤指令
  2. Executor 执行该步骤，调用浏览器工具
  3. Verifier 校验执行结果
  4. 根据 Verifier 的判断决定下一步动作：
     - success → 回到步骤 1（规划下一个步骤）
     - retry   → 回到步骤 2（重试当前步骤）
     - adjust  → 回到步骤 1（带反馈重新规划）
     - done    → 输出最终结果

协调器还负责：
  - 浏览器状态决策与生命周期管理（复用 WebAgent 的逻辑）
  - 统一的 trace 记录（记录三 Agent 协作的全过程）
  - 循环次数与重试次数的安全限制
"""

import json
import re
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

try:
    from core.openai_client import call_openai_llm
    from core.web_tools import WebBrowser, decide_state_from_task
    from agents.base_agent import load_json_config
    from agents.planner_agent import PlannerAgent
    from agents.executor_agent import ExecutorAgent
    from agents.verifier_agent import VerifierAgent, STATUS_SUCCESS, STATUS_RETRY, STATUS_ADJUST, STATUS_DONE
except ImportError:
    # 直接运行脚本时（python agents/multi_agent.py），将项目根目录加入 sys.path
    _root = str(Path(__file__).resolve().parent.parent)
    if _root not in sys.path:
        sys.path.insert(0, _root)
    from core.openai_client import call_openai_llm
    from core.web_tools import WebBrowser, decide_state_from_task
    from agents.base_agent import load_json_config
    from agents.planner_agent import PlannerAgent
    from agents.executor_agent import ExecutorAgent
    from agents.verifier_agent import VerifierAgent, STATUS_SUCCESS, STATUS_RETRY, STATUS_ADJUST, STATUS_DONE


# 默认浏览器状态文件路径
DEFAULT_STATE_FILE = "web_traces/browser_state.json"


class MultiAgentCoordinator:
    """Planner + Executor + Verifier 三 Agent 协调器。

    Args:
        task: 用户任务描述。
        state_file: 浏览器状态持久化文件路径。为 None 时不持久化状态。
        config_path: 多 Agent 配置文件路径。
    """

    def __init__(self, task: str, state_file: Optional[str] = DEFAULT_STATE_FILE, config_path: Optional[str] = None):
        self.task = task
        # 加载多 Agent 配置
        if config_path is None:
            config_path = str(Path(__file__).resolve().parent.parent / "config" / "multi_agent_config.json")
        config = load_json_config(Path(config_path))

        self.config = config
        coord_cfg = config.get("coordinator", {})
        self.max_cycles = coord_cfg.get("max_cycles", 12)
        self.max_retries_per_step = coord_cfg.get("max_retries_per_step", 2)
        self.max_adjusts = coord_cfg.get("max_adjusts", 3)
        self.max_executor_steps = coord_cfg.get("max_executor_steps", 4)
        trace_file_rel = coord_cfg.get("trace_file", "web_traces/multi_agent_trace.jsonl")
        self.trace_file = str(Path(trace_file_rel).resolve())

        # 浏览器与工具
        self.browser = WebBrowser(state_file=state_file)
        # 如果任务中包含 URL，记下来以便启动时自动打开
        m = re.search(r"(https?://[^\s,]+)", task)
        self.initial_url = m.group(1) if m else None
        self.tools = {
            "browser_open": self.browser.browser_open,
            "browser_observe": self.browser.browser_observe,
            "browser_click": self.browser.browser_click,
            "browser_type": self.browser.browser_type,
            "browser_select": self.browser.browser_select,
            "browser_extract": self.browser.browser_extract,
            "browser_screenshot": self.browser.browser_screenshot,
            "browser_clear_state": self.browser.browser_clear_state,
        }

        # 三 Agent 实例
        self.planner = PlannerAgent(task=task, llm_call=call_openai_llm, config=config.get("planner", {}))
        self.verifier = VerifierAgent(task=task, llm_call=call_openai_llm, config=config.get("verifier", {}))
        # Executor 延迟创建：需要 current_step，在 run 中动态创建/复用
        self.executor: Optional[ExecutorAgent] = None
        self.executor_config = config.get("executor", {})
        self.tool_metadata = self.executor_config.get("tools", {})

        # 运行状态
        self.trace: List[Dict[str, Any]] = []
        self.cycle_count = 0
        self.output: Optional[str] = None
        self.error: Optional[str] = None

    def add_trace(self, phase: str, record: Dict[str, Any]) -> None:
        """记录协调器级别的 trace：追加到内存列表并写入 JSONL 文件。"""
        entry = {
            "cycle": self.cycle_count,
            "phase": phase,
            **record,
        }
        self.trace.append(entry)
        with open(self.trace_file, "a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, ensure_ascii=False) + "\n")

    def _get_browser_context(self) -> str:
        """获取浏览器当前页面状态摘要，供 Executor 作为上下文。

        返回当前页面的 URL 和标题，让 Executor 知道页面是否已打开，
        避免在每个新步骤开始时盲目重新打开页面。
        """
        try:
            page = self.browser.page
            if page is None or getattr(page, "is_closed", lambda: False)():
                return "浏览器尚未打开任何页面"
            url = page.url or "未知"
            title = ""
            try:
                title = page.title() or ""
            except Exception:
                pass
            if url and url != "about:blank":
                return f"已打开页面: {title} | {url}" if title else f"已打开页面: {url}"
            return "浏览器已启动但未打开具体页面"
        except Exception:
            return "无法获取浏览器状态"

    def _ensure_executor(self, current_step: str) -> ExecutorAgent:
        """创建或复用 Executor 实例，并注入当前浏览器状态上下文。"""
        browser_ctx = self._get_browser_context()
        if self.executor is None:
            self.executor = ExecutorAgent(
                task=self.task,
                current_step=current_step,
                tools=self.tools,
                tool_metadata=self.tool_metadata,
                llm_call=call_openai_llm,
                config=self.executor_config,
                max_steps=self.max_executor_steps,
                trace_file=self.trace_file,
            )
        else:
            self.executor.reset_for_new_step(current_step)
            # 同步 trace_file 以防被覆盖
            self.executor.trace_file = self.trace_file
        # 注入浏览器当前状态，让 Executor 知道页面是否已打开
        self.executor.browser_context = browser_ctx
        return self.executor

    def run(self) -> str:
        """执行三 Agent 协调主循环。"""
        # 清空旧 trace 文件
        if Path(self.trace_file).exists():
            try:
                Path(self.trace_file).unlink()
            except OSError:
                pass

        try:
            # 步骤 0：浏览器状态决策与启动
            use_saved_state = decide_state_from_task(
                task=self.task,
                llm_call=call_openai_llm,
                state_file=self.browser.state_file,
            )
            if use_saved_state:
                self.browser.start(load_state=True)
                self.add_trace("init", {
                    "thought_summary": "LLM 决策：复用已保存的浏览器状态",
                    "observation": "已加载已保存的浏览器状态（cookies/localStorage）。",
                })
            else:
                self.browser.start(load_state=False)
                self.add_trace("init", {
                    "thought_summary": "LLM 决策：不使用已保存的浏览器状态，以全新环境启动",
                    "observation": "已跳过状态加载，以全新浏览器环境启动。",
                })

            # 自动打开初始 URL（如有）
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
                self.add_trace("init", {
                    "thought_summary": "自动打开初始 URL",
                    "tool": "browser_open",
                    "args": {"url": self.initial_url},
                    "observation": obs,
                })

            # 三 Agent 主循环
            adjust_count = 0
            verify_feedback: Optional[str] = None

            while self.cycle_count < self.max_cycles:
                self.cycle_count += 1

                # 阶段 1：Planner 规划下一个步骤
                plan = self.planner.plan_next_step(verify_feedback=verify_feedback)
                current_step = plan.get("current_step", "").strip()
                expected_result = plan.get("expected_result", "").strip()
                is_final_step = plan.get("is_final_step", False)

                self.add_trace("planner", {
                    "thought": plan.get("thought", ""),
                    "current_step": current_step,
                    "expected_result": expected_result,
                    "is_final_step": is_final_step,
                })

                if not current_step:
                    self.error = "Planner 未输出有效的步骤指令，终止执行。"
                    self.add_trace("planner", {
                        "thought": "Planner 输出为空",
                        "observation": self.error,
                    })
                    return self.error

                # 如果 Planner 标记为最终步骤，直接作为最终答案输出（无需执行工具）
                if is_final_step:
                    self.output = current_step
                    self.add_trace("planner", {
                        "thought": "Planner 标记为最终步骤，直接输出最终答案",
                        "observation": f"最终答案：{current_step}",
                    })
                    return self.output

                # 阶段 2：Executor 执行当前步骤
                retry_count = 0
                execution_result = ""
                while True:
                    executor = self._ensure_executor(current_step)
                    execution_result = executor.run()
                    self.add_trace("executor", {
                        "current_step": current_step,
                        "execution_result": execution_result,
                        "retry_count": retry_count,
                    })

                    # 阶段 3：Verifier 验证执行结果
                    verify_result = self.verifier.verify(
                        current_step=current_step,
                        expected_result=expected_result,
                        execution_result=execution_result,
                        is_final_step=is_final_step,
                    )
                    status = verify_result.get("status", STATUS_SUCCESS)
                    feedback = verify_result.get("feedback", "")
                    is_task_complete = verify_result.get("is_task_complete", False)

                    self.add_trace("verifier", {
                        "current_step": current_step,
                        "execution_result": execution_result,
                        "status": status,
                        "feedback": feedback,
                        "is_task_complete": is_task_complete,
                    })

                    if status == STATUS_DONE or is_task_complete:
                        # 整个任务完成
                        self.output = feedback or execution_result
                        return self.output

                    if status == STATUS_SUCCESS:
                        # 当前步骤成功，记录历史并进入下一个规划周期
                        self.planner.add_history({
                            "current_step": current_step,
                            "expected_result": expected_result,
                            "execution_result": execution_result,
                            "verify_status": status,
                            "verify_feedback": feedback,
                        })
                        verify_feedback = None
                        break  # 跳出 retry 循环，回到外层 while 进行下一个规划周期

                    if status == STATUS_RETRY:
                        retry_count += 1
                        if retry_count > self.max_retries_per_step:
                            # 重试次数耗尽，记录历史并让 Planner 决定是否调整
                            self.planner.add_history({
                                "current_step": current_step,
                                "expected_result": expected_result,
                                "execution_result": execution_result,
                                "verify_status": status,
                                "verify_feedback": f"已重试 {self.max_retries_per_step} 次仍失败。{feedback}",
                            })
                            verify_feedback = f"步骤「{current_step}」已重试 {self.max_retries_per_step} 次仍失败，请调整计划。{feedback}"
                            break  # 跳出 retry 循环，回到外层 while，Planner 会根据 feedback 调整
                        # 否则继续重试当前步骤
                        continue

                    if status == STATUS_ADJUST:
                        # 需要调整计划，记录历史并带反馈回到 Planner
                        self.planner.add_history({
                            "current_step": current_step,
                            "expected_result": expected_result,
                            "execution_result": execution_result,
                            "verify_status": status,
                            "verify_feedback": feedback,
                        })
                        verify_feedback = feedback
                        adjust_count += 1
                        if adjust_count > self.max_adjusts:
                            self.error = f"已达到最大调整次数（{self.max_adjusts}），终止执行。"
                            self.add_trace("coordinator", {
                                "thought": "达到最大调整次数限制",
                                "observation": self.error,
                            })
                            return self.error
                        break  # 跳出 retry 循环，回到外层 while 让 Planner 调整

                    # 未知状态，保守处理为 success
                    self.planner.add_history({
                        "current_step": current_step,
                        "expected_result": expected_result,
                        "execution_result": execution_result,
                        "verify_status": status,
                        "verify_feedback": feedback,
                    })
                    verify_feedback = None
                    break

            # 达到最大循环次数
            self.error = f"已达到最大循环次数（{self.max_cycles}），任务未完成。"
            self.add_trace("coordinator", {
                "thought": "达到最大循环次数限制",
                "observation": self.error,
            })
            return self.error
        except Exception as exc:
            error_message = f"三 Agent 协调器执行失败：{exc}"
            self.error = error_message
            self.add_trace("coordinator", {
                "thought": "协调器出现异常",
                "observation": error_message,
            })
            return error_message
        finally:
            # 关闭浏览器（自动保存状态）
            try:
                self.browser.close()
            except Exception:
                pass


def main():
    if len(sys.argv) > 1:
        task = " ".join(sys.argv[1:]).strip()
    else:
        print("WebAgent (Multi-Agent): 使用 Planner + Executor + Verifier 架构操作网页。")
        task = input("请输入你的网页任务，例如：打开 https://example.com 并提取页面标题\n> ").strip()

    if not task:
        print("任务不能为空。")
        sys.exit(1)

    coordinator = MultiAgentCoordinator(task)
    result = coordinator.run()
    print("\n=== 结果 ===")
    print(result)
    print(f"追踪已写入：{coordinator.trace_file}")


if __name__ == "__main__":
    main()