"""Independent full-page evidence for final review; never reuse article extraction."""
from datetime import datetime, timezone
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup


def snapshot_text(payload):
    html = payload.get('html') or ''
    if not html:
        raise ValueError('Browser did not return page HTML')
    soup = BeautifulSoup(html, 'html.parser')
    structured = [node.get_text() for node in soup.select('script[type="application/ld+json"]')]
    emails = [unquote(node['href']) for node in soup.select('a[href]')
              if node['href'].lower().startswith('mailto:')]
    for node in soup(['script', 'style', 'noscript']):
        node.decompose()
    # Keep text in hidden accordions too. No main-article selection or truncation.
    dom_text = soup.get_text('\n', strip=True)
    rendered_text = payload.get('inner_text') or ''
    parts = [dom_text]
    # innerText often repeats the same facts with different whitespace.
    # Preserve it only when it contains information absent from the DOM text.
    if rendered_text.strip() and ' '.join(rendered_text.split()) not in ' '.join(dom_text.split()):
        parts.append(rendered_text)
    parts.extend(emails)
    parts.extend(structured)
    return '\n\n'.join(dict.fromkeys(part for part in parts if part.strip()))


def collect_review_source(url, *, manager=None, is_document=False, payload=None):
    check = {'url': url, 'checked_at': datetime.now(timezone.utc).isoformat(),
             'status': 'failed', 'method': 'browser_full_dom'}
    if is_document or urlsplit(url).path.lower().endswith('.pdf'):
        check.update(status='not_applicable', reason='文档原文使用已有专用提取结果')
        return check
    try:
        if urlsplit(url).scheme not in ('http', 'https'):
            raise ValueError('Source is not an HTTP(S) page')
        if payload is None:
            if manager is None:
                from ..browser.playwright_process_manager import get_playwright_manager
                manager = get_playwright_manager()
            payload = manager.get_page_payload(url, scroll_enabled=True, timeout=60)
        if not isinstance(payload, dict):
            raise ValueError('Browser returned no page')
        text = snapshot_text(payload)
        from .text_quality import evaluate_web_content_quality
        _, quality, flags, reason = evaluate_web_content_quality(text)
        if quality != 'good' or set(flags).intersection({'challenge_page', 'unavailable_page', 'portal_shell_page'}):
            raise ValueError('Page quality insufficient: ' + reason + ' ' + ', '.join(flags))
        check.update(status='success', text=text)
    except Exception as exc:
        check['reason'] = str(exc)[:500]
    return check
