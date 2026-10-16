"""Explicit row selection and rerun policy; table row 2 is dataframe index 0."""
import argparse
from dataclasses import dataclass

from .result_schema import STAGE_FIELDS


@dataclass(frozen=True)
class RunOptions:
    rows: tuple[int, ...] | None = None
    rerun: str | None = None
    overwrite: bool = False
    refresh_content: bool = False
    limit: int | None = None


def parse_rows(value):
    rows = set()
    try:
        for part in value.split(","):
            ends = [int(item.strip()) for item in part.strip().split("-")]
            if len(ends) == 1:
                ends *= 2
            if len(ends) != 2 or ends[0] < 2 or ends[1] < ends[0] or ends[1] > 1_000_000:
                raise ValueError
            rows.update(range(ends[0], ends[1] + 1))
    except ValueError as exc:
        raise argparse.ArgumentTypeError("行号应为表格显示行号，例如 2,5-8；标题行 1 不可选") from exc
    return tuple(sorted(rows))


def parse_run_options(argv=None):
    parser = argparse.ArgumentParser(description="处理学术机会；无参数时仅处理 Verifier/Error 均空的行")
    parser.add_argument("mode", nargs="?", choices=["test"], help="test 保留原单行处理方式，会写回表格")
    parser.add_argument("--rows", type=parse_rows, help="表格显示行号，如 2,5-8（不是日志中的零基索引）")
    parser.add_argument("--rerun", choices=["failed", "all"], help="failed 重跑失败行并复用有效成功阶段；all 重跑指定行全部阶段")
    parser.add_argument("--overwrite", action="store_true", help="仅重跑时生效：覆盖本次成功重跑阶段的字段")
    parser.add_argument("--refresh-content", action="store_true", help="跳过正文缓存，重新抓取")
    parser.add_argument("--limit", type=int, help="最多处理多少行")
    args = parser.parse_args(argv)
    if args.rerun == "all" and not args.rows:
        parser.error("--rerun all 必须同时指定 --rows，防止意外重跑整表")
    if args.overwrite and not args.rerun:
        parser.error("--overwrite 必须与 --rerun 一起使用")
    if args.limit is not None and args.limit < 1:
        parser.error("--limit 必须大于 0")
    if args.mode == "test" and args.limit not in (None, 1):
        parser.error("test 模式最多处理一行")
    return RunOptions(args.rows, args.rerun, args.overwrite, args.refresh_content,
                      1 if args.mode == "test" else args.limit)


def select_rows(rows_by_index, options):
    requested = set(options.rows) if options.rows else None
    if requested:
        missing = requested - {index + 2 for index in rows_by_index}
        if missing:
            raise ValueError(f"表格行号不存在: {sorted(missing)}")
    selected = []
    for index, row in rows_by_index.items():
        if requested and index + 2 not in requested:
            continue
        verifier, error = str(row.get("Verifier", "") or "").strip(), str(row.get("Error", "") or "").strip()
        if options.rerun == "all":
            eligible = True
        elif options.rerun == "failed":
            eligible = not verifier and bool(error) and error.upper() != "N"
        else:
            eligible = not verifier and not error
        if eligible:
            selected.append(index)
    return selected[:options.limit] if options.limit else selected


def rerun_updates(row, results, successful_stages, executed_stages, overwrite=False):
    """Preserve existing values by default, and never clear a failed stage."""
    proposed = {}
    for stage in successful_stages:
        for field in STAGE_FIELDS[stage]:
            proposed[field] = results.get(field, "")
    updates, conflicts = {}, []
    executed_fields = {field for stage in executed_stages for field in STAGE_FIELDS[stage]}
    for field, value in proposed.items():
        existing = str(row.get(field, "") or "").strip()
        if not existing or (overwrite and field in executed_fields):
            updates[field] = value
        elif existing != str(value).strip():
            conflicts.append(field)
    return updates, conflicts
