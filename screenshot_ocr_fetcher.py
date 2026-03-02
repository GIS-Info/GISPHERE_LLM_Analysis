"""
截图OCR提取器 - 从截图图像中提取文本内容
支持 Document AI (优先) 和 Tesseract OCR (降级) 两种方案
"""
import logging
from pathlib import Path
from typing import Optional, List
import io

from config import USE_DOCUMENT_AI

logger = logging.getLogger(__name__)

class ScreenshotOCRFetcher:
    """从截图图像中提取文本的提取器，优先使用 Document AI，降级使用 OCR"""
    
    def __init__(self):
        self._ocr_available = self._check_ocr_availability()
        self._document_ai_available = self._check_document_ai_availability()
        
        if self._document_ai_available:
            logger.info("✅ Document AI 可用，将优先使用")
        elif self._ocr_available:
            logger.info("✅ Tesseract OCR 可用，将作为文字提取方案")
        else:
            logger.warning("⚠️ Document AI 和 OCR 都不可用")
    
    def _check_document_ai_availability(self) -> bool:
        """检查 Document AI 是否可用"""
        if not USE_DOCUMENT_AI:
            logger.info("Document AI 已在配置中禁用")
            return False
        
        try:
            from document_ai import get_document_ai_extractor
            extractor = get_document_ai_extractor()
            return extractor.is_available
        except ImportError:
            logger.warning("Document AI 模块导入失败")
            return False
        except Exception as e:
            logger.warning(f"Document AI 检查失败: {e}")
            return False
    
    def _check_ocr_availability(self) -> bool:
        """检查OCR依赖是否可用"""
        try:
            import pytesseract
            from PIL import Image
            import os
            import platform
            
            # Windows系统：尝试自动查找并设置Tesseract路径
            if platform.system() == 'Windows':
                possible_paths = [
                    r'C:\Program Files\Tesseract-OCR\tesseract.exe',
                    r'C:\Program Files (x86)\Tesseract-OCR\tesseract.exe',
                    r'C:\Tesseract-OCR\tesseract.exe',
                ]
                
                for path in possible_paths:
                    if os.path.exists(path):
                        pytesseract.pytesseract.tesseract_cmd = path
                        logger.info(f"✅ 已设置Tesseract路径: {path}")
                        break
            
            # 检查Tesseract是否已安装
            try:
                # 尝试获取Tesseract版本
                version = pytesseract.get_tesseract_version()
                logger.info(f"✅ Tesseract OCR 已安装，版本: {version}")
                return True
            except pytesseract.TesseractNotFoundError:
                logger.error("❌ Tesseract OCR 未安装或不在 PATH 中")
                logger.error("请安装 Tesseract OCR:")
                logger.error("  Windows: 下载并安装 https://github.com/UB-Mannheim/tesseract/wiki")
                logger.error("           安装后添加到系统 PATH，或设置 TESSDATA_PREFIX 环境变量")
                logger.error("  Linux: sudo apt-get install tesseract-ocr tesseract-ocr-chi-sim")
                logger.error("  macOS: brew install tesseract tesseract-lang")
                return False
            except Exception as e:
                logger.warning(f"Tesseract 检查失败: {e}")
                return False
                
        except ImportError as e:
            logger.error(f"❌ OCR依赖库未安装: {e}")
            logger.error("请运行: pip install pytesseract Pillow")
            return False
    
    def extract_text_from_screenshots(self, screenshot_paths: List[str]) -> Optional[str]:
        """
        从截图列表中提取文本
        优先使用 Document AI，失败则降级使用 Tesseract OCR
        
        Args:
            screenshot_paths: 截图文件路径列表
            
        Returns:
            str: 提取的文本内容，失败返回None
        """
        if not screenshot_paths:
            logger.error("截图路径列表为空")
            return None
        
        # 方法1：优先使用 Document AI
        if self._document_ai_available:
            logger.info(f"尝试使用 Document AI 处理 {len(screenshot_paths)} 张截图...")
            text = self._extract_with_document_ai(screenshot_paths)
            if text and text.strip():
                logger.info(f"✅ Document AI 提取成功，共 {len(text)} 字符")
                # 清理截图文件
                self._cleanup_screenshots(screenshot_paths)
                return text
            else:
                logger.warning("Document AI 提取失败，尝试降级使用 OCR...")
        
        # 方法2：降级使用 Tesseract OCR
        if not self._ocr_available:
            logger.error("OCR功能不可用，无法提取文本")
            return None
        
        return self._extract_with_tesseract_ocr(screenshot_paths)
    
    def _extract_with_document_ai(self, screenshot_paths: List[str]) -> Optional[str]:
        """使用 Document AI 从截图中提取文本"""
        try:
            from document_ai import get_document_ai_extractor
            
            extractor = get_document_ai_extractor()
            if not extractor.is_available:
                return None
            
            text = extractor.extract_text_from_images(screenshot_paths)
            return text
            
        except Exception as e:
            logger.error(f"Document AI 提取异常: {e}")
            return None
    
    def _extract_with_tesseract_ocr(self, screenshot_paths: List[str]) -> Optional[str]:
        """使用 Tesseract OCR 从截图中提取文本"""
        try:
            import pytesseract
            from PIL import Image
            from config import OCR_LANGUAGE, SCREENSHOT_CLEANUP_AFTER_USE
            import os
            import platform
            
            # Windows系统：确保Tesseract路径已设置
            if platform.system() == 'Windows':
                possible_paths = [
                    r'C:\Program Files\Tesseract-OCR\tesseract.exe',
                    r'C:\Program Files (x86)\Tesseract-OCR\tesseract.exe',
                    r'C:\Tesseract-OCR\tesseract.exe',
                ]
                
                for path in possible_paths:
                    if os.path.exists(path):
                        pytesseract.pytesseract.tesseract_cmd = path
                        break
            
            logger.info(f"开始OCR处理 {len(screenshot_paths)} 张截图...")
            
            all_texts = []
            
            for i, screenshot_path in enumerate(screenshot_paths, 1):
                try:
                    screenshot_file = Path(screenshot_path)
                    if not screenshot_file.exists():
                        logger.warning(f"截图文件不存在: {screenshot_path}")
                        continue
                    
                    logger.info(f"处理第 {i}/{len(screenshot_paths)} 张截图: {screenshot_file.name}")
                    
                    # 加载图像
                    img = Image.open(screenshot_file)
                    
                    # 图像预处理
                    processed_img = self._preprocess_image(img)
                    
                    # OCR识别 - 使用更好的配置
                    # PSM 3 = 全自动页面分割，无OSD（最适合文档）
                    # PSM 6 = 假设统一文本块
                    # PSM 11 = 稀疏文本，尽可能多地查找文字
                    custom_config = r'--oem 3 --psm 3'
                    
                    text = pytesseract.image_to_string(
                        processed_img,
                        lang=OCR_LANGUAGE,
                        config=custom_config
                    )
                    
                    if text and text.strip():
                        all_texts.append(text.strip())
                        logger.info(f"第 {i} 张截图识别成功，提取 {len(text.strip())} 字符")
                    else:
                        logger.warning(f"第 {i} 张截图未识别到文本")
                    
                    # 如果配置了自动清理，删除截图文件
                    if SCREENSHOT_CLEANUP_AFTER_USE:
                        try:
                            screenshot_file.unlink()
                            logger.debug(f"已删除截图文件: {screenshot_file.name}")
                        except Exception as e:
                            logger.warning(f"删除截图文件失败: {e}")
                    
                except Exception as e:
                    logger.error(f"处理截图 {screenshot_path} 失败: {e}")
                    continue
            
            if all_texts:
                # 合并所有文本
                full_text = '\n\n'.join(all_texts)
                # 清理和标准化文本
                cleaned_text = self._clean_ocr_text(full_text)
                
                logger.info(f"OCR处理完成，共提取 {len(cleaned_text)} 字符")
                return cleaned_text
            else:
                logger.error("所有截图OCR处理失败，未提取到任何文本")
                return None
                
        except Exception as e:
            logger.error(f"OCR处理过程发生异常: {e}")
            return None
    
    def _cleanup_screenshots(self, screenshot_paths: List[str]) -> None:
        """清理截图文件"""
        from config import SCREENSHOT_CLEANUP_AFTER_USE
        
        if not SCREENSHOT_CLEANUP_AFTER_USE:
            return
        
        for screenshot_path in screenshot_paths:
            try:
                screenshot_file = Path(screenshot_path)
                if screenshot_file.exists():
                    screenshot_file.unlink()
                    logger.debug(f"已删除截图文件: {screenshot_file.name}")
            except Exception as e:
                logger.warning(f"删除截图文件失败: {e}")
    
    def _preprocess_image(self, img):
        """
        图像预处理，提高OCR识别率
        
        Args:
            img: PIL Image对象
            
        Returns:
            Image.Image: 处理后的图像
        """
        try:
            from PIL import Image, ImageEnhance, ImageFilter
            import numpy as np
            
            # 转换为RGB模式（如果不是）
            if img.mode != 'RGB':
                img = img.convert('RGB')
            
            # 转换为灰度图
            img = img.convert('L')
            
            # 增强对比度（更激进）
            enhancer = ImageEnhance.Contrast(img)
            img = enhancer.enhance(2.0)  # 从1.5增加到2.0
            
            # 增强亮度（确保文字清晰）
            enhancer = ImageEnhance.Brightness(img)
            img = enhancer.enhance(1.1)
            
            # 增强锐度
            enhancer = ImageEnhance.Sharpness(img)
            img = enhancer.enhance(1.5)  # 从1.2增加到1.5
            
            # 二值化处理（将图像转为黑白，提高文字识别率）
            # 使用自适应阈值
            try:
                import cv2
                img_array = np.array(img)
                # 使用自适应阈值二值化
                img_array = cv2.adaptiveThreshold(
                    img_array, 255, cv2.ADAPTIVE_THRESH_GAUSSIAN_C, 
                    cv2.THRESH_BINARY, 11, 2
                )
                img = Image.fromarray(img_array)
                logger.debug("应用了自适应阈值二值化")
            except ImportError:
                logger.debug("opencv-python未安装，跳过二值化处理")
            except Exception as e:
                logger.debug(f"二值化处理失败: {e}")
            
            return img
            
        except Exception as e:
            logger.warning(f"图像预处理失败，使用原图: {e}")
            return img
    
    def _clean_ocr_text(self, text: str) -> str:
        """
        清理OCR识别的文本
        
        Args:
            text: 原始OCR文本
            
        Returns:
            str: 清理后的文本
        """
        if not text:
            return ""
        
        # 移除多余空白
        import re
        from utils import normalize_text
        
        # 定义要过滤的UI元素关键词（常见的PDF查看器/编辑器UI文本）
        ui_keywords = [
            'view only', 'scroll', 'rotate', 'edit', 'split', 'merge', 'extract text',
            'pdf to word', 'pdf to image', 'ai podcast', 'all translate', 'adjust page',
            'file compress', 'shortcut tools', 'print', 'thumbnail', 'outline',
            'zoom in', 'zoom out', 'next page', 'previous page', 'download', 'share',
            'annotation', 'highlight', 'comment', 'save', 'export', 'upload'
        ]
        
        # 移除包含UI关键词的行
        lines = text.split('\n')
        filtered_lines = []
        for line in lines:
            line_lower = line.lower().strip()
            # 检查是否包含UI关键词
            is_ui_text = any(keyword in line_lower for keyword in ui_keywords)
            # 检查是否是单个图标字符或很短的UI文本
            is_short_ui = len(line_lower) <= 2 or (len(line_lower) <= 5 and not any(c.isdigit() for c in line))
            
            if not is_ui_text and not is_short_ui:
                filtered_lines.append(line)
        
        text = '\n'.join(filtered_lines)
        
        # 移除OCR常见的错误字符
        text = re.sub(r'[^\w\s\u4e00-\u9fff.,;:!?()\[\]{}\-—–\'"\/@#$%&*+=<>]', ' ', text)
        
        # 标准化文本
        text = normalize_text(text)
        
        # 移除过短的行（可能是OCR错误）
        lines = text.split('\n')
        cleaned_lines = []
        for line in lines:
            line = line.strip()
            # 保留长度大于5的行，或者包含常见关键词的行
            if len(line) > 5 or any(keyword in line.lower() for keyword in ['phd', 'university', 'email', 'deadline', 'position', 'research', 'doctor']):
                cleaned_lines.append(line)
        
        # 移除中文字符之间的多余空格
        result = '\n'.join(cleaned_lines)
        # 移除中文字符间的空格（如 "美 国" -> "美国"）
        result = re.sub(r'([\u4e00-\u9fff])\s+([\u4e00-\u9fff])', r'\1\2', result)
        
        return result
    
    def validate_ocr_quality(self, text: Optional[str]) -> bool:
        """
        验证OCR结果质量
        
        Args:
            text: OCR提取的文本
            
        Returns:
            bool: 文本质量是否合格
        """
        if not text or not text.strip():
            return False
        
        # 基本长度检查
        if len(text.strip()) < 50:
            logger.warning(f"OCR文本长度过短: {len(text.strip())} 字符")
            return False
        
        # 检查是否包含常见关键词（学术相关）
        keywords = ['phd', 'university', 'position', 'research', 'student', 'application', 
                   'deadline', 'email', 'contact', 'degree', 'master', 'doctoral']
        text_lower = text.lower()
        keyword_count = sum(1 for keyword in keywords if keyword in text_lower)
        
        if keyword_count < 2:
            logger.warning(f"OCR文本中关键词过少: {keyword_count} 个")
            # 不强制要求，因为有些内容可能不包含这些关键词
        
        # 检查字符分布
        alpha_chars = sum(1 for c in text if c.isalpha())
        total_chars = len(text.replace(' ', '').replace('\n', ''))
        
        if total_chars > 0:
            alpha_ratio = alpha_chars / total_chars
            if alpha_ratio < 0.3:  # 至少30%是字母
                logger.warning(f"OCR文本字母比例过低: {alpha_ratio:.2%}")
                return False
        
        logger.info(f"OCR文本质量验证通过: 长度 {len(text)} 字符, 关键词 {keyword_count} 个")
        return True

