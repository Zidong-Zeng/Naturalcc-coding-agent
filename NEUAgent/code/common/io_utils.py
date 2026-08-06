from __future__ import annotations

import json
import os
from pathlib import Path
from typing import Any, Iterable


def ensure_dir(path: str | Path) -> Path:
    directory = Path(path)
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def read_text(path: str | Path) -> str:
    return Path(path).read_text(encoding="utf-8")


def _atomic_write_text(path: str | Path, text: str) -> Path:
    target = Path(path)
    ensure_dir(target.parent)
    temporary = target.with_name(f".{target.name}.tmp")
    temporary.write_text(text, encoding="utf-8")
    os.replace(temporary, target)
    return target


def write_text(text: str, path: str | Path) -> Path:
    return _atomic_write_text(path, text)


def read_json(path: str | Path) -> Any:
    with Path(path).open("r", encoding="utf-8") as handle:
        return json.load(handle)


def write_json(obj: Any, path: str | Path) -> Path:
    text = json.dumps(obj, ensure_ascii=False, indent=2) + "\n"
    return _atomic_write_text(path, text)


def read_yaml(path: str | Path) -> Any:
    try:
        import yaml
    except ImportError as exc:
        raise RuntimeError("PyYAML is required; install requirements.txt") from exc
    with Path(path).open("r", encoding="utf-8") as handle:
        return yaml.safe_load(handle)


def write_jsonl(records: Iterable[dict[str, Any]], path: str | Path) -> Path:
    text = "".join(json.dumps(record, ensure_ascii=False) + "\n" for record in records)
    return _atomic_write_text(path, text)


def append_jsonl(record: dict[str, Any], path: str | Path) -> Path:
    target = Path(path)
    ensure_dir(target.parent)
    with target.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return target
