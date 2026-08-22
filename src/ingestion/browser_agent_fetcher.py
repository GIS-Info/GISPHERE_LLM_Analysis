"""
browser-use 智能浏览器代理抓取模块（回退链最后一级兜底）

用 LLM 驱动真实浏览器完成多步交互（等待/通过人机验证、关闭 cookie 弹窗、
展开折叠内容）后提取页面正文。只应在 HTTP / Playwright / 截图OCR 全部
失败后调用，单 URL 成本约 1.6万–6.3万 token、1 分钟左右。

LLM 走项目统一的 New API 网关；浏览器优先复用 Playwright 已安装的
Chromium，找不到时由 browser-use 自行下载。
"""
import asyncio
import glob
import logging
import os
import sys
from typing import Optional

from ..core.config import (
    API_BASE_URL,
    BROWSER_AGENT_DIRECTLY_OPEN_URL,
    BROWSER_AGENT_EXTRACTION_MODEL,
    BROWSER_AGENT_FLASH_MODE,
    BROWSER_AGENT_MAX_HISTORY_ITEMS,
    BROWSER_AGENT_MAX_STEPS,
    BROWSER_AGENT_MODEL,
    BROWSER_AGENT_TIMEOUT,
    BROWSER_AGENT_USE_VISION,
    PLAYWRIGHT_HEADLESS,
    check_api_key,
)

logger = logging.getLogger(__name__)

TASK_TEMPLATE = (
    "Open {url} and extract the FULL text of the academic opportunity posting on that page. "
    "If a cookie banner or consent dialog blocks the page, dismiss it first. "
    "If the page shows a human verification / CAPTCHA screen, wait for it to pass automatically. "
    "If the page lists multiple opportunities, extract all of them. "
    "Do NOT navigate to other websites. "
    "Return the extracted text verbatim (title, description, deadline, contact info, requirements) "
    "as your final answer."
)


def _find_browser_executable() -> Optional[str]:
    """
    定位浏览器可执行文件，优先级：正式版 Google Chrome > Playwright Chromium。

    优先真 Chrome 是因为反爬系统（Cloudflare 等）对 Chromium / Chrome for
    Testing 的指纹识别度远高于正式版 Chrome，用真 Chrome 过验证页成功率更高。
    找不到返回 None（browser-use 会自行下载浏览器）。
    """
    if sys.platform == "win32":
        chrome_candidates = [
            os.path.join(os.environ.get("ProgramFiles", r"C:\Program Files"),
                         "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                         "Google", "Chrome", "Application", "chrome.exe"),
            os.path.join(os.environ.get("LOCALAPPDATA", ""),
                         "Google", "Chrome", "Application", "chrome.exe"),
        ]
        pw_base = os.path.join(os.environ.get("LOCALAPPDATA", ""), "ms-playwright")
        pw_patterns = [os.path.join(pw_base, "chromium-*", "chrome-win*", "chrome.exe")]
    elif sys.platform == "darwin":
        chrome_candidates = ["/Applications/Google Chrome.app/Contents/MacOS/Google Chrome"]
        pw_base = os.path.expanduser("~/Library/Caches/ms-playwright")
        pw_patterns = [os.path.join(pw_base, "chromium-*", "chrome-mac*", "Chromium.app",
                                    "Contents", "MacOS", "Chromium")]
    else:
        chrome_candidates = ["/usr/bin/google-chrome", "/usr/bin/google-chrome-stable"]
        pw_base = os.path.expanduser("~/.cache/ms-playwright")
        pw_patterns = [os.path.join(pw_base, "chromium-*", "chrome-linux*", "chrome")]

    for path in chrome_candidates:
        if path and os.path.isfile(path):
            return path

    candidates = []
    for pattern in pw_patterns:
        candidates.extend(glob.glob(pattern))
    if not candidates:
        return None
    # 版本目录名末尾是构建号，取最新
    return sorted(candidates)[-1]


def is_browser_agent_available() -> bool:
    """browser-use 是否可用（已安装且有 API Key）。"""
    if not check_api_key():
        return False
    try:
        import browser_use  # noqa: F401
        return True
    except ImportError:
        return False


async def _run_agent(url: str, api_key: str) -> Optional[str]:
    from browser_use import Agent, ChatOpenAI
    from browser_use.browser import BrowserSession
    from browser_use.browser.profile import BrowserProfile

    llm = ChatOpenAI(
        model=BROWSER_AGENT_MODEL,
        api_key=api_key,
        base_url=API_BASE_URL,
        temperature=0.0,
        # 网关不保证支持 OpenAI json_schema 结构化输出，改为注入 system prompt
        dont_force_structured_output=True,
        add_schema_to_system_prompt=True,
        timeout=120,
    )
    # 页面正文抽取（extract 动作）用最便宜的轻量模型：这一步输入是整页大文本，
    # 但只做“照抄/摘录”，不需要旗舰模型；主 agent 仍用上面的模型做导航决策。
    extraction_llm = ChatOpenAI(
        model=BROWSER_AGENT_EXTRACTION_MODEL,
        api_key=api_key,
        base_url=API_BASE_URL,
        temperature=0.0,
        dont_force_structured_output=True,
        add_schema_to_system_prompt=True,
        timeout=120,
    )
    browser_exe = _find_browser_executable()
    logger.info(f"browser-use 使用浏览器: {browser_exe or '(由 browser-use 自行下载)'}")
    profile = BrowserProfile(
        executable_path=browser_exe,
        headless=PLAYWRIGHT_HEADLESS,
        user_data_dir=None,  # 每次用临时 profile，避免状态串扰
    )
    session = BrowserSession(browser_profile=profile)
    agent = Agent(
        task=TASK_TEMPLATE.format(url=url),
        llm=llm,
        browser_session=session,
        use_vision=BROWSER_AGENT_USE_VISION,
        # ↓ P0 调优：压 token / 减步数（详见 config.py 注释）
        page_extraction_llm=extraction_llm,
        flash_mode=BROWSER_AGENT_FLASH_MODE,
        use_thinking=False,
        max_history_items=BROWSER_AGENT_MAX_HISTORY_ITEMS,
        directly_open_url=BROWSER_AGENT_DIRECTLY_OPEN_URL,
    )
    try:
        history = await agent.run(max_steps=BROWSER_AGENT_MAX_STEPS)
        if history.is_successful() is False:
            logger.warning("browser-use 代理报告任务未成功")
            return None
        final = history.final_result()
        return final.strip() if final and final.strip() else None
    finally:
        try:
            await session.kill()
        except Exception as e:
            logger.debug(f"关闭 browser-use 会话失败（忽略）: {e}")


def fetch_with_browser_agent(url: str) -> Optional[str]:
    """
    用 browser-use 代理抓取页面正文（同步入口）。

    失败（未安装、无 Key、超时、代理未完成任务）一律返回 None，由上层记录。
    """
    try:
        import browser_use  # noqa: F401
    except ImportError:
        logger.warning("browser-use 未安装，跳过智能代理兜底（pip install browser-use，需 Python ≥ 3.11）")
        return None

    api_key = check_api_key()
    if not api_key:
        logger.warning("缺少 API Key，跳过 browser-use 智能代理兜底")
        return None

    if sys.platform == "win32" and not sys.flags.utf8_mode:
        # browser-use 内部按 locale 默认编码写文件，GBK 下遇 \xa0 等字符会报错
        # （代理通常能自行换路绕过，但建议设 PYTHONUTF8=1 彻底避免）
        logger.info("提示: 建议设置环境变量 PYTHONUTF8=1，避免 browser-use 在 GBK 环境下的编码错误")

    os.environ.setdefault("ANONYMIZED_TELEMETRY", "false")
    logger.info(f"启动 browser-use 智能代理兜底: {url} (模型={BROWSER_AGENT_MODEL}, "
                f"max_steps={BROWSER_AGENT_MAX_STEPS})")
    try:
        content = asyncio.run(
            asyncio.wait_for(_run_agent(url, api_key), timeout=BROWSER_AGENT_TIMEOUT)
        )
    except asyncio.TimeoutError:
        logger.warning(f"browser-use 代理超时（{BROWSER_AGENT_TIMEOUT}秒）: {url}")
        return None
    except Exception as e:
        logger.warning(f"browser-use 代理执行失败: {e}")
        return None

    if content:
        logger.info(f"✅ browser-use 代理成功提取内容，长度: {len(content)} 字符")
    else:
        logger.warning("browser-use 代理未提取到有效内容")
    return content
