from __future__ import annotations

import json
import re
from pathlib import Path

from skills import resolve_data_path


def _text_to_markdown(text: str) -> str:
    lines = [line.strip() for line in text.splitlines() if line.strip()]
    if not lines:
        return ""
    if all(":" in line and len(line.split(":", 1)) == 2 for line in lines):
        result = []
        for line in lines:
            key, value = (part.strip() for part in line.split(":", 1))
            result.append(f"**{key}**: {value}")
        return "\n\n".join(result)
    return "\n".join(f"- {line}" for line in lines)


def _text_to_json(text: str) -> str:
    try:
        parsed = json.loads(text)
        return json.dumps(parsed, ensure_ascii=False, indent=2)
    except json.JSONDecodeError:
        result = {}
        for line in text.splitlines():
            line = line.strip()
            if not line:
                continue
            # 优先用半角冒号，其次全角冒号，取第一个出现的位置
            half = line.find(":")
            full = line.find("：")
            if half >= 0 and (full < 0 or half <= full):
                sep = half
            elif full >= 0:
                sep = full
            else:
                continue
            key = line[:sep].strip()
            value = line[sep + 1:].strip()
            if not key:
                continue
            try:
                if "." in value:
                    result[key] = float(value)
                else:
                    result[key] = int(value)
            except ValueError:
                result[key] = value
        # 没有冒号的文本 → 退化为编号数组
        if not result:
            lines = [l.strip() for l in text.splitlines() if l.strip()]
            return json.dumps({"lines": lines}, ensure_ascii=False, indent=2)
        return json.dumps(result, ensure_ascii=False, indent=2)


def _write_output(text: str, output_dir: str | None, filename: str, suffix: str) -> Path:
    # ★ 始终使用默认目录，忽略 B3 传入的 step_dir，保证输出集中到 outputs/read_and_convert/
    directory = Path(__file__).resolve().parents[1] / "outputs" / "read_and_convert"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{filename}{suffix}"
    index = 1
    while path.exists():
        path = directory / f"{filename}({index}){suffix}"
        index += 1
    path.write_text(text, encoding="utf-8")
    return path


def read_and_convert(
    path: str,
    target_format: str,
    max_chars: int | None = None,
    output_filename: str | None = None,
    *,
    data_root: str | None = None,
    output_dir: str | None = None,
) -> dict:
    if max_chars is None:
        max_chars = 5000
    if not isinstance(path, str) or not path.strip():
        raise ValueError("path must be a non-empty string")
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
        raise ValueError("max_chars must be a positive integer")
    source, root = resolve_data_path(path, data_root)
    if source.suffix.lower() not in {".txt", ".md"}:
        raise ValueError("read_and_convert only supports .txt and .md files")
    if not source.is_file():
        raise FileNotFoundError(f"file not found: {path}")
    target = target_format.strip().lower() if isinstance(target_format, str) else ""
    if target not in {"markdown", "json"}:
        raise ValueError("target_format must be markdown or json")
    original = source.read_text(encoding="utf-8")
    content = original[:max_chars]
    if target == "markdown":
        formatted = _text_to_markdown(content)
        suffix = ".md"
    else:
        formatted = _text_to_json(content)
        suffix = ".json"
    filename = output_filename.strip() if isinstance(output_filename, str) and output_filename.strip() else "converted"
    filename = Path(filename).stem
    output_path = _write_output(formatted, output_dir, filename, suffix)
    return {
        "source_path": source.relative_to(root).as_posix(),
        "target_format": target,
        "formatted_text": formatted,
        "output_file": str(output_path),
        "original_chars": len(original),
        "processed_chars": len(content),
        "truncated": len(original) > len(content),
    }
