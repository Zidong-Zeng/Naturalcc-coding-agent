from __future__ import annotations

import math
import re
from collections import Counter

from skills import resolve_data_path


def _tokenize(text: str) -> list[str]:
    tokens = re.findall(r"[\w\u4e00-\u9fff]+", text.lower())
    return [t for t in tokens if len(t) > 1]


def _snippet(text: str, terms: list[str], radius: int = 80) -> str:
    lowered = text.casefold()
    positions = []
    for term in terms:
        start = 0
        while True:
            idx = lowered.find(term.casefold(), start)
            if idx < 0:
                break
            positions.append(idx)
            start = idx + 1
    if not positions:
        return text[:radius * 2].replace("\n", " ").strip() + ("..." if len(text) > radius * 2 else "")
    best_pos = min(positions, key=lambda p: abs(p - len(text) // 4))
    start = max(0, best_pos - radius)
    end = min(len(text), start + radius * 2)
    prefix = "..." if start else ""
    suffix = "..." if end < len(text) else ""
    return prefix + text[start:end].replace("\n", " ").strip() + suffix


def _tfidf_score(query_tokens: list[str], doc_tokens: list[str], idf: dict[str, float]) -> float:
    doc_counts = Counter(doc_tokens)
    doc_len = len(doc_tokens) or 1
    score = 0.0
    for qt in query_tokens:
        if qt in doc_counts:
            tf = doc_counts[qt] / doc_len
            score += tf * idf.get(qt, 1.0)
    return score


def _build_idf(documents: list[list[str]]) -> dict[str, float]:
    n_docs = len(documents)
    df: Counter[str] = Counter()
    for tokens in documents:
        df.update(set(tokens))
    return {term: math.log((n_docs + 1) / (freq + 1)) + 1 for term, freq in df.items()}


def local_file_search(
    query: str,
    root_dir: str = "docs",
    file_types: list[str] | None = None,
    top_k: int | None = None,
    *,
    data_root: str | None = None,
) -> dict:
    if top_k is None:
        top_k = 5
    if not isinstance(query, str) or not query.strip():
        raise ValueError("query must be a non-empty string")
    if not isinstance(top_k, int) or isinstance(top_k, bool) or top_k <= 0:
        raise ValueError("top_k must be a positive integer")
    search_root, data_root_path = resolve_data_path(root_dir, data_root)
    if not search_root.is_dir():
        raise FileNotFoundError(f"search directory not found: {root_dir}")
    extensions = file_types or ["txt", "md"]
    normalized_extensions = {f".{item.lower().lstrip('.')}" for item in extensions}
    if not normalized_extensions.issubset({".txt", ".md", ".csv", ".tsv"}):
        raise ValueError("local_file_search only supports txt, md, csv, tsv")
    terms = [term for term in re.split(r"\s+", query.strip()) if term]
    query_tokens = _tokenize(query)
    files: list[tuple] = []
    for path in sorted(search_root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in normalized_extensions:
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        files.append((path, text))
    if not files:
        return {"results": [], "total_files": 0}
    doc_tokens_list = [_tokenize(text) for _, text in files]
    idf = _build_idf(doc_tokens_list)
    results = []
    for (path, text), doc_tokens in zip(files, doc_tokens_list):
        lowered = text.casefold()
        term_hits = sum(lowered.count(term.casefold()) for term in terms)
        if term_hits == 0 and not any(qt in set(doc_tokens) for qt in query_tokens):
            continue
        tfidf = _tfidf_score(query_tokens, doc_tokens, idf)
        keyword_score = math.log1p(term_hits)
        combined = 0.6 * tfidf + 0.4 * keyword_score
        if combined > 0:
            results.append(
                {
                    "path": path.relative_to(data_root_path).as_posix(),
                    "score": round(combined, 4),
                    "snippet": _snippet(text, terms),
                }
            )
    results.sort(key=lambda item: (-item["score"], item["path"]))
    return {"results": results[:top_k], "total_files": len(files), "query": query}
