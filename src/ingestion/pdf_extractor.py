"""
PDF 文本提取后端（纯函数集合）。

从 fetch_text.py 抽出的五路提取实现，均接收 pdf_path、返回文本或 None：
- extract_with_document_ai：多模态 LLM（优先）
- extract_with_pymupdf：PyMuPDF
- extract_with_pdfplumber：pdfplumber
- extract_with_pypdf2：PyPDF2
- extract_with_ocr：Tesseract OCR（兜底）

注：提取的"编排+校验"仍保留在 ContentFetcher._extract_pdf_text（以兼容测试中对实例方法的覆写）。
"""
import io
import logging
from pathlib import Path
from typing import Optional

import fitz  # PyMuPDF

from ..core.config import USE_DOCUMENT_AI  # noqa: F401  (保留以表明依赖配置开关)
from . import text_quality

logger = logging.getLogger(__name__)


def extract_with_document_ai(pdf_path: Path) -> Optional[str]:
    """使用 Document AI (多模态LLM) 提取PDF文本"""
    try:
        from .document_ai import get_document_ai_extractor

        extractor = get_document_ai_extractor()
        if not extractor.is_available:
            logger.warning("Document AI 不可用")
            return None

        return extractor.extract_text_from_pdf(pdf_path)

    except ImportError:
        logger.warning("Document AI 模块导入失败")
        return None
    except Exception as e:
        logger.error(f"Document AI 提取失败: {e}")
        return None


def extract_with_pymupdf(pdf_path: Path) -> Optional[str]:
    """使用PyMuPDF提取文本"""
    try:
        doc = fitz.open(pdf_path)
        text_content = []

        for page_num in range(len(doc)):
            page = doc.load_page(page_num)

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
                        page_text = text_quality.extract_text_from_dict(result)
                    elif isinstance(result, list):
                        page_text = text_quality.extract_text_from_blocks(result)

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
            return text_quality.clean_and_normalize_text(full_text)

    except Exception as e:
        logger.debug(f"PyMuPDF提取失败: {e}")

    return None


def extract_with_pdfplumber(pdf_path: Path) -> Optional[str]:
    """使用pdfplumber提取文本"""
    try:
        import pdfplumber

        text_content = []
        with pdfplumber.open(pdf_path) as pdf:
            for page in pdf.pages:
                try:
                    text = page.extract_text()
                    if not text:
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
            return text_quality.clean_and_normalize_text(full_text)

    except ImportError:
        logger.debug("pdfplumber未安装，跳过此方法")
    except Exception as e:
        logger.debug(f"pdfplumber提取失败: {e}")

    return None


def extract_with_pypdf2(pdf_path: Path) -> Optional[str]:
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
            return text_quality.clean_and_normalize_text(full_text)

    except ImportError:
        logger.debug("PyPDF2未安装，跳过此方法")
    except Exception as e:
        logger.debug(f"PyPDF2提取失败: {e}")

    return None


def extract_with_ocr(pdf_path: Path) -> Optional[str]:
    """使用OCR提取文本（最后的候补方案）"""
    try:
        import pytesseract
        from PIL import Image

        logger.info("开始OCR处理，这可能需要一些时间...")

        doc = fitz.open(pdf_path)
        text_content = []

        # 只处理前10页，避免OCR时间过长
        max_pages = min(10, len(doc))

        for page_num in range(max_pages):
            try:
                page = doc.load_page(page_num)

                pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))  # 提高分辨率
                img_data = pix.tobytes("ppm")

                img = Image.open(io.BytesIO(img_data))

                text = pytesseract.image_to_string(img, lang='eng')

                if text and text.strip():
                    text_content.append(text)

            except Exception as e:
                logger.debug(f"OCR页面处理失败: {e}")
                continue

        doc.close()

        if text_content:
            full_text = '\n'.join(text_content)
            return text_quality.clean_and_normalize_text(full_text)

    except ImportError:
        logger.debug("OCR依赖库未安装，跳过OCR方法")
    except Exception as e:
        logger.debug(f"OCR处理失败: {e}")

    return None
