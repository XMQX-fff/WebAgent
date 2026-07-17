# WebAgent

基于 Playwright 浏览器工具和 OpenAI 格式大模型的网页自动化 Agent。

采用 **REACT（Reasoning + Acting）** 交互模式：Agent 通过"观察-思考-行动"循环，调用浏览器工具完成用户指定的网页任务，并将每一步 trace 记录下来。

## 目录结构

```
WebAgent/
├── __init__.py             # 包导出入口
├── base_agent.py           # BaseReActAgent 基类与配置加载工具
├── web_agent.py            # WebAgent 主程序与 CLI 入口
├── web_tools.py            # Playwright 浏览器工具封装
├── openai_client.py        # OpenAI 风格大模型调用接口
├── config/
│   └── agent_config.json   # Agent prompt 模板与工具元数据配置
├── web_traces/
│   └── web_agent_trace.jsonl  # 运行 trace 记录
├── requirements.txt        # 依赖列表
└── README.md               # 本文件
```

## 架构说明

- **`base_agent.py`** — 通用 REACT Agent 基类 `BaseReActAgent`，实现 prompt 构建、LLM 响应解析、工具调用调度、trace 记录等核心循环逻辑，独立于具体工具实现。
- **`web_agent.py`** — `WebAgent` 继承 `BaseReActAgent`，绑定浏览器工具集（通过 `web_tools.WebBrowser`）和 OpenAI LLM 调用，提供 CLI 命令行入口。
- **`openai_client.py`** — 封装 OpenAI 格式的 chat completion 调用，支持通过环境变量配置 API Key 和 Base URL。

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

交互模式：

```bash
python web_agent.py
```

直接传入任务：

```bash
python web_agent.py "打开 https://example.com 并提取页面标题"
```

### 4. 作为 Python 包导入

```python
from WebAgent import WebAgent, BaseReActAgent

# 使用 WebAgent
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
| `finish` | 结束任务并输出最终结果 |

## 交互示例

```
请输入你的网页任务，例如：打开 https://example.com 并提取页面标题
> 打开 https://www.example.com 并告诉我页面标题是什么

=== 结果 ===
页面标题是：Example Domain
追踪已写入：web_traces/web_agent_trace.jsonl
```

## 运行结果

- 最终答案打印到终端。
- 每一步的 thought / action / observation 会追加写入 `web_traces/web_agent_trace.jsonl`。
- 截图文件保存在 `web_traces/` 目录。

## 注意

- 目前只支持公开网页自动化，不支持登录凭据、验证码、支付等敏感操作。
- 需要有效的 OpenAI 兼容 API Key。