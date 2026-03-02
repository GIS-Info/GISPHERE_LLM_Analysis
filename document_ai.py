"""
Document AI 模块 - 使用多模态LLM进行文档/图片文字提取
优先于传统OCR方案，提供更高质量的文字提取
"""
import base64
import logging
from pathlib import Path
from typing import Optional, List, Union
import io

from config import (
    DOCUMENT_AI_MODEL, 
    DOCUMENT_AI_TIMEOUT, 
    DOCUMENT_AI_MAX_PAGES,
    OPENAI_BASE_URL,
    check_openai_key
)

logger = logging.getLogger(__name__)


class DocumentAIExtractor:
    """使用多模态LLM从文档/图片中提取文字"""
    
    # 文字提取的系统提示词
    EXTRACTION_SYSTEM_PROMPT = """You are a document text extraction specialist. Your task is to extract all text content from the provided document or image accurately and completely."""
    
    # 文字提取的用户提示词
    EXTRACTION_USER_PROMPT = """Please extract ALL text content from this document/image.

Requirements:
1. Preserve the original structure and paragraphs as much as possible
2. Do NOT add any analysis, interpretation, or commentary
3. Do NOT summarize or paraphrase - extract the exact text
4. If there are tables, preserve the table structure using plain text formatting
5. If text is unclear or partially visible, indicate with [unclear] or [partial]
6. Output ONLY the extracted raw text, nothing else

Extract the text now:"""

    def __init__(self):
        self._client = None
        self._available = self._initialize()
    
    def _initialize(self) -> bool:
        """初始化 Document AI 客户端"""
        try:
            api_key = check_openai_key()
            if not api_key:
                logger.warning("Document AI: No API key found")
                return False
            
            import openai
            self._client = openai.OpenAI(
                api_key=api_key,
                base_url=OPENAI_BASE_URL
            )
            logger.info(f"✅ Document AI initialized with model: {DOCUMENT_AI_MODEL}")
            return True
            
        except ImportError:
            logger.error("Document AI: openai package not installed")
            return False
        except Exception as e:
            logger.error(f"Document AI initialization failed: {e}")
            return False
    
    @property
    def is_available(self) -> bool:
        """检查 Document AI 是否可用"""
        return self._available
    
    def extract_text_from_image(self, image_path: Union[str, Path]) -> Optional[str]:
        """
        从单张图片中提取文字
        
        Args:
            image_path: 图片文件路径
            
        Returns:
            str: 提取的文字内容，失败返回 None
        """
        if not self._available:
            logger.warning("Document AI not available")
            return None
        
        image_path = Path(image_path)
        if not image_path.exists():
            logger.error(f"Image file not found: {image_path}")
            return None
        
        try:
            # 读取图片并转换为 base64
            base64_image = self._encode_image_to_base64(image_path)
            if not base64_image:
                return None
            
            # 确定图片类型
            media_type = self._get_media_type(image_path)
            
            logger.info(f"Extracting text from image: {image_path.name}")
            
            # 调用多模态API
            response = self._client.chat.completions.create(
                model=DOCUMENT_AI_MODEL,
                messages=[
                    {
                        "role": "system",
                        "content": self.EXTRACTION_SYSTEM_PROMPT
                    },
                    {
                        "role": "user",
                        "content": [
                            {
                                "type": "text",
                                "text": self.EXTRACTION_USER_PROMPT
                            },
                            {
                                "type": "image_url",
                                "image_url": {
                                    "url": f"data:{media_type};base64,{base64_image}"
                                }
                            }
                        ]
                    }
                ],
                max_tokens=4096,
                temperature=0.1,
                timeout=DOCUMENT_AI_TIMEOUT
            )
            
            extracted_text = response.choices[0].message.content
            
            if extracted_text and extracted_text.strip():
                logger.info(f"✅ Document AI extracted {len(extracted_text)} characters from image")
                return extracted_text.strip()
            else:
                logger.warning("Document AI returned empty response")
                return None
                
        except Exception as e:
            logger.error(f"Document AI image extraction failed: {e}")
            return None
    
    def extract_text_from_images(self, image_paths: List[Union[str, Path]]) -> Optional[str]:
        """
        从多张图片中提取文字并合并
        
        Args:
            image_paths: 图片文件路径列表
            
        Returns:
            str: 合并后的文字内容，失败返回 None
        """
        if not self._available:
            logger.warning("Document AI not available")
            return None
        
        if not image_paths:
            logger.error("No image paths provided")
            return None
        
        # 限制处理的图片数量
        if len(image_paths) > DOCUMENT_AI_MAX_PAGES:
            logger.warning(f"Too many images ({len(image_paths)}), limiting to {DOCUMENT_AI_MAX_PAGES}")
            image_paths = image_paths[:DOCUMENT_AI_MAX_PAGES]
        
        all_texts = []
        
        for i, image_path in enumerate(image_paths, 1):
            logger.info(f"Processing image {i}/{len(image_paths)}: {Path(image_path).name}")
            
            text = self.extract_text_from_image(image_path)
            if text:
                all_texts.append(f"--- Page {i} ---\n{text}")
            else:
                logger.warning(f"Failed to extract text from image {i}")
        
        if all_texts:
            merged_text = "\n\n".join(all_texts)
            logger.info(f"✅ Document AI extracted text from {len(all_texts)}/{len(image_paths)} images, total {len(merged_text)} characters")
            return merged_text
        else:
            logger.error("Failed to extract text from any image")
            return None
    
    def extract_text_from_pdf(self, pdf_path: Union[str, Path]) -> Optional[str]:
        """
        从PDF文件中提取文字（通过转换为图片）
        
        Args:
            pdf_path: PDF文件路径
            
        Returns:
            str: 提取的文字内容，失败返回 None
        """
        if not self._available:
            logger.warning("Document AI not available")
            return None
        
        pdf_path = Path(pdf_path)
        if not pdf_path.exists():
            logger.error(f"PDF file not found: {pdf_path}")
            return None
        
        try:
            import fitz  # PyMuPDF
            
            logger.info(f"Converting PDF to images for Document AI: {pdf_path.name}")
            
            doc = fitz.open(pdf_path)
            total_pages = len(doc)
            pages_to_process = min(total_pages, DOCUMENT_AI_MAX_PAGES)
            
            logger.info(f"PDF has {total_pages} pages, processing {pages_to_process}")
            
            all_texts = []
            
            for page_num in range(pages_to_process):
                try:
                    page = doc.load_page(page_num)
                    
                    # 转换为图片 (使用2倍分辨率提高清晰度)
                    pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
                    img_data = pix.tobytes("png")
                    
                    # 转换为 base64
                    base64_image = base64.b64encode(img_data).decode('utf-8')
                    
                    logger.info(f"Processing PDF page {page_num + 1}/{pages_to_process}")
                    
                    # 调用多模态API
                    response = self._client.chat.completions.create(
                        model=DOCUMENT_AI_MODEL,
                        messages=[
                            {
                                "role": "system",
                                "content": self.EXTRACTION_SYSTEM_PROMPT
                            },
                            {
                                "role": "user",
                                "content": [
                                    {
                                        "type": "text",
                                        "text": self.EXTRACTION_USER_PROMPT
                                    },
                                    {
                                        "type": "image_url",
                                        "image_url": {
                                            "url": f"data:image/png;base64,{base64_image}"
                                        }
                                    }
                                ]
                            }
                        ],
                        max_tokens=4096,
                        temperature=0.1,
                        timeout=DOCUMENT_AI_TIMEOUT
                    )
                    
                    page_text = response.choices[0].message.content
                    
                    if page_text and page_text.strip():
                        all_texts.append(f"--- Page {page_num + 1} ---\n{page_text.strip()}")
                        logger.info(f"✅ Page {page_num + 1}: extracted {len(page_text)} characters")
                    else:
                        logger.warning(f"Page {page_num + 1}: no text extracted")
                        
                except Exception as e:
                    logger.error(f"Failed to process PDF page {page_num + 1}: {e}")
                    continue
            
            doc.close()
            
            if all_texts:
                merged_text = "\n\n".join(all_texts)
                logger.info(f"✅ Document AI extracted text from {len(all_texts)}/{pages_to_process} pages, total {len(merged_text)} characters")
                return merged_text
            else:
                logger.error("Failed to extract text from any PDF page")
                return None
                
        except ImportError:
            logger.error("PyMuPDF (fitz) not installed, cannot process PDF")
            return None
        except Exception as e:
            logger.error(f"Document AI PDF extraction failed: {e}")
            return None
    
    def _encode_image_to_base64(self, image_path: Path) -> Optional[str]:
        """将图片文件编码为 base64"""
        try:
            with open(image_path, "rb") as f:
                return base64.b64encode(f.read()).decode('utf-8')
        except Exception as e:
            logger.error(f"Failed to encode image to base64: {e}")
            return None
    
    def _get_media_type(self, image_path: Path) -> str:
        """根据文件扩展名获取媒体类型"""
        suffix = image_path.suffix.lower()
        media_types = {
            '.png': 'image/png',
            '.jpg': 'image/jpeg',
            '.jpeg': 'image/jpeg',
            '.gif': 'image/gif',
            '.webp': 'image/webp',
            '.bmp': 'image/bmp'
        }
        return media_types.get(suffix, 'image/png')


# 全局单例
_document_ai_extractor = None


def get_document_ai_extractor() -> DocumentAIExtractor:
    """获取 Document AI 提取器单例"""
    global _document_ai_extractor
    if _document_ai_extractor is None:
        _document_ai_extractor = DocumentAIExtractor()
    return _document_ai_extractor


def test_document_ai():
    """测试 Document AI 功能"""
    import logging
    logging.basicConfig(level=logging.INFO)
    
    extractor = get_document_ai_extractor()
    
    if not extractor.is_available:
        logger.error("Document AI is not available")
        return
    
    logger.info("Document AI is available and ready")
    
    # 测试图片提取 (如果有测试图片)
    test_image = Path("test_images/final_test.png")
    if test_image.exists():
        logger.info(f"Testing with image: {test_image}")
        text = extractor.extract_text_from_image(test_image)
        if text:
            logger.info(f"Extracted text preview: {text[:500]}...")
        else:
            logger.error("Failed to extract text from test image")


if __name__ == "__main__":
    test_document_ai()
