# WebAgent

基于 Playwright 浏览器工具和 OpenAI 格式大模型的网页自动化 Agent。

支持两种架构：
- **三 Agent 架构（推荐）**：Planner + Executor + Verifier，职责分离，带独立验证与动态调整能力。
- **单 Agent 架构（兼容）**：REACT（Reasoning + Acting）模式，"观察-思考-行动"循环。

## 目录结构

```
WebAgent/
├── __init__.py                  # 包导出入口
├── agents/                      # Agent 相关代码
│   ├── __init__.py
│   ├── base_agent.py            # BaseReActAgent 基类与配置加载工具
│   ├── web_agent.py             # WebAgent（单Agent）与 CLI 入口
│   ├── planner_agent.py         # Planner Agent（规划）
│   ├── executor_agent.py        # Executor Agent（执行）
│   ├── verifier_agent.py        # Verifier Agent（验证）
│   └── multi_agent.py           # MultiAgentCoordinator 协调器与 CLI 入口
├── core/                        # 核心基础设施
│   ├── __init__.py
│   ├── openai_client.py         # OpenAI 风格大模型调用接口
│   └── web_tools.py             # Playwright 浏览器工具封装
├── config/
│   ├── agent_config.json        # 单 Agent prompt 模板与工具元数据配置
│   └── multi_agent_config.json  # 三 Agent 配置（planner/executor/verifier）
├── evaluation/                  # 评测系统
│   ├── evaluator_agent.py       # 评测 Agent（运行用例 + LLM 评估）
│   └── saucedemo_evaluation_cases.md  # SauceDemo 评测用例文档
├── web_traces/
│   ├── web_agent_trace.jsonl    # 单 Agent 运行 trace 记录
│   └── multi_agent_trace.jsonl  # 三 Agent 运行 trace 记录
├── docs/
│   ├── LEARNING_AGENT.md        # 项目深度解析与学习指南
│   └── ISSUES_AND_SOLUTIONS.md  # 问题记录与解决方案
├── requirements.txt             # 依赖列表
└── README.md                    # 本文件
```

## 架构说明

### 三 Agent 架构（Planner + Executor + Verifier，推荐）

```
┌─────────────────────────────────────────────────────────────────────┐
│                       MultiAgentCoordinator                        │
│                      (三Agent协调器 + 主循环)                       │
├─────────────────────────────────────────────────────────────────────┤
│                                                                     │
│  ┌─────────────────────┐    ┌─────────────────────┐                │
│  │    Planner Agent     │    │   Executor Agent    │                │
│  │                      │    │                     │                │
│  │ 职责:                │    │ 职责:               │                │
│  │ - 分解任务为步骤序列  │───▶│ - 执行当前步骤      │                │
│  │ - 制定执行计划        │    │ - 调用浏览器工具    │                │
│  │ - 响应验证反馈        │    │ - 处理执行异常      │                │
│  │ - 调整/细化计划       │    │ - 记录执行细节      │                │
│  └──────────┬───────────┘    └──────────┬──────────┘                │
│             ▲                           │                          │
│             │                           ▼                          │
│             │              ┌─────────────────────┐                │
│             │              │   Verifier Agent     │                │
│             │              │                     │                │
│             └──────────────│ 职责:               │                │
│                            │ - 校验执行结果      │                │
│                            │ - 判断步骤是否完成  │                │
│                            │ - 验证最终答案质量  │                │
│                            │ - 决定下一步：      │                │
│                            │   • success → 下一步 │                │
│                            │   • retry → 重试    │                │
│                            │   • adjust → 调整   │                │
│                            │   • done → 返回     │                │
│                            └─────────────────────┘                │
└─────────────────────────────────────────────────────────────────────┘
```

- **`agents/planner_agent.py`** — `PlannerAgent`，将用户任务分解为可执行的原子步骤，每次只输出下一个步骤，并根据验证反馈动态调整计划。不直接调用工具。
- **`agents/executor_agent.py`** — `ExecutorAgent`，继承 `BaseReActAgent`，针对单个步骤运行工具循环，专注执行，不判断整个任务是否完成。
- **`agents/verifier_agent.py`** — `VerifierAgent`，校验执行结果是否符合预期，输出 `success`/`retry`/`adjust`/`done` 四种状态，驱动协调器决策。
- **`agents/multi_agent.py`** — `MultiAgentCoordinator`，编排三 Agent 主循环，管理浏览器生命周期与统一 trace 记录。

#### 三 Agent 协作流程

```
MultiAgentCoordinator.run():
  │
  ├── 0. 浏览器状态决策与启动（LLM 判断是否复用登录态）
  │
  ├── 1. Planner 分析任务，输出下一个步骤指令（current_step + expected_result）
  │     ↓
  ├── 2. Executor 执行该步骤，调用浏览器工具（最多 max_executor_steps 轮）
  │     ↓
  ├── 3. Verifier 校验执行结果
  │     ↓
  ├── 4. 根据 Verifier 决定:
  │     ├── success → 记录历史，回到步骤 1（规划下一个步骤）
  │     ├── retry   → 回到步骤 2（重试当前步骤，最多 max_retries_per_step 次）
  │     ├── adjust  → 带反馈回到步骤 1（Planner 调整计划，最多 max_adjusts 次）
  │     └── done    → 输出最终结果
  │
  └── 5. 统一 trace 记录三 Agent 协作全过程（写入 multi_agent_trace.jsonl）
```

#### 配置说明

三 Agent 架构的配置位于 `config/multi_agent_config.json`，分为四个部分：

| 配置块 | 说明 | 关键参数 |
|--------|------|----------|
| `coordinator` | 协调器运行参数 | `max_cycles`（最大循环数）、`max_retries_per_step`（单步最大重试）、`max_adjusts`（最大调整次数）、`max_executor_steps`（单步最大工具调用轮数） |
| `planner` | Planner 的 system_prompt、max_tokens、prompt 模板 | prompt 包含 intro/instructions/examples/closing |
| `executor` | Executor 的 system_prompt、max_tokens、prompt 模板、工具元数据 | tools 部分与单 Agent 的 `agent_config.json` 一致 |
| `verifier` | Verifier 的 system_prompt、max_tokens、prompt 模板 | 输出四种状态：success/retry/adjust/done |

### 单 Agent 架构（REACT，兼容）

- **`agents/base_agent.py`** — 通用 REACT Agent 基类 `BaseReActAgent`，实现 prompt 构建、LLM 响应解析、工具调用调度、trace 记录等核心循环逻辑，独立于具体工具实现。
- **`agents/web_agent.py`** — `WebAgent` 继承 `BaseReActAgent`，绑定浏览器工具集（通过 `core/web_tools.WebBrowser`）和 OpenAI LLM 调用。
- **`core/openai_client.py`** — 封装 OpenAI 格式的 chat completion 调用，支持通过环境变量配置 API Key 和 Base URL。

这种分层设计使得 `BaseReActAgent` 可以被复用于其他非浏览器的自动化场景——只需继承并传入不同的工具映射和 LLM 调用函数即可。

## 快速开始

### 1. 安装依赖

```bash
pip install -r requirements.txt
playwright install chromium
```

### 2. 设置 API Key

```bash
export OPENAI_API_KEY="your_api_key"
```

如需使用其他兼容 OpenAI 格式的服务（如 SiliconFlow、DeepSeek 等），可通过环境变量覆盖：

```bash
export OPENAI_BASE_URL="https://api.example.com/v1"
```

### 3. 运行 Agent

默认使用**三 Agent 架构**（Planner + Executor + Verifier）：

```bash
# 交互模式
python web_agent.py

# 直接传入任务
python web_agent.py "打开 https://example.com 并提取页面标题"
```

如需回退到**单 Agent 架构**（REACT），添加 `--single` 参数：

```bash
python web_agent.py --single "打开 https://example.com 并提取页面标题"
```

也可以直接运行三 Agent 入口：

```bash
python multi_agent.py "打开 https://example.com 并提取页面标题"
```

### 4. 作为 Python 包导入

```python
from WebAgent import WebAgent, MultiAgentCoordinator, BaseReActAgent

# 使用三 Agent 架构（推荐）
coordinator = MultiAgentCoordinator("打开 https://example.com 并提取页面标题")
result = coordinator.run()
print(result)

# 使用单 Agent 架构（兼容）
agent = WebAgent("打开 https://example.com 并提取页面标题")
result = agent.run()
print(result)

# 或继承 BaseReActAgent 实现自定义 Agent
class MyAgent(BaseReActAgent):
    ...
```

## 支持的浏览器工具

| 工具 | 功能 |
|------|------|
| `browser_open` | 打开指定 URL |
| `browser_observe` | 观察当前页面，返回 URL、标题和可见文本摘要 |
| `browser_click` | 点击页面元素（支持 CSS 选择器或 `text=` 形式） |
| `browser_type` | 在输入框中输入文本 |
| `browser_select` | 选择下拉框选项 |
| `browser_extract` | 从页面中提取指定信息 |
| `browser_screenshot` | 保存当前页面截图 |
| `browser_clear_state` | 清除浏览器状态（cookies/localStorage/sessionStorage）并删除状态文件 |
| `finish` | 结束当前步骤/任务并输出结果 |

## 交互示例

### 三 Agent 架构（默认）

```
WebAgent: 使用 Playwright 操作网页。当前架构：三 Agent (Planner + Executor + Verifier)
请输入你的网页任务，例如：打开 https://example.com 并提取页面标题
> 打开 https://www.example.com 并告诉我页面标题是什么

=== 结果 ===
页面标题是：Example Domain
追踪已写入：web_traces/multi_agent_trace.jsonl
```

三 Agent 架构的 trace 记录按阶段标注，示例：

```jsonl
{"cycle": 0, "phase": "init", "thought_summary": "LLM 决策：不使用已保存的浏览器状态，以全新环境启动", "observation": "已跳过状态加载，以全新浏览器环境启动。"}
{"cycle": 0, "phase": "init", "thought_summary": "自动打开初始 URL", "tool": "browser_open", "args": {"url": "https://www.example.com"}, "observation": "页面已打开: Example Domain | https://www.example.com/"}
{"cycle": 1, "phase": "planner", "thought": "页面已打开，需要提取标题。", "current_step": "提取当前页面的标题文本", "expected_result": "获得页面标题文本", "is_final_step": false}
{"cycle": 1, "phase": "executor", "current_step": "提取当前页面的标题文本", "execution_result": "Example Domain", "retry_count": 0}
{"cycle": 1, "phase": "verifier", "current_step": "提取当前页面的标题文本", "execution_result": "Example Domain", "status": "done", "feedback": "最终答案：页面标题是 Example Domain", "is_task_complete": true}
```

### 单 Agent 架构（`--single`）

```
WebAgent: 使用 Playwright 操作网页。当前架构：单 Agent (REACT)
请输入你的网页任务，例如：打开 https://example.com 并提取页面标题
> 打开 https://www.example.com 并告诉我页面标题是什么

=== 结果 ===
页面标题是：Example Domain
追踪已写入：web_traces/web_agent_trace.jsonl
```

## 运行结果

- 最终答案打印到终端。
- **三 Agent 架构**：协调器按 `init`/`planner`/`executor`/`verifier`/`coordinator` 阶段记录 trace，追加写入 `web_traces/multi_agent_trace.jsonl`。
- **单 Agent 架构**：每一步的 thought / action / observation 追加写入 `web_traces/web_agent_trace.jsonl`。
- 截图文件保存在 `web_traces/` 目录。
- 浏览器状态（cookies/localStorage）在任务结束时自动保存到 `web_traces/browser_state.json`，下次运行时由 LLM 决定是否复用。

## 评测系统

项目内置了一套完整的评测系统，用于评估 Agent 在真实网站上的表现。评测系统位于 `evaluation/` 目录。

### 评测流程

```
加载评测用例 → 运行 Agent → 收集输出 → LLM 评估 → 生成报告
```

1. **加载评测用例**：从 `EVALUATION_CASES` 内置定义中加载（每个用例包含任务描述、预期结果、评测点）
2. **运行 Agent**：对每个用例运行 WebAgent（三 Agent 或单 Agent 架构）
3. **收集输出**：记录 Agent 输出、耗时、步骤数、trace 文件路径
4. **LLM 评估**：使用 LLM 对照预期结果和评测点，对 Agent 输出进行评分（0-100）
5. **生成报告**：生成 Markdown 格式的评测报告，包含总体统计和详细结果

### 使用方式

```bash
# 运行全部用例（三 Agent 架构）
python evaluation/evaluator_agent.py

# 使用单 Agent 架构
python evaluation/evaluator_agent.py --single

# 只运行指定用例
python evaluation/evaluator_agent.py --case 1,3,6

# 指定报告输出路径
python evaluation/evaluator_agent.py --output evaluation/report.md
```

### 评测用例

评测用例基于 [SauceDemo](https://www.saucedemo.com/) 测试电商网站，覆盖从简单到复杂的各类网页自动化场景：

| 用例 | 名称 | 难度 | 评测点 |
|------|------|------|--------|
| 1 | 成功登录 | 简单 | 打开页面、填写表单、点击按钮、提取信息 |
| 2 | 登录失败（错误密码） | 简单 | 识别失败、提取错误信息、如实报告 |
| 3 | 锁定用户登录 | 简单 | 处理登录失败、提取特定错误 |
| 4 | 商品排序（价格从低到高） | 中等 | 下拉框操作、排序验证、信息提取 |
| 5 | 添加商品到购物车 | 中等 | 点击按钮、打开购物车、提取商品信息 |
| 6 | 完整购物流程（结账） | 困难 | 多步骤规划、表单填写、多页面导航、结果验证 |
| 7 | 商品详情页查看 | 中等 | 点击链接、提取详情信息 |
| 8 | 退出登录 | 中等 | 菜单操作、退出验证 |
| 9 | 购物车移除商品 | 中等 | 添加/移除操作、状态验证 |
| 10 | 问题用户登录后操作 | 中等 | 异常处理、诚实报告 |

### 评测评分标准

| 评分项 | 说明 | 权重 |
|--------|------|------|
| 任务完成度 | 是否成功完成目标任务 | 40% |
| 步骤正确性 | 执行步骤是否合理、无多余操作 | 20% |
| 异常处理 | 遇到错误/异常时能否正确处理 | 20% |
| 结果准确性 | 提取的信息是否准确 | 20% |

## 注意

- 目前只支持公开网页自动化，不支持登录凭据、验证码、支付等敏感操作。
- 需要有效的 OpenAI 兼容 API Key。
- 三 Agent 架构相比单 Agent 会消耗更多 LLM 调用（每个步骤至少 3 次：规划 + 执行 + 验证），但具备独立验证与动态调整能力，适合复杂任务。
- 可通过 `config/multi_agent_config.json` 中的 `coordinator` 配置块调整循环与重试上限，控制 token 消耗。

## 相关文档

- [docs/LEARNING_AGENT.md](docs/LEARNING_AGENT.md) — 项目深度解析与学习指南（含评测系统详解）
- [docs/ISSUES_AND_SOLUTIONS.md](docs/ISSUES_AND_SOLUTIONS.md) — 问题记录与解决方案（含评测系统问题）
- [evaluation/saucedemo_evaluation_cases.md](evaluation/saucedemo_evaluation_cases.md) — SauceDemo 评测用例文档
