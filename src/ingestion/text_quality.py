"""
文本质量与清洗工具（纯函数集合）。

从 fetch_text.py 抽出的、与抓取流程无关的通用逻辑：
- 网页内容质量打分（evaluate_web_content_quality）
- PDF 乱码 / 不可用页 / 有效性检测
- PDF 文本清洗与归一化（编码修复、去页面残留、断行修复）
- PyMuPDF dict/blocks 结构的文本拼接

这些函数不依赖任何实例状态，便于复用与单元测试。
"""
import logging
import re
from typing import List, Optional, Tuple

from ..core.utils import normalize_text

logger = logging.getLogger(__name__)


# ──────────────────────────────────────────────────────────────────
# 网页内容质量评估
# ──────────────────────────────────────────────────────────────────
def evaluate_web_content_quality(content: Optional[str]) -> Tuple[int, str, List[str], str]:
    """统一评估网页内容质量，避免将模板页、壳页或过短文本误判为成功。"""
    if not content or not content.strip():
        return 0, "bad", ["empty_content"], "未获取到文本内容"

    normalized = normalize_text(content)
    text_lower = normalized.lower()
    flags: List[str] = []
    score = 100

    content_length = len(normalized)
    word_matches = re.findall(r"\b[\w-]+\b", normalized)
    word_count = len(word_matches)
    unique_words = len(set(word.lower() for word in word_matches if len(word) >= 3))

    if is_likely_pdf_content(normalized):
        flags.append("pdf_like_content")
        score -= 80

    if is_unavailable_content(normalized):
        flags.append("unavailable_page")
        score -= 45

    template_patterns = [
        r"\{\{[^{}]+\}\}",
        r"\{\$[^{}]+\}",
        r"\$\{[^{}]+\}"
    ]
    if any(re.search(pattern, normalized) for pattern in template_patterns):
        flags.append("template_placeholders")
        score -= 60

    if "{{$ctrl" in text_lower:
        flags.append("framework_placeholders")
        score -= 20

    if content_length < 80:
        flags.append("too_short")
        score -= 55
    elif content_length < 200:
        flags.append("short_content")
        score -= 35
    elif content_length < 500:
        flags.append("limited_content")
        score -= 15
    elif content_length > 1500:
        score += 10

    if word_count < 40:
        flags.append("low_word_count")
        score -= 20
    elif word_count > 250:
        score += 5

    if unique_words < 20:
        flags.append("low_unique_words")
        score -= 15

    shell_signals = [
        "sign in",
        "log in",
        "create account",
        "forgot your password",
        "cookie settings",
        "accept cookies",
        "privacy policy",
        "terms of service",
        "accessibility policy",
        "equal opportunity employer",
        "all rights reserved"
    ]
    shell_hits = sum(1 for signal in shell_signals if signal in text_lower)
    if shell_hits >= 3:
        flags.append("shell_page_signals")
        shell_penalty = min(35, shell_hits * (10 if content_length < 800 else 4))
        score -= shell_penalty

    challenge_signals = [
        "just a moment",
        "verifying you are human",
        "performing security verification",
        "verification successful",
        "enable javascript and cookies to continue",
        "performance and security by cloudflare",
        "ray id"
    ]
    challenge_hits = sum(1 for signal in challenge_signals if signal in text_lower)
    if challenge_hits >= 2:
        flags.append("challenge_page")
        score -= 95

    portal_shell_signals = [
        "my jobpage",
        "my job cart",
        "my saved searches",
        "my referrals",
        "my account options",
        "this service is set to disconnect automatically",
        "you have been signed out",
        "beginning of the main content section",
        "return to previous position on page",
        "refer a friend for this job",
        "submit a candidate's profile"
    ]
    portal_hits = sum(1 for signal in portal_shell_signals if signal in text_lower)
    if portal_hits >= 2:
        flags.append("portal_shell_page")
        score -= min(55, portal_hits * 18)

    if re.search(r"\{\d+\}", normalized):
        flags.append("session_placeholders")
        score -= 25

    body_keywords = [
        "description",
        "requirements",
        "qualifications",
        "deadline",
        "responsibilities",
        "application instructions",
        "application process",
        "research",
        "position",
        "university",
        "contact"
    ]
    body_hits = sum(1 for keyword in body_keywords if keyword in text_lower)
    if body_hits == 0:
        flags.append("missing_body_keywords")
        score -= 20
    else:
        score += min(20, body_hits * 4)

    detail_keywords = [
        "required qualifications",
        "preferred qualifications",
        "brief description of duties",
        "job description -",
        "essential duties",
        "minimum qualifications",
        "about the role",
        "application deadline"
    ]
    detail_hits = sum(1 for keyword in detail_keywords if keyword in text_lower)
    if detail_hits >= 2:
        score += min(25, detail_hits * 8)
    elif "portal_shell_page" in flags:
        flags.append("missing_detail_sections")
        score -= 20

    # 短页面如果同时出现 challenge/portal 特征，应直接视为低质量壳页。
    if content_length < 1200 and ("challenge_page" in flags or "portal_shell_page" in flags):
        score -= 25

    score = max(0, min(100, score))

    if score >= 60:
        quality = "good"
        reason = "正文特征充足"
    elif score >= 35:
        quality = "weak"
        reason = "内容存在但质量一般"
    else:
        quality = "bad"
        reason = "内容疑似模板页、壳页或无效文本"

    return score, quality, flags, reason


def is_likely_pdf_content(content: str) -> bool:
    """检查内容是否疑似PDF乱码"""
    if not content or len(content) < 50:
        return False

    pdf_indicators = [
        '%PDF-', 'endobj', 'endstream', '/Type', '/Catalog',
        'Binary data follows', 'Font substitution', 'Invalid encoding',
        '/MediaBox', '/Contents', '/XObject', '/ProcSet'
    ]

    check_content = content[:2000]
    indicator_count = sum(1 for indicator in pdf_indicators if indicator in check_content)

    if indicator_count >= 2:
        logger.info(f"在内容中检测到 {indicator_count} 个PDF指示符")
        return True

    if content.strip().startswith('%PDF-'):
        logger.info("检测到PDF文件头标记")
        return True

    if len(content) > 200:
        sample_size = min(1000, len(content))
        sample_content = content[:sample_size]
        control_chars = sum(1 for c in sample_content if ord(c) < 32 and c not in '\n\r\t ')
        control_ratio = control_chars / sample_size

        if control_ratio > 0.05:
            logger.info(f"检测到异常的控制字符比例: {control_ratio:.2%}")
            return True

        non_ascii_chars = sum(1 for c in sample_content if ord(c) > 127)
        non_ascii_ratio = non_ascii_chars / sample_size

        if non_ascii_ratio > 0.3:
            logger.info(f"检测到高比例非ASCII字符: {non_ascii_ratio:.2%}")
            return True

    return False


def is_unavailable_content(content: str) -> bool:
    """检测内容是否不可用（如"暂不支持您的浏览器"、"无法下载"等）。"""
    if not content or len(content.strip()) < 50:
        return True

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

    if len(content.strip()) < 200:
        error_keywords = ['error', '404', '403', '500', 'not found', 'forbidden']
        if any(keyword in content_lower for keyword in error_keywords):
            logger.info("检测到错误页面")
            return True

    return False


# ──────────────────────────────────────────────────────────────────
# PDF 文本有效性 / 乱码检测
# ──────────────────────────────────────────────────────────────────
def is_valid_text(text: Optional[str]) -> bool:
    """检查提取的文本是否有效（非乱码）- 基础版本"""
    logger.info("开始验证提取的文本质量...")

    if not text or not text.strip():
        logger.warning("文本为空或只包含空白字符")
        return False

    if len(text.strip()) < 50:
        logger.warning(f"文本长度过短: {len(text.strip())} 字符")
        return False

    logger.info(f"文本基本信息: 长度 {len(text)} 字符, 行数 {len(text.splitlines())}")

    if is_pdf_raw_data(text):
        logger.warning("❌ 检测到PDF原始数据或严重乱码")
        return False

    if is_basic_corrupted_text(text):
        logger.warning("❌ 检测到文本乱码")
        return False

    words = text.lower().split()
    if len(words) < 5:
        logger.warning(f"文本单词数量过少: {len(words)} 个单词")
        return False

    logger.info(f"文本包含 {len(words)} 个单词")

    alpha_chars = sum(1 for c in text if c.isalpha())
    total_chars = len(text.replace(' ', '').replace('\n', ''))

    if total_chars == 0:
        logger.warning("文本不包含任何有效字符")
        return False

    alpha_ratio = alpha_chars / total_chars
    logger.info(f"文本字母比例: {alpha_ratio:.2%}")

    if alpha_ratio < 0.2:
        logger.warning(f"❌ 文本字母比例过低: {alpha_ratio:.2%}")
        return False

    logger.info(f"✅ 文本验证通过! 长度: {len(text)} 字符, 字母比例: {alpha_ratio:.2%}")
    return True


def is_pdf_raw_data(text: str) -> bool:
    """检查是否为PDF原始数据或严重乱码 - 基础版本"""
    try:
        if text.strip().startswith('%PDF-'):
            logger.debug("检测到PDF文件头标记")
            return True

        pdf_markers = [
            'endobj', 'endstream', '/Type', '/Catalog', '/Pages',
            '/MediaBox', '/Contents', 'startxref', '%%EOF', 'trailer'
        ]

        marker_count = sum(1 for marker in pdf_markers if marker in text)
        if marker_count >= 3:
            logger.debug(f"检测到{marker_count}个PDF内部标记")
            return True

        control_chars = sum(1 for c in text if ord(c) < 32 and c not in '\n\r\t ')
        if len(text) > 0 and control_chars / len(text) > 0.1:
            logger.debug(f"控制字符比例过高: {control_chars/len(text):.2f}")
            return True

    except Exception as e:
        logger.debug(f"PDF乱码检测异常: {e}")

    return False


def is_basic_corrupted_text(text: str) -> bool:
    """基础乱码检测"""
    try:
        char_counts = {}
        for char in text:
            if char.isalpha():
                char_counts[char] = char_counts.get(char, 0) + 1

        if char_counts:
            max_char_count = max(char_counts.values())
            total_alpha_chars = sum(char_counts.values())
            if max_char_count / total_alpha_chars > 0.4:
                most_common_char = max(char_counts, key=char_counts.get)
                logger.debug(f"字符'{most_common_char}'出现过于频繁: {max_char_count/total_alpha_chars:.2%}")
                return True

        words = [word for word in text.split() if word.isalpha()]
        if words and len(words) > 10:
            avg_word_length = sum(len(word) for word in words) / len(words)
            if avg_word_length < 1 or avg_word_length > 25:
                logger.debug(f"平均单词长度异常: {avg_word_length:.1f}")
                return True

        special_char_sequences = len(re.findall(r'[^\w\s]{4,}', text))
        if special_char_sequences > len(text) / 200:
            logger.debug(f"特殊字符序列过多: {special_char_sequences}")
            return True

    except Exception as e:
        logger.debug(f"基础乱码检测异常: {e}")

    return False


# ──────────────────────────────────────────────────────────────────
# PDF 文本清洗与归一化
# ──────────────────────────────────────────────────────────────────
def clean_and_normalize_text(text: str) -> str:
    """清理和标准化文本"""
    if not text:
        return ""

    text = fix_encoding_issues(text)
    text = re.sub(r'\s+', ' ', text)
    text = remove_page_artifacts(text)
    text = fix_line_breaks(text)

    return text.strip()


def fix_encoding_issues(text: str) -> str:
    """修复常见的编码问题"""
    try:
        if isinstance(text, bytes):
            encodings = ['utf-8', 'latin-1', 'cp1252', 'iso-8859-1']
            for encoding in encodings:
                try:
                    text = text.decode(encoding)
                    break
                except UnicodeDecodeError:
                    continue

        replacements = {
            '\ufeff': '',
            '\u00a0': ' ',
            '\u2013': '-',
            '\u2014': '--',
            '\u201c': '"',
            '\u201d': '"',
            '\u2018': "'",
            '\u2019': "'",
            '\u2026': '...',
        }

        for old, new in replacements.items():
            text = text.replace(old, new)

    except Exception as e:
        logger.debug(f"编码修复失败: {e}")

    return text


def remove_page_artifacts(text: str) -> str:
    """移除页面相关的干扰内容"""
    text = re.sub(r'\n\s*\d+\s*\n', '\n', text)
    text = re.sub(r'\nPage \d+\n', '\n', text)
    text = re.sub(r'\n\s*www\.[^\n]+\n', '\n', text)
    text = re.sub(r'\n\s*http[^\n]+\n', '\n', text)
    return text


def fix_line_breaks(text: str) -> str:
    """修复不合理的断行"""
    text = re.sub(r'([a-z])-\s*\n\s*([a-z])', r'\1\2', text)
    text = re.sub(r'([a-z,])\s*\n\s*([a-z])', r'\1 \2', text)
    return text


# ──────────────────────────────────────────────────────────────────
# PyMuPDF 结构化文本拼接
# ──────────────────────────────────────────────────────────────────
def extract_text_from_dict(text_dict: dict) -> str:
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


def extract_text_from_blocks(blocks: list) -> str:
    """从PyMuPDF块格式提取文本"""
    text_parts = []
    try:
        for block in blocks:
            if isinstance(block, tuple) and len(block) >= 5:
                if len(block) >= 7 and block[6] == 0:
                    text = block[4]
                    if text and text.strip():
                        text_parts.append(text)
    except Exception as e:
        logger.debug(f"块格式文本提取失败: {e}")
    return '\n'.join(text_parts)
