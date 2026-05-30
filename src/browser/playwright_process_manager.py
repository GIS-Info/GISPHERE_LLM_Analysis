"""
Playwright进程管理器 - 通过独立进程运行Playwright，完全避免异步冲突
"""
import subprocess
import json
import logging
from typing import Optional
from pathlib import Path

logger = logging.getLogger(__name__)

class PlaywrightProcessManager:
    """通过独立进程管理Playwright，完全隔离异步环境"""
    
    def __init__(self):
        self.worker_script = Path(__file__).parent / "playwright_worker.py"
        self._check_worker_script()
    
    def _check_worker_script(self):
        """检查worker脚本是否存在"""
        if not self.worker_script.exists():
            logger.error(f"Playwright worker脚本不存在: {self.worker_script}")
            raise FileNotFoundError(f"找不到 {self.worker_script}")
    
    def get_page_content(self, url: str, scroll_enabled: bool = True, timeout: int = 120) -> Optional[str]:
        """
        通过独立进程获取页面内容（仅返回纯文本，向后兼容旧调用）。
        
        Args:
            url: 要访问的URL
            scroll_enabled: 是否启用滚动加载
            timeout: 超时时间（秒）
            
        Returns:
            str: 页面文本内容，失败返回None
        """
        payload = self.get_page_payload(url, scroll_enabled, timeout)
        if isinstance(payload, dict):
            return payload.get('content')
        return None

    def get_page_payload(self, url: str, scroll_enabled: bool = True, timeout: int = 120) -> Optional[dict]:
        """
        通过独立进程获取页面完整载荷：纯文本 + 原始 HTML + 渲染后 innerText。

        供下游"多候选打分择优"正文抽取使用（json-ld / trafilatura / og / innerText）。

        Returns:
            dict: {'content': str, 'html': str, 'inner_text': str}，失败返回None
        """
        result = self._run_playwright_task(url, scroll_enabled, False, timeout, return_full=True)
        if isinstance(result, dict):
            return {
                'content': result.get('content'),
                'html': result.get('html'),
                'inner_text': result.get('inner_text'),
            }
        # 兼容旧返回（纯字符串）
        if isinstance(result, str):
            return {'content': result, 'html': None, 'inner_text': None}
        return None
    
    def capture_screenshots(self, url: str, timeout: int = 120) -> Optional[list]:
        """
        通过独立进程捕获页面截图
        
        Args:
            url: 要访问的URL
            timeout: 超时时间（秒）
            
        Returns:
            list: 截图文件路径列表，失败返回None
        """
        result = self._run_playwright_task(url, False, True, timeout)
        if result and isinstance(result, dict) and result.get('screenshots'):
            return result.get('screenshots')
        return None
    
    def _run_playwright_task(self, url: str, scroll_enabled: bool = True, screenshot_mode: bool = False, timeout: int = 120, return_full: bool = False) -> Optional:
        """
        通过独立进程运行Playwright任务（内部方法）
        
        Args:
            url: 要访问的URL
            scroll_enabled: 是否启用滚动加载
            screenshot_mode: 是否启用截图模式
            timeout: 超时时间（秒）
            
        Returns:
            根据模式返回不同内容：文本模式返回str，截图模式返回dict
        """
        try:
            logger.info(f"启动独立Playwright进程处理: {url}")
            
            # 构建命令
            import sys
            python_executable = sys.executable
            cmd = [
                python_executable,
                str(self.worker_script),
                url,
                'true' if scroll_enabled else 'false',
                'true' if screenshot_mode else 'false'
            ]
            
            # 运行独立进程
            # 使用 errors='replace' 处理 Windows 编码问题
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=timeout,
                encoding='utf-8',
                errors='replace'  # 替换无法解码的字符，避免 UnicodeDecodeError
            )
            
            # 检查进程是否成功
            if result.returncode != 0:
                logger.error(f"Playwright进程失败，返回码: {result.returncode}")
                logger.error(f"错误输出: {result.stderr}")
                return None
            
            # 解析JSON结果
            try:
                response = json.loads(result.stdout)
                
                if response.get('success'):
                    if screenshot_mode:
                        # 截图模式返回完整响应
                        logger.info(f"✅ Playwright进程成功捕获截图，共 {len(response.get('screenshots', []))} 张")
                        return response
                    else:
                        # 文本模式返回内容
                        content = response.get('content')
                        length = response.get('length', 0)
                        logger.info(f"✅ Playwright进程成功获取内容，长度: {length} 字符")
                        if return_full:
                            return response
                        return content
                else:
                    error = response.get('error', 'Unknown error')
                    logger.error(f"Playwright进程报告失败: {error}")
                    return None
                    
            except json.JSONDecodeError as e:
                logger.error(f"无法解析Playwright进程输出: {e}")
                logger.error(f"输出内容: {result.stdout[:500]}")
                return None
                
        except subprocess.TimeoutExpired:
            logger.error(f"Playwright进程超时 ({timeout}秒)")
            return None
        except Exception as e:
            logger.error(f"Playwright进程管理器错误: {e}")
            return None
    
    def test_connection(self) -> bool:
        """测试Playwright是否可用"""
        try:
            logger.info("测试Playwright连接...")
            test_url = "https://www.example.com"
            content = self.get_page_content(test_url, scroll_enabled=False, timeout=60)
            
            if content and len(content) > 100:
                logger.info("✅ Playwright测试成功")
                return True
            else:
                logger.warning("⚠️  Playwright测试失败或返回内容过短")
                return False
        except Exception as e:
            logger.error(f"Playwright测试出错: {e}")
            return False

# 全局单例
_playwright_manager = None

def get_playwright_manager() -> PlaywrightProcessManager:
    """获取Playwright进程管理器单例"""
    global _playwright_manager
    if _playwright_manager is None:
        _playwright_manager = PlaywrightProcessManager()
    return _playwright_manager




