# WebAgent 多 Agent 架构问题记录与解决方案

> 本文档记录在 Planner + Executor + Verifier 三 Agent 架构开发与测试过程中发现的问题、根因分析以及对应的代码修复方案。

---

## 目录

1. [问题一：browser_open 超时导致反复重开页面](#问题一browser_open-超时导致反复重开页面)
2. [问题二：Executor 跨步骤丢失浏览器状态上下文](#问题二executor-跨步骤丢失浏览器状态上下文)
3. [问题三：复合步骤工具轮数不足导致任务中断](#问题三复合步骤工具轮数不足导致任务中断)
4. [问题四：点击后弹出广告导致 browser_click 卡死](#问题四点击后弹出广告导致-browser_click-卡死)

---

## 问题一：browser_open 超时导致反复重开页面

### 问题现象

在运行三 Agent 架构处理登录任务（`https://practice.expandtesting.com/login`）时，trace 中出现大量 `browser_open` 重复调用：

```jsonl
{"step": 1, "tool": "browser_open", "observation": "ERROR[TIMEOUT]: Page.goto: Timeout 15000ms exceeded..."}
{"step": 2, "tool": "browser_open", "observation": "ERROR[TIMEOUT]: Page.goto: Timeout 15000ms exceeded..."}
{"step": 3, "tool": "browser_open", "observation": "ERROR[TIMEOUT]: Page.goto: Timeout 15000ms exceeded..."}
{"step": 4, "tool": "browser_open", "observation": "ERROR[TIMEOUT]: Page.goto: Timeout 15000ms exceeded..."}
```

核心痛点：**页面其实已经成功打开（URL 已跳转、标题已加载），只是浏览器仍在转圈等待某些资源（广告、统计脚本等）加载完成**。Executor 收到 TIMEOUT 后盲目重试 `browser_open`，白白浪费工具调用轮数，且无法推进任务。

### 根因分析

1. **`browser_open` 使用默认的 `wait_until="load"`**：Playwright 的 `page.goto()` 默认等待页面 `load` 事件，即所有资源（图片、脚本、样式、广告等）全部加载完成。很多真实页面永远达不到这个状态，导致 15 秒超时。

2. **超时后直接返回错误**：`goto` 超时后立即返回 `ERROR[TIMEOUT]`，没有检查页面是否其实已经导航到目标 URL。

3. **Executor 收到错误后盲目重试**：LLM 看到 `browser_open` 返回错误，第一反应是重试相同的操作，而不是先观察当前页面状态。

### 解决方案

针对上述三个根因，做了三层修复：

#### 1. `web_tools.py` - 改进 `browser_open` 超时处理

**核心思路**：改用 `domcontentloaded` 而非 `load`，并且 `goto` 超时后不立即报错，而是检查页面是否已加载。

```python
def browser_open(self, url: str) -> Dict[str, Any]:
    try:
        self.start()
        url = self._normalize_url(url)
        try:
            self.page.goto(url, timeout=15000, wait_until="domcontentloaded")
        except PlaywrightTimeoutError:
            # goto 超时不立即报错：页面可能已导航到目标 URL，只是仍在加载资源
            pass
        # 再尝试等待网络空闲，但容忍其失败
        try:
            self.page.wait_for_load_state("networkidle", timeout=5000)
        except PlaywrightTimeoutError:
            pass
        # 检查当前页面状态：即使 goto 超时，页面可能已成功导航
        current_url = self.page.url or ""
        title = ""
        try:
            title = self.page.title() or ""
        except Exception:
            pass
        if current_url and current_url != "about:blank" and title:
            # 页面已导航到目标且有标题，视为成功
            return ToolResult(status="ok", data=f"页面已打开: {title} | {current_url}", ...).to_dict()
        # 页面确实未加载，返回错误并建议先观察当前状态
        return ToolResult(status="error", error_code="TIMEOUT", ...,
                          suggestion="页面加载超时，但页面可能已部分加载。建议先使用 browser_observe 观察当前页面状态...").to_dict()
    except Exception as exc:
        return ToolResult(status="error", error_code="OPEN_ERROR", ...).to_dict()
```

**关键改动**：
- `goto(wait_until="domcontentloaded")` — 不再等待所有资源加载
- `goto` 超时不直接抛错，先检查 `page.url` 和 `title` 是否已可用
- 错误提示明确建议"先 `browser_observe` 观察页面状态，而不是立即重试"

#### 2. `config/multi_agent_config.json` - Executor prompt 增加超时处理指引

在 Executor 的 `instructions` 中新增三条重要指令：

```
"重要：如果 browser_open 返回 TIMEOUT 错误，不要立即重试 browser_open！页面可能已经加载只是仍在转圈。请先使用 browser_observe 观察当前页面状态，如果观察结果显示页面已加载（有 URL 和标题），则当前步骤已成功，使用 finish 结束。",
"重要：如果某个工具返回 ERROR，请先使用 browser_observe 观察当前页面状态，根据观察结果决定下一步操作，而不是盲目重试相同的工具调用。",
"重要：如果当前页面已经处于目标页面（通过观察确认 URL 和内容），不要重复调用 browser_open 打开相同 URL，直接进行后续操作。"
```

**作用**：从 prompt 层面引导 LLM 在超时后先观察而非盲目重试。

#### 3. `executor_agent.py` - 硬编码安全网机制

在与 LLM 交互之外增加**代码级兜底**：当 `browser_open` 返回 `ERROR[TIMEOUT]` 时，Executor 自动执行一次 `browser_observe`，如果观察到页面已加载，直接判定步骤成功。

```python
# 安全网：browser_open 超时后自动观察页面状态，避免盲目重试
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
```

**作用**：即使 LLM 判断失误，也不会浪费工具调用轮数反复重试打开页面。

### 修复效果

修复后，trace 中 `browser_open` 超时后立即成功返回：

```jsonl
{"step": 1, "tool": "browser_open", "observation": "页面已打开: Test Login Page for Automation Testing Practice | https://practice.expandtesting.com/login"}
```

---

## 问题二：Executor 跨步骤丢失浏览器状态上下文

### 问题现象

在第二个 trace 中，三 Agent 协作已经成功打开了页面并提取了登录信息，但当 Planner 进入下一步（"输入用户名密码并点击登录"）时，Executor **第一轮操作竟然是重新 `browser_open`**：

```jsonl
{"cycle": 3, "phase": "planner", "current_step": "在用户名输入框中输入'practice'，在密码输入框中输入'SuperSecretPassword!'，然后点击'Login'按钮。", "is_final_step": false}
{"step": 1, "thought_summary": "当前步骤要求输入用户名密码并点击登录，但页面尚未打开，先打开目标页面。", "tool": "browser_open", "args": {"url": "https://practice.expandtesting.com/login"}}
```

核心痛点：**页面明明已经打开了，但 Executor 不知道，白白浪费一轮 `browser_open`**。

### 根因分析

1. **Executor 每次执行新步骤时会调用 `reset_for_new_step()`**，该方法清空了 `last_observation`、`trace`、`step_count` 等所有内部状态。

2. **Executor 构建 prompt 时显示"当前尚无工具观察结果"**：由于状态被清空，`last_observation` 为 `None`，prompt 呈现的是"未知状态"，LLM 自然认为页面尚未打开。

3. **浏览器是共享的，但 Executor 没有感知**：浏览器实例由协调器持有并在整个任务中复用，但 Executor 的 prompt 中没有注入"当前浏览器在哪个页面"的信息。

### 解决方案

#### 1. `executor_agent.py` - 新增 `browser_context` 字段

```python
# 浏览器当前状态上下文（由协调器在每步开始前注入），避免 LLM 盲目重新打开页面
self.browser_context: Optional[str] = None
```

#### 2. `executor_agent.py` - 在 prompt 中展示浏览器状态

在 `build_react_prompt()` 中新增一行，将浏览器当前状态注入 prompt：

```python
# 浏览器当前状态上下文（由协调器注入），让 LLM 知道页面是否已打开
browser_ctx_text = self.browser_context or "未知（可能尚未打开任何页面）"

prompt_lines = [
    ...
    "当前状态：",
    f"浏览器当前页面: {browser_ctx_text}",   # 新增
    f"最近一次观察: {last_obs_text}",
    ...
]
```

#### 3. `multi_agent.py` - 协调器在每步开始前注入浏览器状态

新增 `_get_browser_context()` 方法，从共享浏览器实例读取当前 URL 和标题：

```python
def _get_browser_context(self) -> str:
    """获取浏览器当前页面状态摘要，供 Executor 作为上下文。"""
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
```

在 `_ensure_executor()` 中注入：

```python
def _ensure_executor(self, current_step: str) -> ExecutorAgent:
    """创建或复用 Executor 实例，并注入当前浏览器状态上下文。"""
    browser_ctx = self._get_browser_context()
    ...
    # 注入浏览器当前状态，让 Executor 知道页面是否已打开
    self.executor.browser_context = browser_ctx
    return self.executor
```

### 修复效果

修复后，Executor 的 prompt 中会明确显示"浏览器当前页面: 已打开页面: Test Login Page... | https://practice.expandtesting.com/login"，LLM 不会再盲目重新打开页面，而是直接进行输入和点击操作。

---

## 问题三：复合步骤工具轮数不足导致任务中断

### 问题现象

在第三个 trace 中，Planner 给出了一个复合步骤："在登录页面输入用户名 'practice' 和密码 'SuperSecretPassword!'，然后点击登录按钮"。Executor 执行过程如下：

```jsonl
{"step": 1, "tool": "browser_observe", "observation": "观察页面获取输入框和按钮选择器..."}
{"step": 2, "tool": "browser_type", "observation": "已在 #username 输入文本: practice"}
{"step": 3, "tool": "browser_type", "observation": "已在 #password 输入文本: SuperSecretPassword!"}
{"step": 4, "tool": "browser_observe", "observation": "再次观察获取登录按钮选择器..."}
{"step": 4, "thought_summary": "当前步骤达到最大工具调用轮数，停止执行。", "observation": "当前步骤达到最大工具调用轮数（4），以最后一次观察作为结果。"}
```

核心痛点：**用户名和密码都已成功输入（观察结果中 `text: "practice"` 和 `text: "SuperSecretPassword!"` 已确认），但还没来得及点击登录按钮就达到了 `max_executor_steps=4` 的上限**。Verifier 判断 retry 后，Executor 又从头开始，**浪费了已完成的输入操作**。

### 根因分析

1. **`max_executor_steps=4` 对复合步骤不够用**：一个"输入用户名 + 输入密码 + 点击登录"的复合步骤至少需要 4-5 轮工具调用（观察 + 输入 + 输入 + 观察/点击），4 轮上限很容易在最后一步被截断。

2. **重试时 Executor 不保留已完成的输入状态**：Verifier 返回 retry 后，Executor 重新执行整个步骤，但不知道输入框已经填好了值，会重复 `browser_type` 浪费轮数。

### 解决方案

#### 1. `config/multi_agent_config.json` - 增加 `max_executor_steps`

将 `max_executor_steps` 从 4 增加到 6，为复合步骤留出更多工具调用空间：

```json
"max_executor_steps": 6
```

#### 2. `config/multi_agent_config.json` - Executor prompt 增加"不重复输入"指引

在 Executor 的 `instructions` 中新增一条指令：

```
"重要：在重试时，请先通过 browser_observe 观察当前页面状态。如果输入框已经填入了正确的值（观察结果中 text 字段显示已填内容），不要重复 browser_type 输入相同的值，直接进行下一步操作（如点击按钮）。"
```

**作用**：引导 LLM 在重试时先观察输入框状态，避免重复输入已填写的值，节省工具调用轮数。

### 修复效果

修复后：
- `max_executor_steps=6` 足够完成"观察 + 输入用户名 + 输入密码 + 点击登录"的完整流程
- 即使 Verifier 返回 retry，Executor 也会先观察输入框状态，跳过已完成的输入操作，直接点击登录按钮

---

## 问题四：点击后弹出广告导致 browser_click 卡死

### 问题现象

Executor 在执行登录操作时，`browser_click` 点击登录按钮后，页面突然弹出广告/弹窗，代码卡死在 `browser_click` 的等待逻辑中：

```python
def browser_click(self, selector: str) -> Dict[str, Any]:
    # 点击页面元素，支持 text= 或 CSS 选择器
    try:
        self.start()
        raw = selector.strip()

        # helper: 点击 locator 并等待导航/网络空闲
        def click_locator_and_wait(loc):
            try:
                loc.scroll_into_view_if_needed()
            except Exception:
                pass
            loc.click(timeout=8000)   # ← 卡在这里
```

核心痛点：**点击按钮后广告/弹窗弹出，遮挡了页面或持续加载资源，导致 `loc.click()` 的 8 秒超时等待无法正常结束，整个 Agent 任务被卡死**。

### 根因分析

1. **点击可能被广告遮挡或触发弹窗**：`loc.click()` 要求元素可点击（可见且未被遮挡），广告弹出后遮挡了目标元素，导致 Playwright 的 actionability 检查一直等待直到超时。

2. **`wait_for_load_state("networkidle")` 可能永远等不到**：广告弹窗会持续加载外部资源（视频、轮播、追踪脚本等），页面永远不会达到 `networkidle` 状态，8 秒后抛出超时异常。

3. **缺少弹窗清理机制**：代码没有在点击后尝试关闭可能弹出的广告/弹窗，导致页面被弹窗占据，后续操作（观察、点击）都会受影响。

### 解决方案

#### 1. `web_tools.py` - 改进 `click_locator_and_wait` 健壮性

**核心思路**：点击不等待 `networkidle`，改用更宽松的 `domcontentloaded`；点击失败时用 JS 强制点击兜底；点击后主动尝试关闭弹窗。

```python
def click_locator_and_wait(loc):
    try:
        loc.scroll_into_view_if_needed()
    except Exception:
        pass
    # 点击可能被广告遮挡或触发弹窗，使用 try/except 避免卡死
    try:
        loc.click(timeout=5000)
    except Exception:
        # 点击失败时尝试强制 JS 点击
        try:
            loc.evaluate("el => el.click()")
        except Exception:
            pass
    # 等待页面加载，但容忍广告等持续加载导致的超时
    try:
        self.page.wait_for_load_state("domcontentloaded", timeout=5000)
    except Exception:
        pass
    # 尝试关闭可能弹出的广告/弹窗（常见广告关闭按钮）
    try:
        self._try_close_popups()
    except Exception:
        pass
    # 短暂等待页面稳定，但不阻塞过久
    try:
        self.page.wait_for_load_state("networkidle", timeout=3000)
    except Exception:
        pass
```

**关键改动**：
- `loc.click(timeout=8000)` → `loc.click(timeout=5000)`：缩短点击超时
- 点击失败时用 `loc.evaluate("el => el.click()")` 强制 JS 点击兜底
- `wait_for_load_state("networkidle", timeout=8000)` → `wait_for_load_state("domcontentloaded", timeout=5000)`：改用 DOM 加载完成而非网络空闲
- 点击后调用 `_try_close_popups()` 主动关闭弹窗
- `networkidle` 等待缩短到 3 秒且完全宽容

#### 2. `web_tools.py` - 新增 `_try_close_popups()` 方法

新增广告/弹窗清理方法，通过常见关闭按钮选择器尝试关闭弹窗：

```python
def _try_close_popups(self) -> None:
    """尝试关闭页面上的广告/弹窗。

    通过 JS 查找常见的弹窗关闭按钮（如 .close、.modal-close、[aria-label=Close] 等）
    并触发点击。此方法为尽力而为，任何异常都会被吞掉。
    """
    if not self.page:
        return
    try:
        # 常见弹窗关闭按钮选择器
        close_selectors = [
            ".modal .close",
            ".modal-close",
            ".popup-close",
            ".ad-close",
            "[aria-label='Close']",
            "[aria-label='close']",
            ".close",
            "button.close",
            ".btn-close",
            "[data-dismiss='modal']",
        ]
        for sel in close_selectors:
            try:
                loc = self.page.locator(sel)
                count = loc.count()
                for i in range(min(count, 3)):
                    try:
                        l = loc.nth(i)
                        if l.is_visible():
                            l.click(timeout=1000)
                    except Exception:
                        continue
            except Exception:
                continue
    except Exception:
        pass
```

**作用**：在点击后、观察前主动清理广告/弹窗，避免弹窗遮挡页面内容影响后续操作。

#### 3. `web_tools.py` - `browser_observe` 也主动关闭弹窗

在 `browser_observe()` 开头调用 `_try_close_popups()`：

```python
def browser_observe(self) -> Dict[str, Any]:
    try:
        self.start()
        # 先尝试关闭可能遮挡页面的广告/弹窗
        try:
            self._try_close_popups()
        except Exception:
            pass
        url = self.page.url or "未打开页面"
        ...
```

**作用**：观察页面之前先清理弹窗，确保观察结果包含页面真实内容而不是广告遮挡。

### 修复效果

修复后：
- 点击被广告遮挡时，先尝试普通点击，失败后用 JS 强制点击兜底，不会再卡死
- 点击后主动关闭广告/弹窗，页面恢复可操作状态
- `browser_observe` 也能在观察到页面真实内容前清理弹窗
- 所有等待都使用宽松超时 + 异常吞掉，不会因广告持续加载资源而阻塞

---

## 涉及文件汇总

| 文件 | 修改内容 |
|------|---------|
| `web_tools.py` | ① `browser_open` 改用 `domcontentloaded`，超时后检查页面状态；② `click_locator_and_wait` 增加 JS 点击兜底与宽松等待；③ 新增 `_try_close_popups()` 广告弹窗清理方法；④ `browser_observe` 观察前主动关闭弹窗 |
| `config/multi_agent_config.json` | ① Executor prompt 新增超时/错误处理指引；② `max_executor_steps` 从 4 增加到 6；③ 新增"不重复输入已填值"指引 |
| `executor_agent.py` | ① 新增 `browser_open` 超时安全网（自动观察页面）；② 新增 `browser_context` 字段并在 prompt 中展示 |
| `multi_agent.py` | 新增 `_get_browser_context()` 方法，在每步执行前向 Executor 注入浏览器当前状态 |
