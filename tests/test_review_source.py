"""Regression cases for extraction omissions and inaccessible original pages."""
import json
import unittest
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.ingestion.review_source import collect_review_source, snapshot_text
from src.core.final_review import FIELDS, review_final_result, review_error
from src.core import observability
from test_final_review import VALUES, SOURCE, report


class OriginalPageTests(unittest.TestCase):
    def setUp(self):
        tracing = patch.object(observability, 'get_tracing_client', return_value=None)
        tracing.start()
        self.addCleanup(tracing.stop)

    def test_full_dom_preserves_hidden_contacts_mailto_and_structured_data(self):
        text = snapshot_text({'html': '''<html><body><article>Research job</article>
            <aside hidden>For questions contact Dr. Jane Example.</aside>
            <a href="mailto:jane%40example.org">Email us</a>
            <script type="application/ld+json">{"applicationContact": "Jane Example"}</script>
            <script>unrelated executable code</script></body></html>''',
            'inner_text': 'Research job'})
        self.assertIn('Dr. Jane Example', text)
        self.assertIn('mailto:jane@example.org', text)
        self.assertIn('applicationContact', text)
        self.assertNotIn('unrelated executable code', text)

    def test_collector_reopens_url_and_rejects_challenge_or_missing_html(self):
        manager = MagicMock()
        manager.get_page_payload.return_value = {'html': '<p>Research job contact Jane</p>'}
        with patch('src.ingestion.text_quality.evaluate_web_content_quality', return_value=(100, 'good', [], 'ok')):
            result = collect_review_source('https://example.org/job', manager=manager)
        self.assertEqual(result['status'], 'success')
        manager.get_page_payload.assert_called_once_with('https://example.org/job', scroll_enabled=True, timeout=60)
        manager.get_page_payload.return_value = {'html': '<p>Just a moment. Verifying you are human. Ray ID</p>'}
        self.assertEqual(collect_review_source('https://example.org/job', manager=manager)['status'], 'failed')
        manager.get_page_payload.return_value = {'content': 'Old extracted text'}
        self.assertEqual(collect_review_source('https://example.org/job', manager=manager)['status'], 'failed')

    def test_documents_are_explicitly_exempt_without_browser(self):
        manager = MagicMock()
        self.assertEqual(collect_review_source('https://example.org/file.pdf?x=1', manager=manager)['status'], 'not_applicable')
        self.assertEqual(collect_review_source('https://example.org/download', manager=manager, is_document=True)['status'], 'not_applicable')
        manager.get_page_payload.assert_not_called()

    def review(self, page, data=None):
        response = MagicMock()
        response.json.return_value = {'choices': [{'finish_reason': 'stop', 'message': {}}]}
        with patch('src.core.final_review.model_post', return_value=(response, json.dumps(data or report()))) as post:
            result = review_final_result(SOURCE, dict(VALUES, Contact_Name='-', Contact_Email='-'),
                source_url='https://example.org/job', original_page=page,
                client=SimpleNamespace(api_base_url='https://offline.invalid', headers={}))
        return result, post

    def test_contact_omitted_by_extractor_is_available_and_citable_in_review(self):
        data = report()
        data['verdict'] = 'needs_review'
        for check in data['checks']:
            if check['field'] in ('Contact_Name', 'Contact_Email'):
                check.update(status='issue', reason='网页联系区域包含提取遗漏的信息',
                             evidence='Dr. Jane Example, jane@example.org')
        page = {'status': 'success', 'text': 'Dr. Jane Example, jane@example.org'}
        result, post = self.review(page, data)
        self.assertEqual(result['status'], 'needs_review')
        self.assertNotIn('evidence_issues', result)
        payload = json.loads(post.call_args.kwargs['body']['messages'][1]['content'])
        self.assertEqual(payload['fields']['Contact_Email'], '-')
        self.assertTrue(any(e['text'] == page['text'] for e in payload['external_evidence']))
        self.assertEqual(result['original_page'], page)

    def test_failed_page_never_passes_even_when_model_approves_every_field(self):
        result, _ = self.review({'status': 'failed', 'reason': 'Browser timed out'})
        self.assertEqual(result['status'], 'needs_review')
        self.assertIn('原始网页核查未完成', review_error(result))
        self.assertEqual(len(result['checks']), len(FIELDS))

    def test_snapshot_over_budget_is_not_silently_truncated(self):
        with patch('src.core.final_review.FINAL_REVIEW_MAX_CHARS', 100):
            result, post = self.review({'status': 'success', 'text': 'x' * 200})
        self.assertEqual(result['status'], 'incomplete')
        post.assert_not_called()

    def test_url_triggers_fresh_snapshot_by_default(self):
        with patch('src.ingestion.review_source.collect_review_source', return_value={'status': 'failed', 'reason': 'offline'}) as collect:
            response = MagicMock()
            response.json.return_value = {'choices': [{'finish_reason': 'stop', 'message': {}}]}
            with patch('src.core.final_review.model_post', return_value=(response, json.dumps(report()))):
                result = review_final_result(SOURCE, VALUES, source_url='https://example.org/job',
                    client=SimpleNamespace(api_base_url='https://offline.invalid', headers={}))
        collect.assert_called_once_with('https://example.org/job', is_document=False)
        self.assertEqual(result['status'], 'needs_review')


if __name__ == '__main__':
    unittest.main()
