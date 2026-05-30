# 基于LLM的学术机会信息智能分析系统

这是一个批量化的智能文本分析系统，能够从网页、PDF、截图、本地 Excel 以及 Google Sheets 中提取学术机会信息。系统自动获取来源内容，通过大语言模型(LLM)进行三阶段分析，对关键字段进行校验，并将结构化结果自动写回数据表。

## 🚀 快速开始

### 环境要求

- **Python**: 推荐 3.10+
- **New API 网关**: `https://newapi.gisphere.info/v1`
- **Python 依赖**: 见 [`requirements.txt`](requirements.txt)（含 `trafilatura`、`lxml_html_clean` 等；**无需** `openai` SDK，LLM 调用走 `requests`）
- **系统工具**（不通过 pip 安装，需单独配置）:
  - **Node.js + npx**: 联系人/方向验证的 Playwright MCP 依赖 `npx @playwright/mcp`（见 `analysis_stage.py`）
  - **Tesseract OCR**: `pytesseract` 回退路径；需安装 **`eng` + `chi_sim`** 语言包（与 `OCR_LANGUAGE = 'eng+chi_sim'` 一致）
  - **Playwright Chromium**: 动态网页、PDF 预览器截图
- **可选**: Ollama 本地模型（无 API Key 时回退）；`opencv-python`（OCR 图像预处理，未安装时自动跳过）

### 安装步骤

```bash
python -m pip install -r requirements.txt
python -m playwright install chromium
```

安装完成后建议先运行系统自检（见下方「运行」）。

### 配置

#### 配置 LLM 服务

在 `keys/api_key.txt` 中写入 API 密钥（单行）：

```text
your-api-key-here
```

> 若 `api_key.txt` 不存在，程序仍兼容旧的 `keys/openai_key.txt` 文件名。

#### 配置数据源

**Google Sheets 模式**：

1. 将 Google API 凭据放在 `keys/credentials.json`
2. 在 `src/core/config.py` 中设置 `GOOGLE_SPREADSHEET_ID`
3. **首次连接**会弹出浏览器 OAuth 授权，成功后生成 `keys/token.pickle`（已 gitignore，勿提交）

**本地 Excel 模式**：若缺少 Google 凭据，程序自动回退，读取项目根目录的 `text_info.xlsx`（工作表 `Unfilled`）。

### 运行

```bash
# 系统自检（推荐首次运行前执行）
python -m src.tools.check_system

# 处理所有未填写的行
python main.py

# 测试单行处理
python main.py test

# 手动刷新模型清单
python -m src.tools.update_models
```

## 🌟 主要特性

### 内容提取能力

- **多种获取方式**: HTTP、Playwright 动态渲染、PDF 解析、VLM 文档提取、Tesseract OCR
- **智能页面加载**: 网络空闲、关键元素、内容/高度稳定性等多策略（`smart_page_loader`）
- **打分式正文抽取**: JSON-LD / trafilatura / og:description / innerText 多路候选择优（`content_extractor`）
- **逐页 PDF 截图**: 在线 PDF 预览器按页裁剪，保证每张恰好一页
- **VLM 文档提取**: `document_ai` 模块通过 `VISION_MODEL_CHAIN` 调用多模态 LLM（**非** Google Cloud Document AI）
- **多层回退**: 见下方「内容提取回退链」

### LLM 智能分析

- **三阶段分析**:
  - 阶段 1：英文基本信息（截止日期、招生人数、研究方向、机构、联系人）
  - 阶段 2：机会类型与地理学相关专业方向分类
  - 阶段 3：中文机构/国家/微信标签
- **统一 `/chat/completions` 路由**: 文本与图片/文档提取统一走 New API
- **价格优先的模型链回退**: `TEXT_MODEL_CHAIN` / `VISION_MODEL_CHAIN` 由低到高逐个尝试
- **死模型熔断**: 401/403 后冷却 30 分钟自动跳过
- **部分成功**: 某阶段失败仍保存已成功字段；`Error` 列记录问题，`Verifier` 仅在**完全成功**时为 `LLM`

### 联系人与方向验证

- **联系人搜索**: **HTTP DuckDuckGo → Bing** 为主；结果不足时再 **Playwright MCP** snapshot 补充
- **方向验证**: 有 MCP 时抓取网页上下文辅助判定；无 MCP 时退化为 LLM 自身知识
- **严格 URL 过滤**: 白名单机制，只分析可信学术页面
- 可通过 `ENABLE_WEB_SEARCH=0` 禁用 MCP 初始化；`CONTACT_VERIFICATION_ENABLED`（config）可关闭联系人验证逻辑

### 数据管理

- **双模式支持**: 本地 Excel 或云端 Google Sheets
- **断点续传**: 跳过 `Verifier` 与 `Error` 均非空的行（见下方规则）
- **实时保存**: 每行处理完成后立即写回表格与日志

## 📊 项目结构

```text
main.py                  # 根入口（转发至 src.main）
src/
  main.py                # 主流程编排
  core/
    config.py            # 全局配置、字段单一真源、模型链
    api_client.py        # New API 客户端（模型链回退 + 熔断）
    llm_agent.py         # 三阶段 LLM 调用
    analysis_stage.py    # 分析编排、MCP 初始化
    contact_verifier.py  # 联系人搜索与验证
    direction_verifier.py
    utils.py             # JSON 解析、依赖检查等
  ingestion/
    excel_handler.py     # Excel / Sheets 读写
    fetch_text.py        # 内容获取总编排
    content_extractor.py # 打分式 HTML 正文抽取
    text_quality.py      # 文本清洗与质量评估
    pdf_extractor.py     # PDF 多后端提取
    google_sources.py    # Google Drive / Docs 处理
    document_ai.py       # VLM 图片/PDF 文字提取
    screenshot_ocr_fetcher.py
  integrations/
    google_sheets_handler.py
    mcp_client.py        # Playwright MCP 客户端
  browser/
    playwright_worker.py / playwright_process_manager.py
    smart_page_loader.py
  tools/
    check_system.py      # 系统自检
    update_models.py     # MODELS.md 生成器
keys/                    # api_key、credentials.json、token.pickle（勿提交）
cache/                   # pdf/、screenshots/
logs/                    # run.log
llm_logs/                # 按行保存的 LLM 对话
MODELS.md
requirements.txt
LICENSE
```

## ⚙️ LLM 配置

主要配置位于 [`src/core/config.py`](src/core/config.py)（**以下与当前代码一致**）：

```python
API_BASE_URL = "https://newapi.gisphere.info/v1"

TEXT_MODEL_CHAIN = ["gpt-5.4-mini", "gemini-2.5-flash", "claude-opus-4.5"]
VISION_MODEL_CHAIN = ["gpt-5.4-mini", "gemini-2.5-flash", "claude-opus-4.5"]

MODEL_COOLDOWN_SECONDS = 1800  # 401/403 熔断时长（秒）
```

文本分析与 VLM 提取均走 **`/chat/completions`**。模型清单见 [`MODELS.md`](MODELS.md)。字段清单：`STAGE1_FIELDS` / `STAGE2_FIELDS` / `STAGE3_FIELDS` / `GEO_FIELDS`。

### 高级配置开关（config.py，非环境变量）

| 配置项 | 默认 | 说明 |
|--------|------|------|
| `USE_PLAYWRIGHT` | `True` | 是否启用 Playwright 子进程抓取 |
| `USE_DOCUMENT_AI` | `True` | 是否优先 VLM 提取 PDF/图片文字 |
| `USE_SCREENSHOT_OCR` | `True` | 是否在常规提取失败后截图 OCR |
| `CONTACT_VERIFICATION_ENABLED` | `True` | 是否执行联系人验证流程 |
| `OCR_LANGUAGE` | `eng+chi_sim` | Tesseract 语言；需安装对应语言包 |

## 🔄 处理流程

1. 加载 Google Sheets 或本地 Excel。
2. 从 `Source`（优先）或 `Notes` 提取 URL。
3. 按回退链获取正文（见下节）。
4. 三阶段 LLM 分析（可部分成功）。
5. 可选：联系人 HTTP/MCP 搜索、方向 MCP 辅助判定。
6. 写回结果、`Verifier` / `Error`，保存 `llm_logs/`。

### 内容提取回退链

**普通网页**

1. HTTP + 打分式正文抽取（`content_extractor`）
2. Playwright 动态渲染 + 再抽取
3. 长页/难页截图 → VLM（`document_ai`）→ Tesseract OCR

**直链 PDF**

1. VLM（`document_ai`，`VISION_MODEL_CHAIN`）
2. PyMuPDF → pdfplumber → PyPDF2
3. 截图 OCR

**在线 PDF 预览（腾讯文档等）**

1. Playwright 逐页裁剪截图 → VLM → OCR

**Google Drive / Google Docs**

- 专用导出或 Playwright 路径（`google_sources.py`）

## 📋 数据表列说明

### 输入列

| 列名 | 说明 |
|------|------|
| `Source` | **首选**链接来源 |
| `Notes` | 备用链接；仅当 `Source` 无有效 URL 时使用 |

### 输出列（分析结果）

阶段 1–3 字段见「分析字段说明」。此外还有：

| 列名 | 说明 |
|------|------|
| `Verifier` | 完全成功时为 `LLM`；部分成功或失败时通常为空 |
| `Error` | 错误或提示信息；**非空时下次运行也会跳过该行** |

### `Error` 列常见值

| 内容 | 含义 |
|------|------|
| 阶段失败摘要 | 如某阶段 LLM 失败、格式校验失败等 |
| `需转换链接` | 使用了 `Notes` 中的链接且 otherwise 成功；建议将链接移到 `Source` |
| `可能是第三方网址` | 链接来自 LinkedIn 等第三方平台（`Source` 列） |
| 抓取失败信息 | 如无有效正文、超时等 |

### 跳过与重跑规则

系统只处理 **`Verifier` 与 `Error` 均为空** 的行：

| 状态 | 下次运行 |
|------|----------|
| `Verifier=LLM`，`Error` 空 | **跳过**（完全成功） |
| `Verifier` 空，`Error` 有内容 | **跳过**（部分成功或已记录失败） |
| 两者皆空 | **会处理** |

若要**重跑**某行：在表格中清空该行的 `Error`（若曾完全成功，还需清空 `Verifier`）。`Ctrl+C` 中断后，已保存的行按上表规则决定是否跳过。

## 🔧 分析字段说明

> 字段名以 `config.py` 中 `STAGE*_FIELDS` / `GEO_FIELDS` 为单一真源。

### 阶段 1：英文基本信息

| 字段 | 说明 |
|------|------|
| Deadline | YYYY-MM-DD 或 "Soon" |
| Number_Places | 招生人数 |
| Direction | 研究方向 |
| University_EN | 机构英文全称 |
| Contact_Name | 联系人（含 Dr./Mr./Ms.） |
| Contact_Email | 联系邮箱 |

### 阶段 2：类型与专业分类

**招生类型**（`"1"` = 适用）：Master Student, Doctoral Student, PostDoc, Research Assistant, Competition, Summer School, Conference, Workshop

**专业方向**（**1–3** 个，`"1"` = 适用）：Physical_Geo, Human_Geo, Urban, GIS, RS, GNSS

### 阶段 3：中文字段

| 字段 | 说明 |
|------|------|
| University_CN | 机构中文全称 |
| Country_CN | 国家中文名 |
| WX_Label1-5 | 微信标签（Label1 必填，单标签 ≤6 字） |

## 🔩 环境变量

| 变量 | 默认值 | 说明 |
|------|--------|------|
| `ENABLE_WEB_SEARCH` | `1` | `0` = 不初始化 Playwright MCP（方向 MCP 上下文不可用；联系人仍可用 HTTP 搜索） |
| `PLAYWRIGHT_MCP_HEADLESS` | `0` | `1` = MCP 浏览器无头模式 |
| `PLAYWRIGHT_HEADLESS` | `0` | 主 Playwright worker 是否无头；默认有头 |
| `MODEL_COOLDOWN_SECONDS` | `1800` | 模型 401/403 熔断秒数 |
| `FORCE_IPV4` | `true` | 强制 IPv4（`src/main.py`） |

## 🛠️ 常见问题

**Q: pip 安装通过后还需要什么？**

1. `python -m playwright install chromium`
2. **Tesseract** + **`chi_sim`** 语言包（Linux: `tesseract-ocr-chi-sim`）
3. **Node.js**（提供 `npx`，供 MCP 使用）

运行 `python -m src.tools.check_system` 检查 Python 包、Tesseract、Chromium、Node/npx。

**Q: 如何查看详细错误？**

- [`logs/run.log`](logs/run.log)
- [`llm_logs/`](llm_logs/)（`row_XXXX_*.txt`）

**Q: 部分成功算成功吗？**

算。部分字段会写入表格，`Error` 记录问题，`Verifier` 不会设为 `LLM`；统计上 `_process_single_row` 仍返回成功。

**Q: 如何重跑失败/部分成功的行？**

清空该行的 `Error`（必要时清空 `Verifier`）。

**Q: Document AI 要配 Google Cloud 吗？**

不需要。本项目 `document_ai` 走的是 New API 上的 **VLM 模型**（`VISION_MODEL_CHAIN`），不是 Google Cloud Document AI 服务。

## 🚑 运维与排错

| 现象 | 可能原因 | 处理建议 |
|------|----------|----------|
| LLM 401/403 | Key 或模型不可用 | 检查 `keys/api_key.txt`、[`MODELS.md`](MODELS.md)；等熔断或换链 |
| 模型被跳过 | 熔断中 | 调整链或等 `MODEL_COOLDOWN_SECONDS` |
| 网页正文极少 | 登录墙/反爬 | `PLAYWRIGHT_HEADLESS=0`；LinkedIn 等可能只有摘要 |
| PDF 截图空白 | 渲染未完成 | 看 `cache/screenshots/`；增大 `SCREENSHOT_PAGE_RENDER_WAIT_MS` |
| OCR 乱码/无中文 | 缺 chi_sim | 安装 `tesseract-ocr-chi-sim` |
| MCP 初始化失败 | 无 Node/npx | 安装 Node.js；HTTP 搜索仍可用 |
| 联系人搜不到 | MCP 与 HTTP 均失败 | 查网络；看 `logs/run.log` 中 DuckDuckGo/Bing 段落 |
| 阶段 2 GEO >3 | LLM 超限 | 查 `llm_logs` stage2；系统自动拒绝 |
| Sheets 首次失败 | 未 OAuth | 删除错误 `token.pickle` 重跑，完成浏览器授权 |

**排错顺序**：`llm_logs/row_*.txt` → `logs/run.log` → `python -m src.tools.check_system`

## 🗓️ 更新日志

### v3.1 - 2026-05

- ✅ LLM `/chat/completions` + 模型链回退 + 401/403 熔断
- ✅ 打分式正文抽取、逐页 PDF 截图、Playwright 默认 headful
- ✅ [`MODELS.md`](MODELS.md) 多模态分表、config 默认链快照
- ✅ `fetch_text` 模块化；core 字段/JSON 统一；阶段 2 GEO **1–3** 个
- ✅ 文档补全：跳过规则、HTTP+MCP 搜索、VLM 说明、OAuth、回退链、Error 语义、Node/Tesseract
- ✅ `check_system` 系统工具检查；MIT [`LICENSE`](LICENSE)

## 📄 许可证

[MIT License](LICENSE)

## 🤝 贡献

欢迎提交 Issue 和 Pull Request！

---

**💡 提示**：遇到问题先查该行的 `llm_logs/`，再查 `logs/run.log`。
