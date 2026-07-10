# WebAgent

这是 `WebAgent` 文件夹内的网页自动化 Agent 实现，基于 Playwright 浏览器工具和 OpenAI 格式大模型调用。

## 目录结构

- `web_agent.py`：Agent 主程序，包含 REACT 交互 loop。
- `web_tools.py`：Playwright 浏览器工具封装。
- `openai_client.py`：OpenAI 风格大模型调用接口。
- `config/agent_config.json`：Agent prompt 与工具元数据配置。
- `requirements.txt`：依赖列表。

## 目标功能

用户在终端输入网页任务，Agent 将：

1. 根据任务自动选择浏览器工具。
2. 使用 Playwright 打开页面、观察页面、点击、输入、提取内容。
3. 使用 OpenAI 格式的模型请求，返回 JSON 格式动作。
4. 将每一步 trace 写入 `web_agent_trace.jsonl`。

## 快速开始

1. 安装依赖：

```bash
pip install -r WebAgent/requirements.txt
python -m playwright install chromium
```

2. 设置 OpenAI API Key：

```bash
export OPENAI_API_KEY="your_api_key"
```

3. 运行 Agent：

```bash
python WebAgent/web_agent.py
```

也可以直接传入任务：

```bash
python WebAgent/web_agent.py "打开 https://example.com 并提取页面标题"
```

## 支持动作工具

- `browser_open`：打开指定 URL。
- `browser_observe`：观察当前页面，返回 URL、title 和可见文本摘要。
- `browser_click`：点击页面元素。
- `browser_type`：在输入框中输入文本。
- `browser_select`：选择下拉框选项。
- `browser_extract`：从页面中提取指定内容。
- `browser_screenshot`：保存页面截图。
- `finish`：结束任务并输出最终结果。

## 例子

在交互提示中输入：

```text
打开 https://example.com 并提取页面标题
```

如果模型判断页面已完成任务，它会返回 `finish` 并输出最终答案。

## 运行结果

- 最终答案会打印到终端。
- 每一步会写入 `web_agent_trace.jsonl`。
- 如果使用了截图，截图文件会保存在 `web_traces/` 目录。

## 注意

- 目前只支持公开网页自动化，不支持登录凭据、验证码、支付等敏感操作。
