"""
网页和PDF内容提取模块
"""
import requests
import fitz  # PyMuPDF
from bs4 import BeautifulSoup
import logging
from pathlib import Path
from urllib.parse import urlparse, urljoin
import hashlib
import time
import re
import io
from typing import Optional
import sys

from config import PDF_CACHE_DIR, REQUEST_TIMEOUT, PDF_DOWNLOAD_TIMEOUT, MAX_RETRIES, USE_DOCUMENT_AI
from utils import (is_pdf_url, sanitize_filename, normalize_text, 
                   is_google_drive_url, convert_google_drive_to_download, extract_google_drive_file_id,
                   is_google_docs_url, convert_google_docs_to_export, extract_google_docs_document_id)

logger = logging.getLogger(__name__)

class ContentFetcher:
    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update({
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
        })
        
        # 确保缓存目录存在
        PDF_CACHE_DIR.mkdir(parents=True, exist_ok=True)
        
        # 尝试初始化Playwright浏览器（用于JavaScript-heavy网站）
        self.playwright_manager = None
        try:
            from config import USE_PLAYWRIGHT
            if USE_PLAYWRIGHT:
                from playwright_process_manager import get_playwright_manager
                self.playwright_manager = get_playwright_manager()
                logger.info("✅ Playwright进程管理器初始化成功（独立进程，无异步冲突）")
            else:
                logger.info("Playwright已在配置中禁用，将使用基础HTTP请求")
        except Exception as e:
            logger.warning(f"Playwright进程管理器初始化失败，将使用基础HTTP请求: {e}")
        
        # 存储当前处理的PDF文件路径，用于后续清理
        self.current_pdf_file = None
        
        # 初始化截图OCR提取器
        self.screenshot_ocr_fetcher = None
        try:
            from config import USE_SCREENSHOT_OCR
            if USE_SCREENSHOT_OCR:
                from screenshot_ocr_fetcher import ScreenshotOCRFetcher
                self.screenshot_ocr_fetcher = ScreenshotOCRFetcher()
                logger.info("✅ 截图OCR提取器初始化成功")
            else:
                logger.info("截图OCR已在配置中禁用")
        except Exception as e:
            logger.warning(f"截图OCR提取器初始化失败: {e}")
    
    def fetch_content(self, url: str) -> Optional[str]:
        """根据URL类型获取内容"""
        if not url:
            logger.error("URL为空")
            return None
        
        logger.info(f"开始获取内容: {url}")
        
        try:
            # 优先处理Google Docs链接
            if is_google_docs_url(url):
                logger.info("检测到Google Docs链接，使用特殊处理流程")
                return self._handle_google_docs_url(url)
            
            # 优先处理Google Drive链接
            if is_google_drive_url(url):
                logger.info("检测到Google Drive链接，使用特殊处理流程")
                return self._handle_google_drive_url(url)
            
            # 首先尝试基于URL判断
            if is_pdf_url(url):
                logger.info("根据URL判断为PDF文件，使用PDF处理流程")
                return self._fetch_pdf_content(url)
            else:
                # 对于不确定的URL，先尝试获取响应头来判断
                logger.info("URL类型不明确，检查内容类型...")
                content_type = self._check_content_type(url)
                
                if content_type and 'pdf' in content_type.lower():
                    logger.info(f"根据Content-Type判断为PDF: {content_type}")
                    return self._fetch_pdf_content(url)
                else:
                    logger.info(f"根据Content-Type判断为网页: {content_type}")
                    
                    # 对于已知的JavaScript-heavy网站，优先使用Playwright
                    if self._is_javascript_heavy_site(url) and self.playwright_manager:
                        logger.info("检测到JavaScript-heavy网站，使用Playwright获取内容")
                        content = self._fetch_web_content_with_playwright(url)
                    else:
                        content = self._fetch_web_content(url)
                    
                    # 对网页内容也进行基本验证
                    if content:
                        if self._is_likely_pdf_content(content):
                            logger.warning("⚠️  网页内容疑似PDF乱码，尝试PDF处理流程")
                            # 如果网页内容看起来像PDF乱码，尝试PDF处理
                            return self._fetch_pdf_content(url)
                        else:
                            logger.info("✅ 网页内容验证通过")
                    
                    # 如果常规方法失败，尝试截图OCR作为fallback
                    if not content or self._is_unavailable_content(content):
                        logger.warning("⚠️  常规方法获取内容失败或内容不可用，尝试截图OCR...")
                        screenshot_content = self._fetch_content_with_screenshot_ocr(url)
                        if screenshot_content:
                            logger.info("✅ 通过截图OCR成功获取内容")
                            return screenshot_content
                        else:
                            logger.warning("截图OCR也失败，返回常规方法的结果（可能为空）")
                    
                    return content
        except Exception as e:
            logger.error(f"获取内容失败: {e}")
            # 异常情况下也尝试截图OCR
            if self.screenshot_ocr_fetcher and self.playwright_manager:
                logger.info("尝试使用截图OCR作为最后的fallback...")
                screenshot_content = self._fetch_content_with_screenshot_ocr(url)
                if screenshot_content:
                    logger.info("✅ 通过截图OCR成功获取内容（异常恢复）")
                    return screenshot_content
            return None
    
    def _handle_google_drive_url(self, url: str) -> Optional[str]:
        """处理Google Drive链接，尝试下载PDF或提取文本"""
        logger.info(f"处理Google Drive链接: {url}")
        
        try:
            # 方法1: 尝试转换为直接下载链接并下载PDF
            logger.info("方法1: 尝试转换为直接下载链接...")
            download_url = convert_google_drive_to_download(url)
            
            if download_url:
                logger.info(f"转换后的下载链接: {download_url}")
                
                # 尝试下载PDF
                pdf_content = self._fetch_pdf_content(download_url)
                if pdf_content:
                    logger.info("✅ 通过直接下载链接成功获取PDF内容")
                    return pdf_content
                
                # 如果下载失败，可能是遇到了病毒扫描警告页面
                # 尝试处理病毒扫描警告
                logger.info("直接下载失败，尝试处理病毒扫描警告...")
                pdf_content = self._handle_google_drive_virus_scan(download_url)
                if pdf_content:
                    logger.info("✅ 通过处理病毒扫描警告成功获取PDF内容")
                    return pdf_content
            
            # 方法2: 使用Playwright从PDF预览器中提取文本（备用方案）
            logger.info("方法2: 使用Playwright从PDF预览器中提取文本...")
            if self.playwright_manager:
                playwright_content = self._fetch_google_drive_with_playwright(url)
                if playwright_content:
                    logger.info("✅ 通过Playwright成功获取内容")
                    return playwright_content
            
            # 方法3: 使用截图OCR作为最后的fallback
            logger.info("方法3: 使用截图OCR作为最后的fallback...")
            screenshot_content = self._fetch_content_with_screenshot_ocr(url)
            if screenshot_content:
                logger.info("✅ 通过截图OCR成功获取内容")
                return screenshot_content
            
            logger.error("所有Google Drive处理方法都失败")
            return None
            
        except Exception as e:
            logger.error(f"处理Google Drive链接失败: {e}")
            # 异常情况下也尝试截图OCR
            if self.screenshot_ocr_fetcher and self.playwright_manager:
                logger.info("尝试使用截图OCR作为异常恢复...")
                screenshot_content = self._fetch_content_with_screenshot_ocr(url)
                if screenshot_content:
                    return screenshot_content
            return None
    
    def _handle_google_drive_virus_scan(self, download_url: str) -> Optional[str]:
        """处理Google Drive的病毒扫描警告页面"""
        try:
            # 第一次请求可能会返回病毒扫描警告页面
            response = self._download_with_retry(download_url, PDF_DOWNLOAD_TIMEOUT)
            if not response:
                return None
            
            # 检查响应内容
            content_type = response.headers.get('content-type', '').lower()
            
            # 如果返回的是PDF，直接处理
            if 'pdf' in content_type:
                # 保存PDF并提取文本
                url_hash = hashlib.md5(download_url.encode()).hexdigest()
                cache_file = PDF_CACHE_DIR / f"{url_hash}_gdrive.pdf"
                self.current_pdf_file = cache_file
                
                with open(cache_file, 'wb') as f:
                    f.write(response.content)
                
                return self._extract_pdf_text(cache_file)
            
            # 如果是HTML（病毒扫描警告页面），尝试提取确认链接
            if 'html' in content_type:
                from bs4 import BeautifulSoup
                soup = BeautifulSoup(response.text, 'html.parser')
                
                # 查找确认下载的链接或表单
                # Google Drive的病毒扫描警告页面通常包含一个确认下载的链接
                confirm_link = None
                
                # 尝试查找确认链接（多种可能的格式）
                for link in soup.find_all('a', href=True):
                    href = link.get('href', '')
                    if 'export=download' in href and 'confirm=' in href:
                        confirm_link = href
                        break
                
                # 如果找到确认链接，再次请求
                if confirm_link:
                    # 如果是相对链接，转换为绝对链接
                    if confirm_link.startswith('/'):
                        confirm_link = urljoin(download_url, confirm_link)
                    
                    logger.info(f"找到确认链接，再次请求: {confirm_link}")
                    confirm_response = self._download_with_retry(confirm_link, PDF_DOWNLOAD_TIMEOUT)
                    
                    if confirm_response:
                        content_type = confirm_response.headers.get('content-type', '').lower()
                        if 'pdf' in content_type:
                            # 保存PDF并提取文本
                            url_hash = hashlib.md5(download_url.encode()).hexdigest()
                            cache_file = PDF_CACHE_DIR / f"{url_hash}_gdrive.pdf"
                            self.current_pdf_file = cache_file
                            
                            with open(cache_file, 'wb') as f:
                                f.write(confirm_response.content)
                            
                            return self._extract_pdf_text(cache_file)
                
                # 如果找不到确认链接，尝试从表单中提取
                form = soup.find('form', {'id': 'download-form'})
                if form:
                    action = form.get('action', '')
                    if action:
                        if action.startswith('/'):
                            action = urljoin(download_url, action)
                        
                        # 提取表单数据
                        form_data = {}
                        for input_tag in form.find_all('input'):
                            name = input_tag.get('name')
                            value = input_tag.get('value', '')
                            if name:
                                form_data[name] = value
                        
                        if form_data:
                            logger.info(f"找到表单，提交确认: {action}")
                            form_response = self.session.post(action, data=form_data, timeout=PDF_DOWNLOAD_TIMEOUT)
                            
                            if form_response.status_code == 200:
                                content_type = form_response.headers.get('content-type', '').lower()
                                if 'pdf' in content_type:
                                    # 保存PDF并提取文本
                                    url_hash = hashlib.md5(download_url.encode()).hexdigest()
                                    cache_file = PDF_CACHE_DIR / f"{url_hash}_gdrive.pdf"
                                    self.current_pdf_file = cache_file
                                    
                                    with open(cache_file, 'wb') as f:
                                        f.write(form_response.content)
                                    
                                    return self._extract_pdf_text(cache_file)
            
        except Exception as e:
            logger.debug(f"处理病毒扫描警告失败: {e}")
        
        return None
    
    def _fetch_google_drive_with_playwright(self, url: str) -> Optional[str]:
        """使用Playwright从Google Drive PDF预览器中提取文本"""
        if not self.playwright_manager:
            return None
        
        try:
            from config import PLAYWRIGHT_TIMEOUT, PLAYWRIGHT_SCROLL_ENABLED
            
            logger.info(f"使用Playwright处理Google Drive链接: {url}")
            
            # 使用Playwright获取页面内容
            content = self.playwright_manager.get_page_content(
                url,
                scroll_enabled=False,  # Google Drive PDF预览器不需要滚动
                timeout=PLAYWRIGHT_TIMEOUT
            )
            
            if not content:
                return None
            
            # 检查内容是否包含PDF预览器的文本
            # Google Drive的PDF预览器通常会在页面中嵌入PDF内容
            # 如果内容太短（只有"Loading..."），说明没有成功加载
            if len(content.strip()) < 200:
                logger.warning("Playwright获取的内容过短，可能未成功加载PDF预览器")
                return None
            
            # 检查是否包含常见的Google Drive占位文本
            placeholder_texts = ['Loading…', 'Sign in', 'Unable to preview', 'Access denied']
            if any(placeholder in content for placeholder in placeholder_texts):
                if len(content.strip()) < 500:  # 如果内容主要是占位文本
                    logger.warning("检测到Google Drive占位文本，PDF预览器可能未成功加载")
                    return None
            
            # 如果内容看起来有效，返回它
            # 注意：Google Drive的PDF预览器可能不会直接显示所有文本
            # 这种情况下，我们返回获取到的内容，让后续的LLM分析处理
            logger.info(f"✅ Playwright获取到内容，长度: {len(content)} 字符")
            return content
            
        except Exception as e:
            logger.warning(f"使用Playwright处理Google Drive链接失败: {e}")
            return None
    
    def _handle_google_docs_url(self, url: str) -> Optional[str]:
        """处理Google Docs链接，尝试导出文本或PDF"""
        logger.info(f"处理Google Docs链接: {url}")
        
        try:
            # 方法1: 尝试导出为文本格式（最轻量，最快）
            logger.info("方法1: 尝试导出为文本格式...")
            txt_export_url = convert_google_docs_to_export(url, format='txt')
            
            if txt_export_url:
                logger.info(f"文本导出链接: {txt_export_url}")
                txt_content = self._fetch_google_docs_export(txt_export_url, format='txt')
                if txt_content:
                    logger.info("✅ 通过文本导出成功获取内容")
                    return txt_content
            
            # 方法2: 尝试导出为PDF格式
            logger.info("方法2: 尝试导出为PDF格式...")
            pdf_export_url = convert_google_docs_to_export(url, format='pdf')
            
            if pdf_export_url:
                logger.info(f"PDF导出链接: {pdf_export_url}")
                pdf_content = self._fetch_google_docs_export(pdf_export_url, format='pdf')
                if pdf_content:
                    logger.info("✅ 通过PDF导出成功获取内容")
                    return pdf_content
            
            # 方法3: 使用Playwright从文档编辑器中提取文本（备用方案）
            logger.info("方法3: 使用Playwright从文档编辑器中提取文本...")
            if self.playwright_manager:
                playwright_content = self._fetch_google_docs_with_playwright(url)
                if playwright_content:
                    logger.info("✅ 通过Playwright成功获取内容")
                    return playwright_content
            
            # 方法4: 使用截图OCR作为最后的fallback
            logger.info("方法4: 使用截图OCR作为最后的fallback...")
            screenshot_content = self._fetch_content_with_screenshot_ocr(url)
            if screenshot_content:
                logger.info("✅ 通过截图OCR成功获取内容")
                return screenshot_content
            
            logger.error("所有Google Docs处理方法都失败")
            return None
            
        except Exception as e:
            logger.error(f"处理Google Docs链接失败: {e}")
            # 异常情况下也尝试截图OCR
            if self.screenshot_ocr_fetcher and self.playwright_manager:
                logger.info("尝试使用截图OCR作为异常恢复...")
                screenshot_content = self._fetch_content_with_screenshot_ocr(url)
                if screenshot_content:
                    return screenshot_content
            return None
    
    def _fetch_google_docs_export(self, export_url: str, format: str = 'txt') -> Optional[str]:
        """获取Google Docs导出内容"""
        try:
            response = self._download_with_retry(export_url, REQUEST_TIMEOUT)
            if not response:
                return None
            
            # 检查响应状态
            if response.status_code != 200:
                logger.warning(f"Google Docs导出失败，状态码: {response.status_code}")
                return None
            
            content_type = response.headers.get('content-type', '').lower()
            
            if format == 'txt':
                # 文本格式直接返回
                if 'text/plain' in content_type or 'text/html' in content_type:
                    text = response.text
                    # 清理文本
                    text = normalize_text(text)
                    if text and len(text.strip()) > 50:
                        logger.info(f"✅ 成功获取文本内容，长度: {len(text)} 字符")
                        return text
                else:
                    # 即使Content-Type不对，也尝试解析为文本
                    try:
                        text = response.text
                        text = normalize_text(text)
                        if text and len(text.strip()) > 50:
                            logger.info(f"✅ 成功获取文本内容（忽略Content-Type），长度: {len(text)} 字符")
                            return text
                    except Exception:
                        pass
            
            elif format == 'pdf':
                # PDF格式需要提取文本
                if 'pdf' in content_type or export_url.endswith('format=pdf'):
                    # 保存PDF到临时文件
                    document_id = extract_google_docs_document_id(export_url) or 'gdocs'
                    url_hash = hashlib.md5(export_url.encode()).hexdigest()
                    cache_file = PDF_CACHE_DIR / f"{url_hash}_{document_id}.pdf"
                    self.current_pdf_file = cache_file
                    
                    with open(cache_file, 'wb') as f:
                        f.write(response.content)
                    
                    # 提取PDF文本
                    pdf_text = self._extract_pdf_text(cache_file)
                    if pdf_text:
                        logger.info(f"✅ 成功从PDF导出提取文本，长度: {len(pdf_text)} 字符")
                        return pdf_text
            
            logger.warning(f"Google Docs导出格式 {format} 处理失败")
            return None
            
        except Exception as e:
            logger.error(f"获取Google Docs导出内容失败: {e}")
            return None
    
    def _fetch_google_docs_with_playwright(self, url: str) -> Optional[str]:
        """使用Playwright从Google Docs编辑器中提取文本"""
        if not self.playwright_manager:
            return None
        
        try:
            from config import PLAYWRIGHT_TIMEOUT
            
            logger.info(f"使用Playwright处理Google Docs链接: {url}")
            
            # 使用Playwright获取页面内容
            content = self.playwright_manager.get_page_content(
                url,
                scroll_enabled=True,  # Google Docs可能需要滚动加载完整内容
                timeout=PLAYWRIGHT_TIMEOUT
            )
            
            if not content:
                return None
            
            # 检查内容是否包含常见的占位文本
            placeholder_texts = [
                'JavaScript isn\'t enabled',
                'Enable and reload',
                'Sign in',
                'Unable to preview',
                'Access denied',
                'This browser version is no longer supported'
            ]
            
            # 如果内容主要是占位文本，说明没有成功加载
            placeholder_count = sum(1 for placeholder in placeholder_texts if placeholder in content)
            if placeholder_count >= 2 and len(content.strip()) < 500:
                logger.warning("检测到Google Docs占位文本，文档可能未成功加载")
                return None
            
            # 清理内容：移除导航栏、工具栏等无关文本
            cleaned_content = self._clean_google_docs_content(content)
            
            if cleaned_content and len(cleaned_content.strip()) > 200:
                logger.info(f"✅ Playwright获取到内容，长度: {len(cleaned_content)} 字符")
                return cleaned_content
            else:
                logger.warning("Playwright获取的内容过短或为空")
                return None
            
        except Exception as e:
            logger.warning(f"使用Playwright处理Google Docs链接失败: {e}")
            return None
    
    def _clean_google_docs_content(self, content: str) -> str:
        """清理Google Docs内容，移除导航栏、工具栏等无关文本"""
        try:
            # 移除常见的Google Docs界面元素文本
            # 这些文本通常出现在页面顶部或底部
            unwanted_patterns = [
                r'File\s+Edit\s+View\s+Tools\s+Help',
                r'Accessibility\s+Debug',
                r'Tab\s+External\s+Share',
                r'Sign in',
                r'JavaScript isn\'t enabled.*?Enable and reload',
                r'This browser version is no longer supported.*?upgrade to a supported browser',
            ]
            
            cleaned = content
            for pattern in unwanted_patterns:
                cleaned = re.sub(pattern, '', cleaned, flags=re.IGNORECASE | re.DOTALL)
            
            # 移除多余空白
            cleaned = normalize_text(cleaned)
            
            return cleaned
            
        except Exception as e:
            logger.debug(f"清理Google Docs内容失败: {e}")
            return content
    
    def _fetch_pdf_content(self, url: str) -> Optional[str]:
        """下载PDF并提取文本内容"""
        logger.info(f"开始下载PDF: {url}")
        
        try:
            # 生成缓存文件名
            url_hash = hashlib.md5(url.encode()).hexdigest()
            parsed_url = urlparse(url)
            filename = Path(parsed_url.path).name
            if not filename.endswith('.pdf'):
                filename = f"{url_hash}.pdf"
            else:
                filename = f"{url_hash}_{sanitize_filename(filename)}"
            
            cache_file = PDF_CACHE_DIR / filename
            
            # 记录当前处理的PDF文件路径
            self.current_pdf_file = cache_file
            
            # 检查缓存是否存在
            if cache_file.exists():
                logger.info(f"使用缓存文件: {cache_file}")
                return self._extract_pdf_text(cache_file)
            
            # 下载PDF文件
            response = self._download_with_retry(url, PDF_DOWNLOAD_TIMEOUT)
            if not response:
                return None
            
            # 验证内容类型
            content_type = response.headers.get('content-type', '').lower()
            if 'pdf' not in content_type and not url.lower().endswith('.pdf'):
                logger.warning(f"URL可能不是PDF文件: {content_type}")
            
            # 保存到缓存
            with open(cache_file, 'wb') as f:
                f.write(response.content)
            
            logger.info(f"PDF下载完成: {cache_file}")
            
            # 提取文本
            return self._extract_pdf_text(cache_file)
            
        except Exception as e:
            logger.error(f"PDF处理失败: {e}")
            return None
    
    def _extract_pdf_text(self, pdf_path: Path) -> Optional[str]:
        """从PDF文件提取文本，使用多种方法和候补策略"""
        logger.info(f"开始提取PDF文本: {pdf_path}")
        
        # 方法0：使用 Document AI (优先方法) - 多模态LLM直接提取
        if USE_DOCUMENT_AI:
            logger.info("尝试方法0: Document AI 文本提取（优先）...")
            text = self._extract_with_document_ai(pdf_path)
            if text and len(text) > 0:
                logger.info(f"Document AI 原始提取结果: {len(text)} 字符")
                if self._is_valid_text(text):
                    logger.info("✅ Document AI 提取成功且验证通过")
                    return text
                else:
                    logger.warning("❌ Document AI 提取的文本未通过验证，尝试其他方法")
            else:
                logger.warning("❌ Document AI 未能提取到文本，尝试其他方法")
        
        # 方法1：使用PyMuPDF (fitz) - 主要方法
        logger.info("尝试方法1: PyMuPDF文本提取...")
        text = self._extract_with_pymupdf(pdf_path)
        if text and len(text) > 0:
            logger.info(f"PyMuPDF原始提取结果: {len(text)} 字符")
            if self._is_valid_text(text):
                logger.info("✅ PyMuPDF提取成功且验证通过")
                return text
            else:
                logger.warning("❌ PyMuPDF提取的文本未通过验证")
        else:
            logger.warning("❌ PyMuPDF未能提取到文本")
        
        # 方法2：使用pdfplumber - 候补方法1
        logger.info("尝试方法2: pdfplumber文本提取...")
        text = self._extract_with_pdfplumber(pdf_path)
        if text and len(text) > 0:
            logger.info(f"pdfplumber原始提取结果: {len(text)} 字符")
            if self._is_valid_text(text):
                logger.info("✅ pdfplumber提取成功且验证通过")
                return text
            else:
                logger.warning("❌ pdfplumber提取的文本未通过验证")
        else:
            logger.warning("❌ pdfplumber未能提取到文本")
        
        # 方法3：使用PyPDF2 - 候补方法2
        logger.info("尝试方法3: PyPDF2文本提取...")
        text = self._extract_with_pypdf2(pdf_path)
        if text and len(text) > 0:
            logger.info(f"PyPDF2原始提取结果: {len(text)} 字符")
            if self._is_valid_text(text):
                logger.info("✅ PyPDF2提取成功且验证通过")
                return text
            else:
                logger.warning("❌ PyPDF2提取的文本未通过验证")
        else:
            logger.warning("❌ PyPDF2未能提取到文本")
        
        # 方法4：尝试OCR - 最后的候补方案
        logger.info("尝试方法4: OCR文本识别...")
        text = self._extract_with_ocr(pdf_path)
        if text and len(text) > 0:
            logger.info(f"OCR原始提取结果: {len(text)} 字符")
            if self._is_valid_text(text):
                logger.info("✅ OCR提取成功且验证通过")
                return text
            else:
                logger.warning("❌ OCR提取的文本未通过验证")
        else:
            logger.warning("❌ OCR未能提取到文本")
        
        logger.error("❌ 所有PDF文本提取方法都失败，无法获取有效文本")
        return None
    
    def _extract_with_document_ai(self, pdf_path: Path) -> Optional[str]:
        """使用 Document AI (多模态LLM) 提取PDF文本"""
        try:
            from document_ai import get_document_ai_extractor
            
            extractor = get_document_ai_extractor()
            if not extractor.is_available:
                logger.warning("Document AI 不可用")
                return None
            
            text = extractor.extract_text_from_pdf(pdf_path)
            return text
            
        except ImportError:
            logger.warning("Document AI 模块导入失败")
            return None
        except Exception as e:
            logger.error(f"Document AI 提取失败: {e}")
            return None
    
    def _extract_with_pymupdf(self, pdf_path: Path) -> Optional[str]:
        """使用PyMuPDF提取文本"""
        try:
            doc = fitz.open(pdf_path)
            text_content = []
            
            for page_num in range(len(doc)):
                page = doc.load_page(page_num)
                
                # 尝试不同的文本提取方法
                text_methods = [
                    lambda p: p.get_text(),
                    lambda p: p.get_text("dict"),
                    lambda p: p.get_text("blocks")
                ]
                
                page_text = ""
                for method in text_methods:
                    try:
                        result = method(page)
                        if isinstance(result, str):
                            page_text = result
                        elif isinstance(result, dict):
                            # 从字典格式提取文本
                            page_text = self._extract_text_from_dict(result)
                        elif isinstance(result, list):
                            # 从块格式提取文本
                            page_text = self._extract_text_from_blocks(result)
                        
                        if page_text and page_text.strip():
                            break
                    except Exception as e:
                        logger.debug(f"PyMuPDF方法失败: {e}")
                        continue
                
                if page_text and page_text.strip():
                    text_content.append(page_text)
            
            doc.close()
            
            if text_content:
                full_text = '\n'.join(text_content)
                return self._clean_and_normalize_text(full_text)
            
        except Exception as e:
            logger.debug(f"PyMuPDF提取失败: {e}")
        
        return None
    
    def _extract_with_pdfplumber(self, pdf_path: Path) -> Optional[str]:
        """使用pdfplumber提取文本"""
        try:
            import pdfplumber
            
            text_content = []
            with pdfplumber.open(pdf_path) as pdf:
                for page in pdf.pages:
                    try:
                        # 尝试多种提取方法
                        text = page.extract_text()
                        if not text:
                            # 尝试表格提取
                            tables = page.extract_tables()
                            if tables:
                                table_text = []
                                for table in tables:
                                    for row in table:
                                        if row:
                                            table_text.append(' '.join(str(cell) for cell in row if cell))
                                text = '\n'.join(table_text)
                        
                        if text and text.strip():
                            text_content.append(text)
                    except Exception as e:
                        logger.debug(f"pdfplumber页面提取失败: {e}")
                        continue
            
            if text_content:
                full_text = '\n'.join(text_content)
                return self._clean_and_normalize_text(full_text)
                
        except ImportError:
            logger.debug("pdfplumber未安装，跳过此方法")
        except Exception as e:
            logger.debug(f"pdfplumber提取失败: {e}")
        
        return None
    
    def _extract_with_pypdf2(self, pdf_path: Path) -> Optional[str]:
        """使用PyPDF2提取文本"""
        try:
            import PyPDF2
            
            text_content = []
            with open(pdf_path, 'rb') as file:
                reader = PyPDF2.PdfReader(file)
                
                for page_num in range(len(reader.pages)):
                    try:
                        page = reader.pages[page_num]
                        text = page.extract_text()
                        
                        if text and text.strip():
                            text_content.append(text)
                    except Exception as e:
                        logger.debug(f"PyPDF2页面提取失败: {e}")
                        continue
            
            if text_content:
                full_text = '\n'.join(text_content)
                return self._clean_and_normalize_text(full_text)
                
        except ImportError:
            logger.debug("PyPDF2未安装，跳过此方法")
        except Exception as e:
            logger.debug(f"PyPDF2提取失败: {e}")
        
        return None
    
    def _extract_with_ocr(self, pdf_path: Path) -> Optional[str]:
        """使用OCR提取文本（最后的候补方案）"""
        try:
            import pytesseract
            from PIL import Image
            import fitz  # PyMuPDF用于转换PDF为图像
            
            logger.info("开始OCR处理，这可能需要一些时间...")
            
            doc = fitz.open(pdf_path)
            text_content = []
            
            # 只处理前10页，避免OCR时间过长
            max_pages = min(10, len(doc))
            
            for page_num in range(max_pages):
                try:
                    page = doc.load_page(page_num)
                    
                    # 转换为图像
                    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))  # 提高分辨率
                    img_data = pix.tobytes("ppm")
                    
                    # 使用PIL加载图像
                    img = Image.open(io.BytesIO(img_data))
                    
                    # OCR识别
                    text = pytesseract.image_to_string(img, lang='eng')
                    
                    if text and text.strip():
                        text_content.append(text)
                        
                except Exception as e:
                    logger.debug(f"OCR页面处理失败: {e}")
                    continue
            
            doc.close()
            
            if text_content:
                full_text = '\n'.join(text_content)
                return self._clean_and_normalize_text(full_text)
                
        except ImportError:
            logger.debug("OCR依赖库未安装，跳过OCR方法")
        except Exception as e:
            logger.debug(f"OCR处理失败: {e}")
        
        return None
    
    def _extract_text_from_dict(self, text_dict: dict) -> str:
        """从PyMuPDF字典格式提取文本"""
        text_parts = []
        
        try:
            if 'blocks' in text_dict:
                for block in text_dict['blocks']:
                    if 'lines' in block:
                        for line in block['lines']:
                            if 'spans' in line:
                                line_text = ''
                                for span in line['spans']:
                                    if 'text' in span:
                                        line_text += span['text']
                                if line_text.strip():
                                    text_parts.append(line_text)
        except Exception as e:
            logger.debug(f"字典格式文本提取失败: {e}")
        
        return '\n'.join(text_parts)
    
    def _extract_text_from_blocks(self, blocks: list) -> str:
        """从PyMuPDF块格式提取文本"""
        text_parts = []
        
        try:
            for block in blocks:
                if isinstance(block, tuple) and len(block) >= 5:
                    # 文本块格式: (x0, y0, x1, y1, "text", block_no, block_type)
                    if len(block) >= 7 and block[6] == 0:  # 文本块
                        text = block[4]
                        if text and text.strip():
                            text_parts.append(text)
        except Exception as e:
            logger.debug(f"块格式文本提取失败: {e}")
        
        return '\n'.join(text_parts)
    
    def _is_valid_text(self, text: Optional[str]) -> bool:
        """检查提取的文本是否有效（非乱码）- 基础版本"""
        logger.info("开始验证提取的文本质量...")
        
        if not text or not text.strip():
            logger.warning("文本为空或只包含空白字符")
            return False
        
        # 基本长度检查
        if len(text.strip()) < 50:
            logger.warning(f"文本长度过短: {len(text.strip())} 字符")
            return False
        
        logger.info(f"文本基本信息: 长度 {len(text)} 字符, 行数 {len(text.splitlines())}")
        
        # 1. 检查是否为PDF原始数据/严重乱码
        if self._is_pdf_raw_data(text):
            logger.warning("❌ 检测到PDF原始数据或严重乱码")
            return False
        
        # 2. 基础乱码检测
        if self._is_basic_corrupted_text(text):
            logger.warning("❌ 检测到文本乱码")
            return False
        
        # 3. 基本单词检查
        words = text.lower().split()
        if len(words) < 5:
            logger.warning(f"文本单词数量过少: {len(words)} 个单词")
            return False
        
        logger.info(f"文本包含 {len(words)} 个单词")
        
        # 4. 基本字母比例检查
        alpha_chars = sum(1 for c in text if c.isalpha())
        total_chars = len(text.replace(' ', '').replace('\n', ''))
        
        if total_chars == 0:
            logger.warning("文本不包含任何有效字符")
            return False
        
        alpha_ratio = alpha_chars / total_chars
        logger.info(f"文本字母比例: {alpha_ratio:.2%}")
        
        # 如果字母比例太低，可能是乱码
        if alpha_ratio < 0.2:  # 放宽到20%
            logger.warning(f"❌ 文本字母比例过低: {alpha_ratio:.2%}")
            return False
        
        logger.info(f"✅ 文本验证通过! 长度: {len(text)} 字符, 字母比例: {alpha_ratio:.2%}")
        return True
    
    def _is_pdf_raw_data(self, text: str) -> bool:
        """检查是否为PDF原始数据或严重乱码 - 基础版本"""
        try:
            # 检查PDF文件头标记
            if text.strip().startswith('%PDF-'):
                logger.debug("检测到PDF文件头标记")
                return True
            
            # 检查明显的PDF内部结构标记
            pdf_markers = [
                'endobj', 'endstream', '/Type', '/Catalog', '/Pages', 
                '/MediaBox', '/Contents', 'startxref', '%%EOF', 'trailer'
            ]
            
            marker_count = sum(1 for marker in pdf_markers if marker in text)
            if marker_count >= 3:  # 需要3个或更多标记
                logger.debug(f"检测到{marker_count}个PDF内部标记")
                return True
            
            # 检查高密度的控制字符
            control_chars = sum(1 for c in text if ord(c) < 32 and c not in '\n\r\t ')
            if len(text) > 0 and control_chars / len(text) > 0.1:  # 超过10%的控制字符
                logger.debug(f"控制字符比例过高: {control_chars/len(text):.2f}")
                return True
                
        except Exception as e:
            logger.debug(f"PDF乱码检测异常: {e}")
        
        return False
    
    def _is_basic_corrupted_text(self, text: str) -> bool:
        """基础乱码检测"""
        try:
            # 1. 检查是否包含过多的重复字符
            char_counts = {}
            for char in text:
                if char.isalpha():
                    char_counts[char] = char_counts.get(char, 0) + 1
            
            if char_counts:
                max_char_count = max(char_counts.values())
                total_alpha_chars = sum(char_counts.values())
                if max_char_count / total_alpha_chars > 0.4:  # 40%重复率
                    most_common_char = max(char_counts, key=char_counts.get)
                    logger.debug(f"字符'{most_common_char}'出现过于频繁: {max_char_count/total_alpha_chars:.2%}")
                    return True
            
            # 2. 检查平均单词长度
            words = [word for word in text.split() if word.isalpha()]
            if words and len(words) > 10:
                avg_word_length = sum(len(word) for word in words) / len(words)
                if avg_word_length < 1 or avg_word_length > 25:  # 极端的平均长度
                    logger.debug(f"平均单词长度异常: {avg_word_length:.1f}")
                    return True
            
            # 3. 检查是否包含大量连续特殊字符
            special_char_sequences = len(re.findall(r'[^\w\s]{4,}', text))
            if special_char_sequences > len(text) / 200:  # 相对于文本长度的特殊字符序列
                logger.debug(f"特殊字符序列过多: {special_char_sequences}")
                return True
            
        except Exception as e:
            logger.debug(f"基础乱码检测异常: {e}")
        
        return False
    
    def _clean_and_normalize_text(self, text: str) -> str:
        """清理和标准化文本"""
        if not text:
            return ""
        
        # 修复常见的编码问题
        text = self._fix_encoding_issues(text)
        
        # 移除多余的空白字符
        text = re.sub(r'\s+', ' ', text)
        
        # 移除页码和页眉页脚模式
        text = self._remove_page_artifacts(text)
        
        # 修复断行问题
        text = self._fix_line_breaks(text)
        
        return text.strip()
    
    def _fix_encoding_issues(self, text: str) -> str:
        """修复常见的编码问题"""
        try:
            # 尝试修复常见的UTF-8编码问题
            if isinstance(text, bytes):
                # 尝试多种编码
                encodings = ['utf-8', 'latin-1', 'cp1252', 'iso-8859-1']
                for encoding in encodings:
                    try:
                        text = text.decode(encoding)
                        break
                    except UnicodeDecodeError:
                        continue
            
            # 修复常见的字符替换
            replacements = {
                '\ufeff': '',  # BOM
                '\u00a0': ' ',  # 非断空格
                '\u2013': '-',  # 短横线
                '\u2014': '--',  # 长横线
                '\u201c': '"',  # 左双引号
                '\u201d': '"',  # 右双引号
                '\u2018': "'",  # 左单引号
                '\u2019': "'",  # 右单引号
                '\u2026': '...',  # 省略号
            }
            
            for old, new in replacements.items():
                text = text.replace(old, new)
            
        except Exception as e:
            logger.debug(f"编码修复失败: {e}")
        
        return text
    
    def _remove_page_artifacts(self, text: str) -> str:
        """移除页面相关的干扰内容"""
        # 移除页码模式
        text = re.sub(r'\n\s*\d+\s*\n', '\n', text)
        text = re.sub(r'\nPage \d+\n', '\n', text)
        
        # 移除常见的页眉页脚
        text = re.sub(r'\n\s*www\.[^\n]+\n', '\n', text)
        text = re.sub(r'\n\s*http[^\n]+\n', '\n', text)
        
        return text
    
    def _fix_line_breaks(self, text: str) -> str:
        """修复不合理的断行"""
        # 修复单词中间的断行
        text = re.sub(r'([a-z])-\s*\n\s*([a-z])', r'\1\2', text)
        
        # 合并被错误断开的句子
        text = re.sub(r'([a-z,])\s*\n\s*([a-z])', r'\1 \2', text)
        
        return text
    
    def _fetch_web_content(self, url: str) -> Optional[str]:
        """获取网页文本内容"""
        logger.info(f"开始获取网页内容: {url}")
        
        try:
            response = self._download_with_retry(url, REQUEST_TIMEOUT)
            if not response:
                return None
            
            # 检测编码
            response.encoding = response.apparent_encoding or 'utf-8'
            
            # 解析HTML
            soup = BeautifulSoup(response.text, 'html.parser')
            
            # 移除脚本和样式元素
            for script in soup(["script", "style", "nav", "header", "footer", "aside"]):
                script.decompose()
            
            # 提取文本内容
            text = soup.get_text()
            
            # 清理文本
            lines = (line.strip() for line in text.splitlines())
            chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
            text = ' '.join(chunk for chunk in chunks if chunk)
            
            text = normalize_text(text)
            
            if not text:
                logger.warning("网页中未找到文本内容")
                return None
            
            logger.info(f"网页文本提取完成，长度: {len(text)} 字符")
            return text
            
        except Exception as e:
            logger.error(f"网页内容获取失败: {e}")
            return None
    
    def _is_javascript_heavy_site(self, url: str) -> bool:
        """判断是否为JavaScript-heavy网站"""
        javascript_heavy_domains = [
            'jobbnorge.no',
            'linkedin.com',
            'indeed.com',
            'glassdoor.com',
            'monster.com',
            'careerbuilder.com',
            'ziprecruiter.com'
        ]
        
        for domain in javascript_heavy_domains:
            if domain in url.lower():
                return True
        return False
    
    def _fetch_web_content_with_playwright(self, url: str) -> Optional[str]:
        """使用Playwright获取网页内容（通过独立进程）"""
        if not self.playwright_manager:
            logger.warning("Playwright进程管理器未初始化，回退到基础HTTP请求")
            return self._fetch_web_content(url)
        
        try:
            from config import PLAYWRIGHT_TIMEOUT, PLAYWRIGHT_SCROLL_ENABLED
            
            logger.info(f"使用Playwright独立进程获取网页内容: {url}")
            content = self.playwright_manager.get_page_content(
                url, 
                scroll_enabled=PLAYWRIGHT_SCROLL_ENABLED,
                timeout=PLAYWRIGHT_TIMEOUT
            )
            
            if content and len(content.strip()) > 100:
                logger.info(f"✅ Playwright独立进程获取内容成功，长度: {len(content)} 字符")
                return content
            else:
                logger.warning("Playwright获取的内容过短或为空，回退到基础HTTP请求")
                return self._fetch_web_content(url)
                
        except Exception as e:
            logger.warning(f"Playwright独立进程获取内容失败: {e}，回退到基础HTTP请求")
            return self._fetch_web_content(url)
    
    def _check_content_type(self, url: str) -> Optional[str]:
        """检查URL的Content-Type"""
        try:
            # 发送HEAD请求获取响应头
            response = self.session.head(url, timeout=10, allow_redirects=True)
            content_type = response.headers.get('content-type', '')
            logger.info(f"检测到Content-Type: {content_type}")
            return content_type
        except Exception as e:
            logger.debug(f"无法获取Content-Type: {e}")
            return None
    
    def _is_likely_pdf_content(self, content: str) -> bool:
        """检查内容是否疑似PDF乱码"""
        if not content or len(content) < 50:
            return False
        
        # 检查是否包含PDF标记
        pdf_indicators = [
            '%PDF-', 'endobj', 'stream', 'endstream', '/Type', '/Catalog',
            'Binary data follows', 'Font substitution', 'Invalid encoding',
            '/MediaBox', '/Contents', '/XObject', '/ProcSet'
        ]
        
        # 检查前2000字符（增加范围）
        check_content = content[:2000]
        indicator_count = sum(1 for indicator in pdf_indicators if indicator in check_content)
        
        # 如果发现PDF指示符，可能是PDF乱码
        if indicator_count >= 1:  # 降低阈值
            logger.info(f"在内容中检测到 {indicator_count} 个PDF指示符")
            return True
        
        # 检查是否以PDF文件头开始
        if content.strip().startswith('%PDF-'):
            logger.info("检测到PDF文件头标记")
            return True
        
        # 检查字符分布是否异常（类似PDF乱码）
        if len(content) > 200:
            # 计算控制字符比例
            sample_size = min(1000, len(content))
            sample_content = content[:sample_size]
            control_chars = sum(1 for c in sample_content if ord(c) < 32 and c not in '\n\r\t ')
            control_ratio = control_chars / sample_size
            
            if control_ratio > 0.05:  # 降低阈值到5%
                logger.info(f"检测到异常的控制字符比例: {control_ratio:.2%}")
                return True
            
            # 检查非ASCII字符比例
            non_ascii_chars = sum(1 for c in sample_content if ord(c) > 127)
            non_ascii_ratio = non_ascii_chars / sample_size
            
            if non_ascii_ratio > 0.3:  # 超过30%非ASCII字符
                logger.info(f"检测到高比例非ASCII字符: {non_ascii_ratio:.2%}")
                return True
        
        return False
    
    def _is_unavailable_content(self, content: str) -> bool:
        """
        检测内容是否不可用（如"暂不支持您的浏览器"、"无法下载"等）
        
        Args:
            content: 页面内容
            
        Returns:
            bool: 如果内容不可用返回True
        """
        if not content or len(content.strip()) < 50:
            return True
        
        # 检测常见的"不可用"提示文本
        unavailable_patterns = [
            '暂不支持您的浏览器',
            '推荐您下载',
            '无法下载',
            '无法打印',
            '不支持下载',
            '不支持打印',
            'download not supported',
            'print not supported',
            'browser not supported',
            'unsupported browser',
            'please download',
            'recommended download',
            '体验更流畅',
            '立即下载',
            'access denied',
            'access restricted',
            'content unavailable'
        ]
        
        content_lower = content.lower()
        for pattern in unavailable_patterns:
            if pattern.lower() in content_lower:
                logger.info(f"检测到不可用内容提示: {pattern}")
                return True
        
        # 检查内容是否过短（可能是错误页面）
        if len(content.strip()) < 200:
            # 检查是否包含常见错误页面关键词
            error_keywords = ['error', '404', '403', '500', 'not found', 'forbidden']
            if any(keyword in content_lower for keyword in error_keywords):
                logger.info("检测到错误页面")
                return True
        
        return False
    
    def _fetch_content_with_screenshot_ocr(self, url: str) -> Optional[str]:
        """
        使用截图OCR获取内容（fallback方法）
        
        Args:
            url: 要访问的URL
            
        Returns:
            str: 提取的文本内容，失败返回None
        """
        if not self.screenshot_ocr_fetcher:
            logger.warning("截图OCR提取器未初始化")
            return None
        
        if not self.playwright_manager:
            logger.warning("Playwright管理器未初始化，无法截图")
            return None
        
        try:
            from config import USE_SCREENSHOT_OCR
            if not USE_SCREENSHOT_OCR:
                logger.info("截图OCR已在配置中禁用")
                return None
            
            logger.info(f"开始使用截图OCR获取内容: {url}")
            
            # 1. 使用Playwright捕获截图
            screenshot_paths = self.playwright_manager.capture_screenshots(url)
            
            if not screenshot_paths:
                logger.error("截图捕获失败")
                return None
            
            logger.info(f"成功捕获 {len(screenshot_paths)} 张截图")
            
            # 2. 使用OCR提取文本
            ocr_text = self.screenshot_ocr_fetcher.extract_text_from_screenshots(screenshot_paths)
            
            if not ocr_text:
                logger.error("OCR文本提取失败")
                return None
            
            # 3. 验证OCR结果质量
            if not self.screenshot_ocr_fetcher.validate_ocr_quality(ocr_text):
                logger.warning("OCR结果质量验证未通过，但继续使用")
                # 不强制要求，因为有些内容可能确实质量不高
            
            logger.info(f"✅ 截图OCR成功提取文本，长度: {len(ocr_text)} 字符")
            return ocr_text
            
        except Exception as e:
            logger.error(f"截图OCR处理失败: {e}")
            return None
    
    def _download_with_retry(self, url: str, timeout: int) -> Optional[requests.Response]:
        """带重试的下载功能"""
        for attempt in range(MAX_RETRIES):
            try:
                logger.info(f"尝试下载 (第{attempt + 1}次): {url}")
                
                response = self.session.get(
                    url, 
                    timeout=timeout,
                    stream=True,
                    allow_redirects=True
                )
                
                response.raise_for_status()
                return response
                
            except requests.exceptions.Timeout:
                logger.warning(f"下载超时 (第{attempt + 1}次): {url}")
            except requests.exceptions.ConnectionError:
                logger.warning(f"连接错误 (第{attempt + 1}次): {url}")
            except requests.exceptions.HTTPError as e:
                logger.warning(f"HTTP错误 (第{attempt + 1}次): {e}")
            except Exception as e:
                logger.warning(f"下载失败 (第{attempt + 1}次): {e}")
            
            if attempt < MAX_RETRIES - 1:
                wait_time = 2 ** attempt  # 指数退避
                logger.info(f"等待 {wait_time} 秒后重试...")
                time.sleep(wait_time)
        
        logger.error(f"下载失败，已达到最大重试次数: {url}")
        return None
    
    def clear_cache(self, max_age_days: int = 7):
        """清理过期的缓存文件"""
        try:
            current_time = time.time()
            max_age_seconds = max_age_days * 24 * 3600
            
            deleted_count = 0
            for cache_file in PDF_CACHE_DIR.glob("*.pdf"):
                file_age = current_time - cache_file.stat().st_mtime
                if file_age > max_age_seconds:
                    cache_file.unlink()
                    deleted_count += 1
                    logger.info(f"删除过期缓存文件: {cache_file}")
            
            if deleted_count > 0:
                logger.info(f"清理了 {deleted_count} 个过期缓存文件")
            else:
                logger.info("没有过期的缓存文件需要清理")
                
        except Exception as e:
            logger.error(f"清理缓存失败: {e}")

    def get_cache_info(self) -> dict:
        """获取缓存信息"""
        try:
            cache_files = list(PDF_CACHE_DIR.glob("*.pdf"))
            total_size = sum(f.stat().st_size for f in cache_files)
            
            return {
                'file_count': len(cache_files),
                'total_size_mb': round(total_size / 1024 / 1024, 2),
                'cache_dir': str(PDF_CACHE_DIR)
            }
        except Exception as e:
            logger.error(f"获取缓存信息失败: {e}")
            return {}

    def delete_current_pdf(self):
        """删除当前处理的PDF文件"""
        if self.current_pdf_file and self.current_pdf_file.exists():
            try:
                self.current_pdf_file.unlink()
                logger.info(f"已删除PDF文件: {self.current_pdf_file}")
                self.current_pdf_file = None
            except Exception as e:
                logger.error(f"删除PDF文件失败: {e}")
        else:
            logger.debug("没有需要删除的PDF文件")

    def get_current_pdf_path(self) -> Optional[Path]:
        """获取当前处理的PDF文件路径"""
        return self.current_pdf_file

def test_content_fetcher():
    """测试内容获取功能"""
    fetcher = ContentFetcher()
    
    # 测试URL列表
    test_urls = [
        "https://example.com",
        "https://arxiv.org/pdf/2301.00001.pdf"  # 示例PDF
    ]
    
    for url in test_urls:
        logger.info(f"\n测试URL: {url}")
        content = fetcher.fetch_content(url)
        if content:
            logger.info(f"成功获取内容，长度: {len(content)} 字符")
            logger.info(f"内容预览: {content[:200]}...")
        else:
            logger.error("获取内容失败")

def test_core_corruption_detection():
    """核心PDF乱码检测测试"""
    fetcher = ContentFetcher()
    
    # 测试案例：重点关注核心问题
    test_cases = [
        (
            "原始PDF乱码", 
            """%PDF-1.7 %     1 0 obj <>/Metadata 52 0 R/ViewerPreferences 53 0 R>> endobj 2 0 obj <> endobj 3 0 obj <>/ExtGState<>/XObject<>/ProcSet[/PDF/Text/ImageB/ImageC/ImageI] >>/MediaBox[ 0 0 595.25 842] /Contents 4 0 R/Group<>/Tabs/S/StructParents 0>> endobj 4 0 obj <> stream x    rܸ ]U  <SE  fkSk  R     >Dy    9 fd    89&* Ʈ  D7}wc   uӒ  z} $W 5 L     _~! ^_   gI    *IB : YN   ~~~  _H{~      [JhF>-  (   - "e^Ō O|л %Y> 9  *y    ߓ  ȧ         (  >h (  *  'm   v  .  7x    l F  y (  Q d      = F   KD   6> ( 8  T  x) P6XJ  5 VB pț ׄ""", 
            False
        ),
        (
            "正常学术文本", 
            """PhD Position in Remote Sensing and Machine Learning
University of Cambridge, United Kingdom

We are seeking a highly motivated PhD student to work on satellite-based forest monitoring using deep learning techniques. The project involves developing novel algorithms for analyzing time-series satellite imagery to detect deforestation patterns.

Requirements:
- Master's degree in Computer Science, Geography, or related field
- Experience with machine learning and remote sensing
- Programming skills in Python

Application deadline: April 30, 2024
Duration: 3.5 years
Positions available: 1

Contact: Prof. Sarah Johnson
Email: s.johnson@cam.ac.uk""", 
            True
        ),
        (
            "轻微空格乱码", 
            """PhD P osition in R emote S ensing and M achine L earning
Uni versity of C ambridge, U nited K ingdom

We are se eking a h ighly m otivated PhD st udent to w ork on s atellite-based f orest m onitoring us ing d eep l earning t echniques.""", 
            True
        ),
        (
            "PDF标记混合乱码", 
            """some random text endobj more random /MediaBox text /Contents stream some content endstream more random endobj text""", 
            False
        )
    ]
    
    logger.info("开始核心PDF乱码检测测试...")
    logger.info("=" * 50)
    
    passed = 0
    total = len(test_cases)
    
    for name, text, expected_valid in test_cases:
        logger.info(f"\n测试: {name}")
        logger.info(f"预期: {'有效' if expected_valid else '无效'}")
        
        is_valid = fetcher._is_valid_text(text)
        result = "有效" if is_valid else "无效"
        status = "✅" if is_valid == expected_valid else "❌"
        
        logger.info(f"结果: {result} {status}")
        
        if is_valid == expected_valid:
            passed += 1
        else:
            # 详细分析失败原因
            logger.info("失败分析:")
            is_pdf_raw = fetcher._is_pdf_raw_data(text)
            is_corrupted = fetcher._is_basic_corrupted_text(text)
            
            logger.info(f"  PDF原始数据: {is_pdf_raw}")
            logger.info(f"  乱码检测: {is_corrupted}")
    
    logger.info(f"\n结果: {passed}/{total} 通过")
    return passed == total

def test_pdf_extraction_fallback():
    """测试PDF提取候补机制"""
    class MockContentFetcher(ContentFetcher):
        def __init__(self):
            super().__init__()
            self.method_call_count = 0
            
        def _extract_with_pymupdf(self, pdf_path):
            self.method_call_count += 1
            logger.info("模拟PyMuPDF提取返回乱码")
            return "%PDF-1.7 corrupted binary data endobj stream random chars"
            
        def _extract_with_pdfplumber(self, pdf_path):
            self.method_call_count += 1
            logger.info("模拟pdfplumber提取返回正常文本")
            return """PhD Position in Machine Learning
            University of Oxford
            We are seeking a motivated PhD student for machine learning research."""
            
        def _extract_with_pypdf2(self, pdf_path):
            self.method_call_count += 1
            logger.info("模拟PyPDF2提取（应该不会被调用）")
            return "Should not reach here"
    
    logger.info("\n" + "=" * 60)
    logger.info("测试PDF提取候补机制...")
    
    mock_fetcher = MockContentFetcher()
    
    # 创建一个假的PDF路径用于测试
    from pathlib import Path
    fake_pdf_path = Path("test.pdf")
    
    # 模拟PDF文本提取流程
    result = mock_fetcher._extract_pdf_text(fake_pdf_path)
    
    logger.info(f"\n最终提取结果: {result[:100] if result else 'None'}...")
    logger.info(f"调用方法次数: {mock_fetcher.method_call_count}")
    
    if result and mock_fetcher.method_call_count == 2:
        logger.info("✅ 候补机制测试通过：检测到乱码后成功切换到候补方法")
    else:
        logger.info("❌ 候补机制测试失败")

def test_real_pdf_failures():
    """测试真实的PDF转换失败场景"""
    fetcher = ContentFetcher()
    
    # 真实PDF转换失败的示例
    real_failure_cases = [
        # 1. 加密PDF导致的乱码
        """6 0 obj
<< /Type /Page /Parent 3 0 R /Resources 5 0 R /MediaBox [0 0 612 792]
/Filter [/FlateDecode] /Length 1234 >>
stream
xn0yCr$@AHm{ՙ!&rM^R7}}~}q
q%]ZE{}NmfѻܮNp{=o~
""",
        # 2. 扫描PDF的OCR失败结果  
        """I I I l l I I l l l I
I l l l l l I I I I l
l I I I I l l l l I I
I I l l l I I I I I l
""",
        # 3. 字体嵌入问题导致的乱码
        """ToUnicode CMap $$$ Invalid encoding $$$ 
Font substitution occurred for font TimesNewRomanPSMT
Text rendering failed for characters: 
""",
        # 4. 版本不兼容导致的结构性乱码
        """startxref
12345
%%EOF
trailer
<< /Size 10 /Root 1 0 R >>
Something went wrong during PDF parsing.
Binary data follows: 
""",
    ]
    
    logger.info("测试真实PDF转换失败场景...")
    all_detected = True
    
    for i, corrupted_text in enumerate(real_failure_cases, 1):
        logger.info(f"\n真实失败案例 {i}:")
        is_valid = fetcher._is_valid_text(corrupted_text)
        result = "有效" if is_valid else "无效(乱码)"
        
        if is_valid:
            logger.error(f"案例{i} 未被检测为乱码！ - {result}")
            all_detected = False
        else:
            logger.info(f"案例{i} 正确检测为乱码 ✅")
    
    return all_detected

if __name__ == "__main__":
    # 设置日志
    logging.basicConfig(level=logging.INFO)
    
    # 运行测试
    if len(sys.argv) > 1 and sys.argv[1] == "test_corruption":
        test_core_corruption_detection()
        test_pdf_extraction_fallback()
        test_real_pdf_failures()
    else:
        test_content_fetcher() 