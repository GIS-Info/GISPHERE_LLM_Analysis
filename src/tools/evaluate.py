"""Score saved predictions offline; model calls require the explicit --live flag."""
import argparse
import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

from ..core.result_schema import STAGE_FIELDS, validation_errors

DEFAULT_CASES = Path(__file__).resolve().parents[2] / "tests" / "fixtures" / "evaluation_cases.json"


def load_cases(path):
    data = json.loads(Path(path).read_text(encoding="utf-8-sig"))
    cases = data.get("cases", [])
    if not isinstance(cases, list) or not cases:
        raise ValueError("评测集必须包含非空 cases 数组")
    identifiers = set()
    for case in cases:
        identifier = case.get("id")
        if not isinstance(identifier, str) or not identifier or identifier in identifiers:
            raise ValueError("案例 id 必须是唯一非空字符串")
        identifiers.add(identifier)
        if not isinstance(case.get("text"), str) or not case["text"].strip():
            raise ValueError(f"{identifier}: 缺少正文快照")
        for stage in STAGE_FIELDS:
            errors = validation_errors(case.get("expected", {}).get(stage), stage)
            if errors:
                raise ValueError(f"{identifier}/{stage}: {'; '.join(errors)}")
        for stage, fields in case.get("accepted", {}).items():
            if stage not in STAGE_FIELDS or stage == "stage2" or not isinstance(fields, dict):
                raise ValueError(f"{identifier}: accepted 格式错误")
            for field, alternatives in fields.items():
                if field not in STAGE_FIELDS[stage] or not isinstance(alternatives, list) or not alternatives:
                    raise ValueError(f"{identifier}: accepted 字段或候选值错误")
                for value in alternatives:
                    candidate = dict(case["expected"][stage], **{field: value})
                    if validation_errors(candidate, stage):
                        raise ValueError(f"{identifier}: accepted 候选不符合字段规则")
    return cases


def evaluate_predictions(cases, predictions):
    if not isinstance(predictions, dict):
        raise ValueError("predictions 必须是以案例 id 为键的对象")
    exact_cases = exact_stages = valid_stages = field_correct = field_total = 0
    true_positive = false_positive = false_negative = 0
    details = []
    for case in cases:
        predicted_case = predictions.get(case["id"], {})
        if not isinstance(predicted_case, dict):
            predicted_case = {}
        case_exact = True
        for stage, fields in STAGE_FIELDS.items():
            predicted = predicted_case.get(stage, {})
            errors = validation_errors(predicted, stage)
            if not isinstance(predicted, dict):
                predicted = {}
            valid_stages += not errors
            expected = case["expected"][stage]
            mismatches = []
            if stage == "stage2":
                actual_set = {key for key, value in predicted.items() if value == "1"}
                expected_set = set(expected)
                true_positive += len(actual_set & expected_set)
                false_positive += len(actual_set - expected_set)
                false_negative += len(expected_set - actual_set)
                mismatches = sorted(actual_set ^ expected_set)
            else:
                for field in fields:
                    choices = case.get("accepted", {}).get(stage, {}).get(field, [expected[field]])
                    correct = field in predicted and predicted[field] in choices
                    field_correct += correct
                    field_total += 1
                    if not correct:
                        mismatches.append(field)
            exact = not errors and not mismatches
            exact_stages += exact
            case_exact = case_exact and exact
            details.append({"case_id": case["id"], "stage": stage, "schema_errors": errors,
                            "mismatched_fields": mismatches, "exact": exact})
        exact_cases += case_exact
    count = len(cases)
    precision = true_positive / (true_positive + false_positive) if true_positive + false_positive else 0
    recall = true_positive / (true_positive + false_negative) if true_positive + false_negative else 0
    return {
        "case_count": count, "schema_valid_rate": valid_stages / (count * 3),
        "exact_case_accuracy": exact_cases / count, "exact_stage_accuracy": exact_stages / (count * 3),
        "stage1_and_stage3_field_accuracy": field_correct / field_total,
        "stage2_precision": precision, "stage2_recall": recall,
        "stage2_f1": 2 * precision * recall / (precision + recall) if precision + recall else 0,
        "unknown_case_ids": sorted(set(predictions) - {case["id"] for case in cases}), "details": details,
    }


def run_live(cases):
    from ..core.llm_agent import LLMAgent
    from ..core.observability import observation, flush_traces

    agent = LLMAgent()
    predictions, errors = {}, {}
    try:
        for case in cases:
            predictions[case["id"]] = {}
            with observation("evaluation-case", metadata={"case_id": case["id"], "synthetic": True}):
                for stage in STAGE_FIELDS:
                    with observation("evaluation-" + stage):
                        try:
                            predictions[case["id"]][stage] = getattr(agent, "analyze_text_" + stage)(case["text"])
                        except Exception as exc:
                            errors[case["id"] + "/" + stage] = type(exc).__name__
                # Evaluation does not write per-row conversation logs or business tables.
                agent.conversation_history.clear()
    finally:
        flush_traces()
    return predictions, errors


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--check-fixtures", action="store_true", help="仅校验基准格式，零模型调用")
    mode.add_argument("--predictions", type=Path, help="读取历史预测 JSON 并离线评分")
    mode.add_argument("--live", action="store_true", help="真实调用模型评测，可能产生费用；不写业务表格")
    parser.add_argument("--cases", type=Path, default=DEFAULT_CASES)
    parser.add_argument("--output", type=Path, help="保存预测和报告的 JSON 路径")
    args = parser.parse_args(argv)
    cases = load_cases(args.cases)
    fingerprint = hashlib.sha256(args.cases.read_bytes()).hexdigest()
    if args.check_fixtures:
        print(f"基准校验通过: {len(cases)} 个合成案例、{len(cases) * 3} 个阶段；未调用模型")
        return 0
    errors = {}
    if args.live:
        predictions, errors = run_live(cases)
    else:
        saved = json.loads(args.predictions.read_text(encoding="utf-8-sig"))
        if not isinstance(saved, dict):
            raise ValueError("预测文件必须是 JSON 对象")
        if saved.get("dataset_sha256") and saved["dataset_sha256"] != fingerprint:
            raise ValueError("预测文件与当前评测集版本不匹配")
        predictions = saved.get("predictions", saved)
    report = evaluate_predictions(cases, predictions)
    print(json.dumps({key: value for key, value in report.items() if key != "details"}, ensure_ascii=False, indent=2))
    output = args.output
    if args.live and output is None:
        output = Path("cache/evaluations") / (datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ") + ".json")
    if output is not None:
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps({"dataset_sha256": fingerprint, "predictions": predictions,
                                     "report": report, "errors": errors}, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"已保存: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
