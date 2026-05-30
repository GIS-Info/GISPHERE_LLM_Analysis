"""
Google Docs / Google Drive 专用抓取流程。

从 fetch_text.py 抽出。这些函数与 ContentFetcher 的实例状态强耦合
（session / playwright_manager / current_pdf_file / _fetch_pdf_content 等），
因此统一以 fetcher 作为第一个参数，行为与原实例方法完全一致。
"""
import hashlib
import logging
import re
from urllib.parse import urljoin
from typing import Optional

from bs4 import BeautifulSoup

from ..core.config import PDF_CACHE_DIR, REQUEST_TIMEOUT, PDF_DOWNLOAD_TIMEOUT
from ..core.utils import (
    normalize_text,
    convert_google_drive_to_download,
    convert_google_docs_to_export,
    extract_google_docs_document_id,
)

logger = logging.getLogger(__name__)


def handle_google_drive_url(fetcher, url: str) -> Optional[str]:
    """处理Google Drive链接，尝试下载PDF或提取文本"""
    logger.info(f"处理Google Drive链接: {url}")

    try:
        logger.info("方法1: 尝试转换为直接下载链接...")
        download_url = convert_google_drive_to_download(url)

        if download_url:
            logger.info(f"转换后的下载链接: {download_url}")

            pdf_content = fetcher._fetch_pdf_content(download_url)
            if pdf_content:
                logger.info("✅ 通过直接下载链接成功获取PDF内容")
                return pdf_content

            logger.info("直接下载失败，尝试处理病毒扫描警告...")
            pdf_content = handle_google_drive_virus_scan(fetcher, download_url)
            if pdf_content:
                logger.info("✅ 通过处理病毒扫描警告成功获取PDF内容")
                return pdf_content

        logger.info("方法2: 使用Playwright从PDF预览器中提取文本...")
        if fetcher.playwright_manager:
            playwright_content = fetch_google_drive_with_playwright(fetcher, url)
            if playwright_content:
                logger.info("✅ 通过Playwright成功获取内容")
                return playwright_content

        logger.info("方法3: 使用截图OCR作为最后的fallback...")
        screenshot_content = fetcher._fetch_content_with_screenshot_ocr(url)
        if screenshot_content:
            logger.info("✅ 通过截图OCR成功获取内容")
            return screenshot_content

        logger.error("所有Google Drive处理方法都失败")
        return None

    except Exception as e:
        logger.error(f"处理Google Drive链接失败: {e}")
        if fetcher.screenshot_ocr_fetcher and fetcher.playwright_manager:
            logger.info("尝试使用截图OCR作为异常恢复...")
            screenshot_content = fetcher._fetch_content_with_screenshot_ocr(url)
            if screenshot_content:
                return screenshot_content
        return None


def handle_google_drive_virus_scan(fetcher, download_url: str) -> Optional[str]:
    """处理Google Drive的病毒扫描警告页面"""
    try:
        response = fetcher._download_with_retry(download_url, PDF_DOWNLOAD_TIMEOUT)
        if not response:
            return None

        content_type = response.headers.get('content-type', '').lower()

        if 'pdf' in content_type:
            url_hash = hashlib.md5(download_url.encode()).hexdigest()
            cache_file = PDF_CACHE_DIR / f"{url_hash}_gdrive.pdf"
            fetcher.current_pdf_file = cache_file

            with open(cache_file, 'wb') as f:
                f.write(response.content)

            return fetcher._extract_pdf_text(cache_file)

        if 'html' in content_type:
            soup = BeautifulSoup(response.text, 'html.parser')

            confirm_link = None
            for link in soup.find_all('a', href=True):
                href = link.get('href', '')
                if 'export=download' in href and 'confirm=' in href:
                    confirm_link = href
                    break

            if confirm_link:
                if confirm_link.startswith('/'):
                    confirm_link = urljoin(download_url, confirm_link)

                logger.info(f"找到确认链接，再次请求: {confirm_link}")
                confirm_response = fetcher._download_with_retry(confirm_link, PDF_DOWNLOAD_TIMEOUT)

                if confirm_response:
                    content_type = confirm_response.headers.get('content-type', '').lower()
                    if 'pdf' in content_type:
                        url_hash = hashlib.md5(download_url.encode()).hexdigest()
                        cache_file = PDF_CACHE_DIR / f"{url_hash}_gdrive.pdf"
                        fetcher.current_pdf_file = cache_file

                        with open(cache_file, 'wb') as f:
                            f.write(confirm_response.content)

                        return fetcher._extract_pdf_text(cache_file)

            form = soup.find('form', {'id': 'download-form'})
            if form:
                action = form.get('action', '')
                if action:
                    if action.startswith('/'):
                        action = urljoin(download_url, action)

                    form_data = {}
                    for input_tag in form.find_all('input'):
                        name = input_tag.get('name')
                        value = input_tag.get('value', '')
                        if name:
                            form_data[name] = value

                    if form_data:
                        logger.info(f"找到表单，提交确认: {action}")
                        form_response = fetcher.session.post(action, data=form_data, timeout=PDF_DOWNLOAD_TIMEOUT)

                        if form_response.status_code == 200:
                            content_type = form_response.headers.get('content-type', '').lower()
                            if 'pdf' in content_type:
                                url_hash = hashlib.md5(download_url.encode()).hexdigest()
                                cache_file = PDF_CACHE_DIR / f"{url_hash}_gdrive.pdf"
                                fetcher.current_pdf_file = cache_file

                                with open(cache_file, 'wb') as f:
                                    f.write(form_response.content)

                                return fetcher._extract_pdf_text(cache_file)

    except Exception as e:
        logger.debug(f"处理病毒扫描警告失败: {e}")

    return None


def fetch_google_drive_with_playwright(fetcher, url: str) -> Optional[str]:
    """使用Playwright从Google Drive PDF预览器中提取文本"""
    if not fetcher.playwright_manager:
        return None

    try:
        from ..core.config import PLAYWRIGHT_TIMEOUT  # noqa: F401

        logger.info(f"使用Playwright处理Google Drive链接: {url}")

        content = fetcher.playwright_manager.get_page_content(
            url,
            scroll_enabled=False,  # Google Drive PDF预览器不需要滚动
            timeout=PLAYWRIGHT_TIMEOUT
        )

        if not content:
            return None

        if len(content.strip()) < 200:
            logger.warning("Playwright获取的内容过短，可能未成功加载PDF预览器")
            return None

        placeholder_texts = ['Loading…', 'Sign in', 'Unable to preview', 'Access denied']
        if any(placeholder in content for placeholder in placeholder_texts):
            if len(content.strip()) < 500:
                logger.warning("检测到Google Drive占位文本，PDF预览器可能未成功加载")
                return None

        logger.info(f"✅ Playwright获取到内容，长度: {len(content)} 字符")
        return content

    except Exception as e:
        logger.warning(f"使用Playwright处理Google Drive链接失败: {e}")
        return None


def handle_google_docs_url(fetcher, url: str) -> Optional[str]:
    """处理Google Docs链接，尝试导出文本或PDF"""
    logger.info(f"处理Google Docs链接: {url}")

    try:
        logger.info("方法1: 尝试导出为文本格式...")
        txt_export_url = convert_google_docs_to_export(url, format='txt')

        if txt_export_url:
            logger.info(f"文本导出链接: {txt_export_url}")
            txt_content = fetch_google_docs_export(fetcher, txt_export_url, format='txt')
            if txt_content:
                logger.info("✅ 通过文本导出成功获取内容")
                return txt_content

        logger.info("方法2: 尝试导出为PDF格式...")
        pdf_export_url = convert_google_docs_to_export(url, format='pdf')

        if pdf_export_url:
            logger.info(f"PDF导出链接: {pdf_export_url}")
            pdf_content = fetch_google_docs_export(fetcher, pdf_export_url, format='pdf')
            if pdf_content:
                logger.info("✅ 通过PDF导出成功获取内容")
                return pdf_content

        logger.info("方法3: 使用Playwright从文档编辑器中提取文本...")
        if fetcher.playwright_manager:
            playwright_content = fetch_google_docs_with_playwright(fetcher, url)
            if playwright_content:
                logger.info("✅ 通过Playwright成功获取内容")
                return playwright_content

        logger.info("方法4: 使用截图OCR作为最后的fallback...")
        screenshot_content = fetcher._fetch_content_with_screenshot_ocr(url)
        if screenshot_content:
            logger.info("✅ 通过截图OCR成功获取内容")
            return screenshot_content

        logger.error("所有Google Docs处理方法都失败")
        return None

    except Exception as e:
        logger.error(f"处理Google Docs链接失败: {e}")
        if fetcher.screenshot_ocr_fetcher and fetcher.playwright_manager:
            logger.info("尝试使用截图OCR作为异常恢复...")
            screenshot_content = fetcher._fetch_content_with_screenshot_ocr(url)
            if screenshot_content:
                return screenshot_content
        return None


def fetch_google_docs_export(fetcher, export_url: str, format: str = 'txt') -> Optional[str]:
    """获取Google Docs导出内容"""
    try:
        response = fetcher._download_with_retry(export_url, REQUEST_TIMEOUT)
        if not response:
            return None

        if response.status_code != 200:
            logger.warning(f"Google Docs导出失败，状态码: {response.status_code}")
            return None

        content_type = response.headers.get('content-type', '').lower()

        if format == 'txt':
            if 'text/plain' in content_type or 'text/html' in content_type:
                text = response.text
                text = normalize_text(text)
                if text and len(text.strip()) > 50:
                    logger.info(f"✅ 成功获取文本内容，长度: {len(text)} 字符")
                    return text
            else:
                try:
                    text = response.text
                    text = normalize_text(text)
                    if text and len(text.strip()) > 50:
                        logger.info(f"✅ 成功获取文本内容（忽略Content-Type），长度: {len(text)} 字符")
                        return text
                except Exception:
                    pass

        elif format == 'pdf':
            if 'pdf' in content_type or export_url.endswith('format=pdf'):
                document_id = extract_google_docs_document_id(export_url) or 'gdocs'
                url_hash = hashlib.md5(export_url.encode()).hexdigest()
                cache_file = PDF_CACHE_DIR / f"{url_hash}_{document_id}.pdf"
                fetcher.current_pdf_file = cache_file

                with open(cache_file, 'wb') as f:
                    f.write(response.content)

                pdf_text = fetcher._extract_pdf_text(cache_file)
                if pdf_text:
                    logger.info(f"✅ 成功从PDF导出提取文本，长度: {len(pdf_text)} 字符")
                    return pdf_text

        logger.warning(f"Google Docs导出格式 {format} 处理失败")
        return None

    except Exception as e:
        logger.error(f"获取Google Docs导出内容失败: {e}")
        return None


def fetch_google_docs_with_playwright(fetcher, url: str) -> Optional[str]:
    """使用Playwright从Google Docs编辑器中提取文本"""
    if not fetcher.playwright_manager:
        return None

    try:
        from ..core.config import PLAYWRIGHT_TIMEOUT  # noqa: F401

        logger.info(f"使用Playwright处理Google Docs链接: {url}")

        content = fetcher.playwright_manager.get_page_content(
            url,
            scroll_enabled=True,  # Google Docs可能需要滚动加载完整内容
            timeout=PLAYWRIGHT_TIMEOUT
        )

        if not content:
            return None

        placeholder_texts = [
            'JavaScript isn\'t enabled',
            'Enable and reload',
            'Sign in',
            'Unable to preview',
            'Access denied',
            'This browser version is no longer supported'
        ]

        placeholder_count = sum(1 for placeholder in placeholder_texts if placeholder in content)
        if placeholder_count >= 2 and len(content.strip()) < 500:
            logger.warning("检测到Google Docs占位文本，文档可能未成功加载")
            return None

        cleaned_content = clean_google_docs_content(content)

        if cleaned_content and len(cleaned_content.strip()) > 200:
            logger.info(f"✅ Playwright获取到内容，长度: {len(cleaned_content)} 字符")
            return cleaned_content
        else:
            logger.warning("Playwright获取的内容过短或为空")
            return None

    except Exception as e:
        logger.warning(f"使用Playwright处理Google Docs链接失败: {e}")
        return None


def clean_google_docs_content(content: str) -> str:
    """清理Google Docs内容，移除导航栏、工具栏等无关文本"""
    try:
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

        cleaned = normalize_text(cleaned)

        return cleaned

    except Exception as e:
        logger.debug(f"清理Google Docs内容失败: {e}")
        return content
