"""
项目配置文件
"""
import os
import sys
import logging
from pathlib import Path

# Python 版本门槛：browser-use（抓取回退链兜底）要求 3.11+。
# 低版本下 browser-use 无法安装，系统会静默降级、疑难页面全部失败，
# 因此在最早的导入点直接终止并提示，而不是带病运行。
MIN_PYTHON = (3, 11)
if sys.version_info < MIN_PYTHON:
    raise SystemExit(
        f"\n❌ Python 版本过低: 当前 {sys.version_info.major}.{sys.version_info.minor}."
        f"{sys.version_info.micro}（{sys.executable}）\n"
        f"   本项目要求 Python >= {MIN_PYTHON[0]}.{MIN_PYTHON[1]}"
        f"（browser-use 智能代理兜底的最低要求）。\n"
        f"   请切换解释器后重试，例如: conda activate py311\n"
    )

# 项目根目录
PROJECT_ROOT = Path(__file__).resolve().parents[2]

# 文件路径配置
EXCEL_FILE = PROJECT_ROOT / "text_info.xlsx"  # 保留作为备用
SHEET_NAME = "Unfilled"

# 密钥和凭证目录
KEYS_DIR = PROJECT_ROOT / "keys"
API_KEY_FILE = KEYS_DIR / "api_key.txt"
LEGACY_OPENAI_KEY_FILE = KEYS_DIR / "openai_key.txt"

# Google Sheets 配置
GOOGLE_CREDENTIALS_FILE = KEYS_DIR / "credentials.json"
GOOGLE_TOKEN_FILE = KEYS_DIR / "token.pickle"
GOOGLE_SPREADSHEET_ID = '1LcfxcTCuj9ZJXXMxyFQwt-xnbAviNP8j9oDr6OG5-Go'
GOOGLE_SCOPES = ['https://www.googleapis.com/auth/spreadsheets']

# 缓存目录
CACHE_DIR = PROJECT_ROOT / "cache"
PDF_CACHE_DIR = CACHE_DIR / "pdf"
SCREENSHOT_CACHE_DIR = CACHE_DIR / "screenshots"

# 日志配置
LOG_DIR = PROJECT_ROOT / "logs"
LLM_LOG_DIR = PROJECT_ROOT / "llm_logs"
RUN_LOG_FILE = LOG_DIR / "run.log"

# 滚动加载配置
SCROLL_STEP = 500  # 每次滚动的像素数
SCROLL_DELAY = 1000  # 每次滚动后的等待时间（毫秒）
MAX_SCROLLS = 50  # 最大滚动次数
NO_NEW_CONTENT_THRESHOLD = 3  # 连续无新内容次数阈值
SCROLL_BUFFER = 1000  # 页面底部缓冲像素数

# Playwright配置
USE_PLAYWRIGHT = True  # 是否使用Playwright（通过独立进程，无异步冲突）
PLAYWRIGHT_TIMEOUT = 120  # Playwright页面加载超时时间（秒）- 增加到120秒
PLAYWRIGHT_SCROLL_ENABLED = True  # 是否启用滚动加载
# 默认有头（headless=False），最大程度避免触发反爬检测；服务器环境可设环境变量覆盖
PLAYWRIGHT_HEADLESS = os.getenv("PLAYWRIGHT_HEADLESS", "0").lower() in ("1", "true", "yes", "on")

# 智能页面加载配置
USE_SMART_PAGE_LOADER = True  # 是否使用智能页面加载检测
SMART_LOAD_INITIAL_WAIT = 5  # 智能检测前的初始等待时间（秒）- 从15秒减少到5秒
SMART_LOAD_MAX_WAIT = 30  # 智能加载最大等待时间（秒）- 从60秒减少到30秒
SMART_LOAD_STABILITY_INTERVAL = 0.5  # 内容稳定性检查间隔（秒）- 从1秒减少到0.5秒
SMART_LOAD_STABILITY_THRESHOLD = 2  # 内容稳定性阈值（连续N次无变化）- 从3次减少到2次
SMART_LOAD_MIN_CONTENT_LENGTH = 200  # 最小内容长度阈值 - 从500减少到200
SMART_LOAD_MAX_RETRIES = 1  # 加载失败时的最大重试次数 - 从2次减少到1次

# 截图OCR配置
USE_SCREENSHOT_OCR = True  # 是否启用截图OCR作为fallback
OCR_LANGUAGE = 'eng+chi_sim'  # OCR语言设置 (eng=英文, chi_sim=简体中文)
SCREENSHOT_QUALITY = 100  # 截图质量 (1-100, 注意: PNG格式不使用此参数)
SCREENSHOT_MAX_PAGES = 10  # 长页面最大截图页数
SCREENSHOT_CLEANUP_AFTER_USE = True  # 使用后是否自动清理截图

# browser-use 智能浏览器代理配置（网页抓取回退链的最后一级兜底）
# 仅在 HTTP / Playwright / 截图OCR 全部失败后触发，用 LLM 驱动浏览器完成
# 多步交互（过验证页、关弹窗、展开内容）后提取正文。成本较高（单 URL 约
# 1.6万–6.3万 token），因此只做兜底。设环境变量 USE_BROWSER_AGENT=0 可关闭。
USE_BROWSER_AGENT = os.getenv("USE_BROWSER_AGENT", "1").lower() in ("1", "true", "yes", "on")
BROWSER_AGENT_MODEL = "gpt-5.6-sol"   # 2026-08-22 切到 gpt-5.6 旗舰 sol（与主链对齐）；旧值 gpt-5.5 若工具调用更稳可回退
BROWSER_AGENT_MAX_STEPS = 12      # 单任务最大代理步数（成本上限）
BROWSER_AGENT_TIMEOUT = 300       # 单 URL 兜底总超时（秒）
# 网关 nginx 对含整页截图的大请求体返回 413，故禁用视觉、仅用 DOM 文本；
# 若网关调大 client_max_body_size 后可改为 True
BROWSER_AGENT_USE_VISION = False

# browser-use 调优（P0：最贵一层，目标 token 直降过半）：
# - page_extraction_llm 指向最便宜的 luna（$0.2/$1.2）：extract 动作要处理整页大文本，
#   用轻量模型直降成本，主 agent 仍用 gpt-5.5 保证工具调用/导航决策稳定。
# - flash_mode + 关闭 thinking：跳过每步的推理段落，压缩 prompt。
# - max_history_items：只带最近 N 步历史进上下文，避免多步任务里历史线性膨胀。
#   注意：browser-use 0.13.3 硬约束该值必须为 None 或 > 5，故最小取 6。
# - directly_open_url：首步直接打开任务里的 URL，省掉一次导航往返。
BROWSER_AGENT_EXTRACTION_MODEL = "gpt-5.6-luna"
BROWSER_AGENT_FLASH_MODE = os.getenv("BROWSER_AGENT_FLASH_MODE", "1").lower() in ("1", "true", "yes", "on")
BROWSER_AGENT_MAX_HISTORY_ITEMS = int(os.getenv("BROWSER_AGENT_MAX_HISTORY_ITEMS", "6"))
BROWSER_AGENT_DIRECTLY_OPEN_URL = os.getenv("BROWSER_AGENT_DIRECTLY_OPEN_URL", "1").lower() in ("1", "true", "yes", "on")

# PDF 查看器（腾讯文档等）逐页截图配置
# 按页码逐页裁剪，确保每张截图恰好是完整一页（不会上一页一半下一页一半）
PDF_VIEWER_PER_PAGE_CLIP = True   # 启用逐页 clip 截图策略
PDF_VIEWER_AUTO_ZOOM = True       # 横向内容溢出时自动适配宽度（避免截断右侧）
PDF_VIEWER_HIDE_UI = True         # 截图前隐藏遮挡正文的工具栏/侧边栏等 UI
SCREENSHOT_MAX_SHOTS = 30         # 单次截图最大张数（防御性上限，防止死循环）
SCREENSHOT_PAGE_RENDER_WAIT_MS = 1200  # 每页滚动到位后的渲染等待（毫秒）
# 通用长页面分段截图滚动参数（供 worker 读取，替代硬编码）
SCREENSHOT_SCROLL_STEP = 400      # 每次滚动像素
SCREENSHOT_SCROLL_WAIT_MS = 250   # 每次滚动后的等待（毫秒）

# LLM 配置
API_BASE_URL = "https://newapi.gisphere.info/v1"
GEMINI_BASE_URL = "https://newapi.gisphere.info/v1beta"

# === LLM 模型路由链（按效果/偏好排序，运行时逐个回退；统一走 /chat/completions）===
# 纯文本三阶段分析使用。任一模型不可用（401/403/404/限流）时自动尝试下一个。
# 2026-08-20 调整：网关下架了整个 gemini 系列（旧的 gemini-3.5-flash 已不存在），
#   改用新上线的 gpt-5.6 三兄弟（sol 旗舰 $5/$30、terra 中端 $2/$12、luna 轻量 $0.2/$1.2）。
#   经 3 篇真实招聘页×三阶段实测（llm_logs/model_benchmark_566_*）：三者均 9/9 通过，
#   实测 terra 性价比最高、sol 旗舰准确度最高。按用户偏好文本链也按能力最强→最弱
#   （sol→terra→luna），luna 最慢且出现联系人/类型抽取瑕疵故垫底；其后回退 deepseek-v4-pro、model-router。
TEXT_MODEL_CHAIN = [
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
    "deepseek-v4-pro",
    "model-router",
]
# 图片/文档（VLM）使用：仅含支持视觉输入的模型，按能力最强→最弱排序。
# deepseek-v4-pro / model-router 非多模态，故视觉链只保留 gpt-5.6 系列。
VISION_MODEL_CHAIN = [
    "gpt-5.6-sol",
    "gpt-5.6-terra",
    "gpt-5.6-luna",
]

# 向后兼容的单值别名（旧模块/日志仍引用）
TEXT_PRIMARY_MODEL = TEXT_MODEL_CHAIN[0]
TEXT_FALLBACK_MODEL = TEXT_MODEL_CHAIN[1] if len(TEXT_MODEL_CHAIN) > 1 else TEXT_MODEL_CHAIN[0]
TEXT_MODEL = TEXT_PRIMARY_MODEL

# 死模型熔断：某模型返回鉴权类错误(401/403)后，本进程内在该时长内跳过它，
# 到期自动恢复重试。避免上游 key 短期被封导致每次调用都空转整条链。
# 429(限流)/超时等临时错误不触发长熔断（见 api_client）。
MODEL_COOLDOWN_SECONDS = int(os.getenv("MODEL_COOLDOWN_SECONDS", "1800"))  # 默认 30 分钟

OLLAMA_BASE_URL = "http://localhost:11434"
OLLAMA_MODEL = "qwen3:14b"

# Backward-compatible names used by older modules/log messages.
OPENAI_KEY_FILE = LEGACY_OPENAI_KEY_FILE
OPENAI_PRIMARY_MODEL = TEXT_PRIMARY_MODEL
OPENAI_FALLBACK_MODEL = TEXT_FALLBACK_MODEL
OPENAI_MODEL = TEXT_MODEL
OPENAI_BASE_URL = API_BASE_URL

# Document AI 配置 (用于PDF/图片文字提取)
USE_DOCUMENT_AI = True  # 是否启用 Document AI 进行文字提取（优先于OCR）
# 多模态文档理解模型 (支持 Vision)，与 VISION_MODEL_CHAIN 对齐（价格升序回退）
DOCUMENT_AI_MODEL = VISION_MODEL_CHAIN[0]
DOCUMENT_AI_FALLBACK_MODEL = VISION_MODEL_CHAIN[1] if len(VISION_MODEL_CHAIN) > 1 else VISION_MODEL_CHAIN[0]
DOCUMENT_AI_MAX_PAGES = 10  # Document AI 处理的最大页数
DOCUMENT_AI_TIMEOUT = 120  # Document AI 调用超时时间（秒）

# === 三阶段分析字段（单一真源）===
# 各模块（analysis_stage / excel_handler / direction_verifier 等）统一引用，避免多处硬编码漂移。
STAGE1_FIELDS = ['Deadline', 'Number_Places', 'Direction', 'University_EN', 'Contact_Name', 'Contact_Email']
# 阶段2：招生类型 + 专业方向（GEO_FIELDS）
POSITION_TYPE_FIELDS = ['Master Student', 'Doctoral Student', 'PostDoc', 'Research Assistant',
                        'Competition', 'Summer School', 'Conference', 'Workshop']
GEO_FIELDS = ['Physical_Geo', 'Human_Geo', 'Urban', 'GIS', 'RS', 'GNSS']
STAGE2_FIELDS = POSITION_TYPE_FIELDS + GEO_FIELDS
STAGE3_FIELDS = ['University_CN', 'Country_CN', 'WX_Label1', 'WX_Label2', 'WX_Label3', 'WX_Label4', 'WX_Label5']

# Excel 列名配置
EXCEL_COLUMNS = {
    # 输入列
    'Notes': 'Notes',
    'Source': 'Source',
    
    # 英文分析阶段1字段
    'Deadline': 'Deadline',
    'Number_Places': 'Number_Places',
    'Direction': 'Direction',
    'University_EN': 'University_EN',
    'Contact_Name': 'Contact_Name',
    'Contact_Email': 'Contact_Email',
    
    # 英文分析阶段2字段 - 招生类型
    'Master Student': 'Master Student',
    'Doctoral Student': 'Doctoral Student',
    'PostDoc': 'PostDoc',
    'Research Assistant': 'Research Assistant',
    'Competition': 'Competition',
    'Summer School': 'Summer School',
    'Conference': 'Conference',
    'Workshop': 'Workshop',
    
    # 英文分析阶段2字段 - 专业方向
    'Physical_Geo': 'Physical_Geo',
    'Human_Geo': 'Human_Geo',
    'Urban': 'Urban',
    'GIS': 'GIS',
    'RS': 'RS',
    'GNSS': 'GNSS',
    
    # 中文分析阶段字段
    'University_CN': 'University_CN',
    'Country_CN': 'Country_CN',
    'WX_Label1': 'WX_Label1',
    'WX_Label2': 'WX_Label2',
    'WX_Label3': 'WX_Label3',
    'WX_Label4': 'WX_Label4',
    'WX_Label5': 'WX_Label5',
    
    # 状态列
    'Verifier': 'Verifier',
    'Error': 'Error'
}

# 请求配置
REQUEST_TIMEOUT = 30
MAX_RETRIES = 3
PDF_DOWNLOAD_TIMEOUT = 60

# 联系人验证配置
CONTACT_VERIFICATION_ENABLED = True
CONTACT_SEARCH_TIMEOUT = 20
MAX_SEARCH_RESULTS = 10
MAX_PAGES_TO_ANALYZE = 3

# 网络搜索配置（基于 Playwright MCP）
# 设置环境变量 ENABLE_WEB_SEARCH=0 可完全禁用联系人网络搜索
ENABLE_WEB_SEARCH = os.getenv("ENABLE_WEB_SEARCH", "1").lower() in ("1", "true", "yes", "on")
# 设置环境变量 PLAYWRIGHT_MCP_HEADLESS=1 可启用无头模式（服务器环境适用）
PLAYWRIGHT_MCP_HEADLESS = os.getenv("PLAYWRIGHT_MCP_HEADLESS", "0").lower() in ("1", "true", "yes", "on")

# 日志配置
LOG_FORMAT = '%(asctime)s - %(name)s - %(levelname)s - %(message)s'
LOG_LEVEL = logging.INFO

def setup_logging():
    """设置日志配置"""
    # 确保日志目录存在
    LOG_DIR.mkdir(exist_ok=True)
    LLM_LOG_DIR.mkdir(exist_ok=True)
    
    # 配置根日志
    logging.basicConfig(
        level=LOG_LEVEL,
        format=LOG_FORMAT,
        handlers=[
            logging.FileHandler(RUN_LOG_FILE, encoding='utf-8'),
            logging.StreamHandler()
        ]
    )
    
    return logging.getLogger(__name__)

def ensure_directories():
    """确保所有必要目录存在"""
    directories = [CACHE_DIR, PDF_CACHE_DIR, SCREENSHOT_CACHE_DIR, LOG_DIR, LLM_LOG_DIR]
    for directory in directories:
        directory.mkdir(parents=True, exist_ok=True)

def check_api_key():
    """检查 API Key 是否存在，优先读取通用文件名，兼容旧 OpenAI 文件名。"""
    for key_file in (API_KEY_FILE, LEGACY_OPENAI_KEY_FILE):
        if not key_file.exists():
            continue
        try:
            with open(key_file, 'r', encoding='utf-8') as f:
                key = f.read().strip().lstrip('\ufeff')
                return key if key else None
        except Exception:
            continue
    return None

def check_openai_key():
    """Backward-compatible alias for older code paths."""
    return check_api_key()

def check_google_credentials():
    """检查Google Sheets凭据文件是否存在"""
    return GOOGLE_CREDENTIALS_FILE.exists() 



