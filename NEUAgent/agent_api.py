"""FastAPI entrypoint for the B1 Agent frontend.

Serves
  * REST API: sessions, messages, polling, template-switch, compress, batch
  * Static chat UI: agent_chat.html at `/`

The API is a thin async wrapper around the synchronous agents in
`code/b1_agent_runtime.py`. CPU-bound work (Qwen inference) is dispatched
through `asyncio.to_thread` so the event loop stays responsive.
"""
from __future__ import annotations

import asyncio
import json
import re
import sys
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

# ---------------------------------------------------------------------------
# Project root + sys.path bootstrap
# ---------------------------------------------------------------------------
PROJECT_ROOT = Path(__file__).resolve().parent
CODE_DIR = PROJECT_ROOT / "code"
if str(CODE_DIR) not in sys.path:
    sys.path.insert(0, str(CODE_DIR))

from common.io_utils import read_json, read_text, write_json, write_text
from common.path_utils import resolve_cli_path
from common.logging_utils import now_iso
from common.path_utils import resolve_cli_path

# ── Baseline internals (code/b1_agent_runtime.py) ─────────────────────────
# ★ 全部加 _ 前缀 → 避免和本模块同名 route handler (list_sessions / delete_session) 冲突
from b1_agent_runtime import (run_single_turn, run_batch_tasks,
                               save_session as _b1_save_session,
                               list_sessions as _b1_list_sessions,
                               load_session as _b1_load_session,
                               delete_session as _b1_delete_session)

# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------
app = FastAPI(title="B1 Agent Web API", version="1.0.0")


def _persist_session(sid: str) -> None:
    """把 session 写到磁盘（供刷新/恢复）。

    委托给 B1 统一的 save_session()：
      - 落盘位置  WEB_SESSION_ROOT/{sid}/session.json
      - 过滤内部字段（_ 开头，如 _plan_state 含 threading.Event）
    """
    session = sessions.get(sid)
    if session is None:
        return
    _b1_save_session(session, root=WEB_SESSION_ROOT)

app_dir = Path(__file__).resolve().parent
app.mount("/static", StaticFiles(directory=str(app_dir)), name="static")

# ---------------------------------------------------------------------------
# In-memory registries (sufficient for single-user demo; restart loses state)
# ---------------------------------------------------------------------------
sessions: dict[str, dict[str, Any]] = {}
tasks: dict[str, dict[str, Any]] = {}

# ★ 事件存储基础设施
# 每个 task_id 对应一个 list[dict]，run_single_turn 的 events_list 参数把事件 append 进来
# 前端通过 polling GET /api/sessions/{sid}/poll?task_id=X&since=Y 读取增量事件
task_events: dict[str, list[dict]] = {}


def record_event(task_id: str, ev: dict) -> None:
    """向指定 task 的事件列表追加一条事件（不存在则自动创建）。"""
    if task_id not in task_events:
        task_events[task_id] = []
    task_events[task_id].append(ev)

import threading as _threading  # 延迟导入 threading

# ★ 统一 session 存储根目录（CLI & Web 共享 outputs/sessions/）
WEB_SESSION_ROOT = PROJECT_ROOT / "outputs" / "sessions"


def _new_task(session_id: str, kind: str) -> dict[str, Any]:
    task_id = uuid.uuid4().hex[:12]
    rec = {
        "task_id": task_id,
        "session_id": session_id,
        "kind": kind,
        "status": "pending",
        "result": None,
        "error": None,
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    tasks[task_id] = rec
    return rec


# ---------------------------------------------------------------------------
# Pydantic request / response models
# ---------------------------------------------------------------------------
class CreateSessionReq(BaseModel):
    user_input: str = ""
    toolset: str = "basic_tools"
    max_turns: int = 3
    template_key: str = "tool_master"
    repl_mode: str = "mock"                 # mock | prompt_json | plan_execute
    model_config_path: str = ""             # relative to project root
    selected_memory_ids: list[str] = []     # B5 memory 选择
    model_name: str | None = None           # B4 多模型选择（None=默认）


class SendMessageReq(BaseModel):
    user_input: str


class SwitchTemplateReq(BaseModel):
    template: str


class BatchReq(BaseModel):
    tasks: list[dict]
    shared_defaults: dict = {}


class UpdateSystemPromptReq(BaseModel):
    append: str = ""           # 追加文字（末尾加）
    replace: str = ""          # 替换全文（优先级高于 append）


class UpdateMemorySwitchReq(BaseModel):
    """用于 POST /api/sessions/{sid}/memory 切换记忆（旧接口）"""
    selected_memory_ids: list[str] = []


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
PROMPTS_DIR = PROJECT_ROOT / "prompts" / "advanced"
TOOL_MASTER = PROMPTS_DIR / "tool_master.txt"


def _load_template(key: str) -> str:
    # ★ Path 穿越防护：只取文件名、限制字符集
    safe_key = Path(key).name
    if not safe_key or not re.fullmatch(r"[A-Za-z0-9_-]+", safe_key):
        raise HTTPException(400, f"invalid template name '{key}'")
    path = PROMPTS_DIR / f"{safe_key}.txt"
    if not path.is_file():
        raise HTTPException(400, f"unknown template '{safe_key}'; available: {available_templates()}")
    try:
        path.relative_to(PROMPTS_DIR.resolve())
    except ValueError:
        raise HTTPException(400, "template path escapes prompts directory")
    return path.read_text(encoding="utf-8").strip()


def available_templates() -> list[str]:
    if not PROMPTS_DIR.is_dir():
        return []
    return sorted(p.stem for p in PROMPTS_DIR.glob("*.txt"))


def _build_segmented_system(template_text: str, rules_text: str,
                             memory_block: str = "", summary_text: str = "") -> str:
    """Build system.content with delimited segments so each concern can be
    updated independently by parse/reattach helpers."""
    parts = [
        "[SYSTEM_TEMPLATE]\n",
        template_text.strip(),
        "\n\n[RULES] ",
        rules_text.strip(),
        "\n[/SYSTEM_TEMPLATE]",
    ]
    if memory_block.strip():
        parts.append("\n\n[MEMORY]\n")
        parts.append(memory_block.strip())
        parts.append("\n[/MEMORY]")
    if summary_text.strip():
        parts.append("\n\n[SUMMARY]\n")
        parts.append(summary_text.strip())
        parts.append("\n[/SUMMARY]")
    return "".join(parts)


def _parse_segmented_system(content: str) -> dict:
    """Return {'template': str, 'rules': str, 'memory': str, 'summary': str}.
    Missing segments fall back to empty string."""
    import re
    def _extract(tag: str, text: str) -> str:
        pattern = rf'\[{tag}\](.*?)\[/{tag}\]'
        m = re.search(pattern, text, re.DOTALL)
        return m.group(1).strip() if m else ""
    template_raw = _extract("SYSTEM_TEMPLATE", content)
    # inside template: text before [RULES] is template_text, after is rules_text
    rules_marker = "[RULES] "
    if rules_marker in template_raw:
        idx = template_raw.index(rules_marker)
        template_text = template_raw[:idx].strip()
        rules_text = template_raw[idx + len(rules_marker):].strip()
    else:
        template_text = template_raw
        rules_text = ""
    return {
        "template": template_text,
        "rules": rules_text,
        "memory": _extract("MEMORY", content),
        "summary": _extract("SUMMARY", content),
    }


def _init_messages(req: CreateSessionReq) -> list[dict]:
    """messages = [system_with_memory, user_first_question]"""
    template_text = _load_template(req.template_key)
    rules_text = (
        "Use available tools when needed. Do not invent file contents. "
        "Each response must contain EITHER a final answer (content) OR tool_calls — never both. "
        "You may call one or more tools in the same response when the steps are independent."
    )

    # ★ B5 Memory 注入
    memory_cfg = str(PROJECT_ROOT / "configs" / "memory.yaml")
    from b5_memory import load_memory

    effective_ids = list(req.selected_memory_ids)
    # 高级 B5：无显式 ID 时，按 user_input 自动检索
    if not effective_ids and (req.user_input or "").strip():
        try:
            from b1_agent_runtime import auto_select_memories
            effective_ids = auto_select_memories(memory_cfg, req.user_input)
        except Exception:
            pass  # 检索失败不阻塞主流程

    memory_block = ""
    if effective_ids:
        mem_result = load_memory(
            memory_cfg,
            effective_ids,
            False,            # use_global_memory 已废弃
            req.user_input or None,
        )
        docs = mem_result.get("selected_memory_docs", [])
        if docs:
            memory_block = "\n\n".join(
                f'<memory id="{d["memory_id"]}" type="{d["memory_type"]}">\n{d["content"].strip()}\n</memory>'
                for d in docs
            )

    system_content = _build_segmented_system(template_text, rules_text, memory_block)
    messages = [{"role": "system", "content": system_content}]
    if req.user_input.strip():
        messages.append({"role": "user", "content": req.user_input.strip()})
    return messages


# ---------------------------------------------------------------------------
# Routes: health + session lifecycle
# ---------------------------------------------------------------------------
@app.on_event("startup")
def _load_sessions_from_disk() -> None:
    """Server 启动时把之前落盘的 sessions 重新加载到内存。"""
    items = _b1_list_sessions(root=WEB_SESSION_ROOT)
    loaded = 0
    for info in items:
        sid = info["session_id"]
        try:
            rec = _b1_load_session(sid, root=WEB_SESSION_ROOT)
            if isinstance(rec.get("messages"), list):
                sessions[sid] = rec
                loaded += 1
            # tasks 不再恢复（已全部执行完毕）
        except Exception as exc:
            print(f"[startup] skip {sid}: {exc}", file=sys.stderr)
    if loaded:
        print(f"[startup] restored {loaded} sessions from disk", file=sys.stderr)

    # ★ B5 高级记忆：预热向量索引（首次运行时后台构建 vectors.json）
    def _warm_memory_vectors() -> None:
        try:
            memory_cfg = str(PROJECT_ROOT / "configs" / "memory.yaml")
            from b5_memory_advanced import search_memory_by_vector
            search_memory_by_vector(memory_cfg, "__warmup__", top_k=1)
            print("[startup] memory vectors warmed", file=sys.stderr)
        except Exception as exc:
            print(f"[startup] memory warmup skipped: {exc}", file=sys.stderr)

    import threading as _t
    _t.Thread(target=_warm_memory_vectors, daemon=True).start()


BATCH_TASK_DIR = PROJECT_ROOT / "data" / "batchTask"


@app.get("/health")
def health() -> dict:
    return {"ok": True, "sessions": len(sessions), "tasks": len(tasks)}


# ── 批量任务 ────────────────────────────────────────────────
@app.get("/api/batch-task/files")
def list_batch_task_files() -> dict:
    if not BATCH_TASK_DIR.is_dir():
        return {"files": []}
    files = sorted(p.name for p in BATCH_TASK_DIR.glob("*.json"))
    return {"files": files}


@app.get("/api/batch-task/file/{name}")
def get_batch_task_file(name: str) -> dict:
    safe = Path(name).name
    if not safe.endswith(".json"):
        raise HTTPException(400, "only .json")
    path = (BATCH_TASK_DIR / safe).resolve()
    try:
        path.relative_to(BATCH_TASK_DIR.resolve())
    except ValueError:
        raise HTTPException(400, "invalid path")
    if not path.is_file():
        raise HTTPException(404, "not found")
    return read_json(path)


@app.get("/api/templates")
def list_templates() -> dict:
    """Return available template names + their previews."""
    out = {}
    for key in available_templates():
        try:
            out[key] = (PROMPTS_DIR / f"{key}.txt").read_text(encoding="utf-8").strip()[:200]
        except OSError:
            pass
    return {"templates": out}


# ---------------------------------------------------------------------------
# Memory (B5 integration)
# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------
# Memory Item CRUD（Settings 面板）
# ---------------------------------------------------------------------------
class UpdateMemoryItemReq(BaseModel):
    title: str
    content: str

class TemplateReq(BaseModel):
    name: str
    content: str


@app.get("/api/memory/list")
def list_memory() -> dict:
    """Return all available memories from memory_index.json (with full content)."""
    from b5_memory import _memory_paths, _read_index
    from common.io_utils import write_json, read_text as _rt
    memory_cfg = str(PROJECT_ROOT / "configs" / "memory.yaml")
    paths = _memory_paths(memory_cfg)
    # 兼容：首次运行时自动建空 index.json
    if not paths["index"].exists():
        write_json({}, paths["index"])
    vectors_path = paths["root"] / "vectors.json"
    if not vectors_path.exists():
        write_json({}, vectors_path)
    index = _read_index(paths["index"])
    items = []
    for mid, meta in index.items():
        content = ""
        if meta.get("path"):
            try:
                doc_path = (paths["root"] / meta["path"]).resolve()
                content = _rt(doc_path)
            except Exception:
                pass
        items.append({
            "memory_id": mid,
            "memory_type": meta.get("memory_type", "conversation"),
            "title": meta.get("title", mid),
            "path": meta.get("path", ""),
            "content_preview": content[:200],
            "content": content,
        })
    return {"memories": items}


@app.put("/api/memory/{memory_id}")
def update_memory_item(memory_id: str, req: UpdateMemoryItemReq) -> dict:
    """编辑记忆（覆写 md + 更新 index 元数据）。"""
    from b5_memory import update_memory_content
    memory_cfg = str(PROJECT_ROOT / "configs" / "memory.yaml")
    try:
        return update_memory_content(memory_cfg, memory_id, req.title, req.content)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))
    except ValueError as exc:
        raise HTTPException(400, str(exc))


@app.delete("/api/memory/{memory_id}")
def delete_memory_item(memory_id: str) -> dict:
    """删除记忆（删 md + 删 vectors + 删 index 项）。"""
    from b5_memory import delete_memory
    memory_cfg = str(PROJECT_ROOT / "configs" / "memory.yaml")
    try:
        return delete_memory(memory_cfg, memory_id)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))


# ---------------------------------------------------------------------------
# B5 Settings 面板 — 智能合并更新 + 错误影响分析（无需 session_id）
# ---------------------------------------------------------------------------
class MemoryUpdateReq(BaseModel):
    """Settings 面板的合并更新请求（无需 session_id）"""
    memory_id: str = ""
    new_messages: list[dict] = []
    new_answer: str = ""
    merge_strategy: str = "smart"     # smart | replace | append


class MemoryAnalyzeReq(BaseModel):
    """Settings 面板的错误分析请求（无需 session_id）"""
    memory_id: str
    final_answer: str


@app.put("/api/settings/memory/update")
def settings_update_memory(req: MemoryUpdateReq) -> dict:
    """设置面板：按 memory_id 合并更新记忆（无需 session_id）。"""
    memory_cfg = str(PROJECT_ROOT / "configs" / "memory.yaml")
    if not req.memory_id:
        raise HTTPException(400, "memory_id required")
    from b5_memory_advanced import update_memory as advanced_update_memory
    try:
        return advanced_update_memory(
            memory_cfg, req.memory_id, req.new_messages, req.new_answer, req.merge_strategy,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))


@app.post("/api/settings/memory/analyze")
def settings_analyze_memory(req: MemoryAnalyzeReq) -> dict:
    """设置面板：分析记忆对最终回答的影响（无需 session_id）。"""
    memory_cfg = str(PROJECT_ROOT / "configs" / "memory.yaml")
    from b5_memory_advanced import analyze_memory_errors
    try:
        return analyze_memory_errors(memory_cfg, req.memory_id, req.final_answer)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))


# ---------------------------------------------------------------------------
# B5 高级记忆检索 + Settings 面板 CRUD
# ---------------------------------------------------------------------------

# ── System Prompt 模板 CRUD ──
def _read_template_content(name: str) -> str:
    safe_name = Path(name).name
    if not safe_name or not re.fullmatch(r"[A-Za-z0-9_-]+", safe_name):
        raise HTTPException(400, f"invalid template name '{name}'")
    path = PROMPTS_DIR / f"{safe_name}.txt"
    if not path.is_file():
        raise HTTPException(404, f"unknown template '{safe_name}'")
    return path.read_text(encoding="utf-8")


@app.get("/api/templates/content/{name}")
def get_template_content(name: str) -> dict:
    """Return single System Prompt template full content."""
    try:
        return {"name": name, "content": _read_template_content(name)}
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(500, str(exc))


@app.post("/api/templates/new")
def create_template(req: TemplateReq) -> dict:
    """新增 System Prompt 模板。"""
    safe_name = Path(req.name).name
    if not safe_name or not re.fullmatch(r"[A-Za-z0-9_-]+", safe_name):
        raise HTTPException(400, f"invalid template name '{req.name}'")
    target = PROMPTS_DIR / f"{safe_name}.txt"
    if target.is_file():
        raise HTTPException(409, f"template '{safe_name}' already exists")
    # ★ 跨目录防护
    try:
        target.relative_to(PROMPTS_DIR.resolve())
    except ValueError:
        raise HTTPException(400, "template path escapes prompts directory")
    write_text(req.content, target)
    return {"status": "success", "name": safe_name, "templates": available_templates()}


@app.put("/api/templates/{name}")
def update_template(name: str, req: TemplateReq) -> dict:
    """编辑 System Prompt 模板（覆写）。"""
    # 验证存在
    _read_template_content(name)
    safe_name = Path(name).name
    target = PROMPTS_DIR / f"{safe_name}.txt"
    write_text(req.content, target)
    return {"status": "success", "name": safe_name}


@app.delete("/api/templates/{name}")
def delete_template(name: str) -> dict:
    """删除 System Prompt 模板。"""
    import os
    # 验证存在
    _read_template_content(name)
    target = PROMPTS_DIR / f"{Path(name).name}.txt"
    os.remove(target)
    return {"status": "success", "deleted": name, "templates": available_templates()}


class SearchReq(BaseModel):
    query: str
    top_k: int = 5


def _do_search(memory_cfg: str, query: str, top_k: int) -> dict:
    """底层检索逻辑：关键词 + 向量混合。"""
    from b5_memory_advanced import search_memory_by_keywords, search_memory_by_vector
    kw = search_memory_by_keywords(memory_cfg, query, top_k=top_k * 2)
    vec = search_memory_by_vector(memory_cfg, query, top_k=top_k * 2)

    kw_scores = {r["memory_id"]: r.get("relevance_score", 0.0) for r in kw.get("results", [])}
    vec_scores = {r["memory_id"]: r.get("similarity_score", 0.0) for r in vec.get("results", [])}
    all_ids = set(kw_scores) | set(vec_scores)
    merged = sorted(
        all_ids,
        key=lambda mid: 0.4 * kw_scores.get(mid, 0.0) + 0.6 * vec_scores.get(mid, 0.0),
        reverse=True,
    )[:top_k]

    from b5_memory import _read_index, _memory_paths
    paths = _memory_paths(memory_cfg)
    index = _read_index(paths["index"])

    def _enrich(result_list, score_key):
        out = []
        for r in result_list:
            meta = index.get(r["memory_id"], {})
            out.append({
                "memory_id": r["memory_id"],
                "memory_type": meta.get("memory_type", r.get("memory_type", "")),
                "title": meta.get("title", r.get("title", r["memory_id"])),
                "score": r.get(score_key, 0.0),
                "content_preview": r.get("content_preview", ""),
            })
        return out

    # 合并所有 content_preview（用于前端展示）
    all_content = {r["memory_id"]: r.get("content_preview", "") for r in kw.get("results", [])}
    all_content.update({r["memory_id"]: r.get("content_preview", "") for r in vec.get("results", [])})

    return {
        "query": query,
        "top_k": top_k,
        "keyword_results": _enrich(kw.get("results", []), "relevance_score")[:top_k],
        "vector_results": _enrich(vec.get("results", []), "similarity_score")[:top_k],
        "merged": [
            {
                "memory_id": mid,
                "memory_type": index.get(mid, {}).get("memory_type", ""),
                "title": index.get(mid, {}).get("title", mid),
                "kw_score": kw_scores.get(mid, 0.0),
                "vec_score": vec_scores.get(mid, 0.0),
                "content_preview": all_content.get(mid, ""),
            }
            for mid in merged
        ],
    }


@app.get("/api/sessions/{sid}/memory/search")
def search_memory(sid: str, query: str = "", top_k: int = 5) -> dict:
    """关键词 + 向量混合检索，返回排序结果（需 session_id）。"""
    if sid not in sessions:
        raise HTTPException(404, "session not found")
    if not query.strip():
        return {"keyword_results": [], "vector_results": [], "merged": []}
    return _do_search(str(PROJECT_ROOT / "configs" / "memory.yaml"), query, top_k)


@app.get("/api/settings/memory/search")
def settings_search_memory(query: str = "", top_k: int = 8) -> dict:
    """设置面板：关键词 + 向量混合检索（无需 session_id）。"""
    if not query.strip():
        return {"keyword_results": [], "vector_results": [], "merged": []}
    return _do_search(str(PROJECT_ROOT / "configs" / "memory.yaml"), query, top_k)


class UpdateMemoryReq(BaseModel):
    memory_id: str
    new_messages: list[dict] = []
    new_answer: str = ""
    merge_strategy: str = "smart"      # smart | replace | append


@app.post("/api/sessions/{sid}/memory/update")
def update_memory(sid: str, req: UpdateMemoryReq) -> dict:
    """更新指定记忆文档，返回冲突分析 + 合并结果。"""
    if sid not in sessions:
        raise HTTPException(404, "session not found")
    memory_cfg = str(PROJECT_ROOT / "configs" / "memory.yaml")

    from b5_memory_advanced import update_memory as advanced_update_memory
    try:
        result = advanced_update_memory(
            memory_cfg,
            req.memory_id,
            req.new_messages,
            req.new_answer,
            req.merge_strategy,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc))
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))

    return result


class AnalyzeMemoryReq(BaseModel):
    memory_id: str
    final_answer: str


@app.post("/api/sessions/{sid}/memory/analyze")
def analyze_memory(sid: str, req: AnalyzeMemoryReq) -> dict:
    """分析指定记忆对最终回答的可靠性影响。"""
    if sid not in sessions:
        raise HTTPException(404, "session not found")
    memory_cfg = str(PROJECT_ROOT / "configs" / "memory.yaml")

    from b5_memory_advanced import analyze_memory_errors
    try:
        result = analyze_memory_errors(memory_cfg, req.memory_id, req.final_answer)
    except FileNotFoundError as exc:
        raise HTTPException(404, str(exc))

    return result


# ---------------------------------------------------------------------------
# Session listing + individual session
# ---------------------------------------------------------------------------
@app.get("/api/sessions/list")
def list_sessions() -> dict:
    """List all persisted sessions (id, template, turns, updated_at, first_user_msg)."""
    items = []
    for sid, s in sessions.items():
        first_user = ""
        for m in s.get("messages", []):
            if m.get("role") == "user":
                first_user = (m.get("content") or "")[:60]
                break
        items.append({
            "session_id": sid,
            "template_key": s.get("template_key", ""),
            "turns": sum(1 for m in s.get("messages", []) if m.get("role") == "user"),
            "message_count": len(s.get("messages", [])),
            "status": s.get("status", "idle"),
            "first_user_msg": first_user,
            "updated_at": s.get("updated_at", ""),
        })
    items.sort(key=lambda x: x.get("updated_at", ""), reverse=True)
    return {"sessions": items}


@app.post("/api/sessions")
def create_session(req: CreateSessionReq) -> dict:
    sid = uuid.uuid4().hex[:12]
    messages = _init_messages(req)
    rec = {
        "session_id": sid,
        "messages": messages,
        "status": "idle",
        "toolset": req.toolset,
        "max_turns": req.max_turns,
        "template_key": req.template_key,
        "repl_mode": req.repl_mode,
        "model_name": req.model_name,      # B4 多模型
        "token_stats": {"input": 0, "output": 0},  # 累积 token
        "created_at": now_iso(),
        "updated_at": now_iso(),
    }
    sessions[sid] = rec
    return {"session_id": sid, "status": "idle", "messages": messages,
            "templates": available_templates()}


@app.get("/api/sessions/{sid}")
def get_session(sid: str) -> dict:
    if sid not in sessions:
        raise HTTPException(404, "session not found")
    s = sessions[sid]
    # ★ .get() 带默认值 — CLI session 可能缺 status / max_turns / template_key 等
    return {"session_id": sid,
            "messages": s.get("messages", []),
            "status": s.get("status", "idle"),
            "template_key": s.get("template_key"),
            "toolset": s.get("toolset", "basic_tools"),
            "max_turns": s.get("max_turns", 5),
            "repl_mode": s.get("repl_mode", "mock"),
            "model_name": s.get("model_name"),
            "token_stats": s.get("token_stats"),
            "updated_at": s.get("updated_at")}


@app.delete("/api/sessions/{sid}")
def delete_session(sid: str) -> dict:
    """Delete session from memory + remove disk directory（委托 B1 delete_session）。"""
    if sid not in sessions:
        raise HTTPException(404, "session not found")
    sessions.pop(sid, None)
    # ★ 清理关联的 tasks + events (防内存泄露)
    orphaned_task_ids = [tid for tid, t in tasks.items() if t.get("session_id") == sid]
    for tid in orphaned_task_ids:
        tasks.pop(tid, None)
        task_events.pop(tid, None)
    # 删磁盘（B1 统一函数）
    _b1_delete_session(sid, root=WEB_SESSION_ROOT)
    return {"ok": True, "deleted": sid}


# ---------------------------------------------------------------------------
# 对话中切换模型 / 模式
# ---------------------------------------------------------------------------
@app.patch("/api/sessions/{sid}/config")
def update_session_config(sid: str, req: dict) -> dict:
    """对话中实时切换 model_name / repl_mode。"""
    if sid not in sessions:
        raise HTTPException(404, "session not found")
    session = sessions[sid]
    if "model_name" in req:
        session["model_name"] = req["model_name"] or None
    if "repl_mode" in req:
        session["repl_mode"] = req["repl_mode"]
    session["updated_at"] = now_iso()
    return {"ok": True,
            "model_name": session.get("model_name"),
            "repl_mode": session.get("repl_mode")}


# ---------------------------------------------------------------------------
# 规划模式：确认 / 取消执行计划
# ---------------------------------------------------------------------------
@app.post("/api/sessions/{sid}/confirm-plan")
def confirm_plan(sid: str, req: dict) -> dict:
    """确认执行计划 → 唤醒等待中的 run_single_turn。"""
    if sid not in sessions:
        raise HTTPException(404, "session not found")
    task = tasks.get(req.get("task_id", ""))
    if task is None or task.get("session_id") != sid:
        raise HTTPException(404, "task not found in this session")
    ps = sessions[sid].get("_plan_state")
    if ps is None:
        raise HTTPException(409, "no pending plan")
    ps["cancelled"] = False
    ps["event"].set()
    return {"ok": True}


@app.post("/api/sessions/{sid}/cancel-plan")
def cancel_plan(sid: str, req: dict) -> dict:
    """取消执行计划 → 唤醒 run_single_turn 并标记取消。"""
    if sid not in sessions:
        raise HTTPException(404, "session not found")
    task = tasks.get(req.get("task_id", ""))
    if task is None or task.get("session_id") != sid:
        raise HTTPException(404, "task not found in this session")
    ps = sessions[sid].get("_plan_state")
    if ps is None:
        raise HTTPException(409, "no pending plan")
    ps["cancelled"] = True
    ps["event"].set()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Messages (the core chat loop turn)
# ---------------------------------------------------------------------------
@app.post("/api/sessions/{sid}/messages")
async def post_message(sid: str, req: SendMessageReq) -> dict:
    """Background coroutine that runs one Agent turn in a worker thread."""
    if sid not in sessions:
        raise HTTPException(404, "session not found")
    session = sessions[sid]
    if session["status"] == "running":
        return {"task_id": "", "status": "ignored",
                "message": "A previous turn is still running; this message was ignored."}

    task = _new_task(session_id=sid, kind="turn")
    session["status"] = "running"
    user_input = req.user_input.strip()

    # ★ 规划模式：初始化 plan 确认状态（每轮新建，线程安全由 status=running 保证）
    session["_plan_state"] = {"event": threading.Event(), "cancelled": False}

    # Append user message to session
    session["messages"].append({"role": "user", "content": user_input})

    asyncio.create_task(_run_turn(task["task_id"], sid, user_input))
    return {"task_id": task["task_id"], "status": "pending"}


async def _run_turn(task_id: str, sid: str, user_input: str) -> None:
    """Run one Agent turn via run_single_turn."""
    task = tasks.get(task_id)
    # ★ Bug #6 修复：session 可能在任务调度后被删除
    if task is None or sid not in sessions:
        if task is not None:
            task["status"] = "error"
            task["error"] = "session not found"
        return
    session = sessions[sid]
    task["status"] = "running"
    task["updated_at"] = now_iso()

    events_list: list[dict] = []
    task_events[task_id] = events_list

    model_cfg = str(PROJECT_ROOT / "configs" / "model.yaml")
    tools_cfg = str(PROJECT_ROOT / "configs" / "tools.yaml")

    try:
        summary: dict = await asyncio.to_thread(
            run_single_turn,
            session.get("messages", []),
            model_cfg,
            tools_cfg,
            session.get("toolset", "basic_tools"),
            session.get("toolset", "basic_tools"),
            session.get("max_turns", 5),
            str(session.get("repl_mode", "mock")),
            None,          # outdir=None → 不写诊断文件到 web_sessions
            None,          # event_callback: None (polling mode)
            events_list,   # events storage
            session.get("model_name"),  # B4 model_name
            session.get("_plan_state"),  # 规划模式确认状态
        )

        task["result"] = summary
        task["status"] = "completed"
        session["status"] = "idle"
        # 累积 token 统计（持久化到 session）
        ts = summary.get("token_stats", {})
        inp = ts.get("input_tokens", 0) or 0
        outp = ts.get("output_tokens", 0) or 0
        if inp or outp:
            cur = session.get("token_stats", {"input": 0, "output": 0})
            cur["input"] = (cur.get("input") or 0) + inp
            cur["output"] = (cur.get("output") or 0) + outp
            session["token_stats"] = cur
    except Exception as exc:
        task["error"] = f"{type(exc).__name__}: {exc}"
        task["status"] = "error"
        session["status"] = "error"
        events_list.append({"type": "error", "error": str(exc)})
    finally:
        task["updated_at"] = now_iso()
        session["updated_at"] = now_iso()
        _persist_session(sid)
        # ★ 清理已完成 task 的 events_list (防内存泄露)
        if task["status"] in ("completed", "error") and task_id in task_events:
            # 延迟 60s 清理，确保前端能读完
            async def _delayed_cleanup():
                await asyncio.sleep(60)
                task_events.pop(task_id, None)
                tasks.pop(task_id, None)
            asyncio.create_task(_delayed_cleanup())

# ---------------------------------------------------------------------------
@app.patch("/api/sessions/{sid}/messages")
async def patch_messages(sid: str, req: dict) -> dict:
    """同步 messages 列表到后端 session (消毒后存储)."""
    if sid not in sessions:
        raise HTTPException(404, "session not found")
    session = sessions[sid]
    new_messages = req.get("messages")
    if not isinstance(new_messages, list):
        raise HTTPException(400, "messages must be a list")
    # ★ Bug #8 消毒：确保每条消息结构合法、content 为字符串
    cleaned = []
    valid_roles = {"system", "user", "assistant", "tool"}
    for m in new_messages:
        if not isinstance(m, dict) or m.get("role") not in valid_roles:
            continue  # skip malformed
        cleaned.append({
            "role": m["role"],
            "content": str(m.get("content", "")),
            "tool_calls": m.get("tool_calls") if isinstance(m.get("tool_calls"), list) else [],
            **({"tool_call_id": str(m["tool_call_id"])} if "tool_call_id" in m else {}),
            **({"name": str(m["name"])} if "name" in m else {}),
            **({"status": str(m["status"])} if "status" in m else {}),
        })
    session["messages"] = cleaned
    session["updated_at"] = now_iso()
    _persist_session(sid)
    return {"ok": True, "messages": len(cleaned)}


# ------------------------------------------------------------------------ ---
# Polling endpoint
# ---------------------------------------------------------------------------
@app.get("/api/tasks/{task_id}")
def get_task(task_id: str) -> dict:
    task = tasks.get(task_id)
    if task is None:
        raise HTTPException(404, "task not found")
    resp: dict[str, Any] = {
        "task_id": task["task_id"],
        "session_id": task["session_id"],
        "kind": task["kind"],
        "status": task["status"],
        "created_at": task["created_at"],
        "updated_at": task["updated_at"],
    }
    if task["status"] == "completed":
        result = task["result"] or {}
        if task["kind"] == "turn":
            # Compact preview; full data is persisted to disk.
            resp["result"] = {
                "status": result.get("status"),
                "final_answer": result.get("final_answer"),
                "tool_rounds": result.get("tool_rounds_used"),
                "llm_calls": result.get("llm_calls"),
                "turns": result.get("turns"),
                "error": result.get("error"),
            }
        else:
            # compress / batch: return the dict as-is
            resp["result"] = result
    elif task["status"] == "error":
        resp["error"] = task["error"]
    return resp


# ---------------------------------------------------------------------------
# ★ Polling 流式传输: GET /api/sessions/{sid}/poll?task_id=X&since=Y
#    返回 events[Y:] + 当前 task 状态，前端轮询驱动。
# ---------------------------------------------------------------------------
@app.get("/api/sessions/{sid}/poll")
async def poll_session(sid: str, task_id: str, since: int = 0):
    """Polling 端点：返回 run_single_turn 产生的事件流。

    用法:
      let since = 0;
      while (true) {
        const r = await fetch(`/api/sessions/${sid}/poll?task_id=${taskId}&since=${since}`);
        const data = await r.json();
        since += data.events.length;
        data.events.forEach(ev => handle(ev));
        if (['completed', 'error', 'ignored'].includes(data.status)) break;
        await sleep(800);
      }
    """
    if sid not in sessions:
        raise HTTPException(404, "session not found")
    task = tasks.get(task_id)
    if task is None:
        raise HTTPException(404, "task not found (use GET /api/tasks/{task_id} for status)")
    # ★ 安全检查：task 必须属于该 session，防止越权读取
    if task.get("session_id") != sid:
        raise HTTPException(404, "task not found in this session")
    events = task_events.get(task_id, [])
    since = max(0, int(since))
    return {
        "events": events[since:],
        "status": task["status"],
        "task_id": task_id,
    }


# ---------------------------------------------------------------------------
# Template switch
# ---------------------------------------------------------------------------
@app.post("/api/sessions/{sid}/switch-template")
def switch_template(sid: str, req: SwitchTemplateReq) -> dict:
    if sid not in sessions:
        raise HTTPException(404, "session not found")
    session = sessions[sid]
    key = req.template.strip()
    # Validate + load to raise early on bad names.
    _load_template(key)
    new_template_text = _load_template(key)
    rules_text = (
        "Use available tools when needed. Do not invent file contents. "
        "Each response must contain EITHER a final answer (content) OR tool_calls — never both. "
        "You may call one or more tools in the same response when the steps are independent."
    )
    # ★ 只替换 TEMPLATE 段，保留 MEMORY + SUMMARY
    segs = _parse_segmented_system(session["messages"][0].get("content", "")) \
        if session["messages"] and session["messages"][0].get("role") == "system" \
        else {"template": "", "rules": "", "memory": "", "summary": ""}
    new_content = _build_segmented_system(
        new_template_text, rules_text, segs["memory"], segs["summary"]
    )
    if session["messages"] and session["messages"][0].get("role") == "system":
        session["messages"][0]["content"] = new_content
    else:
        session["messages"].insert(0, {"role": "system", "content": new_content})
    session["template_key"] = key
    session["updated_at"] = now_iso()
    _persist_session(sid)
    return {"template_key": key, "messages": session["messages"]}


# ---------------------------------------------------------------------------
# System Prompt edit (append / replace)
# ---------------------------------------------------------------------------
@app.post("/api/sessions/{sid}/system-prompt")
def update_system_prompt(sid: str, req: UpdateSystemPromptReq) -> dict:
    if sid not in sessions:
        raise HTTPException(404, "session not found")
    session = sessions[sid]
    if not session["messages"] or session["messages"][0].get("role") != "system":
        session["messages"].insert(0, {"role": "system", "content": ""})

    segs = _parse_segmented_system(session["messages"][0].get("content", ""))
    if req.replace.strip():
        # 替换整体 parsed 格式：template=全文，保留 MEMORY+SUMMARY
        new_segs = _parse_segmented_system(req.replace.strip())
        merged = _build_segmented_system(
            new_segs["template"] or req.replace.strip(),
            new_segs["rules"],
            segs["memory"],
            segs["summary"],
        )
    elif req.append.strip():
        merged = _build_segmented_system(
            segs["template"] + "\n\n" + req.append.strip(),
            segs["rules"],
            segs["memory"],
            segs["summary"],
        )
    else:
        raise HTTPException(400, "append or replace required")

    session["messages"][0]["content"] = merged
    session["updated_at"] = now_iso()
    _persist_session(sid)   # ★ 同步写到磁盘
    return {"ok": True, "content_preview": session["messages"][0]["content"][:200]}


# ---------------------------------------------------------------------------
# Memory update (switch memory mid-conversation)
# ---------------------------------------------------------------------------
@app.post("/api/sessions/{sid}/memory")
def update_session_memory(sid: str, req: UpdateMemorySwitchReq) -> dict:
    """Rebuild system prompt with new memory selection (only touches MEMORY segment)."""
    if sid not in sessions:
        raise HTTPException(404, "session not found")
    session = sessions[sid]
    if not session["messages"] or session["messages"][0].get("role") != "system":
        raise HTTPException(400, "session has no system message")

    segs = _parse_segmented_system(session["messages"][0].get("content", ""))

    # ★ 只替换 MEMORY 段，保留 TEMPLATE + SUMMARY
    memory_block = ""
    if req.selected_memory_ids:
        memory_cfg = str(PROJECT_ROOT / "configs" / "memory.yaml")
        from b5_memory import load_memory
        mem_result = load_memory(
            memory_cfg,
            req.selected_memory_ids,
            False,            # use_global_memory 已废弃
        )
        docs = mem_result.get("selected_memory_docs", [])
        if docs:
            memory_block = "\n\n".join(
                f'<memory id="{d["memory_id"]}" type="{d["memory_type"]}">\n{d["content"].strip()}\n</memory>'
                for d in docs
            )

    session["messages"][0]["content"] = _build_segmented_system(
        segs["template"], segs["rules"], memory_block, segs["summary"]
    )
    session["updated_at"] = now_iso()
    _persist_session(sid)
    return {"ok": True, "docs_included": len(req.selected_memory_ids)}


# ---------------------------------------------------------------------------
# Save session as B5 memory
# ---------------------------------------------------------------------------
class SaveAsMemoryReq(BaseModel):
    title: str = ""             # optional custom title; defaults to first user message
    summary_max_chars: int = 300


@app.post("/api/sessions/{sid}/save-as-memory")
async def save_as_memory(sid: str, req: SaveAsMemoryReq) -> dict:
    """Save the current session as a B5 conversation memory."""
    if sid not in sessions:
        raise HTTPException(404, "session not found")

    from b5_memory import _read_index, _memory_paths

    session = sessions[sid]
    messages = session.get("messages", [])

    # ── extract last assistant content as answer ──
    last_assistant = ""
    for m in reversed(messages):
        if m.get("role") == "assistant":
            c = (m.get("content") or "").strip()
            if c:
                last_assistant = c
                break

    user_turns = [m for m in messages if m.get("role") == "user"]
    if not user_turns:
        raise HTTPException(400, "会话没有任何用户消息，无法保存为记忆")

    memory_cfg = str(PROJECT_ROOT / "configs" / "memory.yaml")
    out_dir = PROJECT_ROOT / "outputs" / "B1_advanced" / "saved_memory" / sid
    out_dir.mkdir(parents=True, exist_ok=True)

    # ── build trace dict from session metadata ──
    trace = {
        "conversation_id": sid,
        "execution_mode": "web",
        "status": "success",
        "toolset": session.get("toolset", "basic_tools"),
        "source": "web_session",
    }

    # ── 高级 B5 save：自动摘要 + B4 LLM + 抽取式 fallback ──
    from b5_memory_advanced import save_memory_advanced

    model_cfg = str(PROJECT_ROOT / "configs" / "model.yaml")
    try:
        saved = await asyncio.to_thread(
            save_memory_advanced,
            memory_cfg,
            sid,
            "conversation",
            messages,
            trace,
            last_assistant or "（无文本回答）",
            True,            # auto_summarize
            True,            # use_llm_summary (B4 → fallback)
            str(out_dir),
        )
    except Exception as exc:
        raise HTTPException(500, f"save_memory_advanced failed: {exc}")

    # ── optionally override title in the index ──
    if req.title.strip():
        from common.io_utils import write_json as _wj
        paths = _memory_paths(memory_cfg)
        index = _read_index(paths["index"])
        memory_id = saved.get("memory_id", f"mem_conversation_{sid}")
        if memory_id in index:
            index[memory_id]["title"] = req.title.strip()
            _wj(index, paths["index"])
            saved["title"] = req.title.strip()

    return {
        "ok": True,
        "memory_id": saved.get("memory_id"),
        "memory_type": saved.get("memory_type"),
        "title": saved.get("title"),
        "path": saved.get("path"),
        "auto_summarized": saved.get("auto_summarized", False),
        "summary": saved.get("summary", "")[:200],
    }


# ---------------------------------------------------------------------------
# History compression
# ---------------------------------------------------------------------------
@app.post("/api/sessions/{sid}/compress")
async def compress(sid: str) -> dict:
    if sid not in sessions:
        raise HTTPException(404, "session not found")
    session = sessions[sid]
    if session["status"] == "running":
        raise HTTPException(409, "session busy")

    task = _new_task(session_id=sid, kind="compress")
    session["status"] = "running"
    asyncio.create_task(_run_compress(task["task_id"], sid))
    return {"task_id": task["task_id"], "status": "pending"}


async def _run_compress(task_id: str, sid: str) -> None:
    """Compress the session history into a summary system message."""
    task = tasks[task_id]
    session = sessions[sid]
    task["status"] = "running"
    task["updated_at"] = now_iso()

    try:
        messages = session["messages"]
        old_count = len(messages)

        user_indices = [i for i, m in enumerate(messages) if m.get("role") == "user"]
        keep_last_k = 0  # 0 = 不保留任何旧消息，全部靠摘要
        if len(user_indices) <= keep_last_k:
            task["result"] = {"compressed": False, "reason": "not_enough_history"}
            task["status"] = "completed"
            session["status"] = "idle"
            return

        cut_until = len(messages)  # keep_last_k=0 → 全部压缩

        # ★ 解析 system 分段（保留 TEMPLATE + MEMORY，只消费 SUMMARY）
        old_segments = {"template": "", "rules": "", "memory": "", "summary": ""}
        if messages[0].get("role") == "system":
            old_segments = _parse_segmented_system(messages[0].get("content") or "")

        # ★ 程序格式化新对话消息为纯文本
        from b1_agent_runtime import _format_messages_for_summary
        messages_plain_text = _format_messages_for_summary(messages[:cut_until])

        if not messages_plain_text.strip():
            task["result"] = {"compressed": False, "reason": "empty"}
            task["status"] = "completed"
            session["status"] = "idle"
            return

        # ★ 调 Qwen 合并旧摘要 + 新消息（失败时不压缩）
        from b1_agent_runtime import _summarise_messages_with_model
        model_cfg = str(PROJECT_ROOT / "configs" / "model.yaml")
        try:
            summary_text = await asyncio.to_thread(
                _summarise_messages_with_model,
                model_cfg, old_segments["summary"], messages_plain_text, "prompt_json",
            )
        except Exception as exc:
            # ★ 失败时记录日志 + 通知前端
            print(f"[compress] LLM 压缩失败 sid={sid}: {exc}", file=sys.stderr, flush=True)
            task["result"] = {"compressed": False, "reason": "llm_failed", "error": str(exc)}
            task["status"] = "completed"
            session["status"] = "idle"
            session["_compress_error"] = str(exc)
            _persist_session(sid)
            return

        # ★ 重建 system：TEMPLATE + MEMORY 原样保留，只替换 SUMMARY
        merged_system_content = _build_segmented_system(
            template_text=old_segments["template"],
            rules_text=old_segments["rules"],
            memory_block=old_segments["memory"],
            summary_text=summary_text,
        )

        new_messages: list[dict] = [
            {"role": "system", "content": merged_system_content}
        ]
        # 追加保留区间内的消息，过滤掉"畸形" 的 assistant 消息
        for msg in messages[cut_until:]:
            role = msg.get("role")
            if role == "assistant":
                content = (msg.get("content") or "").strip()
                if content.startswith("{") and content.endswith("}"):
                    try:
                        json.loads(content)
                        continue
                    except (json.JSONDecodeError, ValueError):
                        pass
                if msg.get("tool_calls"):
                    continue
            new_messages.append(msg)

        session["messages"] = new_messages
        task["result"] = {"compressed": True, "original_count": old_count,
                          "compressed_count": len(new_messages)}
        task["status"] = "completed"
    except Exception as exc:
        task["error"] = f"{type(exc).__name__}: {exc}"
        task["status"] = "error"
    finally:
        task["updated_at"] = now_iso()
        session["status"] = "idle" if task["status"] != "error" else "error"
        session["updated_at"] = now_iso()
        _persist_session(sid)


# ---------------------------------------------------------------------------
# ═══════════════════════════════════════════════════════════════════════════
#  Web 批量任务
# ═══════════════════════════════════════════════════════════════════════════
#  前端 POST /api/batch 创建异步任务，核心执行全在 B1.run_batch_tasks。
#  本模块只做 HTTP 包装：接收请求 → 后台线程调 B1 → 持久化 → 前端 polling。

@app.post("/api/batch")
async def batch(req: BatchReq) -> dict:
    """Web 批量任务入口（薄封装）。

    接收任务列表 → 创建异步 task → 立即返回 task_id。
    前端拿到 task_id 后 polling GET /api/tasks/{task_id} 获取进度。
    """
    task = _new_task(session_id="", kind="batch")
    # ★ create_task 在后台执行，不阻塞 HTTP 响应
    asyncio.create_task(_run_batch(task["task_id"], req))
    return {"task_id": task["task_id"], "status": "pending"}


async def _run_batch(task_id: str, req: BatchReq) -> None:
    """Web 批量任务后台执行：调 B1 核心 + 持久化结果到磁盘。

    执行流程：
      1. 拼装配置路径（model.yaml / tools.yaml）
      2. asyncio.to_thread → 调 B1.run_batch_tasks（同步函数跑在线程池）
      3. 持久化到 outputs/batch/batch_{时间戳}.json
      4. 更新 task 状态 → 前端 polling 读到 completed/error
    """
    task = tasks[task_id]
    task["status"] = "running"
    task["updated_at"] = now_iso()

    model_cfg = str(PROJECT_ROOT / "configs" / "model.yaml")
    tools_cfg = str(PROJECT_ROOT / "configs" / "tools.yaml")

    try:
        # ★ 核心：调 B1 执行批量任务（所有任务串行，各自独立 messages）
        result = await asyncio.to_thread(
            run_batch_tasks,
            tasks=req.tasks,                           # 任务列表（前端传过来的）
            model_cfg=model_cfg,                       # model.yaml 路径
            tools_cfg=tools_cfg,                       # tools.yaml 路径
            model_name=(req.shared_defaults or {}).get("model_name"),  # 全局默认模型
        )

        # ★ 持久化到 outputs/batch/（文件名为时间戳，不覆盖历史结果）
        try:
            outdir = PROJECT_ROOT / "outputs" / "batch"
            outdir.mkdir(parents=True, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            outfile = outdir / f"batch_{ts}.json"
            write_json({"task_id": task_id, "completed_at": now_iso(), **result}, outfile)
        except Exception:
            pass  # 写入失败不影响主流程（结果已在 task.result 中）

        task["result"] = result
        task["status"] = "completed"
    except Exception as exc:
        task["error"] = f"{type(exc).__name__}: {exc}"
        task["status"] = "error"
    finally:
        task["updated_at"] = now_iso()


# ---------------------------------------------------------------------------
# Static chat UI
# ---------------------------------------------------------------------------
@app.get("/")
def root() -> FileResponse:
    return FileResponse(str(app_dir / "agent_chat.html"))


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
if __name__ == "__main__":
    import uvicorn

    uvicorn.run("agent_api:app", host="0.0.0.0", port=8000, reload=False,
                access_log=True, log_level="info")
