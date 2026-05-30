#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Playwright独立进程工作器 - 完全避免异步冲突
通过独立进程运行Playwright，与主进程完全隔离
"""
import sys
import json
import logging
from typing import Optional
import io

# 强制 stdout 使用 UTF-8 编码，避免 Windows 上的 GBK 编码问题
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

# 配置日志 - 降低日志级别，避免在 Windows 上出现编码问题
# 只在严重错误时输出到 stderr，普通日志不输出（避免干扰 stdout 的 JSON 输出）
logging.basicConfig(level=logging.ERROR, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


def html_to_text(html: str) -> str:
    """将 HTML 清洗为纯文本。"""
    from bs4 import BeautifulSoup

    soup = BeautifulSoup(html, 'html.parser')
    for script in soup(["script", "style", "nav", "footer", "header"]):
        script.decompose()

    text = soup.get_text()
    lines = (line.strip() for line in text.splitlines())
    chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
    return ' '.join(chunk for chunk in chunks if chunk)


def is_challenge_page_text(text: str) -> bool:
    """检测是否为 Cloudflare 等安全验证页。"""
    if not text:
        return False

    text_lower = text.lower()
    challenge_signals = [
        "just a moment",
        "verifying you are human",
        "performing security verification",
        "verification successful",
        "enable javascript and cookies to continue",
        "performance and security by cloudflare",
        "ray id"
    ]
    return sum(1 for signal in challenge_signals if signal in text_lower) >= 2


def wait_for_challenge_resolution(page) -> str:
    """
    如果页面处于 challenge 状态，则在同一会话中短暂等待并重试，
    尽量让自动验证完成后再提取正文。
    """
    last_text = ""

    for attempt in range(3):
        html = page.content()
        last_text = html_to_text(html)
        if not is_challenge_page_text(last_text):
            return last_text

        logger.warning(f"检测到验证页，等待自动验证完成（第 {attempt + 1}/3 次）")

        try:
            page.wait_for_load_state('networkidle', timeout=5000)
        except Exception:
            pass

        page.wait_for_timeout(5000)

        if attempt == 1:
            try:
                logger.warning("验证页仍存在，尝试刷新页面一次")
                page.reload(wait_until='domcontentloaded', timeout=30000)
                page.wait_for_timeout(3000)
            except Exception as e:
                logger.warning(f"刷新验证页失败（继续等待）: {e}")

    return last_text

def run_playwright_task(url: str, scroll_enabled: bool = True, screenshot_mode: bool = False) -> dict:
    """
    在独立进程中运行Playwright任务
    
    Args:
        url: 要访问的URL
        scroll_enabled: 是否启用滚动加载
        screenshot_mode: 是否启用截图模式
        
    Returns:
        dict: {'success': bool, 'content': str, 'error': str, 'screenshots': list}
    """
    try:
        from playwright.sync_api import sync_playwright
        from bs4 import BeautifulSoup
        
        logger.info(f"Playwright Worker: 开始处理 {url}")
        
        # 读取 headless 配置（默认有头，降低反爬触发；可用环境变量覆盖）
        try:
            from ..core.config import PLAYWRIGHT_HEADLESS
            headless_mode = PLAYWRIGHT_HEADLESS
        except Exception:
            headless_mode = False

        with sync_playwright() as p:
            # 启动浏览器（headless 由配置决定）
            browser = p.chromium.launch(
                headless=headless_mode,
                args=[
                    '--no-sandbox',
                    '--disable-dev-shm-usage',
                    '--disable-gpu',
                    '--no-first-run',
                    '--disable-blink-features=AutomationControlled',
                    '--disable-web-security',
                    '--disable-features=IsolateOrigins,site-per-process'
                ]
            )
            
            # 创建浏览器上下文
            context = browser.new_context(
                viewport={'width': 1920, 'height': 1080},
                user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                locale='en-US',
                timezone_id='America/New_York',
                java_script_enabled=True
            )
            
            # 创建新页面
            page = context.new_page()
            
            # 设置页面反检测
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined,
                });
                
                delete navigator.__proto__.webdriver;
                
                // 覆盖plugins
                Object.defineProperty(navigator, 'plugins', {
                    get: () => [1, 2, 3, 4, 5],
                });
                
                // 覆盖languages
                Object.defineProperty(navigator, 'languages', {
                    get: () => ['en-US', 'en'],
                });
            """)
            
            # 访问页面
            logger.info(f"正在访问页面: {url}")
            try:
                page.goto(url, wait_until='domcontentloaded', timeout=30000)
            except Exception as e:
                logger.warning(f"初始页面加载超时（尝试继续）: {e}")
            
            # 如果是截图模式，跳过智能加载，使用简单等待
            if screenshot_mode:
                logger.info("进入截图模式（跳过智能加载，使用充分的固定等待）...")
                
                # 等待网络空闲(适用于动态渲染网站)
                try:
                    page.wait_for_load_state('networkidle', timeout=10000)
                except Exception:
                    # 如果网络一直不空闲(如持续的轮询请求)，退回等待load
                    try:
                        page.wait_for_load_state('load', timeout=5000)
                    except Exception:
                        pass
                
                # 长时间等待动态内容(从500ms增加到3000ms)
                page.wait_for_timeout(3000)
            else:
                # 非截图模式：使用智能页面加载检测
                try:
                    from ..core.config import (USE_SMART_PAGE_LOADER, SMART_LOAD_INITIAL_WAIT,
                                      SMART_LOAD_MAX_WAIT, SMART_LOAD_STABILITY_INTERVAL,
                                      SMART_LOAD_STABILITY_THRESHOLD, SMART_LOAD_MIN_CONTENT_LENGTH,
                                      SMART_LOAD_MAX_RETRIES)
                    
                    if USE_SMART_PAGE_LOADER:
                        logger.info("使用智能页面加载检测...")
                        from .smart_page_loader import create_smart_loader
                        
                        smart_loader = create_smart_loader({
                            'max_wait_time': SMART_LOAD_MAX_WAIT,
                            'initial_wait': SMART_LOAD_INITIAL_WAIT,
                            'stability_check_interval': SMART_LOAD_STABILITY_INTERVAL,
                            'stability_threshold': SMART_LOAD_STABILITY_THRESHOLD,
                            'min_content_length': SMART_LOAD_MIN_CONTENT_LENGTH
                        })
                        
                        load_result = smart_loader.wait_for_page_with_retry(
                            page, url, max_retries=SMART_LOAD_MAX_RETRIES
                        )
                        
                        logger.info(f"智能加载完成: 策略={load_result['strategy']}, "
                                  f"耗时={load_result['wait_time']:.2f}s, "
                                  f"内容长度={load_result['content_length']}")
                        
                        if load_result['warnings']:
                            for warning in load_result['warnings']:
                                logger.warning(f"加载警告: {warning}")
                    else:
                        # 使用传统方式等待
                        logger.info("使用传统页面加载等待...")
                        page.wait_for_load_state('networkidle', timeout=30000)
                        page.wait_for_timeout(3000)
                except ImportError as e:
                    logger.warning(f"无法导入智能加载器（使用传统方式）: {e}")
                    page.wait_for_load_state('networkidle', timeout=30000)
                    page.wait_for_timeout(3000)
                except Exception as e:
                    logger.warning(f"智能加载检测失败（继续处理）: {e}")
                    page.wait_for_timeout(3000)
            
            # 如果是截图模式，执行截图
            if screenshot_mode:
                
                screenshot_paths = capture_screenshots(page, url)
                
                browser.close()
                
                if screenshot_paths:
                    logger.info(f"截图成功，共 {len(screenshot_paths)} 张")
                    return {
                        'success': True,
                        'content': None,  # 截图模式下不返回文本内容
                        'error': None,
                        'length': 0,
                        'screenshots': screenshot_paths,
                        'mode': 'screenshot'
                    }
                else:
                    logger.error("截图失败")
                    return {
                        'success': False,
                        'content': None,
                        'error': '截图失败',
                        'length': 0,
                        'screenshots': []
                    }
            
            # 针对 Cloudflare 等验证页，先在同一会话中等待自动跳转
            challenge_checked_text = wait_for_challenge_resolution(page)

            # 执行滚动加载
            if scroll_enabled:
                logger.info("开始滚动加载...")
                scroll_and_load(page)
            
            # 获取页面源码
            page_html = page.content()
            text = html_to_text(page_html)

            # 额外捕获渲染后的 innerText，供下游"多候选打分择优"抽取使用（兜底候选）
            try:
                inner_text = page.evaluate("() => document.body ? document.body.innerText : ''") or ""
            except Exception as e:
                logger.warning(f"获取 innerText 失败: {e}")
                inner_text = ""

            # 如果滚动后拿到的仍然比 challenge 检测阶段更差，则保留更长的那份文本
            if len(challenge_checked_text) > len(text):
                text = challenge_checked_text
            
            # 关闭浏览器
            browser.close()
            
            logger.info(f"内容获取成功，长度: {len(text)} 字符")
            
            return {
                'success': True,
                'content': text,
                'html': page_html,
                'inner_text': inner_text,
                'error': None,
                'length': len(text)
            }
            
    except Exception as e:
        error_msg = f"Playwright处理失败: {str(e)}"
        logger.error(error_msg)
        return {
            'success': False,
            'content': None,
            'error': error_msg,
            'length': 0
        }

def scroll_and_load(page):
    """滚动页面以加载所有动态内容（滚动参数从 config 读取，失败回退默认）"""
    try:
        # 滚动参数（优先读配置）
        try:
            from ..core.config import (SCROLL_STEP, SCROLL_DELAY, MAX_SCROLLS,
                                       NO_NEW_CONTENT_THRESHOLD, SCROLL_BUFFER)
        except Exception:
            SCROLL_STEP = 500
            SCROLL_DELAY = 1000
            MAX_SCROLLS = 50
            NO_NEW_CONTENT_THRESHOLD = 3
            SCROLL_BUFFER = 1000
        
        # 获取页面高度
        page_height = page.evaluate("document.body.scrollHeight")
        logger.info(f"页面总高度: {page_height}px")
        
        current_position = 0
        scroll_count = 0
        no_new_content_count = 0
        last_height = page_height
        
        while scroll_count < MAX_SCROLLS:
            # 滚动到下一个位置
            current_position += SCROLL_STEP
            page.evaluate(f"window.scrollTo(0, {current_position})")
            
            # 等待内容加载
            page.wait_for_timeout(SCROLL_DELAY)
            
            # 检查是否有新内容加载
            new_height = page.evaluate("document.body.scrollHeight")
            
            if new_height > last_height:
                logger.info(f"滚动到 {current_position}px，发现新内容 (高度: {last_height} -> {new_height})")
                last_height = new_height
                no_new_content_count = 0
            else:
                no_new_content_count += 1
            
            # 如果连续几次没有新内容，停止滚动
            if no_new_content_count >= NO_NEW_CONTENT_THRESHOLD:
                logger.info("连续多次无新内容，停止滚动")
                break
            
            # 如果已经滚动到页面底部
            if current_position >= new_height - SCROLL_BUFFER:
                logger.info("已滚动到页面底部")
                break
            
            scroll_count += 1
        
        # 滚动回顶部
        page.evaluate("window.scrollTo(0, 0)")
        page.wait_for_timeout(1000)
        
        logger.info(f"滚动完成，共滚动 {scroll_count} 次，最终页面高度: {last_height}px")
        
    except Exception as e:
        logger.warning(f"滚动加载过程中出错: {e}")

def capture_screenshots(page, url: str) -> list:
    """
    捕获页面截图（支持长页面分段截图和PDF多页截图）
    
    Args:
        page: Playwright页面对象
        url: 页面URL（用于生成文件名）
        
    Returns:
        list: 截图文件路径列表
    """
    try:
        import hashlib
        from pathlib import Path
        from urllib.parse import urlparse
        
        # 获取配置
        try:
            from ..core.config import SCREENSHOT_CACHE_DIR, SCREENSHOT_MAX_PAGES, SCREENSHOT_QUALITY
        except ImportError:
            # 如果无法导入配置，使用默认值
            SCREENSHOT_CACHE_DIR = Path(__file__).parent / "cache" / "screenshots"
            SCREENSHOT_MAX_PAGES = 10
            SCREENSHOT_QUALITY = 90
        
        # 确保截图目录存在
        SCREENSHOT_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        
        # 生成唯一文件名
        url_hash = hashlib.md5(url.encode()).hexdigest()[:8]
        parsed_url = urlparse(url)
        domain = parsed_url.netloc.replace('.', '_')[:20]
        
        screenshot_paths = []
        
        # 检测是否为基于 canvas 的在线 PDF 查看器（腾讯文档等）。
        # 触发条件放宽：URL 含 /pdf/ ，或页面存在 canvas 渲染的多页查看器。
        is_pdf_viewer = '/pdf/' in url
        if not is_pdf_viewer:
            try:
                is_pdf_viewer = page.evaluate(
                    "() => document.querySelectorAll('canvas').length > 0 && !!("
                    "document.querySelector('.multiPage') || "
                    "[...document.querySelectorAll('*')].some(e=>e.scrollHeight>e.clientHeight+50 "
                    "&& e.clientHeight>300 && /(auto|scroll)/.test(getComputedStyle(e).overflowY)))"
                )
            except Exception:
                is_pdf_viewer = False
        if is_pdf_viewer:
            logger.info("检测到在线PDF查看器（canvas 多页），使用逐页 clip 截图策略")
            return capture_pdf_viewer_screenshots(page, domain, url_hash, SCREENSHOT_CACHE_DIR, SCREENSHOT_MAX_PAGES)
        
        # 获取页面高度（带重试机制，防止页面未加载完成）
        try:
            page_height = page.evaluate("document.body.scrollHeight")
            viewport_height = page.viewport_size['height']
            logger.info(f"页面高度: {page_height}px, 视口高度: {viewport_height}px")
        except Exception as e:
            logger.warning(f"获取页面高度失败: {e}，使用默认视口高度")
            viewport_height = 1080
            page_height = viewport_height
        
        # 如果页面高度小于视口高度的1.5倍，只截一张图
        if page_height <= viewport_height * 1.5:
            logger.info("页面较短，截取单张全页截图")
            screenshot_path = SCREENSHOT_CACHE_DIR / f"{domain}_{url_hash}_full.png"
            # PNG格式不支持quality参数，改用type='png'
            page.screenshot(path=str(screenshot_path), full_page=True, type='png')
            screenshot_paths.append(str(screenshot_path))
            logger.info(f"截图已保存: {screenshot_path.name}")
        else:
            # 长页面分段截图
            logger.info(f"页面较长，分段截图（最多 {SCREENSHOT_MAX_PAGES} 张）")
            num_screenshots = min(SCREENSHOT_MAX_PAGES, (page_height // viewport_height) + 1)
            
            for i in range(num_screenshots):
                # 滚动到对应位置
                scroll_position = i * viewport_height
                page.evaluate(f"window.scrollTo(0, {scroll_position})")
                page.wait_for_timeout(200)  # 从500ms缩短到200ms
                
                # 截图（PNG格式不支持quality参数）
                screenshot_path = SCREENSHOT_CACHE_DIR / f"{domain}_{url_hash}_{i:03d}.png"
                page.screenshot(path=str(screenshot_path), type='png')
                screenshot_paths.append(str(screenshot_path))
                logger.info(f"已截图第 {i+1}/{num_screenshots} 张: {screenshot_path.name}")
            
            # 滚动回顶部
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(200)
        
        logger.info(f"截图完成，共 {len(screenshot_paths)} 张")
        return screenshot_paths
        
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        logger.error(f"截图捕获失败: {e}")
        logger.error(f"详细错误信息:\n{error_details}")
        return []

# ── 在线 PDF 查看器逐页 clip 截图（腾讯文档等 canvas 多页）──────────────────
# 思路（已在真实腾讯文档上验证）：
#   1) 定位内部滚动容器（.multiPage 或最大的可滚动元素）；
#   2) 坐标探测隐藏盖在顶部/左侧、遮挡正文的 UI 覆盖条（与具体 class 无关）；
#   3) 逐个把 canvas 滚动进视口并微调，使整页完整可见；
#   4) 用 CDP Page.captureScreenshot 按 canvas 包围盒精确裁剪（绕过 Playwright 等字体的卡顿）。

_JS_FIND_SCROLLER = """
() => {
  const sc = document.querySelector('.multiPage') ||
    [...document.querySelectorAll('*')].filter(e=>e.scrollHeight>e.clientHeight+50 && e.clientHeight>300 &&
      /(auto|scroll)/.test(getComputedStyle(e).overflowY)).sort((a,b)=>b.scrollHeight-a.scrollHeight)[0];
  if(!sc) return null;
  sc.setAttribute('data-sc','1');
  return {sh:sc.scrollHeight, ch:sc.clientHeight};
}
"""

# 坐标探测：把盖在顶部/左侧的覆盖条隐藏（不依赖 class 名）
_JS_HIDE_COVERS = """
() => {
  const sc=document.querySelector('[data-sc]'); const vw=innerWidth, vh=innerHeight;
  const hidden=[];
  const tryHide=(x,y,edge)=>{
    const el=document.elementFromPoint(x,y); if(!el) return;
    let node=el;
    for(let k=0;k<5 && node && node!==document.body && node!==document.documentElement;k++){
      if(node===sc || node.contains(sc) || node.querySelector('canvas')) return;
      const r=node.getBoundingClientRect();
      const isTop = edge==='top' && r.top<=6 && r.height<300 && r.width>vw*0.35;
      const isLeft = edge==='left' && r.left<=6 && r.width<340 && r.height>vh*0.4;
      if(isTop || isLeft){ node.style.setProperty('display','none','important'); hidden.push((node.className||node.tagName).toString().slice(0,30)); return; }
      node=node.parentElement;
    }
  };
  [4,24,48,80,120,160].forEach(y=>tryHide(vw/2,y,'top'));
  [40,120,240,400].forEach(y=>tryHide(8,y,'left'));
  return hidden;
}
"""


def _canvas_rect(page, i: int):
    """返回第 i 个 canvas 在视口内的可见包围盒及完整高度。"""
    return page.evaluate("""(i)=>{
        const c=document.querySelectorAll('canvas')[i]; if(!c) return null;
        const r=c.getBoundingClientRect(); const vw=innerWidth, vh=innerHeight;
        const x=Math.max(0,r.left), y=Math.max(0,r.top);
        const w=Math.min(r.right,vw)-x, h=Math.min(r.bottom,vh)-y;
        return {x:Math.round(x),y:Math.round(y),w:Math.round(w),h:Math.round(h),
                top:Math.round(r.top),bottom:Math.round(r.bottom),fullh:Math.round(r.height),fullw:Math.round(r.width)};
    }""", i)


def _scroller_at_bottom(page) -> bool:
    return page.evaluate(
        "(()=>{const e=document.querySelector('[data-sc]');"
        "return e?(e.scrollTop+e.clientHeight>=e.scrollHeight-5):true;})()"
    )


def _maybe_fit_width(page, viewport_w: int):
    """横向溢出（canvas 比视口宽）时，用 Ctrl+'-' 缩小到适配宽度，避免右侧被裁掉。"""
    try:
        for _ in range(3):
            rect = _canvas_rect(page, 0)
            if not rect or rect.get('fullw', 0) <= viewport_w - 4:
                return
            page.keyboard.down('Control')
            page.keyboard.press('Minus')
            page.keyboard.up('Control')
            page.wait_for_timeout(250)
    except Exception as e:
        logger.debug(f"自动适配宽度失败（忽略）: {e}")


def capture_pdf_viewer_screenshots(page, domain: str, url_hash: str, cache_dir, max_pages: int) -> list:
    """在线 PDF 查看器逐页 clip 截图，确保每张恰好是完整一页。"""
    try:
        import base64

        # 读取配置（失败回退默认值）
        try:
            from ..core.config import (PDF_VIEWER_HIDE_UI, PDF_VIEWER_AUTO_ZOOM,
                                       SCREENSHOT_MAX_SHOTS, SCREENSHOT_PAGE_RENDER_WAIT_MS)
            hide_ui = PDF_VIEWER_HIDE_UI
            auto_zoom = PDF_VIEWER_AUTO_ZOOM
            max_shots = SCREENSHOT_MAX_SHOTS
            render_wait = SCREENSHOT_PAGE_RENDER_WAIT_MS
        except Exception:
            hide_ui, auto_zoom, max_shots, render_wait = True, True, 30, 1200

        screenshot_paths = []

        # 等待 canvas 渲染出来
        for _ in range(20):
            try:
                if page.evaluate("document.querySelectorAll('canvas').length") > 0:
                    break
            except Exception:
                pass
            page.wait_for_timeout(500)
        page.wait_for_timeout(min(render_wait, 1500))

        # 1) 定位滚动容器
        scroller = page.evaluate(_JS_FIND_SCROLLER)
        if not scroller:
            logger.warning("未找到 PDF 查看器滚动容器，回退整页截图")
            fallback = cache_dir / f"{domain}_{url_hash}_full.png"
            page.screenshot(path=str(fallback), type='png', full_page=True)
            return [str(fallback)]
        logger.info(f"滚动容器: {scroller}")

        # 2) 隐藏遮挡 UI
        if hide_ui:
            try:
                hidden = page.evaluate(_JS_HIDE_COVERS)
                page.wait_for_timeout(300)
                logger.info(f"已隐藏遮挡 UI: {hidden}")
            except Exception as e:
                logger.debug(f"隐藏 UI 失败（忽略）: {e}")

        viewport = page.viewport_size or {'width': 1920, 'height': 1080}
        vw, vh = viewport['width'], viewport['height']

        # 3) 横向溢出自动适配宽度
        if auto_zoom:
            _maybe_fit_width(page, vw)

        # 建立 CDP 会话用于精确裁剪截图
        try:
            cdp = page.context.new_cdp_session(page)
        except Exception as e:
            logger.warning(f"无法创建 CDP 会话，回退 page.screenshot: {e}")
            cdp = None

        def shot(path, r):
            if cdp is not None:
                res = cdp.send("Page.captureScreenshot", {"format": "png",
                    "clip": {"x": r["x"], "y": r["y"], "width": r["w"], "height": r["h"], "scale": 1}})
                Path(path).write_bytes(base64.b64decode(res["data"]))
            else:
                page.screenshot(path=str(path), type='png',
                                clip={"x": r["x"], "y": r["y"], "width": r["w"], "height": r["h"]})

        # 4) 逐页滚动 + clip
        limit = min(max_pages, max_shots)
        i = 0
        miss = 0
        while i < limit:
            try:
                exists = page.evaluate("(i)=>i < document.querySelectorAll('canvas').length", i)
            except Exception:
                exists = False
            if not exists:
                if _scroller_at_bottom(page):
                    break
                page.evaluate("(()=>{const e=document.querySelector('[data-sc]');if(e)e.scrollTop+=e.clientHeight*0.8;})()")
                page.wait_for_timeout(700)
                miss += 1
                if miss > 6:
                    break
                continue
            miss = 0

            block = "start" if i == 0 else "center"
            try:
                page.eval_on_selector_all(
                    "canvas", "(els,d)=>els[d.i] && els[d.i].scrollIntoView({block:d.b})", {"i": i, "b": block}
                )
            except Exception:
                pass
            page.wait_for_timeout(450)

            r = _canvas_rect(page, i)
            # 若整页未完全在视口内，微调使其完整可见
            if r and (r["top"] < -2 or r["bottom"] > vh + 2):
                try:
                    page.evaluate("(d)=>{const e=document.querySelector('[data-sc]');if(e)e.scrollTop+=d;}", r["top"])
                    page.wait_for_timeout(350)
                    r = _canvas_rect(page, i)
                except Exception:
                    pass

            # 跳过零尺寸幽灵 canvas
            if not r or r["fullh"] < 50:
                if _scroller_at_bottom(page):
                    break
                i += 1
                continue

            if r["w"] > 50 and r["h"] > 50:
                path = cache_dir / f"{domain}_{url_hash}_p{i+1:02d}.png"
                try:
                    shot(path, r)
                    full = r["h"] >= r["fullh"] - 3
                    logger.info(f"第 {i+1} 页截图: {path.name} h={r['h']}/{r['fullh']} {'FULL' if full else 'PART'}")
                    screenshot_paths.append(str(path))
                except Exception as e:
                    logger.warning(f"第 {i+1} 页截图失败: {e}")
            i += 1

        logger.info(f"PDF查看器逐页截图完成，共 {len(screenshot_paths)} 张")
        return screenshot_paths

    except Exception as e:
        import traceback
        logger.error(f"PDF查看器截图失败: {e}")
        logger.error(f"详细错误信息:\n{traceback.format_exc()}")
        return []

if __name__ == "__main__":
    # 从命令行参数获取URL
    if len(sys.argv) < 2:
        result = {
            'success': False,
            'content': None,
            'error': 'No URL provided',
            'length': 0
        }
    else:
        url = sys.argv[1]
        scroll_enabled = sys.argv[2].lower() == 'true' if len(sys.argv) > 2 else True
        screenshot_mode = sys.argv[3].lower() == 'true' if len(sys.argv) > 3 else False
        result = run_playwright_task(url, scroll_enabled, screenshot_mode)
    
    # 输出JSON结果
    print(json.dumps(result, ensure_ascii=False))




