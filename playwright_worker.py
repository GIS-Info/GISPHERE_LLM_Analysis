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
        
        with sync_playwright() as p:
            # 启动浏览器（无头模式）
            browser = p.chromium.launch(
                headless=False,
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
                    from config import (USE_SMART_PAGE_LOADER, SMART_LOAD_INITIAL_WAIT,
                                      SMART_LOAD_MAX_WAIT, SMART_LOAD_STABILITY_INTERVAL,
                                      SMART_LOAD_STABILITY_THRESHOLD, SMART_LOAD_MIN_CONTENT_LENGTH,
                                      SMART_LOAD_MAX_RETRIES)
                    
                    if USE_SMART_PAGE_LOADER:
                        logger.info("使用智能页面加载检测...")
                        from smart_page_loader import create_smart_loader
                        
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
            content = page.content()
            text = html_to_text(content)

            # 如果滚动后拿到的仍然比 challenge 检测阶段更差，则保留更长的那份文本
            if len(challenge_checked_text) > len(text):
                text = challenge_checked_text
            
            # 关闭浏览器
            browser.close()
            
            logger.info(f"内容获取成功，长度: {len(text)} 字符")
            
            return {
                'success': True,
                'content': text,
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
    """滚动页面以加载所有动态内容"""
    try:
        # 滚动参数
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
            from config import SCREENSHOT_CACHE_DIR, SCREENSHOT_MAX_PAGES, SCREENSHOT_QUALITY
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
        
        # 检测是否为腾讯文档或其他PDF查看器
        if 'docs.qq.com' in url and '/pdf/' in url:
            logger.info("检测到腾讯文档PDF查看器，使用特殊截图策略")
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

def capture_pdf_viewer_screenshots(page, domain: str, url_hash: str, cache_dir, max_pages: int) -> list:
    """
    专门处理PDF查看器（如腾讯文档）的截图
    
    对每一页进行滚动截图，确保捕获完整内容
    
    Args:
        page: Playwright页面对象
        domain: 域名
        url_hash: URL哈希
        cache_dir: 缓存目录
        max_pages: 最大页数
        
    Returns:
        list: 截图文件路径列表
    """
    try:
        screenshot_paths = []
        
        # 等待PDF查看器加载（缩短等待时间）
        page.wait_for_timeout(500)
        
        # 尝试查找并切换到iframe（腾讯文档可能使用iframe）
        try:
            iframe_element = page.query_selector('iframe')
            if iframe_element:
                iframe = iframe_element.content_frame()
                if iframe:
                    logger.info("检测到iframe，切换到iframe内部")
                    page = iframe  # 在iframe内操作
                    page.wait_for_timeout(300)
        except Exception as e:
            logger.debug(f"iframe检测: {e}")
        
        # 尝试隐藏或折叠UI元素（工具栏、侧边栏等）
        try:
            logger.info("尝试隐藏UI元素...")
            page.evaluate("""
                () => {
                    // 隐藏工具栏
                    const toolbars = document.querySelectorAll('[role="toolbar"], .toolbar, [class*="toolbar"]');
                    toolbars.forEach(el => {
                        if (el) el.style.display = 'none';
                    });
                    
                    // 隐藏侧边栏/缩略图面板
                    const sidebars = document.querySelectorAll('[role="complementary"], .sidebar, [class*="sidebar"], [class*="thumbnail"]');
                    sidebars.forEach(el => {
                        if (el) el.style.display = 'none';
                    });
                    
                    // 隐藏按钮和控制元素
                    const buttons = document.querySelectorAll('button:not([class*="page"])');
                    buttons.forEach(el => {
                        if (el && el.textContent && el.textContent.length < 20) {
                            el.style.visibility = 'hidden';
                        }
                    });
                }
            """)
            logger.info("UI元素隐藏完成")
        except Exception as e:
            logger.debug(f"隐藏UI元素失败: {e}")
        
        # 使用Ctrl+鼠标滚轮缩小页面（缩小两次达到约90%）
        try:
            logger.info("尝试缩小页面...")
            
            # 缩小两次
            for i in range(2):
                # 按住Ctrl键
                page.keyboard.down('Control')
                # 向上滚动鼠标（缩小）
                page.mouse.wheel(0, 100)
                page.keyboard.up('Control')
                page.wait_for_timeout(200)  # 从500ms缩短到200ms
                logger.debug(f"第{i+1}次缩小完成")
            
            logger.info("页面缩小完成")
            page.wait_for_timeout(300)  # 从1000ms缩短到300ms
        except Exception as e:
            logger.warning(f"页面缩小失败: {e}，继续使用原始大小")
        
        # 回到第1页（防止之前的操作影响）
        try:
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(200)
        except Exception:
            pass
        
        # 尝试检测页数（腾讯文档的页码指示器）
        try:
            # 查找页码指示器（格式如 "1/2"）
            page_indicator = page.evaluate("""
                () => {
                    // 尝试多种选择器
                    const selectors = [
                        '.page-num-info',  // 腾讯文档常见选择器
                        '[class*="page"]',
                        '[class*="Page"]',
                        'input[type="text"][value*="/"]'
                    ];
                    
                    for (const selector of selectors) {
                        const element = document.querySelector(selector);
                        if (element && element.textContent) {
                            const match = element.textContent.match(/(\\d+)\\s*\\/\\s*(\\d+)/);
                            if (match) {
                                return {current: parseInt(match[1]), total: parseInt(match[2])};
                            }
                        }
                        // 也检查value属性
                        if (element && element.value) {
                            const match = element.value.match(/(\\d+)\\s*\\/\\s*(\\d+)/);
                            if (match) {
                                return {current: parseInt(match[1]), total: parseInt(match[2])};
                            }
                        }
                    }
                    return null;
                }
            """)
            
            if page_indicator and page_indicator.get('total'):
                total_pages = min(page_indicator['total'], max_pages)
                logger.info(f"检测到PDF共 {page_indicator['total']} 页，将截取前 {total_pages} 页")
            else:
                total_pages = 1
                logger.info("未能检测到页数，默认截取1页")
        except Exception as e:
            logger.warning(f"检测页数失败: {e}，默认截取1页")
            total_pages = 1
        
        # 对每一页进行滚动截图
        for page_num in range(1, total_pages + 1):
            try:
                logger.info(f"准备截取第 {page_num}/{total_pages} 页")
                
                # 确保滚动到页面顶部
                try:
                    page.evaluate("window.scrollTo(0, 0)")
                    page.wait_for_timeout(300)
                except Exception as e:
                    logger.debug(f"重置滚动位置失败: {e}")
                
                # 使用滚动截图策略
                page_screenshots = scroll_and_capture_page(page, cache_dir, domain, url_hash, page_num, total_pages)
                screenshot_paths.extend(page_screenshots)
                
                logger.info(f"第 {page_num} 页截图完成，共 {len(page_screenshots)} 张")
                
                # 如果还有下一页，翻页
                if page_num < total_pages:
                    try:
                        logger.info(f"准备翻到第 {page_num + 1} 页")
                        
                        # 尝试多种翻页方法
                        page_turned = False
                        
                        # 方法1: 尝试查找并点击"下一页"按钮
                        try:
                            next_button_selectors = [
                                'button[aria-label*="next"]',
                                'button[aria-label*="Next"]',
                                'button[title*="下一页"]',
                                'button[title*="next"]',
                                '[class*="next-page"]',
                                '[id*="next-page"]'
                            ]
                            
                            for selector in next_button_selectors:
                                next_button = page.query_selector(selector)
                                if next_button:
                                    next_button.click()
                                    logger.info(f"点击下一页按钮: {selector}")
                                    page.wait_for_timeout(500)
                                    page_turned = True
                                    break
                        except Exception as e:
                            logger.debug(f"查找下一页按钮失败: {e}")
                        
                        # 方法2: 如果没找到按钮，使用键盘ArrowDown
                        if not page_turned:
                            try:
                                logger.info("使用键盘ArrowDown翻页")
                                page.keyboard.press('ArrowDown')
                                page.wait_for_timeout(500)
                                page_turned = True
                            except Exception as e:
                                logger.debug(f"键盘翻页失败: {e}")
                        
                        # 验证翻页是否成功
                        if page_turned:
                            try:
                                current_page = page.evaluate("""
                                    () => {
                                        const input = document.querySelector('input[value*="/"]');
                                        if (input && input.value) {
                                            return parseInt(input.value.split('/')[0]);
                                        }
                                        return null;
                                    }
                                """)
                                
                                next_page_num = page_num + 1
                                if current_page == next_page_num:
                                    logger.info(f"翻页验证成功: 当前为第 {current_page} 页")
                                else:
                                    logger.warning(f"翻页验证失败: 期望第 {next_page_num} 页，实际第 {current_page} 页")
                            except Exception as e:
                                logger.debug(f"翻页验证失败: {e}")
                        
                    except Exception as e:
                        logger.warning(f"翻页失败: {e}")
                
            except Exception as e:
                logger.error(f"第 {page_num} 页截图失败: {e}")
                continue
        
        logger.info(f"PDF查看器截图完成，共 {len(screenshot_paths)} 张")
        return screenshot_paths
        
    except Exception as e:
        import traceback
        error_details = traceback.format_exc()
        logger.error(f"PDF查看器截图失败: {e}")
        logger.error(f"详细错误信息:\n{error_details}")
        return []

def scroll_and_capture_page(page, cache_dir, domain: str, url_hash: str, page_num: int, total_pages: int) -> list:
    """
    对单个页面进行滚动截图
    
    Args:
        page: Playwright页面对象
        cache_dir: 缓存目录
        domain: 域名
        url_hash: URL哈希
        page_num: 当前页码
        total_pages: 总页数
        
    Returns:
        list: 截图文件路径列表
    """
    screenshots = []
    
    try:
        # 获取视口高度
        viewport_height = page.viewport_size['height']
        
        # 滚动参数
        scroll_step = 400  # 每次滚动400像素
        max_scrolls = 30   # 最多滚动30次
        no_change_count = 0
        no_change_threshold = 3
        last_scroll_pos = -1
        
        scroll_count = 0
        
        while scroll_count < max_scrolls:
            # 截取当前视口
            screenshot_path = cache_dir / f"{domain}_{url_hash}_page{page_num}_{scroll_count}.png"
            page.screenshot(path=str(screenshot_path), type='png', full_page=False)
            screenshots.append(str(screenshot_path))
            logger.debug(f"第 {page_num} 页截图 {scroll_count}: {screenshot_path.name}")
            
            # 获取当前滚动位置
            try:
                current_scroll_pos = page.evaluate("window.pageYOffset || document.documentElement.scrollTop")
                page_height = page.evaluate("document.documentElement.scrollHeight")
                
                # 检查是否到达底部
                if current_scroll_pos + viewport_height >= page_height - 50:
                    logger.info(f"第 {page_num} 页已到达底部")
                    break
                
                # 检查滚动位置是否变化
                if current_scroll_pos == last_scroll_pos:
                    no_change_count += 1
                    if no_change_count >= no_change_threshold:
                        logger.info(f"第 {page_num} 页滚动位置连续{no_change_threshold}次未变化，停止滚动")
                        break
                else:
                    no_change_count = 0
                    last_scroll_pos = current_scroll_pos
                
            except Exception as e:
                logger.warning(f"获取滚动信息失败: {e}")
            
            # 向下滚动
            try:
                page.evaluate(f"window.scrollBy(0, {scroll_step})")
                page.wait_for_timeout(200)  # 从400ms缩短到200ms
                scroll_count += 1
                
                # 检查是否翻页了
                if page_num < total_pages:
                    try:
                        current_page = page.evaluate("""
                            () => {
                                const input = document.querySelector('input[value*="/"]');
                                if (input && input.value) {
                                    return parseInt(input.value.split('/')[0]);
                                }
                                return null;
                            }
                        """)
                        
                        if current_page and current_page > page_num:
                            logger.info(f"滚动时翻到第{current_page}页，停止当前页截图")
                            break
                    except Exception:
                        pass
                        
            except Exception as e:
                logger.warning(f"滚动失败: {e}")
                break
        
        logger.info(f"第 {page_num} 页完成，共截取 {len(screenshots)} 张")
        return screenshots
        
    except Exception as e:
        logger.error(f"第 {page_num} 页滚动截图失败: {e}")
        # 至少返回一张截图
        if not screenshots:
            try:
                screenshot_path = cache_dir / f"{domain}_{url_hash}_page{page_num}_0.png"
                page.screenshot(path=str(screenshot_path), type='png', full_page=False)
                screenshots.append(str(screenshot_path))
            except Exception:
                pass
        return screenshots

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

