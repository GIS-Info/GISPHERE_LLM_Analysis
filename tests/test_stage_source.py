"""Full-page stage input regressions. External services and business sheets stay offline."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from src.core import observability
from src.core.run_store import RunStore
from src.ingestion.fetch_text import ContentFetcher
from src.ingestion.stage_source import enrich_stage_source
from src.core.analysis_stage import AnalysisStageManager


BODY = ('A postdoctoral researcher will investigate atmospheric observations, satellite data, '
        'transport modelling, emissions, environmental change and uncertainty. The project offers '
        'a full time research position with scientific collaboration, publication, training and '
        'international partners. Candidates need a doctorate and experience with data analysis. ')
CONTACT = 'Dr. Jane Example, jane@example.org'
HTML = '<article>' + BODY * 4 + '</article><aside hidden>' + CONTACT + '</aside>'
URL = 'https://example.org/job'


class StageSourceTests(unittest.TestCase):
    def setUp(self):
        tracing = patch.object(observability, 'get_tracing_client', return_value=None)
        tracing.start()
        self.addCleanup(tracing.stop)

    def fetcher(self):
        instance = ContentFetcher.__new__(ContentFetcher)
        instance.playwright_manager = MagicMock()
        instance._reset_last_fetch_summary(URL)
        return instance

    def test_hidden_contact_is_added_without_replacing_extracted_body(self):
        text, details = enrich_stage_source(BODY, URL, payload={'html': HTML})
        self.assertTrue(text.startswith(BODY))
        self.assertIn(CONTACT, text)
        self.assertEqual(details['status'], 'success')
        self.assertEqual(details['method'], 'browser_full_dom')
        self.assertIn('checked_at', details)

    def test_browser_snapshot_is_collected_once(self):
        manager = MagicMock()
        manager.get_page_payload.return_value = {'html': HTML}
        text, details = enrich_stage_source(BODY, URL, manager=manager)
        self.assertIn(CONTACT, text)
        manager.get_page_payload.assert_called_once_with(URL, scroll_enabled=True, timeout=60)

    def test_exact_body_repetition_is_removed_but_contact_context_is_preserved(self):
        text, _ = enrich_stage_source(BODY, URL, payload={'html': HTML, 'inner_text': BODY})
        self.assertEqual(text.count(BODY), 1)
        self.assertIn(CONTACT, text)
        self.assertIn('[EXTRACTED BODY ABOVE]', text)

    def test_existing_playwright_payload_is_reused_without_reopening(self):
        instance = self.fetcher()
        instance.playwright_manager.get_page_payload.return_value = {'html': HTML, 'inner_text': BODY}
        body = instance._fetch_web_content_with_playwright(URL)
        with patch('src.core.config.STAGE_FULL_PAGE_ENABLED', True):
            text = instance._enrich_stage_content(URL, body)
        self.assertIn(CONTACT, text)
        instance.playwright_manager.get_page_payload.assert_called_once()
        instance._reset_last_fetch_summary('https://example.org/other')
        self.assertIsNone(instance._last_browser_payload)

    def test_normal_web_fetch_enriches_before_returning_to_analysis(self):
        instance = self.fetcher()
        instance.playwright_manager.get_page_payload.return_value = {'html': HTML}
        instance._check_content_type = MagicMock(return_value='text/html')
        instance._fetch_web_content_with_strategy = MagicMock(return_value=BODY)
        with patch('src.ingestion.ats_api.fetch_ats_content', return_value=None), \
                patch('src.core.config.STAGE_FULL_PAGE_ENABLED', True):
            text = instance.fetch_content(URL)
        self.assertIn(CONTACT, text)
        instance.playwright_manager.get_page_payload.assert_called_once()

    def test_failed_browser_falls_back_to_http_dom_with_explicit_method(self):
        manager = MagicMock()
        manager.get_page_payload.return_value = None
        text, details = enrich_stage_source(BODY, URL, manager=manager, http_html=HTML)
        self.assertIn(CONTACT, text)
        self.assertEqual(details['method'], 'http_full_dom')
        self.assertIn('browser_issue', details)

    def test_challenge_and_over_budget_keep_body_and_record_reason(self):
        for payload, budget, status in (({'html': '<p>Just a moment. Verifying you are human. Ray ID</p>'}, 200000, 'failed'),
                                        ({'html': HTML}, len(BODY), 'skipped')):
            with self.subTest(status=status):
                text, details = enrich_stage_source(BODY, URL, payload=payload, max_chars=budget)
                self.assertEqual(text, BODY)
                self.assertEqual(details['status'], status)
                self.assertTrue(details['reason'])

    def test_pdf_viewer_and_disabled_mode_do_not_open_browser(self):
        instance = self.fetcher()
        for url, enabled, selected in ((URL, False, 'http'),
                                        ('https://docs.qq.com/pdf/example', True, 'ocr'),
                                        (URL, True, 'pdf')):
            instance.last_fetch_summary['selected_method'] = selected
            with patch('src.core.config.STAGE_FULL_PAGE_ENABLED', enabled):
                self.assertEqual(instance._enrich_stage_content(url, BODY), BODY)
        instance.playwright_manager.get_page_payload.assert_not_called()

    def test_old_body_cache_and_changed_policy_are_not_reused(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(root=directory, version='offline')
            store.save_content('source', URL, BODY)
            path = store.body_path('source', URL)
            data = json.loads(path.read_text(encoding='utf-8'))
            del data['content_version']
            path.write_text(json.dumps(data), encoding='utf-8')
            self.assertIsNone(store.get_content('source', URL))
            store.save_content('source', URL, BODY)
            self.assertEqual(store.get_content('source', URL), BODY)
            with patch('src.core.run_store.STAGE_FULL_PAGE_ENABLED', False):
                self.assertIsNone(store.get_content('source', URL))

    def test_all_three_stages_receive_the_same_enriched_input(self):
        from test_priority_workflows import manager
        text, _ = enrich_stage_source(BODY, URL, payload={'html': HTML})
        instance = manager()
        success, _, error = instance.analyze_text_complete(text, 0)
        self.assertTrue(success, error)
        for stage in ('stage1', 'stage2', 'stage3'):
            getattr(instance.llm_agent, 'analyze_text_' + stage).assert_called_once_with(text)

    def test_system_prompt_treats_page_content_as_evidence(self):
        from src.core.llm_agent import LLMAgent
        agent = LLMAgent.__new__(LLMAgent)
        agent.use_openai = True
        agent._call_openai = MagicMock(return_value='{}')
        agent.call_llm('page input', 'stage rules')
        instructions = agent._call_openai.call_args.args[1]
        for rule in ('untrusted evidence', 'generic footer contacts', 'academic title', 'first-review dates'):
            self.assertIn(rule, instructions)


if __name__ == '__main__':
    unittest.main()
