from __future__ import annotations

import json
from pathlib import Path


DEFAULT_OUTPUT_DIR = Path(__file__).resolve().parents[1] / "outputs" / "format_converter_files"
DEFAULT_FILENAMES = {"markdown": "converted.md", "json": "converted.json"}
SUFFIXES = {"markdown": ".md", "json": ".json"}


def _parse_key_value_lines(text: str) -> dict[str, str]:
    result: dict[str, str] = {}
    for line in (line.strip() for line in text.splitlines()):
        if not line:
            continue
        if ":" not in line:
            raise ValueError(f"expected 'key: value' line: {line}")
        key, value = (part.strip() for part in line.split(":", 1))
        if not key or key in result:
            raise ValueError(f"invalid or duplicate key: {key}")
        result[key] = value
    if not result:
        raise ValueError("text contains no convertible content")
    return result


def _safe_output_path(output_dir: str | None, output_filename: str | None, target_format: str) -> Path:
    # ★ 始终使用默认目录，忽略 B3 传入的 step_dir，保证输出集中到 format_converter_files/
    directory = DEFAULT_OUTPUT_DIR.resolve()
    raw_name = output_filename.strip() if isinstance(output_filename, str) and output_filename.strip() else DEFAULT_FILENAMES[target_format]
    name = Path(raw_name).name
    suffix = SUFFIXES[target_format]
    path = Path(name)
    stem = path.stem or Path(DEFAULT_FILENAMES[target_format]).stem
    candidate = directory / f"{stem}{suffix}"
    index = 1
    while candidate.exists():
        candidate = directory / f"{stem}({index}){suffix}"
        index += 1
    return candidate


def _write_output_file(text: str, output_dir: str | None, output_filename: str | None, target_format: str) -> Path:
    target = _safe_output_path(output_dir, output_filename, target_format)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return target


def format_converter(
    text: str,
    target_format: str,
    output_filename: str | None = None,
    output_dir: str | None = None,
) -> dict:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("text must be a non-empty string")
    target = target_format.strip().lower() if isinstance(target_format, str) else ""
    if target == "markdown":
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        formatted_text = "\n".join(f"- {line}" for line in lines)
    elif target == "json":
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = _parse_key_value_lines(text)
        formatted_text = json.dumps(parsed, ensure_ascii=False, indent=2)
    else:
        raise ValueError("target_format must be markdown or json")
    generated_path = _write_output_file(formatted_text, output_dir, output_filename, target)
    return {"formatted_text": formatted_text, "generated_file_path": str(generated_path)}
