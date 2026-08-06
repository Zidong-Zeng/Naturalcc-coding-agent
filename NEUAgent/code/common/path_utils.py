from __future__ import annotations

import sys
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DATA_ROOT = PROJECT_ROOT / "data"


def bootstrap_project_root() -> Path:
    root_text = str(PROJECT_ROOT)
    if root_text not in sys.path:
        sys.path.insert(0, root_text)
    return PROJECT_ROOT


def resolve_path(path: str | Path, base_dir: str | Path) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.is_absolute():
        candidate = Path(base_dir) / candidate
    return candidate.resolve()


def resolve_cli_path(path: str | Path) -> Path:
    return resolve_path(path, Path.cwd())


def resolve_from_file(path: str | Path, containing_file: str | Path) -> Path:
    return resolve_path(path, Path(containing_file).resolve().parent)


def require_within(path: str | Path, root: str | Path) -> Path:
    resolved_path = Path(path).resolve()
    resolved_root = Path(root).resolve()
    try:
        resolved_path.relative_to(resolved_root)
    except ValueError as exc:
        raise ValueError(f"path escapes data root: {path}") from exc
    return resolved_path
