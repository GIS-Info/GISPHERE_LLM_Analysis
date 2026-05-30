"""
MODELS.md 生成器：从 New API 网关定价接口拉取可用模型，生成根目录 MODELS.md。

列出每个模型：是否支持多模态（文本+图片/视觉输入）、输入/输出价格（美元/百万 tokens）、所属平台、可用端点。
供使用者按喜好在 src/core/config.py 的 TEXT_MODEL_CHAIN / VISION_MODEL_CHAIN 中自行切换。

价格换算（已对 gpt-4o-mini=$0.15/$0.60、claude-opus-4.5=$5/$25、gemini-2.5-flash=$0.30/$2.50 校验）：
    输入 $/1M = model_ratio × 2
    输出 $/1M = model_ratio × completion_ratio × 2

用法：
    python -m src.tools.update_models           # 强制重新生成
    在主流程中调用 maybe_update_monthly() 实现"每月首次运行自动更新"。
"""
import logging
import re
from datetime import date
from pathlib import Path
from typing import List, Optional

import requests

logger = logging.getLogger(__name__)

PRICING_URL = "https://newapi.gisphere.info/api/pricing"
# 每 1 倍率对应的价格（美元/百万 tokens）
BASE_PRICE_PER_1M = 2.0

MODELS_MD_PATH = Path(__file__).resolve().parents[2] / "MODELS.md"
_GENERATED_RE = re.compile(r"<!--\s*generated:\s*(\d{4})-(\d{2})-\d{2}\s*-->")

# 视觉(图片)能力按模型名族判定（定价接口未直接提供 multimodal 字段）
_VISION_PATTERNS = (
    "gpt-4o", "gpt-4.1", "gpt-5", "o4", "o3",
    "gemini", "claude", "pixtral", "qwen-vl", "qwen2-vl", "qwen2.5-vl",
    "llava", "document-ai", "vision", "-vl",
)
# 明确仅文本 / 非对话多模态的模型（即使名字命中 vision 族也排除）
_TEXT_ONLY_PATTERNS = (
    "text-embedding", "embedding", "imagen", "gpt-image", "dall-e", "dall",
)


def _platform_of(model_name: str, owner_by: str = "") -> str:
    nm = model_name.lower()
    if nm.startswith(("gpt", "o1", "o3", "o4", "chatgpt", "text-", "dall")):
        return "OpenAI"
    if nm.startswith("gemini") or "gemini" in nm:
        return "Google"
    if nm.startswith("claude"):
        return "Anthropic"
    if nm.startswith("deepseek"):
        return "DeepSeek"
    if nm.startswith("kimi") or nm.startswith("moonshot"):
        return "Moonshot"
    if nm.startswith("mistral"):
        return "Mistral"
    if nm.startswith("qwen"):
        return "Qwen"
    return owner_by or "其他"


def _supports_vision(model_name: str) -> bool:
    """是否支持多模态（文本 + 图片/视觉输入）。纯 embedding / 生图模型返回 False。"""
    nm = model_name.lower()
    if any(p in nm for p in _TEXT_ONLY_PATTERNS):
        return False
    return any(p in nm for p in _VISION_PATTERNS)


def _format_price(val: Optional[float]) -> str:
    return f"{val:.3f}" if val is not None else "—"


def _format_row_note(base_note: str, endpoints: str) -> str:
    """合并备注与端点：仅 openai 时不重复展示端点列。"""
    ep = (endpoints or "").strip()
    if ep and ep != "openai":
        ep_part = f"端点: {ep}"
        return f"{base_note}; {ep_part}" if base_note else ep_part
    return base_note


def _append_model_table(lines: List[str], rows: List[dict]) -> None:
    """向 lines 追加一张模型表（端点信息并入备注列）。"""
    lines.extend([
        "| 模型 | 平台 | 输入 $/1M | 输出 $/1M | 备注 |",
        "|:---|:---|---:|---:|:---|",
    ])
    for r in rows:
        note = _format_row_note(r["note"], r["endpoints"])
        lines.append(
            f"| `{r['name']}` | {r['platform']} "
            f"| {_format_price(r['in'])} | {_format_price(r['out'])} "
            f"| {note} |"
        )


def _load_default_chains():
    """读取 config.py 中的默认模型链（生成文档引用块）。"""
    try:
        from ..core.config import TEXT_MODEL_CHAIN, VISION_MODEL_CHAIN
        return list(TEXT_MODEL_CHAIN), list(VISION_MODEL_CHAIN)
    except Exception:
        return [], []


def fetch_pricing(api_key: str, timeout: int = 20) -> List[dict]:
    """拉取定价数据，返回模型行列表。"""
    resp = requests.get(
        PRICING_URL, headers={"Authorization": f"Bearer {api_key}"}, timeout=timeout
    )
    resp.raise_for_status()
    payload = resp.json()
    rows = payload.get("data") or payload.get("models") or []
    if not rows:
        raise RuntimeError("定价接口返回空数据")
    return rows


def _prices(row: dict):
    """返回 (输入$/1M, 输出$/1M, 备注)。"""
    quota_type = row.get("quota_type", 0)
    model_price = row.get("model_price", 0) or 0
    if quota_type == 1 and model_price:
        return None, None, f"按次计费 ${model_price}/次"
    ratio = row.get("model_ratio", 0) or 0
    completion_ratio = row.get("completion_ratio", 1) or 1
    in_price = ratio * BASE_PRICE_PER_1M
    out_price = ratio * completion_ratio * BASE_PRICE_PER_1M
    return in_price, out_price, ""


def generate_markdown(rows: List[dict]) -> str:
    """生成 MODELS.md 内容。"""
    parsed = []
    for row in rows:
        name = row.get("model_name", "")
        if not name:
            continue
        in_p, out_p, note = _prices(row)
        parsed.append({
            "name": name,
            "platform": _platform_of(name, row.get("owner_by", "")),
            "vision": _supports_vision(name),
            "in": in_p,
            "out": out_p,
            "note": note,
            "endpoints": ", ".join(row.get("supported_endpoint_types", []) or []),
            "sort_key": in_p if in_p is not None else 1e9,
        })
    parsed.sort(key=lambda r: (r["sort_key"], r["name"]))
    multimodal = [r for r in parsed if r["vision"]]
    others = [r for r in parsed if not r["vision"]]
    text_chain, vision_chain = _load_default_chains()

    today = date.today().isoformat()
    lines = [
        "# 可用模型清单（Available Models）",
        "",
        f"<!-- generated: {today} -->",
        f"> 自动生成于 **{today}**，数据来源：[New API 网关定价]({PRICING_URL})。",
        "> 每月首次运行主流程时会自动刷新；也可手动运行 `python -m src.tools.update_models`。",
        "> 项目总览与 LLM 配置说明见 [README.md](README.md#-llm-配置)。",
        "",
        "## 如何切换模型",
        "",
        "编辑 [`src/core/config.py`](src/core/config.py) 中的两条模型链（按价格由低到高，运行时逐个回退）：",
        "",
        "- `TEXT_MODEL_CHAIN`：三阶段文本分析（**全部模型**均可选）；",
        "- `VISION_MODEL_CHAIN`：图片/文档(VLM)提取，**仅从下方「多模态模型」表选取**。",
        "",
        "## 当前默认模型链（config.py）",
        "",
        "```python",
        f"TEXT_MODEL_CHAIN = {text_chain!r}",
        f"VISION_MODEL_CHAIN = {vision_chain!r}",
        "```",
        "",
        "> 以上为生成时自 `config.py` 读取的快照；修改模型链后重新运行 `python -m src.tools.update_models` 可刷新本节。",
        "",
        "## 价格说明",
        "",
        "价格单位为 **美元 / 百万 tokens (USD / 1M tokens)**，换算自网关倍率：",
        "`输入 = model_ratio × 2`，`输出 = model_ratio × completion_ratio × 2`。",
        "",
        "多模态能力由模型名族推断（定价接口未提供该字段），个别模型请以平台文档为准。",
        "非 `openai` 端点（如 `gemini, openai`）已并入「备注」列。",
        "",
        f"## 多模态模型（{len(multimodal)} 个，可用于 VISION_MODEL_CHAIN）",
        "",
        "支持**文本 + 图片/视觉输入**，可用于 Document AI 与截图 OCR 的 VLM 路径。",
        "",
    ]
    _append_model_table(lines, multimodal)

    lines.extend([
        "",
        f"## 其他模型（{len(others)} 个，仅文本 / embedding / 生图等）",
        "",
        "不适合作为 `VISION_MODEL_CHAIN` 候选；embedding 与纯生图模型不可用于对话式多模态输入。",
        "",
        "> **说明**：`kimi-k2.6`、`deepseek-v4-flash` 等因模型名未命中多模态推断规则而归入本节；",
        "> 若平台文档确认支持图片输入，可手动加入 `VISION_MODEL_CHAIN`，",
        "> 并在 `src/tools/update_models.py` 的 `_VISION_PATTERNS` 中补充对应规则。",
        "",
    ])
    _append_model_table(lines, others)

    lines.extend([
        "",
        f"> 共 **{len(parsed)}** 个模型：多模态 **{len(multimodal)}** 个，其他 **{len(others)}** 个。",
        "",
    ])
    return "\n".join(lines)


def write_models_md(api_key: str, path: Path = MODELS_MD_PATH) -> bool:
    """拉取定价并写入 MODELS.md，成功返回 True。"""
    try:
        rows = fetch_pricing(api_key)
        content = generate_markdown(rows)
        path.write_text(content, encoding="utf-8")
        logger.info(f"✅ MODELS.md 已更新: {path}")
        return True
    except Exception as e:
        logger.warning(f"生成 MODELS.md 失败: {e}")
        return False


def _needs_monthly_update(path: Path = MODELS_MD_PATH) -> bool:
    """文件不存在，或其中记录的生成月份不是当前月份时，需要更新。"""
    if not path.exists():
        return True
    try:
        m = _GENERATED_RE.search(path.read_text(encoding="utf-8"))
        if not m:
            return True
        gen_year, gen_month = int(m.group(1)), int(m.group(2))
        today = date.today()
        return (gen_year, gen_month) != (today.year, today.month)
    except Exception:
        return True


def maybe_update_monthly(path: Path = MODELS_MD_PATH) -> bool:
    """每月首次运行时自动刷新 MODELS.md。返回是否执行了更新。"""
    if not _needs_monthly_update(path):
        return False
    try:
        from ..core.config import check_api_key
        api_key = check_api_key()
    except Exception:
        api_key = None
    if not api_key:
        logger.info("无 API Key，跳过 MODELS.md 自动更新")
        return False
    logger.info("检测到 MODELS.md 需要按月更新，正在刷新...")
    return write_models_md(api_key, path)


def main():
    logging.basicConfig(level=logging.INFO)
    try:
        from ..core.config import check_api_key
    except ImportError:
        import sys
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from src.core.config import check_api_key
    api_key = check_api_key()
    if not api_key:
        logger.error("未找到 API Key（keys/api_key.txt）")
        return
    write_models_md(api_key)


if __name__ == "__main__":
    main()
