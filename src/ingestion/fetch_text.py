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
from dataclasses import dataclass, field
from typing import Optional, List, Tuple
import sys

from ..core.config import PDF_CACHE_DIR, REQUEST_TIMEOUT, PDF_DOWNLOAD_TIMEOUT, MAX_RETRIES, USE_DOCUMENT_AI
from ..core.utils import (is_pdf_url, sanitize_filename, normalize_text, 
                   is_google_drive_url, convert_google_drive_to_download, extract_google_drive_file_id,
                   is_google_docs_url, convert_google_docs_to_export, extract_google_docs_document_id)
from . import text_quality
from . import pdf_extractor
from . import google_sources

logger = logging.getLogger(__name__)


@dataclass
class FetchAttemptResult:
    """单次抓取尝试的结果，用于统一评估不同抓取方式的质量。"""
    method: str
    content: Optional[str]
    score: int
    quality: str
    flags: List[str] = field(default_factory=list)
    reason: str = ""

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
            from ..core.config import USE_PLAYWRIGHT
            if USE_PLAYWRIGHT:
                from ..browser.playwright_process_manager import get_playwright_manager
                self.playwright_manager = get_playwright_manager()
                logger.info("✅ Playwright进程管理器初始化成功（独立进程，无异步冲突）")
            else:
                logger.info("Playwright已在配置中禁用，将使用基础HTTP请求")
        except Exception as e:
            logger.warning(f"Playwright进程管理器初始化失败，将使用基础HTTP请求: {e}")
        
        # 存储当前处理的PDF文件路径，用于后续清理
        self.current_pdf_file = None
        self.last_fetch_summary = {}
        
        # 初始化截图OCR提取器
        self.screenshot_ocr_fetcher = None
        try:
            from ..core.config import USE_SCREENSHOT_OCR
            if USE_SCREENSHOT_OCR:
                from .screenshot_ocr_fetcher import ScreenshotOCRFetcher
                self.screenshot_ocr_fetcher = ScreenshotOCRFetcher()
                logger.info("✅ 截图OCR提取器初始化成功")
            else:
                logger.info("截图OCR已在配置中禁用")
        except Exception as e:
            logger.warning(f"截图OCR提取器初始化失败: {e}")

    def _reset_last_fetch_summary(self, url: str):
        """重置最近一次抓取摘要。"""
        self.last_fetch_summary = {
            "url": url,
            "content_type": None,
            "selected_method": None,
            "selected_quality": None,
            "selected_score": None,
            "selected_reason": "",
            "attempts": [],
            "final_status": "started"
        }

    def _record_fetch_attempt(self, attempt: FetchAttemptResult):
        """记录一次抓取尝试，供主流程输出摘要日志。"""
        self.last_fetch_summary.setdefault("attempts", []).append({
            "method": attempt.method,
            "quality": attempt.quality,
            "score": attempt.score,
            "flags": list(attempt.flags),
            "reason": attempt.reason,
            "content_length": len(attempt.content) if attempt.content else 0
        })

    def _finalize_fetch_summary(
        self,
        selected_method: Optional[str],
        final_status: str,
        selected_quality: Optional[str] = None,
        selected_score: Optional[int] = None,
        selected_reason: str = ""
    ):
        """记录最终抓取结果。"""
        self.last_fetch_summary["selected_method"] = selected_method
        self.last_fetch_summary["selected_quality"] = selected_quality
        self.last_fetch_summary["selected_score"] = selected_score
        self.last_fetch_summary["selected_reason"] = selected_reason
        self.last_fetch_summary["final_status"] = final_status

    def get_last_fetch_summary(self) -> dict:
        """返回最近一次抓取摘要。"""
        return dict(self.last_fetch_summary)
    
    def fetch_content(self, url: str) -> Optional[str]:
        """根据URL类型获取内容"""
        if not url:
            logger.error("URL为空")
            return None
        
        logger.info(f"开始获取内容: {url}")
        self._reset_last_fetch_summary(url)
        
        try:
            # 优先处理Google Docs链接
            if is_google_docs_url(url):
                logger.info("检测到Google Docs链接，使用特殊处理流程")
                content = self._handle_google_docs_url(url)
                self._finalize_fetch_summary(
                    "google_docs",
                    "success" if content else "failed",
                    "special",
                    len(content) if content else 0,
                    "Google Docs 专用流程"
                )
                return content
            
            # 优先处理Google Drive链接
            if is_google_drive_url(url):
                logger.info("检测到Google Drive链接，使用特殊处理流程")
                content = self._handle_google_drive_url(url)
                self._finalize_fetch_summary(
                    "google_drive",
                    "success" if content else "failed",
                    "special",
                    len(content) if content else 0,
                    "Google Drive 专用流程"
                )
                return content
            
            # 首先尝试基于URL判断
            if is_pdf_url(url):
                logger.info("根据URL判断为PDF文件，使用PDF处理流程")
                content = self._fetch_pdf_content(url)
                self._finalize_fetch_summary(
                    "pdf",
                    "success" if content else "failed",
                    "special",
                    len(content) if content else 0,
                    "PDF 专用流程"
                )
                return content
            else:
                # 对于不确定的URL，先尝试获取响应头来判断
                logger.info("URL类型不明确，检查内容类型...")
                content_type = self._check_content_type(url)
                self.last_fetch_summary["content_type"] = content_type
                
                if content_type and 'pdf' in content_type.lower():
                    logger.info(f"根据Content-Type判断为PDF: {content_type}")
                    content = self._fetch_pdf_content(url)
                    self._finalize_fetch_summary(
                        "pdf",
                        "success" if content else "failed",
                        "special",
                        len(content) if content else 0,
                        f"根据 Content-Type 判定为 PDF: {content_type}"
                    )
                    return content
                else:
                    logger.info(f"根据Content-Type判断为网页: {content_type}")
                    return self._fetch_web_content_with_strategy(url)
        except Exception as e:
            logger.error(f"获取内容失败: {e}")
            # 异常情况下也尝试截图OCR
            if self.screenshot_ocr_fetcher and self.playwright_manager:
                logger.info("尝试使用截图OCR作为最后的fallback...")
                screenshot_content = self._fetch_content_with_screenshot_ocr(url)
                if screenshot_content:
                    logger.info("✅ 通过截图OCR成功获取内容（异常恢复）")
                    self._finalize_fetch_summary(
                        "ocr",
                        "recovered_from_exception",
                        "fallback",
                        len(screenshot_content),
                        "异常恢复后使用截图 OCR 成功"
                    )
                    return screenshot_content
            self._finalize_fetch_summary(None, "exception", None, None, str(e))
            return None

    def _get_web_fetch_strategy(self, url: str) -> List[str]:
        """
        返回网页抓取策略链。

        当前统一从 HTTP 开始，未来可以在这里按域名/历史结果调整优先级，
        为后续方案 C 的“最优抓取方式记忆”预留扩展点。
        """
        strategy = ["http"]

        if self.playwright_manager:
            strategy.append("playwright")

        if self.screenshot_ocr_fetcher and self.playwright_manager:
            strategy.append("ocr")

        logger.info(f"网页抓取策略: {strategy} ({url})")
        return strategy

    def _fetch_web_content_with_strategy(self, url: str) -> Optional[str]:
        """按统一策略链抓取网页内容，并基于内容质量逐级升级抓取方式。"""
        attempts: List[FetchAttemptResult] = []

        for method in self._get_web_fetch_strategy(url):
            attempt = self._run_web_fetch_attempt(url, method)
            attempts.append(attempt)
            self._record_fetch_attempt(attempt)

            logger.info(
                f"抓取尝试[{method}] 质量={attempt.quality}, 分数={attempt.score}, "
                f"标记={attempt.flags if attempt.flags else ['ok']}, 原因={attempt.reason}"
            )

            if "pdf_like_content" in attempt.flags:
                logger.warning("⚠️  检测到网页内容疑似PDF乱码，切换到PDF处理流程")
                pdf_content = self._fetch_pdf_content(url)
                self._finalize_fetch_summary(
                    "pdf",
                    "success" if pdf_content else "failed",
                    "special",
                    len(pdf_content) if pdf_content else 0,
                    "网页内容疑似 PDF 乱码，转为 PDF 处理"
                )
                return pdf_content

            if "challenge_page" in attempt.flags and method == "playwright":
                logger.error("❌ Playwright 仍被验证页阻塞，停止继续升级到 OCR")
                break

            if attempt.quality == "good":
                logger.info(f"✅ 使用 {method} 获取到高质量网页内容")
                self._finalize_fetch_summary(
                    method,
                    "success",
                    attempt.quality,
                    attempt.score,
                    attempt.reason
                )
                return attempt.content

            if attempt.quality == "weak":
                logger.warning(f"⚠️  {method} 获取到的内容质量一般，尝试更强的抓取方式")
            else:
                logger.warning(f"⚠️  {method} 获取到的内容质量较差，继续升级抓取方式")

        best_attempt = self._select_best_web_attempt(attempts)
        if best_attempt and best_attempt.content and best_attempt.score >= 35:
            logger.warning(
                f"未获取到高质量网页内容，返回最佳候选: 方法={best_attempt.method}, "
                f"质量={best_attempt.quality}, 分数={best_attempt.score}"
            )
            self._finalize_fetch_summary(
                best_attempt.method,
                "best_effort",
                best_attempt.quality,
                best_attempt.score,
                best_attempt.reason
            )
            return best_attempt.content

        logger.error("❌ 所有网页抓取方式都未获取到可用内容")
        self._finalize_fetch_summary(None, "failed", None, None, "所有抓取方式均未通过质量门槛")
        return None

    def _run_web_fetch_attempt(self, url: str, method: str) -> FetchAttemptResult:
        """执行单次网页抓取尝试并产出统一结果。"""
        if method == "http":
            content = self._fetch_web_content(url)
        elif method == "playwright":
            content = self._fetch_web_content_with_playwright(url)
        elif method == "ocr":
            content = self._fetch_content_with_screenshot_ocr(url)
        else:
            logger.warning(f"未知抓取方式: {method}")
            content = None

        score, quality, flags, reason = self._evaluate_web_content_quality(content)
        return FetchAttemptResult(
            method=method,
            content=content,
            score=score,
            quality=quality,
            flags=flags,
            reason=reason
        )

    def _select_best_web_attempt(self, attempts: List[FetchAttemptResult]) -> Optional[FetchAttemptResult]:
        """从多次抓取尝试中选择质量最好的结果。"""
        valid_attempts = [attempt for attempt in attempts if attempt.content]
        if not valid_attempts:
            return None
        return max(valid_attempts, key=lambda attempt: attempt.score)

    def _evaluate_web_content_quality(self, content: Optional[str]) -> Tuple[int, str, List[str], str]:
        """统一评估网页内容质量（委托 text_quality）。"""
        return text_quality.evaluate_web_content_quality(content)
    
    def _handle_google_drive_url(self, url: str) -> Optional[str]:
        """处理Google Drive链接（委托 google_sources）。"""
        return google_sources.handle_google_drive_url(self, url)
    
    def _handle_google_drive_virus_scan(self, download_url: str) -> Optional[str]:
        """处理Google Drive病毒扫描警告页（委托 google_sources）。"""
        return google_sources.handle_google_drive_virus_scan(self, download_url)
    
    def _fetch_google_drive_with_playwright(self, url: str) -> Optional[str]:
        """Playwright 提取 Google Drive PDF 预览（委托 google_sources）。"""
        return google_sources.fetch_google_drive_with_playwright(self, url)
    
    def _handle_google_docs_url(self, url: str) -> Optional[str]:
        """处理Google Docs链接（委托 google_sources）。"""
        return google_sources.handle_google_docs_url(self, url)
    
    def _fetch_google_docs_export(self, export_url: str, format: str = 'txt') -> Optional[str]:
        """获取Google Docs导出内容（委托 google_sources）。"""
        return google_sources.fetch_google_docs_export(self, export_url, format)
    
    def _fetch_google_docs_with_playwright(self, url: str) -> Optional[str]:
        """Playwright 提取 Google Docs 编辑器内容（委托 google_sources）。"""
        return google_sources.fetch_google_docs_with_playwright(self, url)
    
    def _clean_google_docs_content(self, content: str) -> str:
        """清理Google Docs内容（委托 google_sources）。"""
        return google_sources.clean_google_docs_content(content)
    
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
        """使用 Document AI (多模态LLM) 提取PDF文本（委托 pdf_extractor）。"""
        return pdf_extractor.extract_with_document_ai(pdf_path)
    
    def _extract_with_pymupdf(self, pdf_path: Path) -> Optional[str]:
        """使用PyMuPDF提取文本（委托 pdf_extractor）。"""
        return pdf_extractor.extract_with_pymupdf(pdf_path)
    
    def _extract_with_pdfplumber(self, pdf_path: Path) -> Optional[str]:
        """使用pdfplumber提取文本（委托 pdf_extractor）。"""
        return pdf_extractor.extract_with_pdfplumber(pdf_path)
    
    def _extract_with_pypdf2(self, pdf_path: Path) -> Optional[str]:
        """使用PyPDF2提取文本（委托 pdf_extractor）。"""
        return pdf_extractor.extract_with_pypdf2(pdf_path)
    
    def _extract_with_ocr(self, pdf_path: Path) -> Optional[str]:
        """使用OCR提取文本（委托 pdf_extractor）。"""
        return pdf_extractor.extract_with_ocr(pdf_path)
    
    def _extract_text_from_dict(self, text_dict: dict) -> str:
        """从PyMuPDF字典格式提取文本（委托 text_quality）。"""
        return text_quality.extract_text_from_dict(text_dict)
    
    def _extract_text_from_blocks(self, blocks: list) -> str:
        """从PyMuPDF块格式提取文本（委托 text_quality）。"""
        return text_quality.extract_text_from_blocks(blocks)
    
    def _is_valid_text(self, text: Optional[str]) -> bool:
        """检查提取的文本是否有效（委托 text_quality）。"""
        return text_quality.is_valid_text(text)
    
    def _is_pdf_raw_data(self, text: str) -> bool:
        """检查是否为PDF原始数据或严重乱码（委托 text_quality）。"""
        return text_quality.is_pdf_raw_data(text)
    
    def _is_basic_corrupted_text(self, text: str) -> bool:
        """基础乱码检测（委托 text_quality）。"""
        return text_quality.is_basic_corrupted_text(text)
    
    def _clean_and_normalize_text(self, text: str) -> str:
        """清理和标准化文本（委托 text_quality）。"""
        return text_quality.clean_and_normalize_text(text)
    
    def _fix_encoding_issues(self, text: str) -> str:
        """修复常见的编码问题（委托 text_quality）。"""
        return text_quality.fix_encoding_issues(text)
    
    def _remove_page_artifacts(self, text: str) -> str:
        """移除页面相关的干扰内容（委托 text_quality）。"""
        return text_quality.remove_page_artifacts(text)
    
    def _fix_line_breaks(self, text: str) -> str:
        """修复不合理的断行（委托 text_quality）。"""
        return text_quality.fix_line_breaks(text)
    
    def _extract_main_text_safe(self, html: Optional[str], rendered_text: Optional[str]) -> Optional[str]:
        """调用智能正文抽取（多候选打分择优 + 评论剥离），失败时返回 None 由上层兜底。"""
        if not html and not rendered_text:
            return None
        try:
            from .content_extractor import extract_main_text
            result = extract_main_text(html, rendered_text=rendered_text)
            if result and result.text and result.text.strip():
                logger.info(
                    f"智能正文抽取: 方法={result.method}, 分数={result.score:.0f}, "
                    f"长度={result.length}"
                )
                return result.text
        except Exception as e:
            logger.warning(f"智能正文抽取失败，回退基础提取: {e}")
        return None

    def _legacy_soup_text(self, html: str) -> Optional[str]:
        """基础 BeautifulSoup 纯文本提取（智能抽取失败时的兜底）。"""
        try:
            soup = BeautifulSoup(html, 'html.parser')
            for script in soup(["script", "style", "nav", "header", "footer", "aside"]):
                script.decompose()
            text = soup.get_text()
            lines = (line.strip() for line in text.splitlines())
            chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
            text = ' '.join(chunk for chunk in chunks if chunk)
            return normalize_text(text) or None
        except Exception as e:
            logger.error(f"基础文本提取失败: {e}")
            return None

    def _fetch_web_content(self, url: str) -> Optional[str]:
        """获取网页文本内容（HTTP 路）：原始 HTML 走智能抽取，失败回退基础提取。"""
        logger.info(f"开始获取网页内容: {url}")
        
        try:
            response = self._download_with_retry(url, REQUEST_TIMEOUT)
            if not response:
                return None
            
            # 检测编码
            response.encoding = response.apparent_encoding or 'utf-8'
            html = response.text
            
            # 优先：多候选打分择优抽取（json-ld / trafilatura / og / soup）
            text = self._extract_main_text_safe(html, None)
            
            # 兜底：基础 BeautifulSoup 提取
            if not text:
                text = self._legacy_soup_text(html)
            
            if not text:
                logger.warning("网页中未找到文本内容")
                return None
            
            logger.info(f"网页文本提取完成，长度: {len(text)} 字符")
            return text
            
        except Exception as e:
            logger.error(f"网页内容获取失败: {e}")
            return None
    
    def _fetch_web_content_with_playwright(self, url: str) -> Optional[str]:
        """使用Playwright获取网页内容（通过独立进程）。"""
        if not self.playwright_manager:
            logger.warning("Playwright进程管理器未初始化")
            return None
        
        try:
            from ..core.config import PLAYWRIGHT_TIMEOUT, PLAYWRIGHT_SCROLL_ENABLED
            
            logger.info(f"使用Playwright独立进程获取网页内容: {url}")
            payload = self.playwright_manager.get_page_payload(
                url, 
                scroll_enabled=PLAYWRIGHT_SCROLL_ENABLED,
                timeout=PLAYWRIGHT_TIMEOUT
            )
            
            if not payload:
                logger.warning("Playwright未返回任何内容")
                return None

            html = payload.get('html')
            inner_text = payload.get('inner_text')
            worker_text = payload.get('content')

            # 优先：基于原始 HTML + 渲染 innerText 的多候选打分择优抽取
            content = self._extract_main_text_safe(html, inner_text)

            # 兜底：worker 自带的纯文本
            if not content:
                content = worker_text
            
            if content and len(content.strip()) > 100:
                logger.info(f"✅ Playwright独立进程获取内容成功，长度: {len(content)} 字符")
                return content
            else:
                logger.warning("Playwright获取的内容过短或为空")
                return None
                
        except Exception as e:
            logger.warning(f"Playwright独立进程获取内容失败: {e}")
            return None
    
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
        """检查内容是否疑似PDF乱码（委托 text_quality）。"""
        return text_quality.is_likely_pdf_content(content)
    
    def _is_unavailable_content(self, content: str) -> bool:
        """检测内容是否不可用（委托 text_quality）。"""
        return text_quality.is_unavailable_content(content)
    
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
            from ..core.config import USE_SCREENSHOT_OCR
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



