"""Business regression tests. All models, browsers and table writes stay offline."""
import copy
import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pandas as pd

from src.core import observability as tracing
from src.core.analysis_stage import AnalysisStageManager
from src.core.result_schema import STAGE_FIELDS, validation_errors, valid_deadline
from src.core.run_options import RunOptions, parse_rows, parse_run_options, rerun_updates, select_rows
from src.core.run_store import RunStore
from src.ingestion.excel_handler import ExcelHandler, validate_analysis_result
from src.main import LLMAnalysisSystem
from src.tools.evaluate import DEFAULT_CASES, evaluate_predictions, load_cases, run_live

CASES = load_cases(DEFAULT_CASES)
GOLD = CASES[0]["expected"]


def manager():
    instance = AnalysisStageManager.__new__(AnalysisStageManager)
    instance._cleaned = True
    instance.llm_agent = MagicMock()
    instance.contact_verifier = None
    instance.direction_verifier = MagicMock()
    instance._execute_contact_verification = MagicMock(return_value=None)
    for stage in STAGE_FIELDS:
        getattr(instance.llm_agent, "analyze_text_" + stage).return_value = copy.deepcopy(GOLD[stage])
    return instance


class OfflineTests(unittest.TestCase):
    def setUp(self):
        patcher = patch.object(tracing, "get_tracing_client", return_value=None)
        patcher.start()
        self.addCleanup(patcher.stop)


class ValidationTests(OfflineTests):
    def test_all_fixture_outputs_pass_shared_validator(self):
        for case in CASES:
            for stage in STAGE_FIELDS:
                with self.subTest(case=case["id"], stage=stage):
                    self.assertTrue(validate_analysis_result(case["expected"][stage], stage))

    def test_calendar_validation_handles_leap_years_and_trailing_text(self):
        for value in ("Soon", "", "2028-02-29"):
            self.assertTrue(valid_deadline(value))
        for value in ("2026-99-99", "2027-02-29", "2030-01-01 extra", "2030-1-1", None):
            self.assertFalse(valid_deadline(value))

    def test_missing_unknown_and_wrong_type_fields_fail(self):
        for result in ({}, dict(GOLD["stage1"], Extra="x"), dict(GOLD["stage1"], Number_Places=2)):
            self.assertTrue(validation_errors(result, "stage1"))
        self.assertTrue(validation_errors({"GIS": 1}, "stage2"))
        self.assertTrue(validation_errors({"GIS": "1", "Unknown": "1"}, "stage2"))
        self.assertTrue(validation_errors({}, "unknown"))

    def test_label_rules_and_empty_reserved_labels(self):
        for field, value in (("WX_Label1", ""), ("WX_Label1", "English"),
                             ("WX_Label1", "一二三四五六七"), ("WX_Label2", "遥感"),
                             ("WX_Label4", "统计学"), ("Country_CN", "UK")):
            with self.subTest(field=field, value=value):
                self.assertTrue(validation_errors(dict(GOLD["stage3"], **{field: value}), "stage3"))

    def test_real_stage2_rejects_unknown_fields_before_direction_retry(self):
        instance = manager()
        instance.llm_agent.analyze_text_stage2.return_value = {"GIS": "1", "Unknown": "1"}
        self.assertFalse(instance._execute_stage2("offline")[0])
        instance.direction_verifier.verify_and_map_direction.assert_not_called()

    def test_direction_retry_is_retained_and_revalidated(self):
        instance = manager()
        instance.llm_agent.analyze_text_stage2.side_effect = lambda text: {"Doctoral Student": "1"}
        instance.direction_verifier.verify_and_map_direction.return_value = {"GIS": "1"}
        self.assertTrue(instance._execute_stage2("offline")[0])
        instance.direction_verifier.verify_and_map_direction.return_value = {"GIS": "1", "Unknown": "1"}
        self.assertFalse(instance._execute_stage2("offline")[0])

    def test_actual_stage1_and_stage3_reject_bad_outputs(self):
        instance = manager()
        instance.llm_agent.analyze_text_stage1.return_value = dict(GOLD["stage1"], Deadline="2026-99-99")
        self.assertFalse(instance._execute_stage1("offline")[0])
        instance.llm_agent.analyze_text_stage3.return_value = dict(GOLD["stage3"], WX_Label1="English")
        self.assertFalse(instance._execute_stage3("offline")[0])

    def test_resume_calls_only_failed_stage(self):
        instance = manager()
        checkpoint = {stage: copy.deepcopy(GOLD[stage]) for stage in ("stage1", "stage3")}
        callback = MagicMock()
        success, result, error = instance.analyze_text_complete("offline", 0,
            completed_stages=checkpoint, on_stage_complete=callback)
        self.assertTrue(success)
        self.assertEqual(error, "")
        self.assertEqual(result["Deadline"], GOLD["stage1"]["Deadline"])
        instance.llm_agent.analyze_text_stage1.assert_not_called()
        instance.llm_agent.analyze_text_stage3.assert_not_called()
        instance.llm_agent.analyze_text_stage2.assert_called_once()
        self.assertEqual(instance.last_run_summary["executed"], ["stage2"])
        callback.assert_called_once()

    def test_invalid_checkpoint_is_not_reused(self):
        instance = manager()
        instance.analyze_text_complete("offline", 0, completed_stages={"stage1": {"Deadline": "bad"}})
        instance.llm_agent.analyze_text_stage1.assert_called_once()

    def test_failure_and_checkpoint_io_error_do_not_skip_later_stages(self):
        instance = manager()
        instance.llm_agent.analyze_text_stage2.return_value = {"Unknown": "1"}
        callback = MagicMock(side_effect=OSError("offline cache error"))
        success, _, error = instance.analyze_text_complete("offline", 0, on_stage_complete=callback)
        self.assertTrue(success)
        self.assertIn("阶段2失败", error)
        instance.llm_agent.analyze_text_stage3.assert_called_once()
        self.assertEqual(callback.call_count, 3)


class SelectionTests(unittest.TestCase):
    def setUp(self):
        self.rows = {0: {"Verifier": "", "Error": ""}, 1: {"Verifier": "", "Error": "阶段2失败"},
                     2: {"Verifier": "LLM", "Error": "需转换链接"}, 3: {"Verifier": "", "Error": "N"}}

    def test_display_rows_and_default_selection(self):
        self.assertEqual(parse_rows("2,4-5,2"), (2, 4, 5))
        self.assertEqual(select_rows(self.rows, RunOptions()), [0])
        self.assertEqual(select_rows(self.rows, RunOptions(rerun="failed")), [1])
        self.assertEqual(select_rows(self.rows, RunOptions(rows=(3,), rerun="all")), [1])

    def test_invalid_and_missing_rows_fail(self):
        for value in ("1", "3-2", "2,", "abc", "2-3-4"):
            with self.subTest(value=value), self.assertRaises(ValueError):
                # argparse.ArgumentTypeError is handled separately below.
                try:
                    parse_rows(value)
                except Exception as exc:
                    raise ValueError from exc
        with self.assertRaises(ValueError):
            select_rows(self.rows, RunOptions(rows=(99,)))

    def test_cli_guards_and_limit(self):
        self.assertEqual(parse_run_options(["test"]).limit, 1)
        for argv in (["--rerun", "all"], ["--overwrite"], ["--limit", "0"]):
            with self.subTest(argv=argv), self.assertRaises(SystemExit):
                parse_run_options(argv)
        self.assertEqual(select_rows(self.rows, RunOptions(rows=(2, 3), rerun="all", limit=1)), [0])

    def test_rerun_preserves_edits_and_only_updates_successful_fields(self):
        result = dict(GOLD["stage1"], **GOLD["stage2"])
        updates, conflicts = rerun_updates({"University_EN": "Human reviewed"}, result,
                                           ["stage1", "stage2"], ["stage2"])
        self.assertNotIn("University_EN", updates)
        self.assertIn("University_EN", conflicts)
        self.assertNotIn("University_CN", updates)

    def test_overwrite_only_affects_executed_stages_and_clears_stale_classification(self):
        row = {"University_EN": "Human reviewed", "Urban": "1"}
        result = dict(GOLD["stage1"], **GOLD["stage2"])
        updates, conflicts = rerun_updates(row, result, ["stage1", "stage2"], ["stage2"], overwrite=True)
        self.assertNotIn("University_EN", updates)
        self.assertIn("University_EN", conflicts)
        self.assertEqual(updates["Urban"], "")


class StoreTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.store = RunStore(temporary.name, version="v1")

    def test_content_cache_expiry_corruption_and_source_isolation(self):
        self.store.save_content("source", "url", "body")
        self.assertEqual(self.store.get_content("source", "url"), "body")
        self.assertIsNone(self.store.get_content("other-source", "url"))
        path = self.store.body_path("source", "url")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["created_at"] = 0
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertIsNone(self.store.get_content("source", "url"))
        path.write_text("{broken", encoding="utf-8")
        self.assertIsNone(self.store.get_content("source", "url"))

    def test_stage_cache_matches_content_version_row_and_source(self):
        self.store.save_stage("source", 0, "url", "body", "stage1", True, GOLD["stage1"], "")
        self.assertIn("stage1", self.store.load_stages("source", 0, "url", "body"))
        self.assertEqual(self.store.load_stages("source", 0, "url", "changed"), {})
        self.assertEqual(self.store.load_stages("source", 1, "url", "body"), {})
        self.assertEqual(self.store.load_stages("other-source", 0, "url", "body"), {})
        self.assertEqual(RunStore(self.store.root, "v2").load_stages("source", 0, "url", "body"), {})

    def test_failed_invalid_and_date_sensitive_checkpoints_are_not_reused(self):
        self.store.save_stage("source", 0, "url", "body", "stage1", True, GOLD["stage1"], "")
        self.store.save_stage("source", 0, "url", "body", "stage2", False, {}, "error")
        self.store.save_stage("source", 0, "url", "body", "stage3", True, {"invalid": "x"}, "")
        path = self.store.checkpoint_path("source", 0, "url")
        data = json.loads(path.read_text(encoding="utf-8"))
        data["stages"]["stage1"]["analysis_date"] = "2000-01-01"
        path.write_text(json.dumps(data), encoding="utf-8")
        self.assertEqual(self.store.load_stages("source", 0, "url", "body"), {})


class WorkflowTests(OfflineTests):
    def setUp(self):
        super().setUp()
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.system = LLMAnalysisSystem.__new__(LLMAnalysisSystem)
        self.system.options = RunOptions(rerun="failed")
        self.system.run_store = RunStore(temporary.name, "v1")
        handler = ExcelHandler(use_google_sheets=False)
        row = {field: "" for fields in STAGE_FIELDS.values() for field in fields}
        row.update(GOLD["stage1"])
        row.update(GOLD["stage3"])
        row.update(Source="https://example.invalid/job", Notes="", Verifier="", Error="阶段2失败")
        handler.df = pd.DataFrame([row], dtype=object)
        self.system.excel_handler = handler
        self.system.content_fetcher = MagicMock()
        self.system.content_fetcher.fetch_content.return_value = "offline body"
        self.system.content_fetcher.get_last_fetch_summary.return_value = {}
        self.system.analysis_manager = manager()
        self.source, self.url = self.system._source_identity(), row["Source"]
        self.system.run_store.save_content(self.source, self.url, "offline body")
        for stage in ("stage1", "stage3"):
            self.system.run_store.save_stage(self.source, 0, self.url, "offline body", stage, True, GOLD[stage], "")
        third_party = patch("src.core.utils.is_third_party_url", return_value=False)
        third_party.start()
        self.addCleanup(third_party.stop)

    def test_failed_rerun_reuses_body_and_successful_stages_and_clears_old_error(self):
        self.assertTrue(self.system._process_single_row(0))
        self.system.content_fetcher.fetch_content.assert_not_called()
        self.system.analysis_manager.llm_agent.analyze_text_stage1.assert_not_called()
        self.system.analysis_manager.llm_agent.analyze_text_stage3.assert_not_called()
        self.system.analysis_manager.llm_agent.analyze_text_stage2.assert_called_once()
        self.assertEqual(self.system.excel_handler.df.loc[0, "Error"], "")
        self.assertEqual(self.system.excel_handler.df.loc[0, "Verifier"], "LLM")

    def test_preserved_human_edit_does_not_get_marked_fully_verified(self):
        self.system.excel_handler.df.loc[0, "University_EN"] = "Human reviewed"
        self.assertTrue(self.system._process_single_row(0))
        self.assertEqual(self.system.excel_handler.df.loc[0, "University_EN"], "Human reviewed")
        self.assertEqual(self.system.excel_handler.df.loc[0, "Verifier"], "")
        self.assertIn("保留已有字段", self.system.excel_handler.df.loc[0, "Error"])

    def test_refresh_changed_body_invalidates_stage_cache(self):
        self.system.options = RunOptions(rerun="failed", refresh_content=True)
        self.system.content_fetcher.fetch_content.return_value = "new offline body"
        self.assertTrue(self.system._process_single_row(0))
        self.system.content_fetcher.fetch_content.assert_called_once()
        self.system.analysis_manager.llm_agent.analyze_text_stage1.assert_called_once()
        self.system.analysis_manager.llm_agent.analyze_text_stage3.assert_called_once()

    def test_all_rerun_overwrites_only_with_explicit_option(self):
        self.system.options = RunOptions(rows=(2,), rerun="all", overwrite=True)
        self.system.excel_handler.df.loc[0, "University_EN"] = "old"
        self.system.excel_handler.df.loc[0, "Urban"] = "1"
        self.assertTrue(self.system._process_single_row(0))
        self.assertEqual(self.system.excel_handler.df.loc[0, "University_EN"], GOLD["stage1"]["University_EN"])
        self.assertEqual(self.system.excel_handler.df.loc[0, "Urban"], "")

    def test_google_dataframe_status_clear_uses_google_handler(self):
        handler = self.system.excel_handler
        google_frame = handler.df.copy()
        google_frame.loc[0, "Verifier"] = "LLM"
        handler.use_google_sheets = True
        handler.google_handler = SimpleNamespace(df=google_frame)
        self.assertTrue(handler.set_row_status(0, "", ""))
        self.assertEqual(google_frame.loc[0, "Error"], "")
        self.assertEqual(google_frame.loc[0, "Verifier"], "")

    def test_failed_full_rerun_clears_stale_verifier_and_preserves_existing_results(self):
        self.system.options = RunOptions(rows=(2,), rerun="all")
        self.system.excel_handler.df.loc[0, "Verifier"] = "LLM"
        for stage in STAGE_FIELDS:
            getattr(self.system.analysis_manager.llm_agent, "analyze_text_" + stage).return_value = None
        self.assertFalse(self.system._process_single_row(0))
        self.assertEqual(self.system.excel_handler.df.loc[0, "Verifier"], "")
        self.assertEqual(self.system.excel_handler.df.loc[0, "University_EN"], GOLD["stage1"]["University_EN"])


class EvaluationTests(OfflineTests):
    def test_gold_predictions_and_accepted_alternatives_score_exactly(self):
        predictions = {case["id"]: copy.deepcopy(case["expected"]) for case in CASES}
        predictions[CASES[0]["id"]]["stage3"]["WX_Label1"] = "生态学"
        report = evaluate_predictions(CASES, predictions)
        self.assertEqual(report["exact_case_accuracy"], 1)
        self.assertEqual(report["stage2_f1"], 1)

    def test_missing_predictions_and_extra_classifications_reduce_scores(self):
        report = evaluate_predictions(CASES, {})
        self.assertEqual(report["exact_case_accuracy"], 0)
        self.assertEqual(report["schema_valid_rate"], 0)
        predictions = {case["id"]: copy.deepcopy(case["expected"]) for case in CASES}
        predictions[CASES[0]["id"]]["stage2"]["Unknown"] = "1"
        report = evaluate_predictions(CASES, predictions)
        self.assertLess(report["stage2_precision"], 1)
        self.assertLess(report["schema_valid_rate"], 1)

    @patch("src.core.llm_agent.LLMAgent")
    def test_live_evaluation_entry_can_be_exercised_without_models_or_table_writes(self, factory):
        agent = factory.return_value
        for stage in STAGE_FIELDS:
            getattr(agent, "analyze_text_" + stage).side_effect = [case["expected"][stage] for case in CASES]
        predictions, errors = run_live(CASES)
        self.assertFalse(errors)
        self.assertEqual(evaluate_predictions(CASES, predictions)["exact_case_accuracy"], 1)
        agent.save_conversation_log.assert_not_called()


if __name__ == "__main__":
    unittest.main()
