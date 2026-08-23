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


def _iter_jsonld_nodes(html: str):
    """遍历页面所有 ld+json 里的节点（展开 list / @graph），逐个 yield dict。"""
    for m in re.finditer(
        r'(?is)<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>', html
    ):
        try:
            data = json.loads(m.group(1).strip())
        except Exception:
            continue
        if isinstance(data, list):
            nodes = data
        elif isinstance(data, dict) and "@graph" in data:
            nodes = data["@graph"] if isinstance(data["@graph"], list) else [data["@graph"]]
        else:
            nodes = [data]
        for node in nodes:
            if isinstance(node, dict):
                yield node


def _jsonld_is_type(node: dict, type_name: str) -> bool:
    t = node.get("@type")
    return t == type_name or (isinstance(t, list) and type_name in t)


def _jsonld_org_name(v) -> Optional[str]:
    if isinstance(v, dict):
        return v.get("name")
    if isinstance(v, str):
        return v
    return None


def _jsonld_location(v) -> str:
    """JobPosting.jobLocation 可能是 Place / Place 列表 / 字符串，抽成 'City, Region, Country'。"""
    items = v if isinstance(v, list) else [v]
    out = []
    for it in items:
        if isinstance(it, str):
            out.append(it)
            continue
        if not isinstance(it, dict):
            continue
        addr = it.get("address")
        if isinstance(addr, dict):
            seg = []
            for k in ("addressLocality", "addressRegion", "addressCountry"):
                x = addr.get(k)
                seg.append(x.get("name") if isinstance(x, dict) else x)
            out.append(", ".join(s for s in seg if s))
        elif isinstance(addr, str):
            out.append(addr)
        elif it.get("name"):
            out.append(it["name"])
    return "; ".join(p for p in out if p)


def _extract_jobposting_jsonld(html: str) -> str:
    """抽取 schema.org JobPosting 结构化数据（AJO/jobRxiv 及多数上 Google Jobs 的招聘页都嵌）。

    与 _extract_jsonld_body（社媒 articleBody）不同：这里组装职位专有字段——标题 + 招聘单位 +
    地点 + 类型 + 起止日期 + 描述(HTML) + 职责/资格/技能/学历/经验要求。是"平台声明的结构化
    正文"，零渲染零反爬，作为高可信候选进入打分择优。取首个内容足够的 JobPosting。
    """
    for node in _iter_jsonld_nodes(html):
        if not _jsonld_is_type(node, "JobPosting"):
            continue
        etype = node.get("employmentType")
        if isinstance(etype, list):
            etype = ", ".join(str(x) for x in etype)
        org = _jsonld_org_name(node.get("hiringOrganization"))
        loc = _jsonld_location(node.get("jobLocation")) if node.get("jobLocation") else ""
        parts = [
            node.get("title"),
            f"Organization: {org}" if org else None,
            f"Location: {loc}" if loc else None,
            f"Employment type: {etype}" if etype else None,
            f"Posted: {node.get('datePosted')}" if node.get("datePosted") else None,
            f"Closes: {node.get('validThrough')}" if node.get("validThrough") else None,
            _strip_html_fragment(node.get("description")),
        ]
        for k in ("responsibilities", "qualifications", "skills",
                  "educationRequirements", "experienceRequirements"):
            v = node.get(k)
            if not v:
                continue
            if isinstance(v, dict):
                v = v.get("name") or v.get("description") or v.get("credentialCategory") or ""
            elif isinstance(v, list):
                v = " ".join(str(x) for x in v)
            frag = _strip_html_fragment(str(v))
            if frag:
                parts.append(frag)
        text = _normalize_ws("\n".join(p for p in parts if p and str(p).strip()))
        if len(text) >= 120:
            return text
    return ""


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


def _extract_resiliparse(html: str) -> str:
    """resiliparse 正文抽取（高召回、C 实现极快、无 GPU）：作为 trafilatura 的并列候选，
    在 trafilatura 抽空/过短时兜底。未安装则优雅降级返回空（不加入候选）。"""
    try:
        from resiliparse.extract.html2text import extract_plain_text
    except Exception:
        return ""
    try:
        out = extract_plain_text(html, main_content=True, alt_texts=False, links=False)
        return _normalize_ws(out or "")
    except Exception as e:
        logger.debug(f"resiliparse 抽取失败: {e}")
        return ""


# PruningContentFilter 用：整块剔除的外壳标签 + 负向 class/id 关键词
_PRUNE_DROP_TAGS = ["nav", "footer", "header", "aside", "script", "style",
                    "form", "iframe", "noscript"]
# 词边界锚定：避免 "ad" 误伤 "gradient/heading"、"nav" 误伤正常词等子串误命中
_PRUNE_NEG_ATTR = re.compile(
    r"\b(nav|footer|header|sidebar|ads?|advert|comments?|promo|social|shares?|"
    r"cookie|banner|menu|breadcrumb|subscribe|newsletter|related|recommend|"
    r"masthead|popup|modal)\b",
    re.IGNORECASE,
)
# 块级标签重要度（越像正文越高），同时作为"叶块"识别集合
_PRUNE_TAG_IMPORTANCE = {
    "article": 1.5, "main": 1.4, "section": 1.3, "div": 1.0,
    "h1": 1.4, "h2": 1.3, "h3": 1.2, "h4": 1.1,
    "p": 1.2, "li": 1.0, "blockquote": 1.1, "td": 0.9, "dd": 1.0, "figcaption": 0.8,
}
_PRUNE_THRESHOLD = 0.48


def _extract_pruning(html: str) -> str:
    """移植 Crawl4AI 的 PruningContentFilter：纯 DOM 密度剪枝，零 LLM。

    思路：先整块剔除外壳标签与负向 class/id 的块，再对每个"叶块"（不含块级子节点的
    p/li/h/div 等）算综合分——文本密度(0.4)+反链接密度(0.2)+标签重要度(0.2)+
    class/id(0.1)+文本长度(0.1)，各指标归一到 ~[0,1]，低于阈值 0.48 的块判为噪声丢弃，
    保留块按文档顺序拼接。与 trafilatura/resiliparse 并列作打分候选，未装 bs4 则返回空。
    """
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return ""
    try:
        import math

        soup = BeautifulSoup(html, "html.parser")
        for tag in soup(_PRUNE_DROP_TAGS):
            tag.decompose()
        # 护栏：主内容常被包在带任意 class 的大 wrapper 里，若某 wrapper 的 class/id 恰好
        # 撞上负向词，绝不能整棵砍掉。故只对"文本占比不大"的块做负向剪除。
        total_text_len = max(len(soup.get_text(" ", strip=True)), 1)
        # 负向 class/id 的块整体剪除（等价于强惩罚）。find_all 是静态快照，剪掉父节点后
        # 其子孙节点会变成已析构状态（attrs=None），需跳过，否则读属性会抛错。
        for el in soup.find_all(True):
            if getattr(el, "decomposed", False) or el.attrs is None:
                continue
            cls = el.get("class") or []
            attr_str = " ".join(cls) + " " + (el.get("id") or "")
            if not (attr_str.strip() and _PRUNE_NEG_ATTR.search(attr_str)):
                continue
            # 占全页文本 >40% 的块判为主内容 wrapper，跳过（宁可保留噪声也不误杀正文）
            if len(el.get_text(" ", strip=True)) > 0.4 * total_text_len:
                continue
            el.decompose()

        keys = list(_PRUNE_TAG_IMPORTANCE.keys())
        norm_len = math.log(201)  # 200+ 字符视为"足够长"
        parts: List[str] = []
        seen = set()
        for node in soup.find_all(keys):
            # 只取叶块：含块级子节点的容器交给其子块，避免父子文本重复计入
            if node.find(keys):
                continue
            text = node.get_text(" ", strip=True)
            tlen = len(text)
            if tlen < 1:
                continue
            link_text = sum(len(a.get_text(" ", strip=True)) for a in node.find_all("a"))
            link_ratio = link_text / tlen
            m_link = max(0.0, 1.0 - link_ratio)                       # 反链接密度
            m_dens = min(tlen / max(len(str(node)), 1), 1.0)          # 文本/标签密度
            m_tagw = _PRUNE_TAG_IMPORTANCE.get(node.name, 1.0) / 1.5  # 标签重要度
            m_clsid = 1.0                                             # 强负向已提前剪除
            m_tlen = min(math.log(tlen + 1) / norm_len, 1.0)         # 文本长度（对数）
            score = (0.4 * m_dens + 0.2 * m_link + 0.2 * m_tagw
                     + 0.1 * m_clsid + 0.1 * m_tlen)
            if score >= _PRUNE_THRESHOLD:
                key = text[:80]
                if key in seen:
                    continue
                seen.add(key)
                parts.append(text)
        return _normalize_ws("\n".join(parts))
    except Exception as e:
        logger.debug(f"pruning 抽取失败: {e}")
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
        job_ld = _extract_jobposting_jsonld(html)
        if job_ld:
            candidates.append(Candidate("json-ld-job", job_ld))

        jsonld = _extract_jsonld_body(html)
        if jsonld:
            candidates.append(Candidate("json-ld", jsonld))

        traf = _extract_trafilatura(html)
        if traf:
            candidates.append(Candidate("trafilatura", traf))

        resi = _extract_resiliparse(html)
        if resi:
            candidates.append(Candidate("resiliparse", resi))

        pruned = _extract_pruning(html)
        if pruned:
            candidates.append(Candidate("pruning", pruned))

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
        "json-ld-job": (1.0, 1200.0),  # 平台声明的结构化职位数据，最可信
        "json-ld": (1.0, 1200.0),
        "trafilatura": (1.0, 300.0),
        "resiliparse": (1.0, 300.0),
        "pruning": (1.0, 250.0),
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
