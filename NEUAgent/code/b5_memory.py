from __future__ import annotations

import argparse
import json
import re
import sys
from pathlib import Path

from common.io_utils import append_jsonl, read_json, read_text, read_yaml, write_json, write_text
from common.logging_utils import now_iso
from common.path_utils import resolve_cli_path, resolve_from_file


def _memory_paths(config_path: str | Path) -> dict[str, Path | int]:
    path = Path(config_path).resolve()
    config = read_yaml(path)
    if not isinstance(config, dict) or not isinstance(config.get("memory"), dict):
        raise ValueError("memory.yaml must define a memory object")
    memory = config["memory"]
    required = ["root_dir", "global_memory_dir", "conversation_memory_dir", "index_path", "max_memory_chars"]
    missing = [name for name in required if name not in memory]
    if missing:
        raise ValueError(f"memory.yaml missing: {', '.join(missing)}")
    root = resolve_from_file(memory["root_dir"], path)
    max_chars = memory["max_memory_chars"]
    if not isinstance(max_chars, int) or isinstance(max_chars, bool) or max_chars <= 0:
        raise ValueError("max_memory_chars must be a positive integer")
    return {
        "root": root,
        "global": root / memory["global_memory_dir"],
        "conversations": root / memory["conversation_memory_dir"],
        "index": root / memory["index_path"],
        "max_chars": max_chars,
    }


def _read_index(index_path: Path) -> dict:
    if not index_path.exists():
        return {}
    index = read_json(index_path)
    if not isinstance(index, dict):
        raise ValueError("memory_index.json must be an object")
    return index


def load_memory(
    config_path: str,
    selected_memory_ids: list[str],
    use_global_memory: bool,
    query: str | None = None,
    outdir: str | None = None,
) -> dict:
    if not isinstance(selected_memory_ids, list) or not all(isinstance(item, str) for item in selected_memory_ids):
        raise ValueError("selected_memory_ids must be a list of strings")
    paths = _memory_paths(config_path)
    index = _read_index(paths["index"])
    ordered_ids = []
    if use_global_memory:
        ordered_ids.extend(sorted(key for key, item in index.items() if item.get("memory_type") == "global"))
    ordered_ids.extend(selected_memory_ids)
    ordered_ids = list(dict.fromkeys(ordered_ids))

    docs = []
    errors = []
    remaining = int(paths["max_chars"])
    any_truncated = False
    for memory_id in ordered_ids:
        metadata = index.get(memory_id)
        if not isinstance(metadata, dict):
            errors.append({"memory_id": memory_id, "type": "MemoryNotFound", "message": "memory_id does not exist"})
            continue
        relative_path = metadata.get("path")
        if not isinstance(relative_path, str):
            errors.append({"memory_id": memory_id, "type": "InvalidMetadata", "message": "memory path is missing"})
            continue
        document_path = (paths["root"] / relative_path).resolve()
        try:
            document_path.relative_to(paths["root"].resolve())
        except ValueError:
            errors.append({"memory_id": memory_id, "type": "InvalidPath", "message": "memory path escapes root"})
            continue
        if not document_path.is_file():
            errors.append({"memory_id": memory_id, "type": "FileNotFoundError", "message": f"memory file not found: {relative_path}"})
            continue
        original = read_text(document_path)
        included = original[:remaining] if remaining > 0 else ""
        truncated = len(included) < len(original)
        any_truncated = any_truncated or truncated
        if included:
            docs.append(
                {
                    "memory_id": memory_id,
                    "memory_type": metadata.get("memory_type"),
                    "title": metadata.get("title", memory_id),
                    "path": relative_path,
                    "content": included,
                    "original_chars": len(original),
                    "included_chars": len(included),
                    "truncated": truncated,
                }
            )
            remaining -= len(included)
    if errors and docs:
        status = "partial"
    elif errors:
        status = "error"
    else:
        status = "success"
    result = {
        "status": status,
        "query": query,
        "selected_memory_docs": docs,
        "max_memory_chars": paths["max_chars"],
        "total_chars": sum(item["included_chars"] for item in docs),
        "truncated": any_truncated,
        "errors": errors,
    }
    if outdir:
        output_dir = Path(outdir)
        write_json(result, output_dir / "selected_memory.json")
        append_jsonl(
            {
                "timestamp": now_iso(),
                "operation": "load",
                "status": status,
                "selected_ids": [item["memory_id"] for item in docs],
                "errors": errors,
            },
            output_dir / "memory_log.jsonl",
        )
    return result


def _safe_conversation_id(conversation_id: str) -> str:
    if not isinstance(conversation_id, str) or not re.fullmatch(r"[A-Za-z0-9_.-]+", conversation_id):
        raise ValueError("conversation_id may only contain letters, numbers, dot, underscore, and hyphen")
    return conversation_id


def save_memory(
    config_path: str,
    conversation_id: str,
    save_type: str,
    messages_path: str,
    trace_path: str,
    answer_path: str,
    outdir: str | None = None,
) -> dict:
    conversation_id = _safe_conversation_id(conversation_id)
    if save_type not in {"conversation", "global"}:
        raise ValueError("save_type must be conversation or global")
    paths = _memory_paths(config_path)
    messages = read_json(messages_path)
    trace = read_json(trace_path)
    answer = read_text(answer_path).strip()
    if not isinstance(messages, list) or not isinstance(trace, dict):
        raise ValueError("messages must be an array and trace must be an object")
    now = now_iso()
    memory_id = f"mem_{save_type}_{conversation_id}"
    target_dir = paths["conversations"] if save_type == "conversation" else paths["global"]
    relative_dir = "conversations" if save_type == "conversation" else "global"
    target_path = Path(target_dir) / f"{conversation_id}.md"
    relative_path = f"{relative_dir}/{conversation_id}.md"
    title = f"{save_type.title()} {conversation_id}"
    summary = answer[:200]
    markdown = (
        f"# {title}\n\n"
        f"- memory_id: `{memory_id}`\n"
        f"- conversation_id: `{conversation_id}`\n"
        f"- created_or_updated_at: `{now}`\n\n"
        "## Final Answer\n\n"
        f"{answer}\n\n"
        "## Messages\n\n```json\n"
        f"{json.dumps(messages, ensure_ascii=False, indent=2)}\n```\n\n"
        "## Trace\n\n```json\n"
        f"{json.dumps(trace, ensure_ascii=False, indent=2)}\n```\n"
    )
    write_text(markdown, target_path)
    index = _read_index(paths["index"])
    existing = index.get(memory_id, {})
    created_at = existing.get("created_at", now)
    index[memory_id] = {
        "memory_id": memory_id,
        "memory_type": save_type,
        "title": title,
        "summary": summary,
        "path": relative_path,
        "conversation_id": conversation_id,
        "created_at": created_at,
        "updated_at": now,
    }
    write_json(index, paths["index"])
    result = {
        "status": "success",
        "memory_id": memory_id,
        "memory_type": save_type,
        "conversation_id": conversation_id,
        "title": title,
        "summary": summary,
        "path": relative_path,
        "index_path": Path(paths["index"]).name,
        "created_at": created_at,
        "updated_at": now,
        "source_paths": {
            "messages": str(messages_path),
            "trace": str(trace_path),
            "answer": str(answer_path),
        },
    }
    if outdir:
        output_dir = Path(outdir)
        write_json(result, output_dir / "saved_memory.json")
        append_jsonl(
            {"timestamp": now, "operation": "save", "status": "success", "memory_id": memory_id},
            output_dir / "memory_log.jsonl",
        )
    return result


# ==================== Memory Item CRUD (供 Settings 面板调用) ====================

def get_memory_content(config_path: str, memory_id: str) -> dict:
    """读取单个记忆的完整内容（供前端编辑用）。"""
    paths = _memory_paths(config_path)
    index = _read_index(paths["index"])
    meta = index.get(memory_id)
    if not isinstance(meta, dict):
        raise FileNotFoundError(f"memory_id not found: {memory_id}")
    relative_path = meta.get("path")
    if not isinstance(relative_path, str):
        raise FileNotFoundError(f"memory path missing: {memory_id}")
    document_path = (paths["root"] / relative_path).resolve()
    try:
        document_path.relative_to(paths["root"].resolve())
    except ValueError:
        raise ValueError(f"memory path escapes root: {memory_id}")
    content = read_text(document_path)
    return {
        "memory_id": memory_id,
        "title": meta.get("title", memory_id),
        "memory_type": meta.get("memory_type", "conversation"),
        "path": relative_path,
        "content": content,
        "created_at": meta.get("created_at", ""),
        "updated_at": meta.get("updated_at", ""),
    }


def update_memory_content(config_path: str, memory_id: str, title: str, content: str) -> dict:
    """覆写记忆 .md 文件 + 更新 index 元数据。"""
    paths = _memory_paths(config_path)
    index = _read_index(paths["index"])
    meta = index.get(memory_id)
    if not isinstance(meta, dict):
        raise FileNotFoundError(f"memory_id not found: {memory_id}")
    relative_path = meta.get("path")
    if not isinstance(relative_path, str):
        raise FileNotFoundError(f"memory path missing: {memory_id}")
    document_path = (paths["root"] / relative_path).resolve()
    try:
        document_path.relative_to(paths["root"].resolve())
    except ValueError:
        raise ValueError(f"memory path escapes root: {memory_id}")
    write_text(content, document_path)
    now = now_iso()
    existing = index[memory_id]
    existing["title"] = title if title else existing.get("title", memory_id)
    existing["updated_at"] = now
    write_json(index, paths["index"])
    return {"status": "success", "memory_id": memory_id, "title": existing["title"], "updated_at": now}


def delete_memory(config_path: str, memory_id: str) -> dict:
    """删 .md + 删 vectors + 删 index 项。"""
    import os
    paths = _memory_paths(config_path)
    index = _read_index(paths["index"])
    meta = index.pop(memory_id, None)
    if meta is None:
        raise FileNotFoundError(f"memory_id not found: {memory_id}")
    # 删文件
    relative_path = meta.get("path")
    if isinstance(relative_path, str):
        document_path = (paths["root"] / relative_path).resolve()
        try:
            document_path.relative_to(paths["root"].resolve())
            if document_path.is_file():
                os.remove(document_path)
        except (ValueError, OSError):
            pass
    # 删向量
    vectors_path = paths["root"] / "vectors.json"
    if vectors_path.is_file():
        try:
            vectors = _read_vectors(vectors_path)
            vectors.pop(memory_id, None)
            write_json(vectors, vectors_path)
        except Exception:
            pass
    # 写回 index
    write_json(index, paths["index"])
    return {"status": "success", "deleted": memory_id}


def _read_vectors(vectors_path):
    """读取 vectors.json（内部用）。"""
    if not vectors_path.is_file():
        return {}
    try:
        data = read_json(vectors_path)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


# ==================== Per-turn 记忆检索（带 token 预算） ====================

def retrieve_memories_for_turn(
    config_path: str,
    messages: list[dict],
    top_k: int = 5,
    budget_chars: int = 2000,
    keyword_weight: float = 0.4,
) -> list[dict]:
    """根据最近对话检索相关记忆（按 token 预算截断）。

    Args:
        config_path: memory.yaml 路径
        messages: 当前会话 messages 列表
        top_k: 最多检索条数
        budget_chars: 记忆内容总字符预算
        keyword_weight: 关键词检索权重 (1-weight 为向量权重)

    Returns:
        记忆文档列表，每项包含 memory_id, title, content 等
    """
    from b5_memory_advanced import search_memory_by_keywords, search_memory_by_vector

    # ★ 构造 query：取最近 2 轮非 system 消息，截断到 500 字符
    recent_parts = []
    for m in reversed(messages):
        if m.get("role") == "system":
            continue
        content = (m.get("content") or "").strip()
        if content:
            recent_parts.append(content)
        if len(recent_parts) >= 4:  # 2轮 ≈ 4条消息
            break
    recent_parts.reverse()
    query = " ".join(recent_parts)[-500:]

    if not query.strip():
        return []

    try:
        kw = search_memory_by_keywords(config_path, query, top_k=top_k * 2)
        vec = search_memory_by_vector(config_path, query, top_k=top_k * 2)
    except Exception:
        return []

    kw_scores = {r["memory_id"]: r.get("relevance_score", 0.0) for r in kw.get("results", [])}
    vec_scores = {r["memory_id"]: r.get("similarity_score", 0.0) for r in vec.get("results", [])}
    all_ids = set(kw_scores) | set(vec_scores)

    ranked = sorted(
        all_ids,
        key=lambda mid: keyword_weight * kw_scores.get(mid, 0.0) + (1 - keyword_weight) * vec_scores.get(mid, 0.0),
        reverse=True,
    )[:top_k]

    if not ranked:
        return []

    # ★ 读取完整内容并按预算截断
    paths = _memory_paths(config_path)
    index = _read_index(paths["index"])
    result = []
    remaining = int(budget_chars)
    for memory_id in ranked:
        meta = index.get(memory_id)
        if not isinstance(meta, dict):
            continue
        relative_path = meta.get("path")
        if not isinstance(relative_path, str):
            continue
        try:
            document_path = (paths["root"] / relative_path).resolve()
            document_path.relative_to(paths["root"].resolve())
            original = read_text(document_path)
        except Exception:
            continue
        included = original[:remaining] if remaining > 0 else ""
        if not included.strip():
            continue
        result.append({
            "memory_id": memory_id,
            "memory_type": meta.get("memory_type", "conversation"),
            "title": meta.get("title", memory_id),
            "content": included,
        })
        remaining -= len(included)
        if remaining <= 0:
            break

    return result


def build_memory_block(docs: list[dict]) -> str:
    """将记忆列表拼接为 [MEMORY] 段落文本。"""
    if not docs:
        return ""
    return "\n\n".join(
        f'<memory id="{d["memory_id"]}" type="{d["memory_type"]}">\n{d["content"].strip()}\n</memory>'
        for d in docs
    )


def parse_bool(value: str) -> bool:
    lowered = value.lower()
    if lowered in {"true", "1", "yes"}:
        return True
    if lowered in {"false", "0", "no"}:
        return False
    raise argparse.ArgumentTypeError("expected true or false")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Select or save local memory documents.")
    parser.add_argument("--config", required=True)
    parser.add_argument("--select_memory_ids", nargs="*")
    parser.add_argument("--use_global_memory", type=parse_bool)
    parser.add_argument("--query")
    parser.add_argument("--save_type", choices=["conversation", "global"])
    parser.add_argument("--save_input_path")
    parser.add_argument("--outdir", required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config_path = resolve_cli_path(args.config)
        outdir = resolve_cli_path(args.outdir)
        if args.save_type or args.save_input_path:
            if not args.save_type or not args.save_input_path:
                raise ValueError("--save_type and --save_input_path must be provided together")
            input_path = resolve_cli_path(args.save_input_path)
            payload = read_json(input_path)
            if payload.get("save_type") != args.save_type:
                raise ValueError("CLI save_type must match memory_save_input.json")
            base = input_path.parent
            result = save_memory(
                str(config_path),
                payload["conversation_id"],
                args.save_type,
                str((base / payload["messages_path"]).resolve()),
                str((base / payload["trace_path"]).resolve()),
                str((base / payload["answer_path"]).resolve()),
                str(outdir),
            )
            print(outdir / "saved_memory.json")
        else:
            if args.select_memory_ids is None and args.use_global_memory is None:
                raise ValueError("select mode requires --select_memory_ids or --use_global_memory")
            result = load_memory(
                str(config_path),
                args.select_memory_ids or [],
                bool(args.use_global_memory),
                args.query,
                str(outdir),
            )
            print(outdir / "selected_memory.json")
        return 0
    except Exception as exc:
        print(f"fatal: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
