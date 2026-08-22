# -*- coding: utf-8 -*-
"""学术岗位 RSS 直连采集（免反爬、结构化）。

提供 fetch_academic_rss_jobs() 拉取学术招聘站的公开 RSS feed，解析为结构化职位列表。

定位：这是"岗位发现/采集"能力，独立于现有的"URL→正文"抓取链（fetch_text/ats_api）。
本模块只负责「发现 + 结构化」；发现的岗位如何进入主流程（去重、写回 Google Sheets、
触发正文抓取与 LLM 分析）需按业务另行接线，故此处不自动接入主流程。

已验证可用（2026-08）：THE unijobs、HigherEdJobs。
jobs.ac.uk 旧的 /feeds/subject-areas RSS 已失效（现返回 HTML），暂不纳入，待确认新入口。
"""
import html as _html
import logging
import re
import xml.etree.ElementTree as ET
from typing import Dict, List, Optional

import requests

logger = logging.getLogger(__name__)

_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
_TIMEOUT = 20

# 站点键 -> 由 query（关键词/分类）生成 feed URL 的函数
ACADEMIC_FEEDS = {
    "the-unijobs": lambda q: (
        f"https://www.timeshighereducation.com/unijobs/jobsrss/?keywords={q or ''}"),
    # HigherEdJobs 用数字 catID；默认 101（Computer Sciences）。
    "higheredjobs": lambda q: (
        f"https://www.higheredjobs.com/rss/categoryFeed.cfm?catID={q or '101'}"),
}


def _strip_html(raw: str) -> str:
    if not raw:
        return ""
    text = re.sub(r"<[^>]+>", " ", _html.unescape(raw))
    return re.sub(r"\s+", " ", text).strip()


def _parse_rss(xml_bytes: bytes, source: str) -> List[Dict]:
    """解析 RSS 2.0（item/title/link/description/pubDate），失败返回空列表。"""
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError as e:
        logger.warning(f"学术 RSS 解析失败 [{source}]: {e}")
        return []
    jobs: List[Dict] = []
    for item in root.findall(".//item"):
        title = (item.findtext("title") or "").strip()
        link = (item.findtext("link") or "").strip()
        if not (title and link):
            continue
        jobs.append({
            "title": _html.unescape(title),
            "url": link,
            "description": _strip_html(item.findtext("description") or ""),
            "published": (item.findtext("pubDate") or "").strip(),
            "source": source,
        })
    return jobs


def fetch_academic_rss_jobs(sources: Optional[List[str]] = None,
                            query: str = "",
                            limit: Optional[int] = None) -> List[Dict]:
    """拉取学术岗位 RSS，返回结构化职位列表 [{title,url,description,published,source}]。

    Args:
        sources: 站点键列表（默认全部 ACADEMIC_FEEDS）。
        query: 关键词（the-unijobs）或分类 catID（higheredjobs）。
        limit: 每个源的条数上限。
    单源失败不影响其它源（记 warning 并跳过），返回已成功部分。
    """
    sources = sources or list(ACADEMIC_FEEDS)
    out: List[Dict] = []
    for key in sources:
        builder = ACADEMIC_FEEDS.get(key)
        if not builder:
            logger.warning(f"未知学术 RSS 源: {key}")
            continue
        url = builder(query)
        try:
            resp = requests.get(url, headers={"User-Agent": _UA}, timeout=_TIMEOUT)
            resp.raise_for_status()
            jobs = _parse_rss(resp.content, key)
            if limit:
                jobs = jobs[:limit]
            logger.info(f"学术 RSS[{key}] 取到 {len(jobs)} 条职位")
            out.extend(jobs)
        except Exception as e:
            logger.warning(f"学术 RSS[{key}] 拉取失败: {e}")
    return out
