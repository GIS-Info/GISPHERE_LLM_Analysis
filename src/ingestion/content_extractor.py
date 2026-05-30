"""
网页正文智能抽取（方案 B：多候选 + 打分择优 + 评论噪声剥离）。

设计目标：不依赖"按域名写死规则"，而是对同一页面并行生成多个正文候选，
再用统一打分函数选出最干净、最完整的一个。覆盖：
  - 社媒帖子（LinkedIn 等）：json-ld articleBody / og:description 是平台声明的正文，零评论；
  - 普通招聘/实验室页：trafilatura(高召回) 保留要点列表；
  - 兜底：渲染后的 innerText / BeautifulSoup 纯文本。

对外主入口：extract_main_text(html, rendered_text=None) -> ExtractionResult
"""
import re
import json
import html as _html
import logging
from dataclasses import dataclass, field
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# 评论区 / 社交 UI 噪声标志（出现越多越像评论流而非正文）
_COMMENT_MARKERS = [
    "report this comment",
    "report this post",
    "to view or add a comment",
    "see more comments",
    "add a comment",
    "like reply",
    "reactions",
    "reaction",
    "show more replies",
]

# 页面外壳 / 导航 / 法务样板噪声
_BOILERPLATE_MARKERS = [
    "skip to main content",
    "sign in",
    "log in",
    "join now",
    "create account",
    "forgot password",
    "cookie policy",
    "cookie settings",
    "accept cookies",
    "privacy policy",
    "terms of service",
    "user agreement",
    "all rights reserved",
    "explore content categories",
    "toggle navigation",
    "back to top",
]

# 反爬验证页标志
_CHALLENGE_MARKERS = [
    "just a moment",
    "verifying you are human",
    "performing security verification",
    "enable javascript and cookies to continue",
    "performance and security by cloudflare",
]


@dataclass
class Candidate:
    method: str
    text: str
    score: float = 0.0
    detail: str = ""


@dataclass
class ExtractionResult:
    method: str
    text: str
    score: float
    candidates: List[Candidate] = field(default_factory=list)

    @property
    def length(self) -> int:
        return len(self.text or "")


def _strip_html_fragment(s: Optional[str]) -> str:
    """把可能带 HTML 标签的片段（如 json-ld articleBody）清洗为纯文本，保留段落换行。"""
    if not s:
        return ""
    s = re.sub(r"(?is)<(script|style)[^>]*>.*?</\1>", " ", s)
    s = re.sub(r"(?i)<br\s*/?>", "\n", s)
    s = re.sub(r"(?i)</(p|div|li|h[1-6]|tr)>", "\n", s)
    s = re.sub(r"<[^>]+>", " ", s)
    s = _html.unescape(s)
    return _normalize_ws(s)


def _normalize_ws(s: str) -> str:
    if not s:
        return ""
    s = s.replace("\u00a0", " ").replace("\ufeff", "")
    s = re.sub(r"[ \t]+", " ", s)
    s = re.sub(r" *\n *", "\n", s)
    s = re.sub(r"\n{3,}", "\n\n", s)
    return s.strip()


def _count_markers(text_lower: str, markers: List[str]) -> int:
    return sum(text_lower.count(m) for m in markers)


def strip_comment_noise(text: str) -> str:
    """剥离社媒评论流：从明显的评论分隔标志处截断，并删除残留的 UI token 行。"""
    if not text:
        return ""
    lowered = text.lower()
    # 找到评论区起点（取最早出现的强标志），从那里截断
    cut_positions = []
    for marker in ("to view or add a comment", "see more comments", "report this comment"):
        idx = lowered.find(marker)
        if idx != -1:
            cut_positions.append(idx)
    if cut_positions:
        text = text[: min(cut_positions)]
    # 逐行清理：删除仅由 UI token 组成的短行
    ui_only = re.compile(
        r"^\s*(like|reply|comment|share|repost|follow|connect|\d+\s*(reaction|reactions|comments?|followers?))\s*$",
        re.IGNORECASE,
    )
    kept = [ln for ln in text.splitlines() if not ui_only.match(ln)]
    return _normalize_ws("\n".join(kept))


def _extract_jsonld_body(html: str) -> str:
    """从 <script type=application/ld+json> 中取最长的 articleBody（其次 description）。"""
    best = ""
    for m in re.finditer(
        r'(?is)<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html
    ):
        raw = m.group(1).strip()
        try:
            data = json.loads(raw)
        except Exception:
            continue
        nodes = data if isinstance(data, list) else (data.get("@graph") if isinstance(data, dict) and "@graph" in data else [data])
        for node in nodes:
            if not isinstance(node, dict):
                continue
            for key in ("articleBody", "description"):
                val = node.get(key)
                if isinstance(val, str) and len(val) > len(best):
                    best = val
    return _strip_html_fragment(best)


def _extract_meta(html: str, prop: str, attr: str = "property") -> str:
    m = re.search(
        rf'(?is)<meta[^>]+{attr}=["\']{re.escape(prop)}["\'][^>]*content=["\'](.*?)["\']', html
    )
    if not m:
        m = re.search(
            rf'(?is)<meta[^>]+content=["\'](.*?)["\'][^>]*{attr}=["\']{re.escape(prop)}["\']', html
        )
    return _normalize_ws(_html.unescape(m.group(1))) if m else ""


def _extract_trafilatura(html: str) -> str:
    try:
        import trafilatura
    except Exception:
        return ""
    try:
        out = trafilatura.extract(html, include_comments=False, favor_recall=True)
        return _normalize_ws(out or "")
    except Exception as e:
        logger.debug(f"trafilatura 抽取失败: {e}")
        return ""


def _soup_text(html: str) -> str:
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return ""
    try:
        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(["script", "style", "nav", "header", "footer", "aside", "form"]):
            tag.decompose()
        return _normalize_ws(soup.get_text("\n"))
    except Exception:
        return ""


def _score_candidate(cand: Candidate, trust: float, prior: float) -> Candidate:
    """对候选打分：有效长度 × 方法信任系数 − 噪声扣分 + 方法先验。

    - trust: 方法精度系数。trafilatura/json-ld 是"主正文抽取器"给 1.0；
      innerText/soup 是整页 body（约一半是导航/页脚噪声）给较低系数，从而让长度噪声
      按比例打折——既能在普通页让干净抽取胜出，又能在 JS 空页时让 innerText 兜底。
    - prior: 小幅可信度加分（json-ld articleBody 是平台声明正文，给最高）。
    """
    text = strip_comment_noise(cand.text)
    cand.text = text
    if not text:
        cand.score = 0.0
        cand.detail = "empty"
        return cand

    lowered = text.lower()
    length = len(text)
    comment_hits = _count_markers(lowered, _COMMENT_MARKERS)
    boiler_hits = _count_markers(lowered, _BOILERPLATE_MARKERS)
    challenge_hits = _count_markers(lowered, _CHALLENGE_MARKERS)

    # 超短行密度（导航、菜单常见）
    lines = [ln for ln in text.splitlines() if ln.strip()]
    short_lines = sum(1 for ln in lines if len(ln) <= 3)
    short_ratio = (short_lines / len(lines)) if lines else 0.0

    penalty = comment_hits * 400 + boiler_hits * 120 + challenge_hits * 5000
    penalty += short_ratio * length * 0.3

    score = length * trust - penalty + prior
    cand.score = max(0.0, score)
    cand.detail = (
        f"len={length} trust={trust:.2f} prior={prior:.0f} comment={comment_hits} "
        f"boiler={boiler_hits} challenge={challenge_hits} short_ratio={short_ratio:.2f}"
    )
    return cand


def extract_main_text(html: Optional[str], rendered_text: Optional[str] = None) -> ExtractionResult:
    """多候选 + 打分择优，返回最优正文。

    Args:
        html: 页面原始 HTML（HTTP 响应或 Playwright page.content()）。
        rendered_text: 可选，Playwright 渲染后的 document.body.innerText（兜底候选）。
    """
    candidates: List[Candidate] = []

    if html:
        jsonld = _extract_jsonld_body(html)
        if jsonld:
            candidates.append(Candidate("json-ld", jsonld))

        traf = _extract_trafilatura(html)
        if traf:
            candidates.append(Candidate("trafilatura", traf))

        og = _extract_meta(html, "og:description") or _extract_meta(html, "description", attr="name")
        if og:
            candidates.append(Candidate("og:description", og))

    if rendered_text and rendered_text.strip():
        candidates.append(Candidate("innerText", _normalize_ws(rendered_text)))

    if html and not candidates:
        soup = _soup_text(html)
        if soup:
            candidates.append(Candidate("soup", soup))

    if not candidates:
        return ExtractionResult(method="none", text="", score=0.0, candidates=[])

    # (信任系数, 先验)：json-ld/trafilatura 是主正文抽取器，trust=1.0；
    # innerText/soup 是整页 body，噪声多，trust 低，按比例打折；og 常被截断。
    weights = {
        "json-ld": (1.0, 1200.0),
        "trafilatura": (1.0, 300.0),
        "og:description": (0.9, -200.0),
        "innerText": (0.40, 0.0),
        "soup": (0.35, 0.0),
    }
    for cand in candidates:
        trust, prior = weights.get(cand.method, (0.5, 0.0))
        _score_candidate(cand, trust, prior)

    candidates.sort(key=lambda c: c.score, reverse=True)
    best = candidates[0]
    return ExtractionResult(method=best.method, text=best.text, score=best.score, candidates=candidates)
