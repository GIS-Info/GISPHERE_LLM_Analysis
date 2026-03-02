# 基于LLM的文本智能分析与数据字段自动填写系统

这是一个智能的文本分析系统，能够自动从网页或PDF链接中提取内容，通过大语言模型(LLM)进行三阶段分析，并将结果自动填入Excel表格或Google Sheets的相应字段中。

## 🚀 快速开始

### 环境要求

- **Python**: 3.8+
- **必需依赖**: 见 `requirements.txt`
- **外部工具**:
  - **Tesseract OCR**: 截图文字识别引擎（可选，用于OCR fallback）
  - **Playwright浏览器**: Chromium浏览器（用于动态网页加载 + 联系人验证搜索）

### 安装步骤

```bash
# 1. 克隆项目
git clone [项目地址]
cd LLM_Analysis

# 2. 安装Python依赖
pip install -r requirements.txt

# 3. 安装Playwright浏览器
playwright install chromium

# 4. 可选：安装opencv-python以获得更好的OCR效果
pip install opencv-python
```

### 配置

#### 配置LLM服务

在 `keys/openai_key.txt` 中添加API密钥：
```bash
echo "your-api-key-here" > keys/openai_key.txt
```

或使用Ollama本地模型：
```bash
ollama serve
ollama pull qwen3:14b
```

#### 配置数据源

**Google Sheets模式**：
1. 配置 `keys/credentials.json`（Google API凭据）
2. 在 `config.py` 中设置 `GOOGLE_SPREADSHEET_ID`

**本地Excel模式**：
- 将数据放入 `text_info.xlsx` 的 `Unfilled` 工作表

### 运行

```bash
# 处理所有未填写的行
python main.py

# 测试单行处理
python main.py test
```

## 🌟 主要特性

### 内容提取能力

- **智能页面加载**: 四重策略检测（网络空闲、关键元素、内容稳定性、高度稳定性）
- **多种获取方式**: HTTP请求、Playwright动态渲染、PDF下载、Google Drive/Docs处理
- **Document AI**: 使用多模态LLM进行高质量文档文字提取（优先于传统OCR）
- **截图OCR fallback**: 当常规方法失败时，自动截图并OCR识别
- **智能重试机制**: 失败自动切换方案，多层fallback策略

### LLM智能分析

- **三阶段分析**:
  - 阶段1：英文基本信息提取（含联系人验证）
  - 阶段2：类型和专业方向分类
  - 阶段3：中文字段提取
- **多模型支持**: 兼容OpenAI API和Ollama本地模型
- **上下文管理**: 智能对话历史管理
- **部分成功处理**: 即使某阶段失败也会保存已成功的结果

### 联系人验证

- **自动搜索**: 通过 Playwright MCP + **DuckDuckGo** 搜索联系人学术主页
- **严格URL过滤**: 白名单机制，只访问 .edu、学术平台（ResearchGate/ORCID/Scholar）、大学个人主页等可信页面，自动拦截广告、招聘、社交媒体等无关链接
- **三层过滤**:
  1. Bing/DuckDuckGo搜索结果提取后硬过滤
  2. LLM 页面选择 prompt 严格约束
  3. LLM 选择结果再次硬过滤兜底
- **页面加载保障**: 导航后二段等待 + 内容校验，确保页面完全渲染后再提取

### 数据管理

- **双模式支持**: 本地Excel或云端Google Sheets
- **断点续传**: 支持中断后继续处理
- **实时保存**: 每行处理完成后立即保存
- **自动清理**: 处理完成后自动清理临时PDF文件

## 📊 系统架构

```
main.py                      # 主入口，协调整体流程
├── config.py                # 项目配置文件
├── utils.py                 # 工具函数模块
├── excel_handler.py         # Excel/数据管理
├── google_sheets_handler.py # Google Sheets API处理
├── fetch_text.py            # 内容提取（网页/PDF）
├── llm_agent.py             # LLM调用模块
├── analysis_stage.py        # 三阶段分析流程
├── contact_verifier.py      # 联系人验证（DuckDuckGo + MCP）
├── mcp_client.py            # Playwright MCP客户端
├── document_ai.py           # Document AI文档提取
├── screenshot_ocr_fetcher.py # 截图OCR提取
├── smart_page_loader.py     # 智能页面加载检测
├── playwright_process_manager.py # Playwright进程管理
└── playwright_worker.py     # Playwright工作进程
```

## ⚙️ 配置选项

在 `config.py` 中可以修改：

### LLM配置
```python
OPENAI_MODEL = "gpt-5-chat-latest"        # OpenAI模型
OPENAI_BASE_URL = "https://oneapi.gisphere.info/v1"  # API地址
OLLAMA_MODEL = "qwen3:14b"                # Ollama本地模型
OLLAMA_BASE_URL = "http://localhost:11434"
```

### Document AI配置
```python
USE_DOCUMENT_AI = True                    # 启用Document AI
DOCUMENT_AI_MODEL = "gemini-2.5-flash"    # 多模态模型
DOCUMENT_AI_MAX_PAGES = 10                # 最大处理页数
```

### Playwright配置
```python
USE_PLAYWRIGHT = True                     # 启用Playwright
PLAYWRIGHT_TIMEOUT = 120                  # 超时时间（秒）
PLAYWRIGHT_SCROLL_ENABLED = True          # 启用滚动加载
```

### 智能页面加载配置
```python
USE_SMART_PAGE_LOADER = True              # 启用智能加载检测
SMART_LOAD_INITIAL_WAIT = 5               # 初始等待时间（秒）
SMART_LOAD_MAX_WAIT = 30                  # 最大等待时间（秒）
SMART_LOAD_STABILITY_THRESHOLD = 2        # 稳定性阈值
```

### 截图OCR配置
```python
USE_SCREENSHOT_OCR = True                 # 启用截图OCR
OCR_LANGUAGE = 'eng+chi_sim'              # OCR语言
SCREENSHOT_MAX_PAGES = 10                 # 最大截图页数
SCREENSHOT_CLEANUP_AFTER_USE = True       # 自动清理截图
```

## 📁 目录结构

```
LLM_Analysis/
├── main.py                       # 主程序入口
├── config.py                     # 配置文件
├── utils.py                      # 工具函数
├── llm_agent.py                  # LLM代理
├── excel_handler.py              # Excel处理
├── google_sheets_handler.py      # Google Sheets处理
├── fetch_text.py                 # 内容获取
├── analysis_stage.py             # 分析阶段管理
├── contact_verifier.py           # 联系人验证（DuckDuckGo+MCP）
├── mcp_client.py                 # Playwright MCP客户端
├── document_ai.py                # Document AI提取
├── screenshot_ocr_fetcher.py     # 截图OCR
├── smart_page_loader.py          # 智能页面加载
├── playwright_process_manager.py # Playwright进程管理
├── playwright_worker.py          # Playwright工作进程
├── safe_playwright.py            # Playwright安全封装
├── check_dependencies.py         # 依赖检查工具
├── check_system.py               # 系统检查工具
├── requirements.txt              # Python依赖
├── keys/                         # 密钥文件目录
│   ├── openai_key.txt           # OpenAI API密钥（需自行创建）
│   ├── credentials.json         # Google API凭据（可选）
│   └── token.pickle            # Google授权令牌（自动生成）
├── text_info.xlsx               # 本地Excel数据文件
├── cache/                       # 缓存目录
│   ├── pdf/                    # PDF缓存
│   └── screenshots/            # 截图缓存
├── logs/                        # 运行日志
│   └── run.log
└── llm_logs/                   # LLM对话记录
```

## 🔧 分析字段说明

### 阶段1：英文基本信息提取

| 字段 | 说明 |
|------|------|
| Deadline | 申请截止日期（YYYY-MM-DD格式或"Soon"） |
| Number_Places | 招生人数 |
| Direction | 研究方向 |
| University_EN | 机构英文全称 |
| Contact_Name | 联系人姓名（含Dr./Mr./Ms.前缀） |
| Contact_Email | 联系邮箱 |

### 阶段2：类型和专业分类

**招生类型**（标记"1"表示适用）：
- Master Student, Doctoral Student, PostDoc, Research Assistant
- Competition, Summer School, Conference, Workshop

**专业方向**（选择1-3个）：
- Physical_Geo, Human_Geo, Urban, GIS, RS, GNSS

### 阶段3：中文字段提取

| 字段 | 说明 |
|------|------|
| University_CN | 机构中文全称 |
| Country_CN | 所在国家中文名 |
| WX_Label1-5 | 专业领域标签（Label1必填） |

## 🛠️ 常见问题

### 安装相关

**Q: Tesseract OCR安装后仍然提示"未找到"？**

Windows用户确认添加到PATH，或程序会自动查找以下路径：
- `C:\Program Files\Tesseract-OCR\tesseract.exe`
- `C:\Program Files (x86)\Tesseract-OCR\tesseract.exe`

**Q: Playwright浏览器安装失败？**

```bash
# 设置镜像后重试
export PLAYWRIGHT_DOWNLOAD_HOST=https://playwright.azureedge.net
playwright install chromium
```

### 使用相关

**Q: 如何查看详细的错误信息？**

- 运行日志：`logs/run.log`
- LLM对话记录：`llm_logs/row_xxxx_yyyymmdd_hhmmss_UTC.txt`

**Q: 如何中断并恢复处理？**

- 使用 `Ctrl+C` 中断，系统会自动保存当前进度
- 再次运行时自动跳过已处理的行（Verifier不为空的行）

**Q: 链接提取优先级是什么？**

系统优先从 `Source` 列提取链接，只有当 `Source` 列没有有效链接时才从 `Notes` 列提取。

**Q: 联系人验证搜索不到结果？**

- 检查 `logs/run.log` 中 `MCP DuckDuckGo搜索` 相关日志
- 确认 Playwright MCP 客户端已正常启动（日志中应有"✅ Playwright MCP客户端已连接"）
- 程序会自动重试最多3次，若仍失败则跳过验证步骤

## 📝 更新日志

### v2.2 - 2026-02
- ✅ 联系人验证搜索引擎从 Bing 切换至 **DuckDuckGo**（避免 Bing 机器人检测导致的重定向问题）
- ✅ 新增严格 URL 白名单过滤机制（三层过滤，自动拦截广告/招聘/社交媒体页面）
- ✅ 优化页面加载等待策略（二段等待 + 内容校验 + 最多3次重试）
- ✅ 修复 `AnalysisStageManager` 清理时的 `AttributeError`
- ✅ 升级 `openai` 依赖至 v2，解决 `proxies` 参数兼容性问题
- ✅ 密钥文件统一移至 `keys/` 目录

### v2.1 - 2025-01
- ✅ 新增 Document AI 文档提取功能（优先于传统OCR）
- ✅ 优化智能页面加载检测，提升加载成功率
- ✅ 改进LLM配置，支持自定义API地址
- ✅ 优化部分成功处理逻辑，保留有效结果

### v2.0 - 2025-01
- ✅ 新增截图OCR智能回退功能
- ✅ 优化多页PDF文档处理
- ✅ 改进链接提取优先级（Source优先）
- ✅ 增强OCR文本清理能力
- ✅ 添加opencv-python支持

### v1.5 - 2024-12
- ✅ Google Sheets支持
- ✅ 联系人验证功能
- ✅ 完整的LLM对话记录

### v1.0 - 2024
- ✅ 基础三阶段分析
- ✅ 多种内容提取方式
- ✅ 本地Excel支持

## 📄 许可证

[MIT License]

## 🤝 贡献

欢迎提交Issue和Pull Request！

---

**💡 提示**：遇到问题时，首先查看对应行的LLM对话记录（`llm_logs/`），这通常能帮助您快速定位问题所在！
