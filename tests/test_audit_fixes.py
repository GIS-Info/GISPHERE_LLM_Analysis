"""Audit regressions: fake models and an in-memory Sheets API only."""
import copy
import json
import re
import tempfile
import unittest
from unittest.mock import MagicMock, patch

import pandas as pd

from src.core import observability
from src.core.contact_verifier import ContactVerifier
from src.core.final_review import validate_review, quote_match, field_quote_issue
from src.core.run_store import RunStore
from src.integrations.google_sheets_handler import GoogleSheetsHandler
from test_final_review import VALUES, SOURCE, report


class CitationTests(unittest.TestCase):
    def test_blank_and_punctuation_citations_never_match(self):
        for quote in ('', ' ', '\n\t', '\u3000', '...', '；'):
            with self.subTest(quote=quote):
                self.assertIsNone(quote_match(quote, [SOURCE]))
                data = report()
                for check in data['checks']:
                    check['evidence'] = quote
                checked = validate_review(data, SOURCE, [], VALUES)
                self.assertEqual(checked['verdict'], 'needs_review')

    def test_existing_but_unrelated_quote_does_not_support_email_name_or_date(self):
        for field, quote in (('Contact_Email', 'Example University'),
                             ('Contact_Name', 'United Kingdom'), ('Deadline', 'forest monitoring')):
            with self.subTest(field=field):
                data = report()
                next(c for c in data['checks'] if c['field'] == field)['evidence'] = quote
                checked = validate_review(data, SOURCE, [], VALUES)
                self.assertEqual(checked['verdict'], 'needs_review')
                self.assertEqual(checked['evidence_issues'][0]['field'], field)

    def test_email_normalization_and_exact_mailbox_boundary(self):
        self.assertIsNone(field_quote_issue('Contact_Email', 'jane@example.org', 'jane [at] example [dot] org'))
        self.assertIsNone(field_quote_issue('Contact_Email', 'jane@example.org', 'Email JANE@example.org.'))
        self.assertTrue(field_quote_issue('Contact_Email', 'jane@example.org', 'otherjane@example.org'))
        self.assertTrue(field_quote_issue('Contact_Email', 'jane@example.org', 'jane@example.org.evil'))

    def test_real_posting_email_and_professor_formats_do_not_create_false_alarms(self):
        for value, quote in (
            ('phuong.dao@austin.utexas.edu', 'Dr. Phuong Dao at phuong.dao@austin.utexas.edu'),
            ('levente.juhasz@ufl.edu', 'Email Dr. Juhász at **levente.juhasz@ufl.edu**'),
            ('jane@example.org', 'Jane Example at jane@example.org'),
            ('jane@example.org', '__jane@example.org__'),
            ('jane@example.org', '`jane@example.org`'),
            ('jane@example.org', '[jane@example.org](mailto:jane@example.org)'),
            ('jane@example.org', 'jane at example dot org'),
        ):
            with self.subTest(quote=quote):
                self.assertIsNone(field_quote_issue('Contact_Email', value, quote))
        self.assertTrue(field_quote_issue('Contact_Email', 'jane@example.org', '_jane@example.org'))
        self.assertIsNone(field_quote_issue('Contact_Name', 'Dr. Filip Biljecki',
            'supervised by the Principal Investigator of the Lab – Prof Filip Biljecki'))

    def test_verified_email_heading_with_short_prefix_is_a_valid_ordered_quote(self):
        self.assertEqual(quote_match('EMAIL: ... filip@nus.edu.sg',
            ['EMAIL:\nfilip@nus.edu.sg']), 'ordered_excerpts')
        self.assertIsNone(quote_match('EMAIL: ... filip@nus.edu.sg',
            ['EMAIL:', 'filip@nus.edu.sg']))

    def test_date_formats_and_inferred_year_without_accepting_a_conflicting_year(self):
        for quote in ('May 15, 2030', '15 May 2030', '2030-05-15', '15/05/2030',
                      '05/15/2030', '2030/05/15', '2030年5月15日', 'May 15'):
            with self.subTest(quote=quote):
                self.assertIsNone(field_quote_issue('Deadline', '2030-05-15', quote))
        for quote in ('May 15, 2031', '2031-05-15', 'May 16, 2030', '15/05/2031'):
            self.assertTrue(field_quote_issue('Deadline', '2030-05-15', quote))

    def test_title_alone_or_bare_name_does_not_prove_doctorate(self):
        self.assertTrue(field_quote_issue('Contact_Name', 'Dr. Jane Example', 'Jane Example'))
        self.assertTrue(field_quote_issue('Contact_Name', 'Dr. Jane Example', 'Professor Other Person'))
        self.assertIsNone(field_quote_issue('Contact_Name', 'Dr. Jane Example', 'Professor Jane Example'))
        self.assertIsNone(field_quote_issue('Contact_Name', 'Dr. Jane Example', 'Jane Example earned a doctorate.'))
        self.assertIsNone(field_quote_issue('Contact_Name', 'Dr. Jane Example', 'Dr Jane Example'))


class ContactTitleTests(unittest.TestCase):
    def setUp(self):
        tracer = patch.object(observability, 'get_tracing_client', return_value=None)
        tracer.start()
        self.addCleanup(tracer.stop)
        self.verifier = ContactVerifier.__new__(ContactVerifier)
        self.verifier._cleaned = True
        self.verifier.llm_agent = MagicMock()

    def page(self, degree, confidence='high', text='Jane Example has a PhD.'):
        return {'analysis': {'has_doctorate': degree, 'confidence': confidence, 'evidence': text},
                'review_source': {'url': 'https://offline.invalid/profile', 'text': text}}

    def test_string_false_numbers_and_empty_claims_do_not_supply_doctorate(self):
        for degree in ('false', 'true', 1, 0, None, False):
            with self.subTest(degree=degree):
                prefix, _, _ = self.verifier._synthesize_contact_info([self.page(degree)], 'Jane Example')
                self.assertEqual(prefix, '')

    def test_parser_rejects_non_boolean_degree_claims(self):
        for degree in ('false', 'true', 1, 0, [], {}):
            self.verifier.llm_agent.call_llm.return_value = json.dumps({'has_doctorate': degree})
            self.assertIsNone(self.verifier._analyze_page_with_llm('Jane Example profile', 'Jane Example'))

    def test_one_confirmed_doctorate_is_not_outvoted_by_uninformative_pages(self):
        pages = [self.page(True), self.page(None, text='Jane Example staff directory.'),
                 self.page(None, text='Jane Example publications.')]
        prefix, _, _ = self.verifier._synthesize_contact_info(pages, 'Jane Example')
        self.assertEqual(prefix, 'Dr.')
        self.assertEqual(len(self.verifier.last_review_evidence), 1)

    def test_low_confidence_unquoted_unrelated_and_conflicting_claims_stay_unresolved(self):
        bad_quote = self.page(True)
        bad_quote['analysis']['evidence'] = 'Invented quote'
        for pages in ([self.page(True, confidence='low')], [bad_quote],
                      [self.page(True, text='Other Person has a PhD.')],
                      [self.page(True), self.page(False, text='Jane Example has no doctorate.')]):
            with self.subTest(pages=pages):
                self.assertEqual(self.verifier._synthesize_contact_info(pages, 'Jane Example')[0], '')

    def test_generated_dr_without_posting_evidence_still_triggers_profile_lookup(self):
        self.verifier._is_generic_or_mismatched_email = MagicMock(return_value=False)
        self.assertTrue(self.verifier.should_verify_contact(
            'Dr. Jane Example', 'jane@example.org', 'Contact Jane Example at jane@example.org')[0])
        self.assertFalse(self.verifier.should_verify_contact(
            'Dr. Jane Example', 'jane@example.org', 'Contact Prof Jane Example at jane@example.org')[0])

    def test_profile_proving_unchanged_title_is_forwarded_to_final_review(self):
        self.verifier.mcp_client = object()
        self.verifier._is_generic_or_mismatched_email = MagicMock(return_value=False)
        self.verifier.search_contact_info = MagicMock(return_value=[{'url': 'official-profile'}])
        page = self.page(True)
        self.verifier.analyze_contact_pages = lambda *_: self.verifier._synthesize_contact_info([page], 'Jane Example')
        with patch('src.core.contact_verifier.CONTACT_VERIFICATION_ENABLED', True):
            result = self.verifier.verify_and_update_contact('Example University', 'Dr. Jane Example',
                'jane@example.org', 'Contact Jane Example at jane@example.org')
        self.assertEqual(result['Contact_Name'], 'Dr. Jane Example')
        self.assertEqual(self.verifier.last_review_evidence[0]['decision_fields'], ['Contact_Name'])


class DocumentCacheTests(unittest.TestCase):
    def test_document_metadata_survives_without_a_downloaded_file(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(directory, version='offline')
            store.save_content('sheet', 'https://offline.invalid/download?id=1', 'PDF text', is_document=True)
            self.assertEqual(store.get_content('sheet', 'https://offline.invalid/download?id=1', with_metadata=True),
                             {'content': 'PDF text', 'is_document': True})
            self.assertEqual(store.get_content('sheet', 'https://offline.invalid/download?id=1'), 'PDF text')

    def test_legacy_or_invalid_metadata_requires_a_refetch(self):
        with tempfile.TemporaryDirectory() as directory:
            store = RunStore(directory, version='offline')
            for value in (None, 'false', 1):
                store.save_content('sheet', 'url', 'body')
                path = store.body_path('sheet', 'url')
                data = json.loads(path.read_text(encoding='utf-8'))
                if value is None:
                    del data['is_document']
                else:
                    data['is_document'] = value
                path.write_text(json.dumps(data), encoding='utf-8')
                self.assertIsNone(store.get_content('sheet', 'url'))


class SheetsSaveTests(unittest.TestCase):
    def setUp(self):
        self.handler = GoogleSheetsHandler.__new__(GoogleSheetsHandler)
        self.handler.spreadsheet_id = 'offline'
        self.handler.sheet_name = "Filled's data"
        self.handler._initialize_service = MagicMock()
        self.handler.service = MagicMock()
        self.api = self.handler.service.spreadsheets.return_value.values.return_value
        self.remote = [['Source', 'Direction', 'Verifier'], ['job-1', '', ''], ['job-2', 'old', '']]
        self.formulas = copy.deepcopy(self.remote)
        self.handler.df = pd.DataFrame(copy.deepcopy(self.remote[1:]), columns=self.remote[0], dtype=object)
        self.handler.original_df = self.handler.df.copy(deep=True)
        self.handler.original_formulas = copy.deepcopy(self.formulas)
        self.api.batchGet.side_effect = self.read
        self.api.batchUpdate.side_effect = self.write

    def read(self, **kwargs):
        grid = self.formulas if kwargs['valueRenderOption'] == 'FORMULA' else self.remote
        snapshots = []
        for address in kwargs['ranges']:
            row = int(re.search(r'!A(\d+):', address)[1]) - 1
            snapshots.append({'values': [copy.deepcopy(grid[row])] if row < len(grid) else []})
        request = MagicMock()
        request.execute.return_value = {'valueRanges': snapshots}
        return request

    def write(self, **kwargs):
        self.assertEqual(kwargs['body']['valueInputOption'], 'RAW')
        responses = []
        for entry in kwargs['body']['data']:
            match = re.search(r'!([A-Z]+)(\d+)$', entry['range'])
            col = 0
            for letter in match[1]:
                col = col * 26 + ord(letter) - 64
            row, col = int(match[2]) - 1, col - 1
            value = entry['values'][0][0]
            self.remote[row][col] = self.formulas[row][col] = value
            responses.append({'updatedData': {'values': [[value]] if value != '' else []}})
        request = MagicMock()
        request.execute.return_value = {'responses': responses}
        return request

    def test_only_changed_cells_are_saved_and_other_remote_edits_survive(self):
        self.remote[2][1] = self.formulas[2][1] = 'human edit'
        self.handler.df.loc[0, 'Direction'] = '=1+1'
        self.handler.df.loc[0, 'Verifier'] = 'LLM'
        self.assertTrue(self.handler.save_data())
        body = self.api.batchUpdate.call_args.kwargs['body']
        self.assertEqual([entry['range'] for entry in body['data']],
                         ["'Filled''s data'!B2", "'Filled''s data'!C2"])
        self.assertEqual(self.remote[1][1], '=1+1')
        self.assertEqual(self.remote[2][1], 'human edit')
        self.assertEqual(self.handler.original_df.loc[1, 'Direction'], 'old')
        self.assertTrue(self.handler.save_data())
        self.api.batchUpdate.assert_called_once()

    def test_any_edit_to_the_audited_row_prevents_all_writes(self):
        self.handler.df.loc[0, 'Direction'] = 'new'
        self.handler.df.loc[1, 'Direction'] = 'new too'
        self.remote[1][0] = 'different-job'
        self.assertFalse(self.handler.save_data())
        self.api.batchUpdate.assert_not_called()
        self.assertIn('重新审核', self.handler.last_save_error)
        self.assertEqual(self.handler.original_df.loc[0, 'Direction'], '')

    def test_formula_change_with_the_same_displayed_value_is_a_conflict(self):
        self.handler.df.loc[0, 'Direction'] = 'new'
        self.formulas[1][0] = '=SOME_OTHER_FORMULA()'
        self.assertFalse(self.handler.save_data())
        self.api.batchUpdate.assert_not_called()

    def test_header_change_and_incomplete_snapshot_do_not_write(self):
        self.handler.df.loc[0, 'Direction'] = 'new'
        self.remote[0][0] = 'Renamed'
        self.assertFalse(self.handler.save_data())
        self.api.batchUpdate.assert_not_called()
        self.api.batchGet.side_effect = None
        self.api.batchGet.return_value.execute.return_value = {'valueRanges': []}
        self.assertFalse(self.handler.save_data())
        self.api.batchUpdate.assert_not_called()

    def test_clearing_values_is_explicit_and_success_advances_the_baseline(self):
        self.handler.df.loc[1, 'Direction'] = ''
        self.assertTrue(self.handler.save_data())
        self.assertEqual(self.remote[2][1], '')
        self.assertEqual(self.handler.original_df.loc[1, 'Direction'], '')
        self.handler.df.loc[1, 'Direction'] = '2030-05-15'
        self.assertTrue(self.handler.save_data())
        self.assertEqual(self.remote[2][1], '2030-05-15')

    def test_read_failure_and_mismatched_write_response_are_reported(self):
        self.handler.df.loc[0, 'Direction'] = 'new'
        self.api.batchGet.side_effect = RuntimeError('offline read failure')
        self.assertFalse(self.handler.save_data())
        self.api.batchUpdate.assert_not_called()
        self.api.batchGet.side_effect = self.read
        self.api.batchUpdate.side_effect = None
        self.api.batchUpdate.return_value.execute.return_value = {
            'responses': [{'updatedData': {'values': [['different value']]}}]}
        self.assertFalse(self.handler.save_data())
        self.assertEqual(self.handler.original_df.loc[0, 'Direction'], '')

    def test_no_changes_needs_no_api_and_structural_changes_are_rejected(self):
        self.assertTrue(self.handler.save_data())
        self.api.batchGet.assert_not_called()
        self.api.batchUpdate.assert_not_called()
        self.handler.df.loc[2] = ['job-3', '', '']
        self.assertFalse(self.handler.save_data())
        self.api.batchUpdate.assert_not_called()

    def test_load_establishes_display_and_formula_baselines_before_save(self):
        handler = GoogleSheetsHandler()
        handler.service = self.handler.service
        handler.spreadsheet_id = 'offline'
        handler.sheet_name = self.handler.sheet_name
        handler._initialize_service = MagicMock()
        self.handler.service.spreadsheets.return_value.get.return_value.execute.return_value = {
            'sheets': [{'properties': {'title': handler.sheet_name, 'sheetId': 0}}]}
        initial = [['Source', 'Notes', 'Verifier', 'Error', 'Direction'], ['job-1', '', '', '', '']]
        self.api.get.return_value.execute.return_value = {'values': initial}
        self.assertTrue(handler.load_data())
        self.assertEqual(self.api.get.call_count, 2)
        self.assertEqual(self.api.get.call_args.kwargs['valueRenderOption'], 'FORMULA')
        self.assertEqual(handler.original_formulas, initial)
        self.assertTrue(handler.df.equals(handler.original_df))
        self.assertTrue(handler.save_data())
        self.api.batchUpdate.assert_not_called()

    def test_direct_update_and_append_also_use_raw(self):
        self.handler.update_data_in_sheet("'Filled''s data'!B2", [['=1+1']])
        self.assertEqual(self.api.update.call_args.kwargs['valueInputOption'], 'RAW')
        self.handler.append_data_to_sheet("'Filled''s data'!B2", [['=1+1']])
        self.assertEqual(self.api.append.call_args.kwargs['valueInputOption'], 'RAW')


if __name__ == '__main__':
    unittest.main()
