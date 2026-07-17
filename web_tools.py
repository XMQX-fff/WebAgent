"""WebAgent 的浏览器工具封装模块（基于 Playwright）。

此模块提供 `WebBrowser` 类封装常用浏览器操作工具函数，返回统一的 `ToolResult` 字典结构，
以便上层 Agent 调用并将结果序列化写入 trace。模块内方法均对异常做友好降级并返回错误码与建议。

支持浏览器状态持久化：通过 `state_file` 参数在浏览器启动时加载已保存的 cookies/localStorage，
并在关闭时自动保存，方便跨任务复用登录态。

此外还提供 `decide_state_from_task` 工具函数，通过 LLM 判断任务是否需要复用已保存的浏览器状态。
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

try:
    from playwright.sync_api import TimeoutError as PlaywrightTimeoutError, sync_playwright
except ImportError:
    sync_playwright = None
    PlaywrightTimeoutError = Exception


class ToolResult:
    """统一的工具返回结构封装。

    - status: "ok" or "error"
    - data: 成功时的返回文本或JSON字符串
    - meta: 额外元信息（例如 URL、title、screenshot 路径）
    - error_code/error_msg/suggestion: 失败时的诊断信息
    """

    def __init__(self, status: str, data: Any = None, meta: Optional[Dict[str, Any]] = None, error_code: Optional[str] = None, error_msg: Optional[str] = None, suggestion: Optional[str] = None):
        self.status = status
        self.data = data
        self.meta = meta or {}
        self.error_code = error_code
        self.error_msg = error_msg
        self.suggestion = suggestion

    def to_dict(self) -> Dict[str, Any]:
        # 将结果序列化为 dict，以便 Agent 统一消费
        if self.status == "ok":
            return {"status": "ok", "data": self.data, "meta": self.meta}
        return {"status": "error", "error_code": self.error_code, "error_msg": self.error_msg, "suggestion": self.suggestion}


class WebBrowser:
    """Playwright 浏览器封装，支持状态持久化。

    提供 start/close 以及常用操作：open/observe/click/type/select/extract/screenshot/clear_state。
    所有操作均返回 `ToolResult.to_dict()` 便于与上层 Agent 集成。

    状态持久化：
    - 通过 `state_file` 指定状态文件路径
    - `start(load_state=True)` 时自动加载已保存的 cookies/localStorage
    - `close()` 时自动保存当前状态
    - `browser_clear_state()` 清除当前状态并删除状态文件
    """

    def __init__(self, trace_dir: str = "web_traces", state_file: Optional[str] = None):
        # trace_dir: 存放截图等运行产物的目录
        # state_file: 浏览器状态持久化文件路径（cookies/localStorage/sessionStorage）
        self.trace_dir = Path(trace_dir)
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        self.state_file = state_file
        self._playwright = None
        self.browser = None
        self.context = None
        self.page = None

    def start(self, load_state: bool = True):
        """启动 Playwright 浏览器（懒加载），多次调用会重用同一 browser context。

        Args:
            load_state: 是否从 state_file 加载已保存的浏览器状态。
                        仅当 state_file 已设置且文件存在时有效。
        """
        if sync_playwright is None:
            raise RuntimeError("缺少 playwright 依赖，请安装 playwright 并运行 `python -m playwright install chromium`。")

        # 如果已有 page 且未关闭，则复用
        try:
            if self.page is not None and not getattr(self.page, "is_closed", lambda: False)():
                return
        except Exception:
            pass

        # 启动 playwright 并创建 browser/context/page
        self._playwright = sync_playwright().start()
        headless_env = os.getenv("WEBAGENT_HEADLESS")
        if headless_env is not None:
            headless = headless_env.lower() not in ("0", "false", "no")
        else:
            headless = os.getenv("PWDEBUG") is None
        self.browser = self._playwright.chromium.launch(headless=headless)

        # 根据 load_state 和 state_file 决定是否加载已保存的状态
        storage_state_path = None
        if load_state and self.state_file and Path(self.state_file).exists():
            storage_state_path = self.state_file

        self.context = self.browser.new_context(storage_state=storage_state_path)
        self.page = self.context.new_page()

    def save_state(self, path: Optional[str] = None):
        """保存当前浏览器状态（cookies、localStorage、sessionStorage）到文件。

        如果未指定 path，则使用初始化时传入的 state_file。
        如果 state_file 也未设置，则不执行任何操作。
        """
        if not self.context:
            return
        target = path or self.state_file
        if not target:
            return
        try:
            self.context.storage_state(path=target)
        except Exception:
            pass

    def close(self):
        """关闭浏览器并自动保存状态。"""
        # 在销毁 context 前保存状态
        self.save_state()
        if self.page:
            try:
                self.page.close()
            except Exception:
                pass
            self.page = None
        if self.context:
            try:
                self.context.close()
            except Exception:
                pass
            self.context = None
        if self.browser:
            try:
                self.browser.close()
            except Exception:
                pass
            self.browser = None
        if self._playwright:
            try:
                self._playwright.stop()
            except Exception:
                pass
            self._playwright = None

    def _make_screenshot_path(self, prefix: str = "screenshot") -> str:
        name = f"{prefix}-{int(time.time() * 1000)}.png"
        path = self.trace_dir / name
        return str(path)

    def _normalize_url(self, url: str) -> str:
        # 规范化 URL，缺失 scheme 时补 https://
        url = url.strip()
        if not url:
            raise ValueError("url 不能为空")
        if not re.match(r"^https?://", url, re.IGNORECASE):
            url = "https://" + url
        return url

    def _resolve_selector(self, selector: str) -> str:
        # 简单校验 selector，不做额外转换，保留 caller 提供的选择器格式
        selector = selector.strip()
        if not selector:
            raise ValueError("selector 不能为空")
        if selector.startswith(("css=", "xpath=", "text=", "id=", "role=", "aria-label=", "aria/")):
            return selector
        return selector

    def browser_open(self, url: str) -> Dict[str, Any]:
        # 打开指定 URL 并等待页面空闲，返回 title 与最终 url
        try:
            self.start()
            url = self._normalize_url(url)
            self.page.goto(url, timeout=15000)
            self.page.wait_for_load_state("networkidle", timeout=10000)
            title = self.page.title()
            current_url = self.page.url
            return ToolResult(status="ok", data=f"页面已打开: {title} | {current_url}", meta={"title": title, "url": current_url}).to_dict()
        except PlaywrightTimeoutError as exc:
            return ToolResult(status="error", error_code="TIMEOUT", error_msg=str(exc), suggestion="页面加载超时，请检查 URL 或重试。").to_dict()
        except Exception as exc:
            return ToolResult(status="error", error_code="OPEN_ERROR", error_msg=str(exc), suggestion="检查 URL 格式或浏览器环境。" ).to_dict()

    def browser_observe(self) -> Dict[str, Any]:
        # 观察页面并返回简要摘要：URL、title、可见文本片段和交互元素列表
        try:
            self.start()
            url = self.page.url or "未打开页面"
            title = self.page.title() or "无标题"
            # 获取 body 文本，用于自动摘要（严格限制长度以节省 token）
            body_text = self.page.locator("body").inner_text() or ""
            # 简单截断并做非常基础的句级摘要：优先保留前 600 字，尝试按句子分割以取前两句
            raw = body_text.replace("\n", " ").strip()
            if not raw:
                visible_text_summary = ""
            else:
                max_chars = 600
                if len(raw) <= max_chars:
                    visible_text_summary = raw
                else:
                    # 尝试按中文句号或英文句号分割
                    import re as _re
                    sents = _re.split(r'(?<=[。\.\!\?])\s*', raw[: max_chars * 2])
                    # 取前两句拼接，若不足再截断到 max_chars
                    selected = "".join(s for s in sents if s)[:max_chars]
                    visible_text_summary = selected

            # 收集常见交互元素（a, button, input, textarea, select），JS 侧完成文本清洗/截断/选择器生成
            elements = self.page.eval_on_selector_all(
                "a,button,input,textarea,select",
                "els => els.slice(0, 20).map(el => ({"
                "  role: el.tagName.toLowerCase(),"
                "  id: el.id || '',"
                "  name: el.getAttribute('name') || '',"
                "  type: el.getAttribute('type') || '',"
                "  aria_label: el.getAttribute('aria-label') || '',"
                "  classes: el.className || '',"
                "  text: ((el.innerText || el.value || '').trim().replace(/\\s+/g, ' ')).slice(0, 120),"
                "  selector_sample: el.id ? '#' + el.id : el.tagName.toLowerCase(),"
                "  outer: (el.outerHTML || '').slice(0, 300)"
                "}))"
            )
            observation = {
                "url": url,
                "title": title,
                "visible_text_summary": visible_text_summary,
                "interactive_elements": elements,
            }
            return ToolResult(status="ok", data=json.dumps(observation, ensure_ascii=False), meta={"url": url, "title": title}).to_dict()
        except Exception as exc:
            return ToolResult(status="error", error_code="OBSERVE_ERROR", error_msg=str(exc), suggestion="观察页面失败，请先打开页面或检查选择器。" ).to_dict()

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
                loc.click(timeout=8000)
                try:
                    self.page.wait_for_load_state("networkidle", timeout=8000)
                except Exception:
                    pass

            # 1) 如果是 id 选择器或含 '#'，优先按 id 定位
            try_candidates = []
            if raw.startswith('#') or re.match(r'^[A-Za-z0-9_\-]+$', raw):
                # treat as id or plain token -> try id
                if raw.startswith('#'):
                    try_candidates.append(raw)
                else:
                    try_candidates.append(f"#{raw}")

            # 2) 如果是 text=xxx, 提取文本
            text_match = None
            if raw.startswith('text='):
                text_match = raw.split('=', 1)[1].strip()

            # 3) 优先尝试按钮 role+name
            if text_match:
                try_candidates.append(('role_name', text_match))
            else:
                # 当传入为简单 'Login' 文本，也尝试 role+name
                if not any(ch in raw for ch in ' #.>') and len(raw) < 60:
                    try_candidates.append(('role_name', raw))

            # 4) 尝试 button[type=submit] 或 button 有文本匹配
            try_candidates.append('button[type="submit"]')
            try_candidates.append('button')
            try_candidates.append(raw)

            last_err = None
            debug_matches = []
            for cand in try_candidates:
                try:
                    if isinstance(cand, tuple) and cand[0] == 'role_name':
                        name = cand[1]
                        loc = self.page.get_by_role('button', name=name)
                        if loc.count() > 0:
                            # pick first visible
                            for i in range(loc.count()):
                                l = loc.nth(i)
                                if l.is_visible() and l.is_enabled():
                                    click_locator_and_wait(l)
                                    return ToolResult(status="ok", data=f"已点击 role button '{name}'，当前 URL {self.page.url}", meta={"url": self.page.url}).to_dict()
                            debug_matches.append({'candidate': f"role button name={name}", 'count': loc.count()})
                        else:
                            debug_matches.append({'candidate': f"role button name={name}", 'count': 0})
                        continue

                    # cand is selector string
                    sel = cand
                    sel = self._resolve_selector(sel)
                    loc = self.page.locator(sel)
                    count = loc.count()
                    debug_info = {'candidate': sel, 'count': count}
                    if count == 0:
                        debug_matches.append(debug_info)
                        continue
                    # 找到可见且可用的元素
                    clicked = False
                    for i in range(count):
                        l = loc.nth(i)
                        try:
                            if l.is_visible() and l.is_enabled():
                                click_locator_and_wait(l)
                                return ToolResult(status="ok", data=f"已点击 {sel}（第{i}个匹配），当前 URL {self.page.url}", meta={"url": self.page.url}).to_dict()
                        except Exception:
                            continue
                    # 记录匹配但未点击的信息（例如第一个元素不可见）
                    # 尝试记录第一个元素的 outerHTML 片段
                    try:
                        outer = loc.nth(0).evaluate("el => el.outerHTML.slice(0,300)")
                    except Exception:
                        outer = ''
                    debug_info['outer_html_snippet'] = outer
                    debug_matches.append(debug_info)
                except Exception as exc:
                    last_err = exc
                    continue

            # 最后回退：尝试用 JS 在页面上查找按钮文本并触发 click
            try:
                if text_match:
                    script = "(text) => { const els = Array.from(document.querySelectorAll('button,input[type=submit]')); for (const e of els) { if ((e.innerText||e.value||'').trim().includes(text)) { e.click(); return true; } } return false; }"
                    ok = self.page.evaluate(script, text_match)
                    if ok:
                        try:
                            self.page.wait_for_load_state("networkidle", timeout=8000)
                        except Exception:
                            pass
                        return ToolResult(status="ok", data=f"已通过 JS 点击包含文本 '{text_match}' 的按钮，当前 URL {self.page.url}", meta={"url": self.page.url}).to_dict()
            except Exception:
                pass

            # 如果都失败，返回带 debug_matches 的错误信息，便于 trace 分析
            return ToolResult(status="error", error_code="TIMEOUT", error_msg=str(last_err) or "未找到可点击元素", suggestion="尝试更精确选择器，例如 #submit-login 或使用 role+name 定位。", meta={"candidates": debug_matches}).to_dict()
        except PlaywrightTimeoutError as exc:
            return ToolResult(status="error", error_code="TIMEOUT", error_msg=str(exc), suggestion="点击操作超时，可能没有找到元素。" ).to_dict()
        except Exception as exc:
            return ToolResult(status="error", error_code="CLICK_ERROR", error_msg=str(exc), suggestion="请确认 selector 是否正确，或者使用 text=... 形式。" ).to_dict()

    def browser_type(self, selector: str, text: str) -> Dict[str, Any]:
        # 在输入框中填写文本（不会提交表单）
        try:
            self.start()
            selector = self._resolve_selector(selector)
            self.page.fill(selector, text, timeout=10000)
            return ToolResult(status="ok", data=f"已在 {selector} 输入文本: {text[:100]}", meta={"selector": selector}).to_dict()
        except PlaywrightTimeoutError as exc:
            return ToolResult(status="error", error_code="TIMEOUT", error_msg=str(exc), suggestion="输入操作超时，可能未找到输入框。" ).to_dict()
        except Exception as exc:
            return ToolResult(status="error", error_code="TYPE_ERROR", error_msg=str(exc), suggestion="请确认 selector 和 text 格式是否正确。" ).to_dict()

    def browser_select(self, selector: str, value: str) -> Dict[str, Any]:
        # 选择下拉框的值（根据 option 的 value 字段）
        try:
            self.start()
            selector = self._resolve_selector(selector)
            self.page.select_option(selector, value)
            return ToolResult(status="ok", data=f"已选择 {selector} 的值 {value}", meta={"selector": selector, "value": value}).to_dict()
        except Exception as exc:
            return ToolResult(status="error", error_code="SELECT_ERROR", error_msg=str(exc), suggestion="请确认 selector 和 option 值是否匹配。" ).to_dict()

    def browser_extract(self, instruction: str) -> Dict[str, Any]:
        # 基于指令提取页面内容，支持在 instruction 中写入 selector: <CSS selector>
        try:
            self.start()
            selector = None
            match = re.search(r"selector\s*[:=]\s*['\"]?([^'\"]+)['\"]?", instruction, re.IGNORECASE)
            if match:
                selector = match.group(1).strip()
            if selector:
                selector = self._resolve_selector(selector)
                text = self.page.locator(selector).inner_text() or ""
            else:
                # 未指定 selector 时默认读取 body 文本
                text = self.page.locator("body").inner_text() or ""
            summary = text[:3000].strip()
            return ToolResult(status="ok", data=f"提取内容：{summary}", meta={"selector": selector or "body"}).to_dict()
        except Exception as exc:
            return ToolResult(status="error", error_code="EXTRACT_ERROR", error_msg=str(exc), suggestion="提取失败，请使用 selector: CSS 语法或更具体的说明。" ).to_dict()

    def browser_screenshot(self) -> Dict[str, Any]:
        # 保存整页截图并返回路径
        try:
            self.start()
            path = self._make_screenshot_path("browser-screenshot")
            self.page.screenshot(path=path, full_page=True)
            return ToolResult(status="ok", data=f"已保存截图：{path}", meta={"screenshot": path, "url": self.page.url}).to_dict()
        except Exception as exc:
            return ToolResult(status="error", error_code="SCREENSHOT_ERROR", error_msg=str(exc), suggestion="截图失败，请确认页面是否已加载。" ).to_dict()

    def browser_clear_state(self) -> Dict[str, Any]:
        """清除当前浏览器状态（cookies、localStorage、sessionStorage）并删除已保存的状态文件。"""
        try:
            self.start()
            # 清除 cookies
            if self.context:
                self.context.clear_cookies()
            # 清除 localStorage 和 sessionStorage
            try:
                self.page.evaluate("localStorage.clear(); sessionStorage.clear();")
            except Exception:
                pass
            # 删除状态文件
            if self.state_file and Path(self.state_file).exists():
                Path(self.state_file).unlink()
            return ToolResult(status="ok", data="已清除浏览器状态（cookies、localStorage、sessionStorage）及状态文件。").to_dict()
        except Exception as exc:
            return ToolResult(status="error", error_code="CLEAR_STATE_ERROR", error_msg=str(exc), suggestion="清除状态失败，请重试。" ).to_dict()


def decide_state_from_task(task: str, llm_call: Callable[[str, str, int], str], state_file: Optional[str] = None) -> bool:
    """通过 LLM 判断给定任务是否需要复用已保存的浏览器状态。

    如果状态文件不存在，直接返回 False（无需加载）。
    否则调用 LLM 做轻量判断（仅消耗 ~20 token）。

    Args:
        task: 用户任务描述。
        llm_call: LLM 调用函数，签名 (system_prompt, user_prompt, max_tokens) -> str。
        state_file: 状态文件路径。为 None 或文件不存在时返回 False。

    Returns:
        True 表示需要加载已保存的状态，False 表示需要全新环境。
    """
    if not state_file or not Path(state_file).exists():
        return False

    decision_prompt = (
        "你是一个浏览器 Agent 的状态决策器。根据用户的任务描述，判断是否需要复用已保存的浏览器状态"
        "（cookies、localStorage、sessionStorage）。\n\n"
        "需要复用状态的场景（返回 true）：\n"
        "- 查看邮件、搜索信息、浏览网页等常规操作\n"
        "- 需要保持登录态才能完成的任务\n"
        "- 连续操作类任务（如购物、填写表单）\n\n"
        "不需要复用状态的场景（返回 false）：\n"
        "- 测试登录、注册、退出登录功能\n"
        "- 测试密码重置、验证码等认证流程\n"
        "- 需要以全新身份访问页面的场景\n"
        "- 测试清除 cookies 或隐私相关功能\n\n"
        f"用户任务：{task}\n\n"
        "请只返回一个 JSON 对象，格式为：{\"use_saved_state\": true} 或 {\"use_saved_state\": false}"
    )

    try:
        response = llm_call(
            system_prompt="你是一个状态决策器，只返回 JSON。",
            user_prompt=decision_prompt,
            max_tokens=50,
        )
        parsed = json.loads(response.strip())
        return bool(parsed.get("use_saved_state", False))
    except Exception:
        # 解析失败时保守处理：不加载状态
        return False