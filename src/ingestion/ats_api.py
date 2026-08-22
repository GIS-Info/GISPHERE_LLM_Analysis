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
import xml.etree.ElementTree as ET
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


def _get_text(url: str) -> Optional[str]:
    """请求文本型接口（如 Personio 的 XML feed），失败返回 None。"""
    headers = {"User-Agent": _UA}
    try:
        resp = requests.get(url, headers=headers, timeout=_TIMEOUT)
        resp.raise_for_status()
        # requests 对未带 charset 的 text/* 默认按 ISO-8859-1 解码，会把 UTF-8 正文
        # 变成乱码（如右单引号变 â）。ATS feed 实际都是 UTF-8，直接按 UTF-8 解。
        return resp.content.decode("utf-8", errors="replace")
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
# Ashby
# ──────────────────────────────────────────────────────────────────
def _fetch_ashby(url: str) -> Optional[str]:
    parsed = urlparse(url)
    segments = [s for s in parsed.path.split("/") if s]
    if len(segments) < 2:
        return None  # 仅有 job board 列表页，无法定位单条职位
    org, job_id = segments[0], segments[1]
    board = _get_json(f"https://api.ashbyhq.com/posting-api/job-board/{org}?includeCompensation=true")
    if not board:
        return None
    job = next((j for j in (board.get("jobs") or []) if j.get("id") == job_id), None)
    if not job:
        return None
    return _join([
        job.get("title"),
        f"Location: {job.get('location')}" if job.get("location") else None,
        f"Type: {job.get('employmentType')}" if job.get("employmentType") else None,
        f"Department: {job.get('department')}" if job.get("department") else None,
        job.get("descriptionPlain") or _html_to_text(job.get("descriptionHtml")),
    ]) or None


# ──────────────────────────────────────────────────────────────────
# SmartRecruiters
# ──────────────────────────────────────────────────────────────────
def _fetch_smartrecruiters(url: str) -> Optional[str]:
    parsed = urlparse(url)
    segments = [s for s in parsed.path.split("/") if s]
    if len(segments) < 2:
        return None
    company = segments[0]
    # 第二段形如 {postingId}-{slug}，postingId 为纯数字
    m = re.match(r"(\d+)", segments[1])
    if not m:
        return None
    posting_id = m.group(1)
    data = _get_json(f"https://api.smartrecruiters.com/v1/companies/{company}/postings/{posting_id}")
    if not data:
        return None
    loc = data.get("location") or {}
    loc_str = ", ".join(x for x in [loc.get("city"), loc.get("country")] if x)
    sections = (data.get("jobAd") or {}).get("sections") or {}
    section_text = []
    for sec in sections.values():
        if isinstance(sec, dict):
            section_text.append(_join([sec.get("title"), _html_to_text(sec.get("text"))]))
    return _join([
        data.get("name"),
        f"Location: {loc_str}" if loc_str else None,
        _join(section_text),
    ]) or None


# ──────────────────────────────────────────────────────────────────
# Recruitee
# ──────────────────────────────────────────────────────────────────
def _fetch_recruitee(url: str) -> Optional[str]:
    parsed = urlparse(url)
    company = parsed.netloc.lower().split(".")[0]
    segments = [s for s in parsed.path.split("/") if s]
    # 单条职位 URL 形如 /o/{slug} 或 /careers/{id}；取末段用于匹配
    target = segments[-1] if segments else ""
    if not target:
        return None
    data = _get_json(f"https://{company}.recruitee.com/api/offers/")
    if not data:
        return None
    offers = data.get("offers") or []
    # 优先按 slug 精确匹配，其次按末段中的数字 id
    m = re.search(r"(\d+)$", target)
    tail_id = m.group(1) if m else None
    offer = next((o for o in offers if o.get("slug") == target), None)
    if not offer and tail_id:
        offer = next((o for o in offers if str(o.get("id")) == tail_id), None)
    if not offer:
        return None
    loc = offer.get("location") or offer.get("city")
    return _join([
        offer.get("title"),
        f"Location: {loc}" if loc else None,
        f"Department: {offer.get('department')}" if offer.get("department") else None,
        f"Employment type: {offer.get('employment_type_code') or offer.get('employment_type')}"
        if (offer.get("employment_type_code") or offer.get("employment_type")) else None,
        _html_to_text(offer.get("description")),
        _html_to_text(offer.get("requirements")),
    ]) or None


# ──────────────────────────────────────────────────────────────────
# Workable
# ──────────────────────────────────────────────────────────────────
def _fetch_workable(url: str) -> Optional[str]:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    segments = [s for s in parsed.path.split("/") if s]
    # 兼容两种域名布局：
    #   apply.workable.com/{slug}/j/{shortcode}/
    #   {slug}.workable.com/j/{shortcode} 或 /jobs/{shortcode}
    slug = segments[0] if host.startswith("apply.") and segments else host.split(".")[0]
    if not slug:
        return None
    shortcode = None
    if "j" in segments:
        i = segments.index("j")
        if i + 1 < len(segments):
            shortcode = segments[i + 1]
    if not shortcode:
        # 兜底：Workable shortcode 形如大写字母数字串
        cand = segments[-1] if segments else ""
        if re.fullmatch(r"[A-Z0-9]{6,}", cand):
            shortcode = cand
    if not shortcode:
        return None
    data = _get_json(f"https://apply.workable.com/api/v1/widget/accounts/{slug}?details=true")
    if not data:
        return None
    job = next((j for j in (data.get("jobs") or []) if str(j.get("shortcode")) == shortcode), None)
    if not job:
        return None
    loc = job.get("location") or {}
    if isinstance(loc, dict):
        loc_str = ", ".join(x for x in [loc.get("city"), loc.get("country")] if x)
    else:
        loc_str = str(loc)
    return _join([
        job.get("title"),
        f"Location: {loc_str}" if loc_str else None,
        f"Employment type: {job.get('employment_type')}" if job.get("employment_type") else None,
        _html_to_text(job.get("description")),
        _html_to_text(job.get("requirements")),
        _html_to_text(job.get("benefits")),
    ]) or None


# ──────────────────────────────────────────────────────────────────
# Personio（XML feed，含全部职位全文）
# ──────────────────────────────────────────────────────────────────
def _fetch_personio(url: str) -> Optional[str]:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    qs = parse_qs(parsed.query)
    segments = [s for s in parsed.path.split("/") if s]
    # 职位 id 可能在 path(/job/{id}) 或 query(?id=/?jobId=)
    job_id = None
    if "job" in segments:
        i = segments.index("job")
        if i + 1 < len(segments):
            mm = re.match(r"\d+", segments[i + 1])
            job_id = mm.group(0) if mm else None
    if not job_id:
        for k in ("id", "jobId", "job_id"):
            if qs.get(k):
                job_id = qs[k][0]
                break
    xml_text = _get_text(f"https://{host}/xml?language=en")
    if not xml_text:
        return None
    try:
        root = ET.fromstring(xml_text)
    except ET.ParseError:
        return None
    positions = root.findall(".//position")
    target = next((p for p in positions if (p.findtext("id") or "").strip() == job_id), None) if job_id else None
    # URL 未能解析出 id 且 feed 仅一个职位时，直接用之
    if target is None and not job_id and len(positions) == 1:
        target = positions[0]
    if target is None:
        return None
    descs = [_join([jd.findtext("name"), _html_to_text(jd.findtext("value"))])
             for jd in target.findall(".//jobDescription")]
    return _join([
        target.findtext("name"),
        f"Location: {target.findtext('office')}" if target.findtext("office") else None,
        f"Department: {target.findtext('department')}" if target.findtext("department") else None,
        f"Employment type: {target.findtext('employmentType')}" if target.findtext("employmentType") else None,
        _join(descs),
    ]) or None


# ──────────────────────────────────────────────────────────────────
# BambooHR
# ──────────────────────────────────────────────────────────────────
def _fetch_bamboohr(url: str) -> Optional[str]:
    parsed = urlparse(url)
    company = parsed.netloc.lower().split(".")[0]
    segments = [s for s in parsed.path.split("/") if s]
    # 单条职位 URL 形如 /careers/{id}
    job_id = None
    if "careers" in segments:
        i = segments.index("careers")
        if i + 1 < len(segments):
            mm = re.match(r"\d+", segments[i + 1])
            job_id = mm.group(0) if mm else None
    if not job_id:
        return None
    data = _get_json(f"https://{company}.bamboohr.com/careers/{job_id}/detail")
    if not data:
        return None
    r = data.get("result") if isinstance(data, dict) else None
    job = (r or {}).get("jobOpening") or r or data
    loc = job.get("location") or {}
    if isinstance(loc, dict):
        loc_str = ", ".join(x for x in [loc.get("city"), loc.get("state"), loc.get("country")] if x)
    else:
        loc_str = str(loc)
    return _join([
        job.get("jobOpeningName") or job.get("title"),
        f"Location: {loc_str}" if loc_str else None,
        f"Department: {job.get('departmentLabel') or job.get('department')}"
        if (job.get("departmentLabel") or job.get("department")) else None,
        f"Employment type: {job.get('employmentStatusLabel')}" if job.get("employmentStatusLabel") else None,
        _html_to_text(job.get("description")),
    ]) or None


# ──────────────────────────────────────────────────────────────────
# Oracle HCM / ORC（Recruiting Cloud）
# ──────────────────────────────────────────────────────────────────
def _fetch_oracle_hcm(url: str) -> Optional[str]:
    parsed = urlparse(url)
    host = parsed.netloc
    segments = [s for s in parsed.path.split("/") if s]
    # 单条职位 URL 形如 .../sites/{site}/job/{id}
    site = None
    job_id = None
    if "sites" in segments:
        i = segments.index("sites")
        if i + 1 < len(segments):
            site = segments[i + 1]
    if "job" in segments:
        i = segments.index("job")
        if i + 1 < len(segments):
            # Oracle 的 requisition id 常是字母数字混合（如 REQUISITION_VT_B-6866），取整段
            job_id = segments[i + 1]
    if not (site and job_id):
        return None
    api = (f"https://{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitionDetails"
           f"?expand=all&onlyData=true&finder=ById;Id=%22{job_id}%22,siteNumber={site}")
    data = _get_json(api)
    items = (data or {}).get("items") or []
    if not items:
        return None
    it = items[0]
    return _join([
        it.get("Title"),
        f"Location: {it.get('PrimaryLocation')}" if it.get("PrimaryLocation") else None,
        _html_to_text(it.get("ExternalDescriptionStr")),
        _html_to_text(it.get("ExternalResponsibilitiesStr")),
        _html_to_text(it.get("ExternalQualificationsStr")),
    ]) or None


# ──────────────────────────────────────────────────────────────────
# Teamtailor（careers 站 jobs.json；字段随主题略有差异，防御性解析）
# ──────────────────────────────────────────────────────────────────
def _fetch_teamtailor(url: str) -> Optional[str]:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    segments = [s for s in parsed.path.split("/") if s]
    # 单条职位 URL 形如 /jobs/{id}-{slug} 或 /jobs/{id}
    job_id = None
    if "jobs" in segments:
        i = segments.index("jobs")
        if i + 1 < len(segments):
            mm = re.match(r"\d+", segments[i + 1])
            job_id = mm.group(0) if mm else None
    if not job_id:
        return None
    data = _get_json(f"https://{host}/jobs.json")
    items = data.get("items") if isinstance(data, dict) else None
    if not isinstance(items, list):
        return None
    # jobs.json 是 JSON Feed：item.id 为 UUID，职位数字 id 在 item.url 的 /jobs/{id}-slug 中
    def _url_id(it):
        m = re.search(r"/jobs/(\d+)", it.get("url") or "")
        return m.group(1) if m else None
    item = next((it for it in items if _url_id(it) == job_id), None)
    if not item:
        return None
    jp = item.get("_jobposting") if isinstance(item.get("_jobposting"), dict) else {}
    loc = None
    jl = jp.get("jobLocation") or {}
    if isinstance(jl, dict):
        addr = jl.get("address") or {}
        loc = addr.get("addressLocality") if isinstance(addr, dict) else None
    return _join([
        item.get("title"),
        f"Location: {loc}" if loc else None,
        f"Employment type: {jp.get('employmentType')}" if jp.get("employmentType") else None,
        _html_to_text(item.get("content_html")),
    ]) or None


# ──────────────────────────────────────────────────────────────────
# Eightfold（api/apply/v2/jobs；需公司主域 domain 参数，按子域推断兜底）
# ──────────────────────────────────────────────────────────────────
def _fetch_eightfold(url: str) -> Optional[str]:
    parsed = urlparse(url)
    host = parsed.netloc.lower()
    company = host.split(".")[0]
    qs = parse_qs(parsed.query)
    pid = (qs.get("pid") or qs.get("jobid") or qs.get("job_id") or [None])[0]
    if not pid:
        for s in reversed([s for s in parsed.path.split("/") if s]):
            if s.isdigit() and len(s) >= 6:
                pid = s
                break
    if not pid:
        return None
    # domain 无法从 URL 确知，按 {company}.com 推断；接口返回列表时再按 pid 精确匹配
    domain = f"{company}.com"
    data = _get_json(f"https://{host}/api/apply/v2/jobs?domain={domain}&pid={pid}&start=0&num=1")
    if not data:
        return None
    positions = data.get("positions") or data.get("jobs") or []
    job = next((p for p in positions if str(p.get("id") or p.get("pid")) == str(pid)), None)
    if not job and positions:
        job = positions[0]
    if not job:
        return None
    locs = job.get("locations") or ([job.get("location")] if job.get("location") else [])
    loc_str = ", ".join(x for x in locs if x) if isinstance(locs, list) else str(locs)
    return _join([
        job.get("name") or job.get("title"),
        f"Location: {loc_str}" if loc_str else None,
        f"Department: {job.get('department')}" if job.get("department") else None,
        _html_to_text(job.get("job_description") or job.get("description")),
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
    if "lever.co" in host:
        return "lever"
    if "ashbyhq.com" in host:
        return "ashby"
    if "smartrecruiters.com" in host:
        return "smartrecruiters"
    if "recruitee.com" in host:
        return "recruitee"
    if "workable.com" in host:
        return "workable"
    if "personio." in host:  # {company}.jobs.personio.de / .com
        return "personio"
    if "bamboohr.com" in host:
        return "bamboohr"
    if "teamtailor.com" in host:
        return "teamtailor"
    if "eightfold.ai" in host:
        return "eightfold"
    # Oracle 常用自定义域，故除 oraclecloud.com 外再看 URL 特征路径
    if "oraclecloud.com" in host or "CandidateExperience" in url or "hcmUI" in url:
        return "oracle_hcm"
    return None


_DISPATCH = {
    "workday": _fetch_workday,
    "greenhouse": _fetch_greenhouse,
    "lever": _fetch_lever,
    "ashby": _fetch_ashby,
    "smartrecruiters": _fetch_smartrecruiters,
    "recruitee": _fetch_recruitee,
    "workable": _fetch_workable,
    "personio": _fetch_personio,
    "bamboohr": _fetch_bamboohr,
    "teamtailor": _fetch_teamtailor,
    "eightfold": _fetch_eightfold,
    "oracle_hcm": _fetch_oracle_hcm,
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
