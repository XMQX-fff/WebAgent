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
        if self.page is not None:
            return
        if sync_playwright is None:
            raise RuntimeError("缺少 playwright 依赖，请安装 playwright 并运行 `python -m playwright install chromium`。")
        self._playwright = sync_playwright().start()
        self.browser = self._playwright.chromium.launch(headless=True)
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
            # 获取 body 文本并截断，用于给模型快速阅读
            body_text = self.page.locator("body").inner_text() or ""
            visible_text_summary = body_text[:2000].replace("\n", " ").strip()
            # 收集常见交互元素（a, button, input, textarea, select），限制最多 40 个
            elements = self.page.eval_on_selector_all(
                "a,button,input,textarea,select",
                "els => els.slice(0, 40).map(el => ({ role: el.tagName.toLowerCase(), text: el.innerText || el.value || el.getAttribute('aria-label') || el.getAttribute('name') || '', selector: el.tagName.toLowerCase() }))"
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
            selector = self._resolve_selector(selector)
            self.page.click(selector, timeout=10000)
            self.page.wait_for_load_state("networkidle", timeout=8000)
            return ToolResult(status="ok", data=f"已点击 {selector}，当前 URL {self.page.url}", meta={"url": self.page.url}).to_dict()
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
