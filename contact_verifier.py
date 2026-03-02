"""
联系人验证模块
用于通过 Playwright MCP 搜索验证和补充联系人信息（PhD 学位 + 邮箱）
"""
import logging
import json
import re
import time
from typing import Optional, Dict, List, Tuple

from config import (REQUEST_TIMEOUT, MAX_RETRIES, CONTACT_VERIFICATION_ENABLED,
                    CONTACT_SEARCH_TIMEOUT, MAX_SEARCH_RESULTS, MAX_PAGES_TO_ANALYZE)
from utils import normalize_text, is_valid_url

logger = logging.getLogger(__name__)


class ContactVerifier:
    def __init__(self, llm_agent, mcp_client=None):
        """
        初始化联系人验证器

        Args:
            llm_agent: LLM 代理实例，用于分析搜索结果
            mcp_client: MCPClientWrapper 实例（可选）。提供时使用 Playwright MCP 进行
                        网络搜索和页面抓取；为 None 时跳过网络搜索。
        """
        self.llm_agent = llm_agent
        self.mcp_client = mcp_client
        self._cleaned = False

        if self.mcp_client:
            logger.info("✅ ContactVerifier: 使用 Playwright MCP 进行网络搜索")
        else:
            logger.info("⚠️  ContactVerifier: 未提供 MCP 客户端，网络搜索不可用")

        # 优先域名（用于结果排序评分）
        self.priority_domains = [
            'scholar.google',
            'researchgate.net',
            'linkedin.com',
            'orcid.org',
            'academia.edu',
            '.edu',
            '.ac.',
            '.org'
        ]

    # ─────────────────────────────────────────────
    #  MCP 搜索与页面获取
    # ─────────────────────────────────────────────

    def _mcp_search(self, query: str) -> List[Dict]:
        """
        通过 Playwright MCP 执行 Google 搜索，用 LLM 从页面快照中提取结果列表。

        Returns:
            List[Dict]: 每项含 title / url / snippet 的搜索结果
        """
        if not self.mcp_client:
            return []

        search_url = f"https://duckduckgo.com/?q={query.replace(' ', '+')}\u0026kp=-2\u0026kl=us-en\u0026ia=web"
        logger.info(f"MCP DuckDuckGo搜索: {query}")

        MAX_RETRIES = 3
        snapshot_raw = None

        for attempt in range(1, MAX_RETRIES + 1):
            try:
                # 1. 导航到搜索结果页
                self.mcp_client.call_tool(
                    "mcp_playwright_browser_navigate",
                    {"url": search_url}
                )
                time.sleep(3.0)  # 等待 Bing 页面加载

                # 触发一次渲染，确保动态内容已生成
                self.mcp_client.call_tool(
                    "mcp_playwright_browser_snapshot",
                    {}
                )
                time.sleep(1.0)

                # 2. 获取页面可访问性快照文本
                snap = self.mcp_client.call_tool(
                    "mcp_playwright_browser_snapshot",
                    {}
                )

                if not snap or len(snap.strip()) < 100:
                    logger.warning(f"第{attempt}次尝试: snapshot 为空或过短，重试...")
                    time.sleep(2.0)
                    continue

                # 校验页面是否为真实 DuckDuckGo 搜索结果页
                snap_lower = snap.lower()
                is_results_page = any(kw in snap_lower for kw in [
                    'result', 'web result', 'search result',
                    'result__body', 'result__title',  # DuckDuckGo 特定元素
                    'href="http', "href='http",       # 页面包含外部链接
                    '.edu', '.ac.', 'university', 'professor', 'scholar',
                ])
                if not is_results_page:
                    logger.warning(f"第{attempt}次尝试: 页面内容不像搜索结果（可能还未加载），重试...")
                    time.sleep(2.0)
                    continue

                snapshot_raw = snap
                logger.debug(f"MCP snapshot 开头内容: {repr(snapshot_raw[:200])}")
                break

            except Exception as e:
                logger.warning(f"第{attempt}次尝试异常: {e}")
                time.sleep(2.0)

        if not snapshot_raw:
            logger.warning("MCP 页面多次尝试后仍为空，放弃")
            return []

        try:
            # 3. 用 LLM 解析快照，提取搜索结果列表
            prompt = f"""以下是 DuckDuckGo 搜索结果页面的可访问性树文本（snapshot）。
搜索词：{query}

请从中提取搜索结果条目，每个条目包含：
- title: 结果标题
- url: 完整 URL（必须以 http 开头，且必须是真实的目标网站 URL，不得是 bing.com 或 duckduckgo.com 开头的链接）
- snippet: 简短摘要

请以 JSON 数组格式返回，最多返回 {MAX_SEARCH_RESULTS} 条，格式示例：
[
  {{"title": "...", "url": "https://...", "snippet": "..."}},
  ...
]

如果页面内容不是搜索结果（如验证码、错误页面），返回空数组 []。

Snapshot 内容：
{snapshot_raw[:6000]}
"""
            response = self.llm_agent.call_llm(prompt)
            if not response:
                return []

            # 提取 JSON 数组
            results = self._parse_json_list(response)
            # 过滤非 http 开头的 URL 及广告/无关页面
            results = [r for r in results if isinstance(r, dict)
                       and r.get('url', '').startswith('http')
                       and self._is_useful_url(r.get('url', ''))]
            logger.info(f"MCP DuckDuckGo搜索获得 {len(results)} 个有效结果")
            return results

        except Exception as e:
            logger.warning(f"MCP 搜索解析失败: {e}")
            return []

    def _mcp_get_page_content(self, url: str) -> Optional[str]:
        """
        通过 Playwright MCP 获取指定页面的文本内容。

        Returns:
            str | None: 页面文本内容
        """
        if not self.mcp_client:
            return None

        logger.info(f"MCP 获取页面内容: {url}")
        try:
            self.mcp_client.call_tool(
                "mcp_playwright_browser_navigate",
                {"url": url}
            )
            time.sleep(4.0)  # 等待页面及 JS 内容加载

            # 触发一次渲染，注意部分较重的 JS 页面需要额外时间
            self.mcp_client.call_tool(
                "mcp_playwright_browser_snapshot",
                {}
            )
            time.sleep(1.5)

            snapshot_raw = self.mcp_client.call_tool(
                "mcp_playwright_browser_snapshot",
                {}
            )

            if not snapshot_raw or len(snapshot_raw.strip()) < 50:
                logger.warning("MCP 页面快照内容过短")
                return None

            # 截断到合理长度
            return snapshot_raw[:8000]

        except Exception as e:
            logger.warning(f"MCP 页面获取失败 {url}: {e}")
            return None

    # ─────────────────────────────────────────────
    #  核心公共接口
    # ─────────────────────────────────────────────

    def should_verify_contact(self, contact_name: str, contact_email: str,
                               original_text: str) -> Tuple[bool, str]:
        """
        判断是否需要进行联系人验证

        Returns:
            Tuple[bool, str]: (是否需要验证, 验证原因)
        """
        if not contact_name or contact_name.strip() in ['-', '', 'N/A']:
            return False, "缺失联系人，无需验证"

        contact_name = contact_name.strip()
        has_email = contact_email and contact_email.strip() not in ['-', '', 'N/A']

        # 第一阶段已识别 Dr. 前缀且有邮箱 → 无需验证
        if contact_name.startswith("Dr. ") and has_email:
            return False, "第一阶段已识别博士学位且有邮箱，无需验证"

        # 第一阶段已识别 Dr. 但缺邮箱 → 搜索邮箱
        if contact_name.startswith("Dr. ") and not has_email:
            return True, "第一阶段已识别博士学位但缺少邮箱，需要搜索邮箱"

        # 检查原文是否有明确学位/职称标识
        clean_name = self._clean_contact_name(contact_name)
        title_patterns = [
            r'\bDr\.?\s+' + re.escape(clean_name),
            r'\bProf\.?\s+' + re.escape(clean_name),
            r'\bProfessor\s+' + re.escape(clean_name),
            r'\bAssistant\s+Professor\s+' + re.escape(clean_name),
            r'\bAssociate\s+Professor\s+' + re.escape(clean_name),
            r'\bDoctor\s+' + re.escape(clean_name),
            clean_name + r'\s*,?\s*Ph\.?D',
            clean_name + r'\s*,?\s*PhD',
            clean_name + r'\s*,?\s*Professor',
        ]
        has_clear_title = any(re.search(p, original_text, re.IGNORECASE)
                              for p in title_patterns)

        if has_clear_title and has_email:
            return False, "原始文本中有明确学位标识且有邮箱，无需验证"
        elif has_clear_title and not has_email:
            return True, "原始文本中有学位标识但缺少邮箱，需要搜索邮箱"
        else:
            return True, "联系人学位信息不明确，需要验证学位和邮箱"

    def search_contact_info(self, university_en: str, contact_name: str) -> List[Dict]:
        """
        搜索联系人信息（优先使用 Playwright MCP，不可用时返回空列表）

        Returns:
            List[Dict]: 去重并排序后的搜索结果，最多 10 条
        """
        if not contact_name or not university_en:
            logger.warning("搜索参数不完整")
            return []

        clean_name = self._clean_contact_name(contact_name)
        query = f'{clean_name} {university_en}'
        logger.info(f"搜索联系人信息: {query}")

        all_results = []

        if self.mcp_client:
            all_results = self._mcp_search(query)
        else:
            logger.warning("MCP 客户端不可用，无法执行网络搜索")
            return []

        unique_results = self._remove_duplicate_results(all_results)
        sorted_results = self._sort_results_by_priority(unique_results)
        logger.info(f"获得 {len(sorted_results)} 个唯一搜索结果")
        return sorted_results[:10]

    def analyze_contact_pages(self, search_results: List[Dict],
                               contact_name: str) -> Tuple[str, str, str]:
        """
        分析联系人相关页面，获取学位信息和邮箱

        Returns:
            Tuple[str, str, str]: (称谓前缀, 邮箱地址, 分析说明)
        """
        logger.info(f"开始分析联系人页面，共 {len(search_results)} 个结果")

        # 用 LLM 选择最相关的页面 URL
        if len(search_results) > 1:
            selected_urls = self._select_relevant_pages(search_results, contact_name)
        else:
            selected_urls = [r['url'] for r in search_results]

        best_pages = []
        for url in selected_urls[:MAX_PAGES_TO_ANALYZE]:
            try:
                page_content = self._fetch_page_content(url)
                if page_content:
                    analysis = self._analyze_page_with_llm(page_content, contact_name)
                    if analysis:
                        best_pages.append({
                            'url': url,
                            'analysis': analysis,
                            'content': page_content[:2000]
                        })
            except Exception as e:
                logger.warning(f"分析页面失败 {url}: {e}")
                continue

        return self._synthesize_contact_info(best_pages, contact_name)

    def verify_and_update_contact(self, university_en: str, contact_name: str,
                                   contact_email: str, original_text: str) -> Dict:
        """
        完整的联系人验证和更新流程

        Returns:
            Dict: 更新后的联系人信息
        """
        logger.info(f"开始验证联系人: {contact_name} @ {university_en}")

        if not CONTACT_VERIFICATION_ENABLED:
            logger.info("联系人验证功能已禁用")
            return {
                'Contact_Name': contact_name,
                'Contact_Email': contact_email,
                'verification_performed': False,
                'verification_reason': "验证功能已禁用",
                'verification_details': ""
            }

        should_verify, reason = self.should_verify_contact(
            contact_name, contact_email, original_text
        )

        result = {
            'Contact_Name': contact_name,
            'Contact_Email': contact_email,
            'verification_performed': should_verify,
            'verification_reason': reason,
            'verification_details': ""
        }

        if not should_verify:
            logger.info(f"无需验证: {reason}")
            return result

        # 无 MCP 时直接返回（不执行网络搜索）
        if not self.mcp_client:
            result['verification_details'] = "MCP 客户端不可用，跳过网络搜索"
            logger.warning("MCP 客户端不可用，跳过联系人网络搜索")
            return result

        try:
            search_results = self.search_contact_info(university_en, contact_name)

            if not search_results:
                result['verification_details'] = "搜索未找到相关结果"
                logger.warning("搜索未找到结果")
                return result

            title_prefix, found_email, explanation = self.analyze_contact_pages(
                search_results, contact_name
            )

            # 更新联系人姓名
            if contact_name.startswith("Dr. "):
                logger.info(f"联系人已有 Dr. 前缀，保持不变: {contact_name}")
            elif title_prefix:
                formatted_name = self._validate_and_format_name(contact_name, title_prefix)
                if formatted_name and formatted_name != contact_name:
                    result['Contact_Name'] = formatted_name
                    logger.info(f"联系人姓名已格式化: {contact_name} -> {formatted_name}")

            # 更新邮箱（仅在原本无邮箱时填入）
            if found_email:
                if not contact_email or contact_email.strip() in ['-', '', 'N/A']:
                    result['Contact_Email'] = found_email

            result['verification_details'] = explanation
            logger.info(f"验证完成: {result['Contact_Name']}, {result['Contact_Email']}")

        except Exception as e:
            error_msg = f"验证过程出错: {str(e)}"
            result['verification_details'] = error_msg
            logger.error(error_msg)

        return result

    def cleanup(self):
        """清理资源（MCP 客户端由外部 AnalysisStageManager 管理，此处不负责关闭）"""
        if self._cleaned:
            return
        self._cleaned = True
        logger.info("ContactVerifier 资源已清理")

    def __del__(self):
        self.cleanup()

    # ─────────────────────────────────────────────
    #  私有辅助方法
    # ─────────────────────────────────────────────

    def _clean_contact_name(self, contact_name: str) -> str:
        """清理联系人姓名，移除称谓前缀和后缀"""
        if not contact_name:
            return ""
        name = contact_name.strip()
        prefixes = ['Dr.', 'Prof.', 'Professor', 'Assistant Professor',
                    'Associate Professor', 'Mr.', 'Ms.', 'Miss', 'Mrs.', 'Doctor']
        for prefix in prefixes:
            if name.startswith(prefix + ' '):
                name = name[len(prefix):].strip()
                break
        suffixes = [', Ph.D.', ', PhD', ', Ph.D', ', Professor', ', Prof.', ', Dr.']
        for suffix in suffixes:
            if name.endswith(suffix):
                name = name[:-len(suffix)].strip()
        name = re.sub(r'\s*"[^"]*"\s*', ' ', name)
        name = ' '.join(name.split())
        return name

    def _validate_and_format_name(self, contact_name: str, title_prefix: str = "") -> str:
        """验证并格式化联系人姓名"""
        if not contact_name:
            return ""
        clean_name = self._clean_contact_name(contact_name)
        if not clean_name:
            return ""
        clean_name = re.sub(r'\b(Ph\.?D\.?|PhD|Doctor|Professor|Prof\.?)\b', '',
                            clean_name, flags=re.IGNORECASE)
        clean_name = re.sub(r'\([^)]*\)', '', clean_name)
        clean_name = re.sub(r'\[[^\]]*\]', '', clean_name)
        clean_name = re.sub(r'[,;]', '', clean_name)
        clean_name = re.sub(r'[^a-zA-Z\s\.\-]', '', clean_name)
        clean_name = ' '.join(clean_name.split())
        if not clean_name:
            return ""
        if title_prefix == "Dr.":
            return f"Dr. {clean_name}"
        elif title_prefix == "Mr.":
            return f"Mr. {clean_name}"
        elif title_prefix == "Ms.":
            return f"Ms. {clean_name}"
        else:
            return clean_name

    def _remove_duplicate_results(self, results: List[Dict]) -> List[Dict]:
        """移除重复的搜索结果"""
        seen_urls = set()
        unique = []
        for result in results:
            url = result.get('url', '')
            if url and url not in seen_urls:
                seen_urls.add(url)
                unique.append(result)
        return unique

    def _sort_results_by_priority(self, results: List[Dict]) -> List[Dict]:
        """按优先级对搜索结果排序"""
        def get_score(result):
            url = result.get('url', '').lower()
            title = result.get('title', '').lower()
            snippet = result.get('snippet', '').lower()
            score = 0
            for i, domain in enumerate(self.priority_domains):
                if domain in url:
                    score += (len(self.priority_domains) - i) * 10
                    break
            for kw in ['professor', 'dr.', 'phd', 'faculty', 'researcher', 'scholar']:
                if kw in title or kw in snippet:
                    score += 5
            for ind in ['homepage', 'profile', 'bio', 'cv', 'resume']:
                if ind in url or ind in title:
                    score += 3
            return score

        return sorted(results, key=get_score, reverse=True)

    def _is_useful_url(self, url: str) -> bool:
        """
        判断 URL 是否值得访问（严格白名单模式）。
        只允许明确的学术/学校/个人主页类域名，其余一律拒绝。
        """
        url_lower = url.lower()

        # ── 黑名单：优先拒绝广告/追踪链接 ────────────────────────
        blocked_patterns = [
            'bing.com/aclick', 'bing.com/ck/', 'bing.com/fd/',
            'googleadservices.com', 'doubleclick.net', 'pagead', 'adclick', '/aclk?',
            'linkedin.com/jobs', 'indeed.com', 'glassdoor.com',
            'monster.com', 'ziprecruiter.com', 'simplyhired.com',
            'wikipedia.org', 'wikimedia.org',
            'quora.com', 'reddit.com', 'stackexchange.com', 'stackoverflow.com',
            'medium.com', 'substack.com',
            'twitter.com', 'x.com', 'facebook.com', 'instagram.com',
            'tiktok.com', 'youtube.com', 'weibo.com',
            'amazon.com', 'ebay.com', 'alibaba.com', 'shopify.com',
            'bing.com/search', 'bing.com/aclick', 'bing.com/ck/', 'bing.com/fd/',
            'duckduckgo.com', 'duck.com',          # DuckDuckGo 自身及代理链接
            'msn.com',
            'google.com/search', 'google.com/aclk',
        ]
        for pattern in blocked_patterns:
            if pattern in url_lower:
                return False

        # ── 白名单：只允许以下类型的域名/路径 ────────────────────
        allowed_patterns = [
            # 学校域名
            '.edu', '.ac.uk', '.ac.jp', '.ac.cn', '.ac.nz', '.ac.za',
            '.edu.au', '.edu.cn', '.edu.hk', '.edu.sg', '.edu.tw',
            # 学术平台
            'scholar.google', 'researchgate.net', 'orcid.org',
            'academia.edu', 'semanticscholar.org', 'scopus.com',
            'pubmed.ncbi', 'ncbi.nlm', 'arxiv.org',
            'springer.com', 'nature.com', 'science.org',
            'ieee.org', 'acm.org', 'ssrn.com',
            # LinkedIn 个人页（非职位页）
            'linkedin.com/in/',
            # 大学个人主页常见路径特征（适用于非 .edu 的欧洲/亚洲大学）
            '/people/', '/faculty/', '/staff/', '/profile/', '/researcher/',
            '/~', '/en/persons/', '/en/researchers/',
        ]
        for pattern in allowed_patterns:
            if pattern in url_lower:
                return True

        # ── 其余 URL 一律拒绝 ─────────────────────────────────────
        return False

    def _select_relevant_pages(self, search_results: List[Dict],
                                contact_name: str) -> List[str]:
        """使用 LLM 从搜索结果中选择最相关的页面 URL"""
        if len(search_results) <= 3:
            return [r['url'] for r in search_results]

        results_summary = []
        for i, result in enumerate(search_results[:10]):
            results_summary.append(
                f"{i+1}. {result['title']}\n   URL: {result['url']}\n   摘要: {result['snippet']}"
            )

        prompt = f"""Please analyze the following search results and select web pages that are most likely to contain academic profile information about "{contact_name}".

Search Results:
{chr(10).join(results_summary)}

STRICT SELECTION RULES:
✅ ALLOWED page types (select from these only):
  1. University / institution faculty or staff profile pages (e.g. domain contains .edu, .ac.uk, etc.)
  2. Personal academic homepage hosted on a university server
  3. Academic platforms: Google Scholar, ResearchGate, ORCID, Academia.edu, Semantic Scholar
  4. Official lab or research group pages listing the person

❌ FORBIDDEN page types (never select these):
  - Job boards or recruitment sites (LinkedIn Jobs, Indeed, Glassdoor, etc.)
  - News articles, Wikipedia, or general encyclopedia pages
  - Social media feeds (Twitter/X, Facebook, Instagram, YouTube, etc.)
  - Advertisement or redirect pages
  - E-commerce or commercial sites
  - Q&A / forum sites (Quora, Reddit, StackExchange)

Select the most reliable 1-3 ALLOWED pages. If no ALLOWED pages exist in the results, return an empty list.

Return JSON only:
{{
    "selected_urls": ["url1", "url2"],
    "reasoning": "Brief reason for selection"
}}
"""
        try:
            response = self.llm_agent.call_llm(prompt)
            if response:
                data = self._parse_json_obj(response)
                selected_urls = data.get('selected_urls', [])
                # 兜底：对 LLM 返回的 URL 再过一次硬过滤
                selected_urls = [u for u in selected_urls if self._is_useful_url(u)]
                logger.info(f"LLM选择页面 ({len(selected_urls)}个): {data.get('reasoning', '')}")
                return selected_urls
        except Exception as e:
            logger.warning(f"LLM页面选择失败: {e}")
        # fallback 同样过滤
        return [r['url'] for r in search_results[:3] if self._is_useful_url(r.get('url', ''))]

    def _fetch_page_content(self, url: str) -> Optional[str]:
        """获取页面内容（优先使用 MCP）"""
        # 1. MCP 路径
        if self.mcp_client:
            content = self._mcp_get_page_content(url)
            if content:
                return content
            logger.warning(f"MCP 页面内容为空，跳过: {url}")

        # 2. MCP 不可用时降级到基础 HTTP 请求
        try:
            import requests
            from bs4 import BeautifulSoup
            session = requests.Session()
            session.headers.update({
                'User-Agent': ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) '
                               'AppleWebKit/537.36 (KHTML, like Gecko) '
                               'Chrome/120.0.0.0 Safari/537.36')
            })
            response = session.get(url, timeout=REQUEST_TIMEOUT)
            response.raise_for_status()
            soup = BeautifulSoup(response.content, 'html.parser')
            for tag in soup(["script", "style"]):
                tag.decompose()
            text = normalize_text(soup.get_text())
            if len(text) > 5000:
                text = text[:5000] + "..."
            return text
        except Exception as e:
            logger.warning(f"HTTP 获取页面内容失败 {url}: {e}")
            return None

    def _analyze_page_with_llm(self, page_content: str, contact_name: str) -> Optional[Dict]:
        """使用 LLM 分析页面内容，提取学位和邮箱信息"""
        prompt = f"""Please analyze the following web page content and extract relevant information about contact person "{contact_name}".

Web Page Content:
{page_content}

Please carefully search for and extract the following information:
1. Degree information: Does this person have a doctorate degree (PhD/Ph.D.) or professor position?
2. Email address: Any email address related to this person
3. Title: Professor, Dr., Mr., Ms., etc.

Please return in JSON format:
{{
    "has_doctorate": true/false,
    "title_prefix": "Dr./Mr./Ms.",
    "email_address": "found email address or null",
    "gender": "male/female/unknown",
    "confidence": "high/medium/low",
    "evidence": "specific evidence supporting the judgment"
}}

Important notes:
- Set has_doctorate to true when confirmed PhD degree OR professor position
- If uncertain, choose conservative title (Mr./Ms.)
- Email address must be in valid format (contain @)
"""
        try:
            response = self.llm_agent.call_llm(prompt)
            if response:
                return self._parse_json_obj(response)
        except Exception as e:
            logger.warning(f"LLM页面分析失败: {e}")
        return None

    def _synthesize_contact_info(self, analyzed_pages: List[Dict],
                                  contact_name: str) -> Tuple[str, str, str]:
        """综合分析结果，确定最终联系人信息"""
        if not analyzed_pages:
            return "Mr./Ms.", "", "未找到相关页面信息"

        all_analyses = [p['analysis'] for p in analyzed_pages if p['analysis']]
        if not all_analyses:
            return "Mr./Ms.", "", "页面分析失败"

        has_doctorate_count = sum(1 for a in all_analyses if a.get('has_doctorate', False))
        emails = [a.get('email_address') for a in all_analyses if a.get('email_address')]
        genders = [a.get('gender') for a in all_analyses
                   if a.get('gender') and a.get('gender') != 'unknown']

        if has_doctorate_count > len(all_analyses) / 2:
            title_prefix = "Dr."
        else:
            if genders:
                counts: Dict[str, int] = {}
                for g in genders:
                    counts[g] = counts.get(g, 0) + 1
                majority = max(counts, key=counts.get)
                title_prefix = "Mr." if majority == "male" else ("Ms." if majority == "female" else "Mr./Ms.")
            else:
                title_prefix = "Mr./Ms."

        best_email = emails[0] if emails else ""

        explanation = f"分析了{len(analyzed_pages)}个页面，"
        explanation += (f"{has_doctorate_count}个页面确认有博士学位。"
                        if has_doctorate_count > 0 else "未找到明确的博士学位证据。")
        explanation += (f"找到邮箱地址：{best_email}" if best_email else "未找到有效邮箱地址。")

        logger.info(f"联系人验证结果: {title_prefix} {contact_name}, {best_email}")
        return title_prefix, best_email, explanation

    # ─────────────────────────────────────────────
    #  JSON 解析辅助
    # ─────────────────────────────────────────────

    def _parse_json_list(self, text: str) -> list:
        """从 LLM 返回文本中提取 JSON 数组"""
        text = text.strip()
        # 去掉 markdown 代码块
        text = re.sub(r'^```[a-z]*\n?', '', text)
        text = re.sub(r'\n?```$', '', text)
        text = text.strip()
        try:
            data = json.loads(text)
            if isinstance(data, list):
                return data
        except Exception:
            pass
        # 尝试从文本中找到 JSON 数组片段
        match = re.search(r'\[.*\]', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except Exception:
                pass
        return []

    def _parse_json_obj(self, text: str) -> dict:
        """从 LLM 返回文本中提取 JSON 对象"""
        text = text.strip()
        text = re.sub(r'^```[a-z]*\n?', '', text)
        text = re.sub(r'\n?```$', '', text)
        text = text.strip()
        try:
            data = json.loads(text)
            if isinstance(data, dict):
                return data
        except Exception:
            pass
        match = re.search(r'\{.*\}', text, re.DOTALL)
        if match:
            try:
                return json.loads(match.group())
            except Exception:
                pass
        return {}
