"""
安全的Playwright包装器 - 避免异步冲突，支持滚动加载
"""
import logging
import threading
import time
from typing import Optional
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

class SafePlaywrightWrapper:
    """安全的Playwright包装器，避免异步冲突"""
    
    def __init__(self):
        self.playwright = None
        self.browser = None
        self.context = None
        self._lock = threading.Lock()
        self._initialized = False
        
    def _safe_init(self):
        """在独立线程中安全初始化Playwright"""
        if self._initialized:
            return True
            
        with self._lock:
            if self._initialized:
                return True
                
            try:
                logger.info("尝试在独立线程中初始化Playwright...")
                
                # 用于存储初始化结果的容器
                result_container = {'playwright': None, 'browser': None, 'context': None, 'error': None}
                
                # 在独立线程中初始化，避免异步冲突
                def init_playwright_in_thread():
                    try:
                        from playwright.sync_api import sync_playwright
                        
                        playwright = sync_playwright().start()
                        browser = playwright.chromium.launch(
                            headless=False,
                            args=[
                                '--no-sandbox',
                                '--disable-dev-shm-usage',
                                '--disable-gpu',
                                '--no-first-run',
                                '--disable-blink-features=AutomationControlled'
                            ]
                        )
                        
                        context = browser.new_context(
                            viewport={'width': 1920, 'height': 1080},
                            user_agent='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                            locale='en-US',
                            timezone_id='America/New_York'
                        )
                        
                        result_container['playwright'] = playwright
                        result_container['browser'] = browser
                        result_container['context'] = context
                        
                    except Exception as e:
                        result_container['error'] = str(e)
                        logger.error(f"Playwright初始化失败: {e}")
                
                # 创建并启动独立线程
                init_thread = threading.Thread(target=init_playwright_in_thread, daemon=True)
                init_thread.start()
                
                # 等待线程完成（最多等待10秒）
                init_thread.join(timeout=10)
                
                if init_thread.is_alive():
                    logger.error("Playwright初始化超时")
                    return False
                
                if result_container['error']:
                    logger.warning(f"Playwright初始化失败: {result_container['error']}")
                    logger.info("将使用基础HTTP请求作为备选方案")
                    return False
                
                if result_container['playwright'] is not None:
                    self.playwright = result_container['playwright']
                    self.browser = result_container['browser']
                    self.context = result_container['context']
                    self._initialized = True
                    logger.info("✅ Playwright在独立线程中初始化成功")
                    return True
                else:
                    logger.warning("Playwright初始化失败，将使用基础HTTP请求")
                    return False
                    
            except Exception as e:
                logger.error(f"Playwright安全初始化失败: {e}")
                return False
    
    def get_page_content(self, url: str) -> Optional[str]:
        """获取页面内容（带滚动加载）"""
        if not self._safe_init():
            return None
            
        try:
            logger.info(f"使用Playwright获取页面内容: {url}")
            page = self.context.new_page()
            
            # 设置页面反检测
            page.add_init_script("""
                Object.defineProperty(navigator, 'webdriver', {
                    get: () => undefined,
                });
                
                delete navigator.__proto__.webdriver;
            """)
            
            # 访问页面
            page.goto(url, wait_until='networkidle', timeout=30000)
            
            # 等待页面完全加载
            page.wait_for_timeout(3000)
            
            # 执行滚动加载策略
            logger.info("开始滚动页面以加载所有动态内容...")
            self._scroll_and_load_content(page)
            
            # 获取页面源码
            content = page.content()
            soup = BeautifulSoup(content, 'html.parser')
            
            # 移除脚本和样式
            for script in soup(["script", "style", "nav", "footer", "header"]):
                script.decompose()
            
            # 提取主要文本内容
            text = soup.get_text()
            
            # 清理文本
            lines = (line.strip() for line in text.splitlines())
            chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
            text = ' '.join(chunk for chunk in chunks if chunk)
            
            page.close()
            logger.info(f"Playwright页面内容获取成功，长度: {len(text)} 字符")
            return text
            
        except Exception as e:
            logger.warning(f"Playwright获取页面内容失败 {url}: {e}")
            return None
    
    def _scroll_and_load_content(self, page):
        """滚动页面以加载所有动态内容"""
        try:
            # 导入配置参数
            try:
                from config import (SCROLL_STEP, SCROLL_DELAY, MAX_SCROLLS, 
                                  NO_NEW_CONTENT_THRESHOLD, SCROLL_BUFFER)
            except ImportError:
                # 如果无法导入配置，使用默认值
                SCROLL_STEP = 500
                SCROLL_DELAY = 1000
                MAX_SCROLLS = 50
                NO_NEW_CONTENT_THRESHOLD = 3
                SCROLL_BUFFER = 1000
            
            # 获取页面高度
            page_height = page.evaluate("document.body.scrollHeight")
            logger.info(f"页面总高度: {page_height}px")
            logger.info(f"滚动参数: 步长={SCROLL_STEP}px, 延迟={SCROLL_DELAY}ms, 最大次数={MAX_SCROLLS}")
            
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
                    logger.info(f"滚动到 {current_position}px，无新内容 ({no_new_content_count}/{NO_NEW_CONTENT_THRESHOLD})")
                
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
            # 即使滚动失败，也继续获取内容
    
    def close(self):
        """关闭浏览器"""
        if self.browser:
            try:
                self.browser.close()
                logger.info("Playwright浏览器已关闭")
            except Exception as e:
                logger.warning(f"关闭Playwright浏览器时出错: {e}")
        
        if self.playwright:
            try:
                self.playwright.stop()
                logger.info("Playwright已停止")
            except Exception as e:
                logger.warning(f"停止Playwright时出错: {e}")

# 全局实例
_safe_playwright = None

def get_safe_playwright():
    """获取安全的Playwright实例"""
    global _safe_playwright
    if _safe_playwright is None:
        _safe_playwright = SafePlaywrightWrapper()
    return _safe_playwright
