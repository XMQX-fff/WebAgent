"""SauceDemo 评测 Agent。

评测流程：
  1. 加载评测用例（从 JSON 配置或内置定义）
  2. 对每个用例运行 WebAgent（三 Agent 架构 / 单 Agent 架构）
  3. 收集 Agent 输出、耗时、trace 信息
  4. 使用 LLM 对 Agent 输出进行效果评估（对照预期结果）
  5. 生成评测报告（MD 格式）

用法：
  python evaluation/evaluator_agent.py                    # 运行全部用例（三 Agent 架构）
  python evaluation/evaluator_agent.py --single           # 使用单 Agent 架构
  python evaluation/evaluator_agent.py --case 1,3,6       # 只运行指定用例
  python evaluation/evaluator_agent.py --output report.md # 指定报告输出路径
"""

import json
import os
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

# 将项目根目录加入 sys.path
_ROOT = str(Path(__file__).resolve().parent.parent)
if _ROOT not in sys.path:
    sys.path.insert(0, _ROOT)

from core.openai_client import call_openai_llm
from agents.multi_agent import MultiAgentCoordinator
from agents.web_agent import WebAgent


# ============================================================
# 评测用例定义
# ============================================================

# 每个用例包含：
#   id: 用例编号
#   name: 用例名称
#   difficulty: 难度（简单/中等/困难）
#   task: 任务描述（传给 Agent）
#   expected: 预期结果（用于 LLM 评估）
#   check_points: 评测点（用于 LLM 评估）
#   requires_clear_state: 运行前是否清除浏览器状态（默认 True）

EVALUATION_CASES: List[Dict[str, Any]] = [
    {
        "id": 1,
        "name": "成功登录",
        "difficulty": "简单",
        "task": "打开 https://www.saucedemo.com/ ，使用用户名 standard_user 和密码 secret_sauce 登录，登录成功后告诉我页面上显示的商品数量。",
        "expected": "成功登录并跳转到商品列表页（inventory.html），正确报告商品数量为 6。",
        "check_points": [
            "能否正确打开页面",
            "能否定位并填写用户名/密码输入框",
            "能否点击登录按钮",
            "能否验证登录成功并提取商品数量",
        ],
        "requires_clear_state": True,
    },
    {
        "id": 2,
        "name": "登录失败（错误密码）",
        "difficulty": "简单",
        "task": "打开 https://www.saucedemo.com/ ，使用用户名 standard_user 和密码 wrong_password 尝试登录，告诉我登录是否成功，如果失败请告诉我页面显示的错误信息。",
        "expected": "登录失败，停留在登录页，正确提取并报告错误信息（Username and password do not match any user in this service）。",
        "check_points": [
            "能否识别登录失败",
            "能否提取页面上的错误提示文本",
            "能否如实报告失败而非编造成功",
        ],
        "requires_clear_state": True,
    },
    {
        "id": 3,
        "name": "锁定用户登录",
        "difficulty": "简单",
        "task": "打开 https://www.saucedemo.com/ ，使用用户名 locked_out_user 和密码 secret_sauce 登录，告诉我登录结果和页面显示的错误信息。",
        "expected": "登录失败，显示错误信息 Sorry, this user has been locked out.，Agent 能正确提取并报告该错误。",
        "check_points": [
            "能否正确处理登录失败",
            "能否提取特定错误信息",
            "能否如实报告失败",
        ],
        "requires_clear_state": True,
    },
    {
        "id": 4,
        "name": "商品排序（价格从低到高）",
        "difficulty": "中等",
        "task": "打开 https://www.saucedemo.com/ ，使用 standard_user 登录后，将商品列表按价格从低到高排序，然后告诉我价格最低的商品名称和价格。",
        "expected": "成功登录，通过下拉框选择 Price (low to high)，正确识别价格最低的商品（Sauce Labs Onesie，$7.99）。",
        "check_points": [
            "能否操作下拉框",
            "能否验证排序是否生效",
            "能否提取排序后的商品信息",
        ],
        "requires_clear_state": True,
    },
    {
        "id": 5,
        "name": "添加商品到购物车并验证",
        "difficulty": "中等",
        "task": "打开 https://www.saucedemo.com/ 使用 standard_user 登录，将 Sauce Labs Backpack 和 Sauce Labs Bike Light 添加到购物车，然后打开购物车页面，告诉我购物车中有哪些商品以及总数量。",
        "expected": "成功添加 2 件商品到购物车，打开购物车页面，正确报告商品名称（Sauce Labs Backpack、Sauce Labs Bike Light）和数量（2 件）。",
        "check_points": [
            "能否定位并点击 Add to cart 按钮",
            "能否打开购物车",
            "能否提取购物车中的商品信息",
        ],
        "requires_clear_state": True,
    },
    {
        "id": 6,
        "name": "完整购物流程（结账）",
        "difficulty": "困难",
        "task": "打开 https://www.saucedemo.com/ 使用 standard_user 登录，将 Sauce Labs Backpack 加入购物车，进入购物车，点击 Checkout，填写结账信息（名字 John，姓氏 Doe，邮编 12345），继续到总览页面，告诉我总价是多少，然后完成订单。",
        "expected": "完成登录→添加商品→购物车→结账信息→总览→完成订单，正确报告总价（$29.99），最终显示 Thank you for your order! 成功页面。",
        "check_points": [
            "多步骤任务规划能力",
            "表单填写能力",
            "多页面导航能力",
            "最终结果验证能力",
        ],
        "requires_clear_state": True,
    },
    {
        "id": 7,
        "name": "商品详情页查看",
        "difficulty": "中等",
        "task": "打开 https://www.saucedemo.com/ 使用 standard_user 登录，点击 Sauce Labs Bolt T-Shirt 商品，告诉我该商品的描述和价格。",
        "expected": "成功进入商品详情页，正确提取商品描述和价格（$15.99）。",
        "check_points": [
            "能否点击商品链接进入详情页",
            "能否提取详情页中的商品信息",
        ],
        "requires_clear_state": True,
    },
    {
        "id": 8,
        "name": "退出登录",
        "difficulty": "中等",
        "task": "打开 https://www.saucedemo.com/ 使用 standard_user 登录，然后通过侧边栏菜单退出登录，告诉我退出后页面是否回到了登录页。",
        "expected": "成功打开侧边栏菜单，点击 Logout 退出，回到登录页（URL 为 https://www.saucedemo.com/）。",
        "check_points": [
            "能否打开汉堡菜单",
            "能否点击菜单项",
            "能否验证退出结果",
        ],
        "requires_clear_state": True,
    },
    {
        "id": 9,
        "name": "购物车移除商品",
        "difficulty": "中等",
        "task": "打开 https://www.saucedemo.com/ 使用 standard_user 登录，将 Sauce Labs Backpack 和 Sauce Labs Bike Light 添加到购物车，然后打开购物车，移除 Sauce Labs Bike Light，告诉我购物车中剩余的商品名称。",
        "expected": "成功添加 2 件商品，成功移除 1 件商品，购物车中剩余 Sauce Labs Backpack。",
        "check_points": [
            "添加/移除商品操作",
            "购物车状态验证",
        ],
        "requires_clear_state": True,
    },
    {
        "id": 10,
        "name": "问题用户登录后操作",
        "difficulty": "中等",
        "task": "打开 https://www.saucedemo.com/ 使用 problem_user 登录，尝试添加 Sauce Labs Backpack 到购物车，然后告诉我操作是否成功。",
        "expected": "登录成功，添加商品操作可能失败（problem_user 的按钮行为异常），Agent 应如实报告操作结果，而非编造成功。",
        "check_points": [
            "异常场景处理能力",
            "诚实报告失败的能力",
        ],
        "requires_clear_state": True,
    },
]


# ============================================================
# 评测 Agent
# ============================================================

class EvaluatorAgent:
    """评测 Agent：运行评测用例并评估 Agent 效果。"""

    def __init__(
        self,
        use_single: bool = False,
        cases: Optional[List[Dict[str, Any]]] = None,
        output_path: str = "evaluation/evaluation_report.md",
        state_file: str = "web_traces/browser_state.json",
    ):
        self.use_single = use_single
        self.cases = cases or EVALUATION_CASES
        self.output_path = output_path
        self.state_file = state_file
        self.results: List[Dict[str, Any]] = []

    def _clear_browser_state(self) -> None:
        """清除浏览器状态文件，避免登录态干扰。"""
        state_path = Path(self.state_file)
        if state_path.exists():
            try:
                state_path.unlink()
                print(f"  [清理] 已删除浏览器状态文件: {self.state_file}")
            except OSError as exc:
                print(f"  [警告] 删除状态文件失败: {exc}")

    def _run_agent(self, task: str) -> Dict[str, Any]:
        """运行 Agent 并返回结果。"""
        start_time = time.time()
        try:
            if self.use_single:
                agent = WebAgent(task, state_file=self.state_file)
                result = agent.run()
                trace_file = agent.trace_file
                step_count = agent.step_count
            else:
                coordinator = MultiAgentCoordinator(task, state_file=self.state_file)
                result = coordinator.run()
                trace_file = coordinator.trace_file
                step_count = coordinator.cycle_count

            elapsed = time.time() - start_time
            return {
                "output": result,
                "trace_file": trace_file,
                "elapsed": elapsed,
                "step_count": step_count,
                "error": None,
            }
        except Exception as exc:
            elapsed = time.time() - start_time
            return {
                "output": "",
                "trace_file": "",
                "elapsed": elapsed,
                "step_count": 0,
                "error": str(exc),
            }

    def _evaluate_with_llm(self, case: Dict[str, Any], agent_output: str) -> Dict[str, Any]:
        """使用 LLM 评估 Agent 输出是否符合预期。"""
        system_prompt = (
            "你是一个网页 Agent 评测专家。根据评测用例的预期结果和评测点，"
            "评估 Agent 的实际输出是否达到预期。"
            "请只返回一个 JSON 对象，格式为："
            '{"score": 0-100, "passed": true/false, "summary": "简要评估总结", "issues": ["问题1", "问题2"]}'
        )

        user_prompt = f"""请评估以下网页 Agent 的执行结果。

## 评测用例
- 用例名称：{case['name']}
- 任务描述：{case['task']}
- 预期结果：{case['expected']}
- 评测点：{json.dumps(case['check_points'], ensure_ascii=False)}

## Agent 实际输出
{agent_output if agent_output else "（无输出）"}

## 评估要求
1. 根据预期结果和评测点，判断 Agent 输出是否达到预期
2. score 为 0-100 的整数，表示任务完成度
3. passed 为 true/false，表示是否通过评测
4. summary 为简要评估总结（中文，50 字以内）
5. issues 列出未达标的评测点（如果没有问题则为空数组）

请只返回 JSON 对象。"""

        try:
            response = call_openai_llm(system_prompt, user_prompt, max_tokens=300)
            # 解析 JSON（可能包含 markdown 代码块）
            cleaned = response.strip()
            if cleaned.startswith("```"):
                cleaned = cleaned.split("```")[1]
                if cleaned.startswith("json"):
                    cleaned = cleaned[4:]
                cleaned = cleaned.strip()
            parsed = json.loads(cleaned)
            return {
                "score": int(parsed.get("score", 0)),
                "passed": bool(parsed.get("passed", False)),
                "summary": parsed.get("summary", ""),
                "issues": parsed.get("issues", []),
            }
        except Exception as exc:
            return {
                "score": 0,
                "passed": False,
                "summary": f"LLM 评估失败: {exc}",
                "issues": ["LLM 评估失败"],
            }

    def run(self) -> str:
        """运行所有评测用例并生成报告。"""
        print("=" * 60)
        print(f"SauceDemo 评测 Agent")
        print(f"架构: {'单 Agent (REACT)' if self.use_single else '三 Agent (Planner + Executor + Verifier)'}")
        print(f"用例数量: {len(self.cases)}")
        print("=" * 60)

        for idx, case in enumerate(self.cases, 1):
            case_id = case["id"]
            case_name = case["name"]
            print(f"\n[{idx}/{len(self.cases)}] 用例 {case_id}: {case_name}")

            # 运行前清除浏览器状态（避免登录态干扰）
            if case.get("requires_clear_state", True):
                self._clear_browser_state()

            # 运行 Agent
            print(f"  [运行] 任务: {case['task'][:80]}...")
            run_result = self._run_agent(case["task"])

            if run_result["error"]:
                print(f"  [错误] Agent 运行异常: {run_result['error']}")
                evaluation = {
                    "score": 0,
                    "passed": False,
                    "summary": f"Agent 运行异常: {run_result['error'][:100]}",
                    "issues": ["Agent 运行异常"],
                }
            else:
                print(f"  [输出] {run_result['output'][:100]}...")
                print(f"  [耗时] {run_result['elapsed']:.1f}s | 步骤数: {run_result['step_count']}")
                # LLM 评估
                print("  [评估] 正在使用 LLM 评估...")
                evaluation = self._evaluate_with_llm(case, run_result["output"])
                print(f"  [评分] {evaluation['score']}/100 | 通过: {evaluation['passed']} | {evaluation['summary']}")

            # 记录结果
            self.results.append({
                "case": case,
                "run": run_result,
                "evaluation": evaluation,
            })

        # 生成报告
        report = self._generate_report()
        self._save_report(report)
        return report

    def _generate_report(self) -> str:
        """生成评测报告（MD 格式）。"""
        lines = []
        lines.append("# SauceDemo Agent 评测报告")
        lines.append("")
        lines.append(f"- **评测时间**: {time.strftime('%Y-%m-%d %H:%M:%S')}")
        lines.append(f"- **评测架构**: {'单 Agent (REACT)' if self.use_single else '三 Agent (Planner + Executor + Verifier)'}")
        lines.append(f"- **评测用例数**: {len(self.results)}")
        lines.append("")

        # 统计
        passed_count = sum(1 for r in self.results if r["evaluation"]["passed"])
        total_score = sum(r["evaluation"]["score"] for r in self.results)
        avg_score = total_score / len(self.results) if self.results else 0
        total_time = sum(r["run"]["elapsed"] for r in self.results)

        lines.append("## 总体统计")
        lines.append("")
        lines.append("| 指标 | 值 |")
        lines.append("|------|-----|")
        lines.append(f"| 通过用例 | {passed_count}/{len(self.results)} |")
        lines.append(f"| 平均得分 | {avg_score:.1f}/100 |")
        lines.append(f"| 总耗时 | {total_time:.1f}s |")
        lines.append("")

        # 各用例结果
        lines.append("## 用例结果")
        lines.append("")
        lines.append("| 用例 | 名称 | 难度 | 得分 | 通过 | 耗时(s) | 步骤数 | 评估摘要 |")
        lines.append("|------|------|------|------|------|---------|--------|----------|")
        for r in self.results:
            case = r["case"]
            ev = r["evaluation"]
            run = r["run"]
            passed_mark = "✅" if ev["passed"] else "❌"
            lines.append(
                f"| {case['id']} | {case['name']} | {case['difficulty']} | "
                f"{ev['score']} | {passed_mark} | {run['elapsed']:.1f} | {run['step_count']} | {ev['summary']} |"
            )
        lines.append("")

        # 详细结果
        lines.append("## 详细结果")
        lines.append("")
        for r in self.results:
            case = r["case"]
            ev = r["evaluation"]
            run = r["run"]
            lines.append(f"### 用例 {case['id']}: {case['name']}")
            lines.append("")
            lines.append(f"- **难度**: {case['difficulty']}")
            lines.append(f"- **任务**: {case['task']}")
            lines.append(f"- **预期结果**: {case['expected']}")
            lines.append(f"- **得分**: {ev['score']}/100")
            lines.append(f"- **通过**: {'✅ 是' if ev['passed'] else '❌ 否'}")
            lines.append(f"- **耗时**: {run['elapsed']:.1f}s")
            lines.append(f"- **步骤数**: {run['step_count']}")
            lines.append("")
            lines.append("**Agent 输出**:")
            lines.append("")
            lines.append("```")
            lines.append(run["output"] if run["output"] else "（无输出）")
            lines.append("```")
            lines.append("")
            lines.append("**评估摘要**:")
            lines.append("")
            lines.append(f"> {ev['summary']}")
            lines.append("")
            if ev["issues"]:
                lines.append("**未达标评测点**:")
                lines.append("")
                for issue in ev["issues"]:
                    lines.append(f"- {issue}")
                lines.append("")
            if run["trace_file"]:
                lines.append(f"**Trace 文件**: `{run['trace_file']}`")
                lines.append("")
            lines.append("---")
            lines.append("")

        return "\n".join(lines)

    def _save_report(self, report: str) -> None:
        """保存评测报告到文件。"""
        output_path = Path(self.output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("w", encoding="utf-8") as handle:
            handle.write(report)
        print(f"\n评测报告已保存: {self.output_path}")


# ============================================================
# CLI 入口
# ============================================================

def main():
    import argparse

    parser = argparse.ArgumentParser(description="SauceDemo 评测 Agent")
    parser.add_argument("--single", action="store_true", help="使用单 Agent 架构（默认三 Agent）")
    parser.add_argument("--case", type=str, default="", help="只运行指定用例，逗号分隔，如: 1,3,6")
    parser.add_argument("--output", type=str, default="evaluation/evaluation_report.md", help="报告输出路径")
    parser.add_argument("--state-file", type=str, default="web_traces/browser_state.json", help="浏览器状态文件路径")
    args = parser.parse_args()

    # 筛选用例
    cases = EVALUATION_CASES
    if args.case:
        case_ids = [int(x.strip()) for x in args.case.split(",") if x.strip()]
        cases = [c for c in cases if c["id"] in case_ids]
        if not cases:
            print(f"错误: 未找到用例 {args.case}")
            sys.exit(1)

    evaluator = EvaluatorAgent(
        use_single=args.single,
        cases=cases,
        output_path=args.output,
        state_file=args.state_file,
    )
    evaluator.run()


if __name__ == "__main__":
    main()