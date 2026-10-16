"""Independent review and selected-source provenance, with all networking mocked."""
import copy
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from src.core import observability
from src.core.final_review import (FIELDS, candidate_fields, review_final_result,
                                   validate_review, review_error, review_fingerprint,
                                   quote_match, evidence_documents)
from src.core.config import FINAL_REVIEW_MODEL
from src.core.contact_verifier import ContactVerifier
from src.core.run_store import RunStore
from src.core.result_schema import discard_overlong_labels

CASE = json.loads((Path(__file__).parent / 'fixtures/evaluation_cases.json').read_text(encoding='utf-8'))['cases'][0]
VALUES = {k: v for result in CASE['expected'].values() for k, v in result.items()}
SOURCE = 'Verbatim supporting evidence from an offline source.'


def report():
    return {'verdict': 'pass', 'summary': '字段审核通过', 'checks': [
        {'field': k, 'status': 'ok', 'reason': '有依据或符合缺失默认规则', 'evidence': SOURCE}
        for k in FIELDS]}


class FinalReviewTests(unittest.TestCase):
    def setUp(self):
        tracing = patch.object(observability, 'get_tracing_client', return_value=None)
        tracing.start()
        self.addCleanup(tracing.stop)
        self.client = SimpleNamespace(api_base_url='https://offline.invalid/v1', headers={})
        self.response = MagicMock()
        self.response.json.return_value = {'choices': [{'finish_reason': 'stop', 'message': {}}]}

    def call(self, data=None, source=SOURCE, evidence=None):
        with patch('src.core.final_review.model_post', return_value=(self.response, json.dumps(data or report(), ensure_ascii=False))) as post:
            value = review_final_result(source, VALUES, client=self.client, evidence=evidence)
        return value, post

    def test_one_opus_request_contains_all_stages_and_no_fallback_options(self):
        result, post = self.call()
        self.assertEqual(result['status'], 'passed')
        post.assert_called_once()
        body = post.call_args.kwargs['body']
        self.assertEqual(body['model'], 'claude-opus-5-5')
        self.assertEqual(set(body), {'model', 'messages', 'max_tokens'})
        payload = json.loads(body['messages'][1]['content'])
        self.assertEqual(set(payload['fields']), set(FIELDS))
        self.assertEqual(payload['source_text'], SOURCE)

    def test_issue_or_uncertainty_does_not_pass_or_correct_fields(self):
        for status in ('issue', 'uncertain'):
            data = report()
            data['verdict'] = 'needs_review'
            data['checks'][0]['status'] = status
            data['checks'][0]['reason'] = '日期依据不足'
            before = copy.deepcopy(VALUES)
            result, _ = self.call(data)
            self.assertEqual(result['status'], 'needs_review')
            self.assertIn('Deadline', review_error(result))
            self.assertEqual(before, VALUES)

    def test_missing_duplicate_unknown_checks_and_conflicting_verdict_are_rejected(self):
        for mode in ('missing', 'duplicate', 'unknown', 'contradiction'):
            data = report()
            if mode == 'missing':
                data['checks'].pop()
            elif mode == 'duplicate':
                data['checks'][-1]['field'] = FIELDS[0]
            elif mode == 'unknown':
                data['checks'][-1]['field'] = 'Unknown'
            else:
                data['checks'][0]['status'] = 'issue'
            with self.subTest(mode=mode):
                self.assertEqual(self.call(data)[0]['status'], 'incomplete')

    def test_truncation_refusal_and_malformed_json_never_pass(self):
        for reason in ('length', 'content_filter', None):
            self.response.json.return_value = {'choices': [{'finish_reason': reason}]}
            self.assertEqual(self.call()[0]['status'], 'incomplete')
        self.response.json.return_value = {'choices': [{'finish_reason': 'stop', 'message': {'refusal': 'no'}}]}
        self.assertEqual(self.call()[0]['status'], 'incomplete')
        self.response.json.return_value = {'choices': [{'finish_reason': 'stop'}]}
        with patch('src.core.final_review.model_post', return_value=(self.response, 'not JSON')):
            self.assertEqual(review_final_result(SOURCE, VALUES, client=self.client)['status'], 'incomplete')

    def test_timeout_is_one_attempt_not_an_extraction_model_fallback(self):
        with patch('src.core.final_review.model_post', side_effect=TimeoutError('offline')) as post:
            self.assertEqual(review_final_result(SOURCE, VALUES, client=self.client)['status'], 'incomplete')
        post.assert_called_once()

    def test_discarded_primary_is_reviewed_without_restoring_rejected_value(self):
        values, rejected = discard_overlong_labels(dict(VALUES, WX_Label1='生成式人工智能'))
        with patch('src.core.final_review.model_post', return_value=(self.response, json.dumps(report(), ensure_ascii=False))) as post:
            result = review_final_result(SOURCE, values, client=self.client, rejected_labels=rejected)
        self.assertEqual(result['status'], 'passed')
        post.assert_called_once()
        payload = json.loads(post.call_args.kwargs['body']['messages'][1]['content'])
        self.assertEqual(payload['fields']['WX_Label1'], '')
        self.assertEqual(payload['rejected_labels'], rejected)
        self.assertEqual(result['rejected_labels'], rejected)

    def test_unexplained_empty_primary_does_not_use_the_rejection_exception(self):
        with patch('src.core.final_review.model_post', return_value=(self.response, json.dumps(report(), ensure_ascii=False))) as post:
            result = review_final_result(SOURCE, dict(VALUES, WX_Label1=''), client=self.client)
        self.assertEqual(result['status'], 'needs_review')
        post.assert_called_once()
        self.assertTrue(any(c['field'] == 'WX_Label1' and c['status'] == 'issue' for c in result['checks']))

    def test_single_json_fence_is_supported_but_surrounding_prose_is_rejected(self):
        raw = json.dumps(report(), ensure_ascii=False)
        for wrapper, expected in (('```json\n' + raw + '\n```', 'passed'),
                                  ('I approve it: ' + raw, 'incomplete')):
            with patch('src.core.final_review.model_post', return_value=(self.response, wrapper)):
                self.assertEqual(review_final_result(SOURCE, VALUES, client=self.client)['status'], expected)

    def test_missing_quote_or_fabricated_quote_is_rejected(self):
        for quote in ('made up evidence', ''):
            data = report()
            next(c for c in data['checks'] if c['field'] == 'University_EN')['evidence'] = quote
            result, _ = self.call(data)
            self.assertEqual(result['status'], 'needs_review')
            self.assertTrue(any(c['field'] == 'University_EN' and c['status'] == 'uncertain' for c in result['checks']))
            self.assertTrue(any(c['field'] == 'Deadline' and c['status'] == 'ok' for c in result['checks']))
            self.assertEqual(result['evidence_issues'][0]['quote'], quote)

    def test_quote_matching_accepts_whitespace_and_ordered_excerpts(self):
        docs = ['Fall 2027 enrollment.\n\nCandidates should apply by December 1 for full consideration.']
        self.assertEqual(quote_match('Fall 2027 enrollment. Candidates should apply', docs), 'format_normalized')
        self.assertEqual(quote_match('Fall 2027 enrollment; Candidates should apply by December 1', docs), 'ordered_excerpts')
        self.assertEqual(quote_match('Fall 2027 enrollment ... Candidates should apply by December 1', docs), 'ordered_excerpts')

    def test_quote_matching_never_accepts_reordered_or_cross_source_fragments(self):
        self.assertIsNone(quote_match('Fall 2027 enrollment ... Candidates should apply by December 1',
            ['Fall 2027 enrollment', 'Candidates should apply by December 1']))
        self.assertIsNone(quote_match('Candidates should apply by December 1 ... Fall 2027 enrollment',
            ['Fall 2027 enrollment. Candidates should apply by December 1']))
        self.assertIsNone(quote_match('博士 ... university', ['博士 university']))
        self.assertIsNone(quote_match('The deadline is January 2', ['The deadline is January 1']))

    def test_proposed_values_and_decision_metadata_are_not_quote_evidence(self):
        docs = evidence_documents('source text', [{'text': 'actual source', 'decision_fields': ['Contact_Email'],
            'model_judgment': 'invented claim'}])
        self.assertIsNone(quote_match('Contact_Email', docs))
        self.assertIsNone(quote_match('invented claim', docs))

    def test_external_decision_page_can_support_contact_absent_from_posting(self):
        external = [{'url': 'https://example.invalid/profile', 'text': 'Jane Doe\nEmail: jane@example.invalid',
                     'decision_fields': ['Contact_Email']}]
        data = report()
        next(c for c in data['checks'] if c['field'] == 'Contact_Email')['evidence'] = 'Email: jane@example.invalid'
        result, post = self.call(data, evidence=external)
        self.assertEqual(result['status'], 'passed')
        self.assertEqual(json.loads(post.call_args.kwargs['body']['messages'][1]['content'])['external_evidence'], external)

    def test_oversize_input_is_not_truncated_and_invalid_fields_do_not_call_model(self):
        with patch('src.core.final_review.FINAL_REVIEW_MAX_CHARS', 10):
            result, post = self.call()
            self.assertEqual(result['status'], 'incomplete')
            post.assert_not_called()
        with patch('src.core.final_review.model_post') as post:
            self.assertEqual(review_final_result(SOURCE, {}, client=self.client)['status'], 'incomplete')
            post.assert_not_called()

    def test_candidate_is_exact_merge_and_fingerprint_changes_with_fields_or_source(self):
        candidate = candidate_fields({'University_EN': 'Old', 'Urban': '1', 'Contact_Email': float('nan')}, {'University_EN': 'New'})
        self.assertEqual(candidate['University_EN'], 'New')
        self.assertEqual(candidate['Urban'], '1')
        self.assertEqual(candidate['Contact_Email'], '')
        self.assertNotEqual(review_fingerprint('a', candidate, []), review_fingerprint('b', candidate, []))
        self.assertNotEqual(review_fingerprint('a', candidate, []), review_fingerprint('a', candidate, [{'text': 'x'}]))


class ProvenanceTests(unittest.TestCase):
    def verifier(self):
        instance = ContactVerifier.__new__(ContactVerifier)
        instance._cleaned = True
        instance._is_generic_or_mismatched_email = MagicMock(return_value=False)
        return instance

    def page(self, url, degree=False, email='', gender='unknown'):
        return {'url': url, 'analysis': {'has_doctorate': degree, 'email_address': email, 'gender': gender},
                'review_source': {'url': url, 'text': 'Exact snapshot ' + url}}

    def test_only_adopted_email_page_and_supporting_title_page_survive(self):
        instance = self.verifier()
        pages = [self.page('email-source', email='jane@example.invalid'),
                 self.page('title-source', degree=True), self.page('other-positive-page', degree=True)]
        prefix, email, _ = instance._synthesize_contact_info(pages, 'Jane Doe')
        self.assertEqual((prefix, email), ('Dr.', 'jane@example.invalid'))
        self.assertEqual([p['url'] for p in instance.last_review_evidence], ['email-source', 'title-source'])
        self.assertEqual(instance.last_review_evidence[0]['decision_fields'], ['Contact_Email'])
        self.assertEqual(instance.last_review_evidence[1]['decision_fields'], ['Contact_Name'])

    def test_second_email_page_and_unrelated_candidates_are_not_forwarded(self):
        instance = self.verifier()
        instance._synthesize_contact_info([self.page('chosen', email='one@example.invalid'),
            self.page('not-chosen', email='two@example.invalid'), self.page('unrelated')], 'Jane Doe')
        self.assertEqual([p['url'] for p in instance.last_review_evidence], ['chosen'])

    def test_resume_restores_decision_page_and_review_is_saved_separately(self):
        with tempfile.TemporaryDirectory() as folder:
            store = RunStore(folder, version='offline')
            evidence = [{'url': 'selected', 'text': 'snapshot', 'decision_fields': ['Contact_Email']}]
            store.save_stage('sheet', 0, 'url', 'body', 'stage1', True, CASE['expected']['stage1'], '', evidence=evidence)
            self.assertEqual(store.load_stage_evidence('sheet', 0, 'url', 'body')['stage1'], evidence)
            self.assertEqual(store.load_stage_evidence('sheet', 0, 'url', 'changed'), {})
            self.assertTrue(store.save_review('sheet', 0, 'url', {'status': 'passed'}))
            self.assertEqual(len(list((Path(folder) / 'reviews').glob('*.json'))), 1)

    def test_evidence_for_unchanged_contact_is_not_forwarded(self):
        instance = self.verifier()
        instance.mcp_client = object()
        instance.should_verify_contact = MagicMock(return_value=(True, 'offline check'))
        instance.search_contact_info = MagicMock(return_value=[{'url': 'offline'}])
        instance._validate_and_format_name = MagicMock(return_value='Dr. Jane Doe')
        def analyze(*args):
            instance.last_review_evidence = [
                {'url': 'name-page', 'text': 'Professor Jane Doe', 'decision_fields': ['Contact_Name']},
                {'url': 'email-page', 'text': 'other@example.invalid', 'decision_fields': ['Contact_Email']}]
            return 'Dr.', 'other@example.invalid', 'offline'
        instance.analyze_contact_pages = analyze
        with patch('src.core.contact_verifier.CONTACT_VERIFICATION_ENABLED', True):
            result = instance.verify_and_update_contact('Example University', 'Dr. Jane Doe', 'jane@example.invalid', 'offline source')
        self.assertEqual(result['Contact_Email'], 'jane@example.invalid')
        self.assertEqual(instance.last_review_evidence, [])


if __name__ == '__main__':
    unittest.main()
