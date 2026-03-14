"""
工具函数模块
"""
import re
import sys
import subprocess
import importlib
import logging
import json
from pathlib import Path
from datetime import datetime
from urllib.parse import urlparse

logger = logging.getLogger(__name__)

def check_dependencies():
    """检查依赖包是否已安装"""
    # 包名映射：pip包名 -> 导入名
    package_mapping = {
        'openai': 'openai',
        'requests': 'requests', 
        'pandas': 'pandas',
        'openpyxl': 'openpyxl',
        'beautifulsoup4': 'bs4',  # beautifulsoup4 包导入时使用 bs4
        'PyMuPDF': 'fitz',        # PyMuPDF 包导入时使用 fitz
        'tqdm': 'tqdm',
        'urllib3': 'urllib3',
        'lxml': 'lxml'
    }
    
    missing_packages = []
    
    for pip_name, import_name in package_mapping.items():
        try:
            importlib.import_module(import_name)
            logger.info(f"✅ {pip_name} 已安装")
        except ImportError:
            missing_packages.append(pip_name)
            logger.warning(f"❌ {pip_name} 未安装")
    
    if missing_packages:
        logger.error(f"缺少依赖包: {', '.join(missing_packages)}")
        logger.info("请运行: pip install -r requirements.txt")
        return False
    
    logger.info("所有依赖包检查完成")
    return True

def is_third_party_url(url):
    """检查URL是否为第三方平台网址（非官方独立网站）"""
    if not url:
        return False
        
    try:
        parsed = urlparse(url)
        domain = parsed.netloc.lower()
        
        # 常见的第三方平台域名关键字
        third_party_domains = [
            'linkedin.com', 'twitter.com', 'x.com', 
            'zhihu.com', 'xiaohongshu.com', 'weibo.com', 
            'facebook.com', 'instagram.com', 'github.com',
            'weixin.qq.com', 'mp.weixin.qq.com', 'wechat.com',
            'findaphd.com', 'researchgate.net', 'google.com',
            'medium.com', 'scholar.google.com', 'phdportal.com',
            'mastersportal.com', 'zhuanlan.zhihu.com'
        ]
        
        for tp_domain in third_party_domains:
            if tp_domain in domain:
                return True
                
        return False
    except Exception:
        return False

def is_valid_url(text):
    """检查文本是否为有效的URL"""
    if not text or not isinstance(text, str):
        return False
    
    text = text.strip()
    
    # 检查是否以http或https开头
    if not text.startswith(('http://', 'https://')):
        return False
    
    try:
        result = urlparse(text)
        return all([result.scheme, result.netloc])
    except Exception:
        return False

def extract_url_from_text(text):
    """从文本中提取URL"""
    if not text or not isinstance(text, str):
        return None
    
    # 如果整个文本就是一个URL
    if is_valid_url(text.strip()):
        return text.strip()
    
    # 使用正则表达式查找URL
    url_pattern = r'https?://[^\s<>"{}|\\^`\[\]]+'
    matches = re.findall(url_pattern, text)
    
    if matches:
        # 返回第一个找到的URL
        return matches[0]
    
    return None

def is_pdf_url(url):
    """检查URL是否指向PDF文件"""
    if not url:
        return False
    
    # 检查URL是否以.pdf结尾
    if url.lower().endswith('.pdf'):
        return True
    
    # 检查URL路径中是否包含.pdf
    try:
        parsed = urlparse(url)
        return '.pdf' in parsed.path.lower()
    except Exception:
        return False

def is_google_drive_url(url):
    """检查URL是否为Google Drive链接"""
    if not url:
        return False
    
    try:
        parsed = urlparse(url)
        # 检查是否为 drive.google.com 域名
        if 'drive.google.com' in parsed.netloc.lower():
            # 检查是否为文件查看链接或下载链接
            if '/file/d/' in parsed.path or '/uc?' in parsed.path:
                return True
    except Exception:
        pass
    
    return False

def extract_google_drive_file_id(url):
    """从Google Drive URL中提取文件ID"""
    if not url:
        return None
    
    try:
        # 匹配格式: https://drive.google.com/file/d/FILE_ID/view
        # 或: https://drive.google.com/file/d/FILE_ID/edit
        # 或: https://drive.google.com/open?id=FILE_ID
        match = re.search(r'/file/d/([a-zA-Z0-9_-]+)', url)
        if match:
            return match.group(1)
        
        # 匹配格式: https://drive.google.com/open?id=FILE_ID
        match = re.search(r'[?&]id=([a-zA-Z0-9_-]+)', url)
        if match:
            return match.group(1)
        
        # 匹配格式: https://drive.google.com/uc?id=FILE_ID
        match = re.search(r'/uc[?&]id=([a-zA-Z0-9_-]+)', url)
        if match:
            return match.group(1)
            
    except Exception as e:
        logger.debug(f"提取Google Drive文件ID失败: {e}")
    
    return None

def convert_google_drive_to_download(url):
    """将Google Drive查看链接转换为直接下载链接"""
    file_id = extract_google_drive_file_id(url)
    if not file_id:
        return None
    
    # 转换为直接下载链接
    download_url = f"https://drive.google.com/uc?export=download&id={file_id}"
    return download_url

def is_google_docs_url(url):
    """检查URL是否为Google Docs链接"""
    if not url:
        return False
    
    try:
        parsed = urlparse(url)
        # 检查是否为 docs.google.com 域名
        if 'docs.google.com' in parsed.netloc.lower():
            # 检查是否为文档链接
            if '/document/d/' in parsed.path:
                return True
    except Exception:
        pass
    
    return False

def extract_google_docs_document_id(url):
    """从Google Docs URL中提取文档ID"""
    if not url:
        return None
    
    try:
        # 匹配格式: https://docs.google.com/document/d/DOCUMENT_ID/edit
        # 或: https://docs.google.com/document/d/DOCUMENT_ID/view
        match = re.search(r'/document/d/([a-zA-Z0-9_-]+)', url)
        if match:
            return match.group(1)
    except Exception as e:
        logger.debug(f"提取Google Docs文档ID失败: {e}")
    
    return None

def convert_google_docs_to_export(url, format='txt'):
    """
    将Google Docs查看链接转换为导出链接
    
    Args:
        url: Google Docs链接
        format: 导出格式，可选值: 'txt', 'pdf', 'html', 'docx', 'rtf'
    
    Returns:
        str: 导出链接，失败返回None
    """
    document_id = extract_google_docs_document_id(url)
    if not document_id:
        return None
    
    # 支持的导出格式
    valid_formats = ['txt', 'pdf', 'html', 'docx', 'rtf', 'odt', 'epub']
    if format not in valid_formats:
        format = 'txt'  # 默认使用文本格式
    
    # 转换为导出链接
    export_url = f"https://docs.google.com/document/d/{document_id}/export?format={format}"
    return export_url

def sanitize_filename(filename):
    """清理文件名，移除不安全字符"""
    # 移除不安全字符
    filename = re.sub(r'[<>:"/\\|?*]', '_', filename)
    # 限制长度
    if len(filename) > 100:
        name, ext = filename.rsplit('.', 1) if '.' in filename else (filename, '')
        filename = name[:95] + ('.' + ext if ext else '')
    return filename

def save_llm_conversation(row_index, conversation_data):
    """保存LLM对话记录"""
    from config import LLM_LOG_DIR
    import datetime as dt
    
    # 使用英国时间(UTC)生成文件名，精确到秒
    utc_now = dt.datetime.now(dt.timezone.utc)
    timestamp_str = utc_now.strftime('%Y%m%d_%H%M%S')
    
    log_file = LLM_LOG_DIR / f"row_{row_index:04d}_{timestamp_str}_UTC.txt"
    
    try:
        with open(log_file, 'w', encoding='utf-8') as f:
            f.write("=" * 80 + "\n")
            f.write(f"LLM对话记录 - 行 {row_index}\n")
            f.write(f"生成时间(UTC): {utc_now.strftime('%Y-%m-%d %H:%M:%S')} UTC\n")
            f.write(f"生成时间(本地): {dt.datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
            f.write("=" * 80 + "\n\n")
            
            for i, conv in enumerate(conversation_data, 1):
                stage = conv.get('stage', f'对话{i}')
                model = conv.get('model', '未知模型')
                timestamp = conv.get('timestamp', 0)
                prompt = conv.get('prompt', '')
                response = conv.get('response', '')
                original_text = conv.get('original_text', '')
                
                # 格式化时间戳
                if timestamp:
                    time_str = dt.datetime.fromtimestamp(timestamp).strftime('%H:%M:%S')
                else:
                    time_str = '未知时间'
                
                f.write(f"阶段: {stage} | 模型: {model} | 时间: {time_str}\n")
                f.write("-" * 80 + "\n")
                
                # 添加原始文本内容部分
                if original_text:
                    f.write("原始文本内容:\n")
                    f.write("-" * 40 + "\n")
                    f.write(original_text + "\n\n")
                    f.write("-" * 40 + "\n")
                
                f.write("输入提示词:\n")
                f.write(prompt + "\n\n")
                f.write("模型响应:\n")
                f.write(response + "\n\n")
                f.write("=" * 80 + "\n\n")
        
        logger.info(f"LLM对话记录已保存: {log_file}")
    except Exception as e:
        logger.error(f"保存LLM对话记录失败: {e}")

def validate_json_response(response_text):
    """验证并解析JSON响应，专门处理qwen3等带思考步骤的模型输出"""
    try:
        # 尝试找到JSON部分
        response_text = response_text.strip()
        
        # 方法1: 查找代码块中的JSON
        if '```json' in response_text:
            start = response_text.find('```json') + 7
            end = response_text.find('```', start)
            if end != -1:
                json_text = response_text[start:end].strip()
                try:
                    return json.loads(json_text)
                except json.JSONDecodeError:
                    pass
        
        # 方法2: 查找普通代码块中的JSON
        if '```' in response_text:
            start = response_text.find('```') + 3
            end = response_text.find('```', start)
            if end != -1:
                json_text = response_text[start:end].strip()
                try:
                    return json.loads(json_text)
                except json.JSONDecodeError:
                    pass
        
        # 方法3: 查找以{开头}结尾的JSON对象
        start_pos = response_text.find('{')
        end_pos = response_text.rfind('}')
        if start_pos != -1 and end_pos != -1 and start_pos < end_pos:
            json_text = response_text[start_pos:end_pos + 1]
            try:
                return json.loads(json_text)
            except json.JSONDecodeError:
                pass
        
        # 方法4: 使用正则表达式查找JSON对象
        # 匹配最完整的JSON对象
        json_pattern = r'\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}'
        matches = re.findall(json_pattern, response_text, re.DOTALL)
        
        for match in matches:
            try:
                # 尝试解析匹配到的JSON
                parsed = json.loads(match)
                # 验证是否包含期望的字段（简单检查）
                if isinstance(parsed, dict) and len(parsed) > 0:
                    return parsed
            except json.JSONDecodeError:
                continue
        
        # 方法5: 如果上述方法都失败，尝试直接解析整个响应
        try:
            return json.loads(response_text)
        except json.JSONDecodeError:
            pass
        
        # 方法6: 特殊处理思考型模型的输出格式
        # 查找可能的最终答案部分
        final_answer_markers = [
            "最终答案：", "Final Answer:", "答案：", "Answer:", 
            "结果：", "Result:", "输出：", "Output:"
        ]
        
        for marker in final_answer_markers:
            if marker in response_text:
                start_idx = response_text.find(marker) + len(marker)
                remaining_text = response_text[start_idx:].strip()
                
                # 在标记后查找JSON
                start_pos = remaining_text.find('{')
                end_pos = remaining_text.rfind('}')
                if start_pos != -1 and end_pos != -1 and start_pos < end_pos:
                    json_text = remaining_text[start_pos:end_pos + 1]
                    try:
                        return json.loads(json_text)
                    except json.JSONDecodeError:
                        continue
        
        logger.error(f"所有JSON提取方法都失败")
        logger.error(f"响应内容: {response_text[:500]}...")  # 只显示前500字符
        return None
        
    except Exception as e:
        logger.error(f"JSON解析过程中发生异常: {e}")
        logger.error(f"响应内容: {response_text[:500]}...")
        return None

def check_ollama_availability():
    """检查Ollama是否可用"""
    try:
        import requests
        from config import OLLAMA_BASE_URL
        
        response = requests.get(f"{OLLAMA_BASE_URL}/api/tags", timeout=5)
        return response.status_code == 200
    except Exception as e:
        logger.warning(f"Ollama连接失败: {e}")
        return False

def clean_email_format(email):
    """
    清理邮箱格式，将常见的混淆写法转换为标准格式
    
    Args:
        email: 原始邮箱字符串
        
    Returns:
        str: 清理后的标准邮箱格式
        
    Examples:
        "yichun.fan[at]duke.edu" -> "yichun.fan@duke.edu"
        "contact(at)example.com" -> "contact@example.com"
        "user [at] domain [dot] com" -> "user@domain.com"
    """
    if not email or email.strip() in ['-', '', 'N/A']:
        return email
    
    # 转换为小写并去除首尾空格
    cleaned = email.strip().lower()
    
    # 替换 [at], (at), " at " 等为 @
    at_patterns = [
        r'\[at\]',
        r'\(at\)',
        r'\s+at\s+',
        r'\s+AT\s+',
        r'\[AT\]',
        r'\(AT\)',
    ]
    for pattern in at_patterns:
        cleaned = re.sub(pattern, '@', cleaned, flags=re.IGNORECASE)
    
    # 替换 [dot], (dot), " dot " 等为 .
    dot_patterns = [
        r'\[dot\]',
        r'\(dot\)',
        r'\s+dot\s+',
        r'\s+DOT\s+',
        r'\[DOT\]',
        r'\(DOT\)',
    ]
    for pattern in dot_patterns:
        cleaned = re.sub(pattern, '.', cleaned, flags=re.IGNORECASE)
    
    # 移除多余的空格
    cleaned = re.sub(r'\s+', '', cleaned)
    
    # 验证是否看起来像邮箱（基本格式检查）
    if '@' in cleaned and '.' in cleaned.split('@')[-1]:
        return cleaned
    else:
        # 如果清理后不像邮箱，返回原始值
        return email

def normalize_text(text):
    """标准化文本，移除多余空白和特殊字符"""
    if not text:
        return ""
    
    # 移除多余的空白字符
    text = re.sub(r'\s+', ' ', text)
    text = text.strip()
    
    return text 