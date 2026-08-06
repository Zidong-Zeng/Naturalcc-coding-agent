from __future__ import annotations

import argparse
import importlib
import inspect
import sys
from pathlib import Path
from time import perf_counter

from common.error_codes import ErrorCode, classify_error, make_error_result
from common.io_utils import append_jsonl, read_json, write_json
from common.logging_utils import now_iso
from common.path_utils import DEFAULT_DATA_ROOT, bootstrap_project_root, resolve_cli_path
from common.schemas import make_skill_result


bootstrap_project_root()


SKILL_MODULES = {
    "calculator": "skills.calculator",
    "file_reader": "skills.file_reader",
    "local_file_search": "skills.local_file_search",
    "table_analyzer": "skills.table_analyzer",
    "format_converter": "skills.format_converter",
    "code_executor": "skills.code_executor",
    "read_and_convert": "skills.read_and_convert",
}


def run_skill(skill_name: str, input_data: dict, data_root: str | None = None, output_dir: str | None = None) -> dict:
    if skill_name not in SKILL_MODULES:
        error_info = make_error_result(ErrorCode.SKILL_NOT_FOUND, f"unknown skill: {skill_name}", skill_name)
        return make_skill_result(skill_name, "error", input_data, None, error_info, 0)
    if not isinstance(input_data, dict):
        error_info = make_error_result(ErrorCode.INVALID_INPUT, "skill input must be a JSON object", skill_name)
        return make_skill_result(skill_name, "error", input_data, None, error_info, 0)
    try:
        module = importlib.import_module(SKILL_MODULES[skill_name])
        function = getattr(module, skill_name)
    except Exception as exc:
        error_code, error_msg = classify_error(exc)
        error_info = make_error_result(error_code, f"failed to load skill: {error_msg}", skill_name)
        return make_skill_result(skill_name, "error", input_data, None, error_info, 0)
    kwargs = dict(input_data)
    signature = inspect.signature(function)
    if "data_root" in signature.parameters:
        kwargs["data_root"] = data_root or str(DEFAULT_DATA_ROOT)
    if "output_dir" in signature.parameters:
        kwargs["output_dir"] = output_dir
    start = perf_counter()
    try:
        output = function(**kwargs)
        status = "success"
        error = None
        error_code = ErrorCode.SUCCESS
    except Exception as exc:
        output = None
        status = "error"
        error_code, error_msg = classify_error(exc)
        error = {
            "type": type(exc).__name__,
            "message": error_msg,
            "error_code": error_code,
            "error_category": make_error_result(error_code, "", skill_name)["error_category"],
            "recoverable": make_error_result(error_code, "", skill_name)["recoverable"],
        }
    latency_ms = round((perf_counter() - start) * 1000, 3)
    return make_skill_result(skill_name, status, input_data, output, error, latency_ms)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run one local Agent skill.")
    parser.add_argument("--skill", required=True, choices=sorted(SKILL_MODULES))
    parser.add_argument("--input", required=True)
    parser.add_argument("--outdir", required=True)
    parser.add_argument("--data_root", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        input_path = resolve_cli_path(args.input)
        outdir = resolve_cli_path(args.outdir)
        input_data = read_json(input_path)
        data_root = str(resolve_cli_path(args.data_root)) if args.data_root else None
        outdir.mkdir(parents=True, exist_ok=True)
        result = run_skill(args.skill, input_data, data_root, str(outdir))
        result_path = outdir / f"{args.skill}_result.json"
        write_json(result, result_path)
        append_jsonl(
            {
                "timestamp": now_iso(),
                "skill_name": args.skill,
                "status": result["status"],
                "result_path": str(result_path),
                "latency_ms": result["latency_ms"],
                "error_code": result.get("error", {}).get("error_code") if result.get("error") else ErrorCode.SUCCESS,
            },
            outdir / "skill_run_log.jsonl",
        )
        print(result_path)
        return 0
    except Exception as exc:
        print(f"fatal: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
