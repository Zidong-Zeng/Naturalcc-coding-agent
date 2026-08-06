from __future__ import annotations

import csv
import statistics

from skills import resolve_data_path


def table_analyzer(
    path: str,
    max_rows_preview: int | None = None,
    describe: bool | None = None,
    *,
    data_root: str | None = None,
) -> dict:
    if max_rows_preview is None:
        max_rows_preview = 5
    if describe is None:
        describe = True
    if not isinstance(max_rows_preview, int) or isinstance(max_rows_preview, bool) or max_rows_preview < 0:
        raise ValueError("max_rows_preview must be a non-negative integer")
    source, root = resolve_data_path(path, data_root)
    if source.suffix.lower() not in {".csv", ".tsv"}:
        raise ValueError("table_analyzer only supports .csv and .tsv files")
    if not source.is_file():
        raise FileNotFoundError(f"table file not found: {path}")
    delimiter = "\t" if source.suffix.lower() == ".tsv" else ","
    with source.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle, delimiter=delimiter)
        if not reader.fieldnames:
            raise ValueError("table must contain a header row")
        rows = list(reader)
        columns = list(reader.fieldnames)
    stats: dict[str, dict] = {}
    if describe:
        for column in columns:
            raw_values = [row.get(column, "").strip() for row in rows]
            if not raw_values or any(value == "" for value in raw_values):
                continue
            try:
                values = [float(value) for value in raw_values]
            except ValueError:
                continue
            stats[column] = {
                "count": len(values),
                "min": min(values),
                "max": max(values),
                "mean": statistics.fmean(values),
            }
    return {
        "path": source.relative_to(root).as_posix(),
        "num_rows": len(rows),
        "num_columns": len(columns),
        "columns": columns,
        "preview": rows[:max_rows_preview],
        "describe": stats,
    }
