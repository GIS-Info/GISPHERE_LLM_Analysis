"""Add full-page evidence to the shared input of all three analysis stages."""
import json

from .review_source import collect_review_source


CONTENT_VERSION = 'full-page-stage-input-v1'


def enrich_stage_source(text, url, *, manager=None, payload=None, http_html=None,
                        max_chars=200000):
    """Reuse a browser payload, otherwise open once; fall back to available HTTP DOM.

    An enrichment failure never discards usable extracted text. An oversized
    snapshot is explicitly rejected rather than silently truncated.
    """
    if payload is not None or manager is not None:
        page = collect_review_source(url, manager=manager, payload=payload)
    else:
        page = {'status': 'failed', 'reason': 'Browser unavailable'}
    browser_issue = page.get('reason') if page.get('status') != 'success' else None
    if page.get('status') != 'success' and http_html:
        page = collect_review_source(url, payload={'html': http_html})
        page['method'] = 'http_full_dom'
    details = {key: value for key, value in page.items() if key != 'text'}
    if browser_issue:
        details['browser_issue'] = browser_issue
    if page.get('status') != 'success':
        return text, details
    snapshot = page['text']
    if snapshot in text:
        details['added_chars'] = 0
        return text, details
    # Keep every additional fact, but do not send exact copies of the whole
    # extracted body again (DOM and rendered text can both repeat it).
    if text.strip():
        snapshot = snapshot.replace(text.strip(), '[EXTRACTED BODY ABOVE]')
    metadata = json.dumps({k: page[k] for k in ('url', 'checked_at', 'method')},
                          ensure_ascii=False)
    combined = (text + '\n\n[ORIGINAL PAGE EVIDENCE]\n' + metadata + '\n'
                + snapshot + '\n[END ORIGINAL PAGE EVIDENCE]')
    if len(combined) > max_chars:
        details.update(status='skipped', reason='Full-page stage input exceeds character budget',
                       required_chars=len(combined), max_chars=max_chars)
        return text, details
    details['added_chars'] = len(combined) - len(text)
    return combined, details
