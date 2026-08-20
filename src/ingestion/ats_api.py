# -*- coding: utf-8 -*-
"""
已知 ATS（第三方招聘系统）直连 JSON 接口。

Workday / Greenhouse / Lever 等平台的职位正文由前端 JS 客户端渲染，纯 HTTP 只能拿到
外壳，Playwright 又常被 Cloudflare 拦。但它们的数据其实来自**公开 JSON 接口**，可按
URL 直接命中，免渲染、免反爬、毫秒级返回。

用法：fetch_ats_content(url) -> Optional[str]
    命中已知 ATS 且成功取到正文则返回纯文本，否则返回 None（交由通用抓取流程回退）。

参考：
    Workday    POST/GET https://{tenant}.wd{N}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/job/{path}
    Greenhouse GET      https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{id}
    Lever      GET      https://api.lever.co/v0/postings/{company}/{id}?mode=json
"""
import html as _html
import logging
import re
from typing import Optional
from urllib.parse import urlparse, parse_qs

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_TIMEOUT = 20


def _get_json(url: str, method: str = "GET", json_body: Optional[dict] = None):
    """请求 ATS JSON 接口，失败返回 None。"""
    headers = {"User-Agent": _UA, "Accept": "application/json"}
    try:
        if method == "POST":
            resp = requests.post(url, headers=headers, json=json_body, timeout=_TIMEOUT)
        else:
            resp = requests.get(url, headers=headers, timeout=_TIMEOUT)
        resp.raise_for_status()
        return resp.json()
    except Exception as e:
        logger.warning(f"ATS 接口请求失败 [{url}]: {e}")
        return None


def _html_to_text(raw: Optional[str]) -> str:
    """把（可能被 HTML 实体编码的）HTML 片段转为纯文本。"""
    if not raw:
        return ""
    # Greenhouse 的 content 是二次实体编码（&lt;div&gt;），先反转义再解析
    unescaped = _html.unescape(raw)
    text = BeautifulSoup(unescaped, "html.parser").get_text("\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return text.strip()


def _join(parts) -> str:
    return "\n".join(p for p in parts if p and str(p).strip())


# ──────────────────────────────────────────────────────────────────
# Greenhouse
# ──────────────────────────────────────────────────────────────────
def _greenhouse_ids(url: str):
    """从 Greenhouse 各类 URL 中解析 (token, job_id)。"""
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    path = parsed.path
    qs = parse_qs(parsed.query)

    # embed 形式：/embed/job_app?token={job_id}&for={board_token}
    if "embed" in path and "for" in qs:
        job_id = (qs.get("token") or qs.get("gh_jid") or [None])[0]
        return qs["for"][0], job_id

    # 标准：boards.greenhouse.io/{token}/jobs/{id} 或 job-boards.greenhouse.io/...
    m = re.search(r"/([^/]+)/jobs/(\d+)", path)
    if m and ("greenhouse" in host):
        return m.group(1), m.group(2)

    # 子域：{token}.greenhouse.io，job id 走 gh_jid 查询参数
    m = re.match(r"([^.]+)\.greenhouse\.io", host)
    if m:
        job_id = (qs.get("gh_jid") or [None])[0]
        return m.group(1), job_id
    return None, None


def _fetch_greenhouse(url: str) -> Optional[str]:
    token, job_id = _greenhouse_ids(url)
    if not token or not job_id:
        return None
    data = _get_json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}")
    if not data:
        return None
    loc = (data.get("location") or {}).get("name")
    return _join([
        data.get("title"),
        f"Company: {data.get('company_name')}" if data.get("company_name") else None,
        f"Location: {loc}" if loc else None,
        f"Application deadline: {data.get('application_deadline')}" if data.get("application_deadline") else None,
        _html_to_text(data.get("content")),
    ]) or None


# ──────────────────────────────────────────────────────────────────
# Lever
# ──────────────────────────────────────────────────────────────────
def _fetch_lever(url: str) -> Optional[str]:
    parsed = urlparse(url)
    m = re.match(r"/([^/]+)/([0-9a-f-]{16,})", parsed.path)
    if not m:
        return None
    company, posting_id = m.group(1), m.group(2)
    data = _get_json(f"https://api.lever.co/v0/postings/{company}/{posting_id}?mode=json")
    if not data:
        return None
    if isinstance(data, list):
        data = data[0] if data else {}
    if not data:
        return None
    cats = data.get("categories") or {}
    lists_text = []
    for lst in (data.get("lists") or []):
        lists_text.append(_join([lst.get("text"), _html_to_text(lst.get("content"))]))
    return _join([
        data.get("text"),
        f"Location: {cats.get('location')}" if cats.get("location") else None,
        f"Commitment: {cats.get('commitment')}" if cats.get("commitment") else None,
        f"Team: {cats.get('team')}" if cats.get("team") else None,
        data.get("descriptionPlain") or _html_to_text(data.get("description")),
        _join(lists_text),
        data.get("additionalPlain") or _html_to_text(data.get("additional")),
    ]) or None


# ──────────────────────────────────────────────────────────────────
# Workday
# ──────────────────────────────────────────────────────────────────
def _fetch_workday(url: str) -> Optional[str]:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    if "myworkdayjobs.com" not in host:
        return None
    tenant = host.split(".")[0]
    segments = [s for s in parsed.path.split("/") if s]
    # 路径形如 /{locale?}/{site}/job/{loc}/{title}_{JR}；以 'job' 段为界
    if "job" not in segments:
        return None
    ji = segments.index("job")
    if ji == 0:
        return None
    site = segments[ji - 1]
    job_path = "/" + "/".join(segments[ji:])  # /job/{loc}/{title}_{JR}
    api = f"https://{host}/wday/cxs/{tenant}/{site}{job_path}"
    data = _get_json(api)
    if not data:
        return None
    info = data.get("jobPostingInfo") or {}
    if not info:
        return None
    return _join([
        info.get("title"),
        f"Location: {info.get('location')}" if info.get("location") else None,
        f"Posted: {info.get('postedOn')}" if info.get("postedOn") else None,
        f"Start date: {info.get('startDate')}" if info.get("startDate") else None,
        _html_to_text(info.get("jobDescription")),
    ]) or None


# ──────────────────────────────────────────────────────────────────
# 分发
# ──────────────────────────────────────────────────────────────────
def detect_ats_kind(url: str) -> Optional[str]:
    """按 URL 判断可直连 JSON 的 ATS 类型（仅覆盖已实现直连的）。"""
    host = (urlparse(url).netloc or "").lower()
    if "myworkdayjobs.com" in host:
        return "workday"
    if "greenhouse.io" in host:
        return "greenhouse"
    if "jobs.lever.co" in host or "lever.co" in host:
        return "lever"
    return None


_DISPATCH = {
    "workday": _fetch_workday,
    "greenhouse": _fetch_greenhouse,
    "lever": _fetch_lever,
}


def fetch_ats_content(url: str) -> Optional[str]:
    """命中已知 ATS 则直连 JSON 取正文，返回纯文本；否则返回 None。"""
    if not url:
        return None
    kind = detect_ats_kind(url)
    if not kind:
        return None
    try:
        logger.info(f"识别到可直连 JSON 的 ATS: {kind}，尝试接口直取（免渲染）")
        text = _DISPATCH[kind](url)
        if text and len(text.strip()) >= 200:
            logger.info(f"✅ ATS[{kind}] 直连成功，正文 {len(text)} 字符")
            return text.strip()
        logger.info(f"ATS[{kind}] 直连未取到实质正文，回退通用抓取")
    except Exception as e:
        logger.warning(f"ATS[{kind}] 直连异常，回退通用抓取: {e}")
    return None
