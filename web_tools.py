"""WebAgent 的浏览器工具封装模块（基于 Playwright）。

此模块提供 `WebBrowser` 类封装常用浏览器操作工具函数，返回统一的 `ToolResult` 字典结构，
以便上层 Agent 调用并将结果序列化写入 trace。模块内方法均对异常做友好降级并返回错误码与建议。
"""

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

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
    """Playwright 浏览器封装。

    提供 start/close 以及常用操作：open/observe/click/type/select/extract/screenshot。
    所有操作均返回 `ToolResult.to_dict()` 便于与上层 Agent 集成。
    """

    def __init__(self, trace_dir: str = "web_traces"):
        # trace_dir: 存放截图等运行产物的目录
        self.trace_dir = Path(trace_dir)
        self.trace_dir.mkdir(parents=True, exist_ok=True)
        self._playwright = None
        self.browser = None
        self.context = None
        self.page = None

    def start(self):
        # 启动 Playwright 浏览器（懒加载），多次调用会重用同一 browser context
        if sync_playwright is None:
            raise RuntimeError("缺少 playwright 依赖，请安装 playwright 并运行 `python -m playwright install chromium`。")

        # 如果已有 page 且未关闭，则复用
        try:
            if self.page is not None and not getattr(self.page, "is_closed", lambda: False)():
                return
        except Exception:
            # 若检查状态失败则继续创建新 page
            pass

        # 启动 playwright 并创建 browser/context/page
        self._playwright = sync_playwright().start()
        headless_env = os.getenv("WEBAGENT_HEADLESS")
        if headless_env is not None:
            headless = headless_env.lower() not in ("0", "false", "no")
        else:
            headless = os.getenv("PWDEBUG") is None
        self.browser = self._playwright.chromium.launch(headless=headless)
        self.context = self.browser.new_context()
        self.page = self.context.new_page()

    def close(self):
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

            # 收集常见交互元素（a, button, input, textarea, select），采集更多字段便于构造精确选择器
            elements = self.page.eval_on_selector_all(
                "a,button,input,textarea,select",
                "els => els.slice(0, 80).map(el => ({ role: el.tagName.toLowerCase(), id: el.id||'', name: el.getAttribute('name')||'', type: el.getAttribute('type')||'', aria_label: el.getAttribute('aria-label')||'', classes: el.className||'', text: (el.innerText||el.value||'').trim(), outer: (el.outerHTML||'').slice(0,300) }))"
            )
            # 后处理：保留最多 20 个元素，文本截断到 120 字，移除过多空白
            processed = []
            for el in (elements or [])[:20]:
                text = (el.get('text') or '')
                text = re.sub(r"\s+", " ", text).strip()
                if len(text) > 120:
                    text = text[:117] + '...'
                selector_sample = el.get('role')
                if el.get('id'):
                    selector_sample = f"#{el.get('id')}"
                processed.append({
                    'role': el.get('role'),
                    'id': el.get('id'),
                    'name': el.get('name'),
                    'type': el.get('type'),
                    'aria_label': el.get('aria_label'),
                    'classes': el.get('classes'),
                    'text': text,
                    'selector_sample': selector_sample,
                    'outer_html_snippet': el.get('outer')
                })
            elements = processed
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
