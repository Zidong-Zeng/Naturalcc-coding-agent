from __future__ import annotations

import argparse
import json
import sys
import uuid
from copy import deepcopy
from pathlib import Path
from time import perf_counter, sleep
import os
from typing import Any

from common.io_utils import append_jsonl, read_json, read_text, read_yaml, write_json, write_text
from common.logging_utils import now_iso
from common.path_utils import resolve_cli_path, resolve_from_file
from common.schemas import validate_ai_message


def _validate_runtime_input(payload: dict) -> dict:
    if not isinstance(payload, dict):
        raise ValueError("runtime_input.json must contain an object")
    execution_mode = payload.setdefault("execution_mode", "integrated")
    if execution_mode not in {"integrated", "fixture"}:
        raise ValueError("execution_mode must be integrated or fixture")
    required = ["conversation_id", "user_input", "system_prompt_path", "toolset", "max_turns", "save_memory"]
    missing = [field for field in required if field not in payload]
    if missing:
        raise ValueError(f"runtime input missing: {', '.join(missing)}")
    if not isinstance(payload["conversation_id"], str) or not payload["conversation_id"]:
        raise ValueError("conversation_id must be a non-empty string")
    if not isinstance(payload["user_input"], str) or not payload["user_input"].strip():
        raise ValueError("user_input must be a non-empty string")
    if not isinstance(payload["max_turns"], int) or isinstance(payload["max_turns"], bool) or payload["max_turns"] < 1:
        raise ValueError("max_turns must be a positive integer")
    if payload["save_memory"] not in {"none", "conversation", "global"}:
        raise ValueError("save_memory must be none, conversation, or global")
    if execution_mode == "fixture":
        fixtures = payload.get("fixtures")
        if not isinstance(fixtures, dict):
            raise ValueError("fixture mode requires a fixtures object")
        required_fixtures = [
            "selected_memory_path",
            "tools_schema_path",
            "ai_messages_path",
            "tool_messages_path",
        ]
        missing_fixtures = [field for field in required_fixtures if not isinstance(fixtures.get(field), str)]
        if missing_fixtures:
            raise ValueError(f"fixtures missing paths: {', '.join(missing_fixtures)}")
        if payload["save_memory"] != "none":
            raise ValueError("fixture mode requires save_memory=none")
    else:
        selected_ids = payload.setdefault("selected_memory_ids", [])
        if not isinstance(selected_ids, list) or not all(isinstance(item, str) for item in selected_ids):
            raise ValueError("selected_memory_ids must be a list of strings")
        payload.setdefault("use_global_memory", False)
        if not isinstance(payload["use_global_memory"], bool):
            raise ValueError("use_global_memory must be boolean")
    return payload


def _memory_context(selected_memory: dict) -> str:
    """把 B5 返回的记忆文档列表拼成 <memory> 标签文本，注入 system prompt。

    每个记忆文档被包裹在 <memory id="..." type="..."> 标签里，
    多个记忆之间用空行分隔。
    输出示例：
        <memory id="mem_001" type="global">
        这是全局记忆内容...
        </memory>

        <memory id="mem_002" type="conversation">
        之前对话的记忆...
        </memory>
    """
    sections = []
    for document in selected_memory.get("selected_memory_docs", []):
        sections.append(
            f'<memory id="{document["memory_id"]}" type="{document["memory_type"]}">\n'
            f'{document["content"].strip()}\n</memory>'
        )
    return "\n\n".join(sections)


def auto_select_memories(config_path: str, query: str, top_k: int = 5,
                         keyword_weight: float = 0.4) -> list[str]:
    """根据用户 query 自动检索最相关的记忆 ID（关键词 + 向量混合检索）。

    适用场景：用户没有显式勾选记忆时，根据问题自动找相关记忆。
    策略：
      1. 分别用关键词和向量检索，各取 top_k*2 条候选（扩大召回）
      2. 加权融合分数：keyword_weight * kw_score + (1-weight) * vec_score
      3. 按融合分数排序，取 top_k
    检索失败时返回空列表（不阻塞主流程）。
    """
    from b5_memory_advanced import search_memory_by_keywords, search_memory_by_vector

    try:
        # 各检索 top_k*2 条，扩大候选集以便融合后更准确
        kw = search_memory_by_keywords(config_path, query, top_k=top_k * 2)
        vec = search_memory_by_vector(config_path, query, top_k=top_k * 2)
    except Exception:
        return []  # 检索失败 → 空列表，不阻塞

    # 提取各记忆的分数，建立 memory_id → score 的映射
    kw_scores = {r["memory_id"]: r.get("relevance_score", 0.0) for r in kw.get("results", [])}
    vec_scores = {r["memory_id"]: r.get("similarity_score", 0.0) for r in vec.get("results", [])}
    all_ids = set(kw_scores) | set(vec_scores)

    # 加权融合排序，取 top_k
    ranked = sorted(
        all_ids,
        key=lambda mid: keyword_weight * kw_scores.get(mid, 0.0) + (1 - keyword_weight) * vec_scores.get(mid, 0.0),
        reverse=True,
    )
    return ranked[:top_k]


def _default_llm_mode(model_config: Path) -> str:
    config = read_yaml(model_config)
    return config.get("runtime", {}).get("default_mode", "mock")


def generate_ai_message(*args, **kwargs) -> dict:
    """B1 Agent Runtime — single-turn + advanced multi-turn / resume / batch / compress / template.

Baseline (PPT Slide 8):
  * ``fixture`` mode — personal demo driven by preset module responses.
  * ``integrated`` mode — full B3 → B4 → B5 Agent loop (single user question).

Advanced (PPT Slide 14) — capability-equivalent to ``b1_advanced_agent_runtime``:
  * ``advanced_repl`` — multi-turn interactive REPL with inline ``/commands``.
  * ``--resume`` — restore a previous session (interactive selection or --resume-session-id).
  * ``advanced_batch`` — run a JSON list of tasks through the baseline ``run_agent``.

The baseline ``run_agent`` is preserved verbatim; advanced features are added on top.
"""
    from b4_local_agent_llm import generate_ai_message as b4_generate_ai_message

    return b4_generate_ai_message(*args, **kwargs)


def _load_fixture_inputs(input_file: Path, runtime: dict) -> dict:
    fixtures = runtime["fixtures"]
    selected_memory = read_json(resolve_from_file(fixtures["selected_memory_path"], input_file))
    tools_schema = read_json(resolve_from_file(fixtures["tools_schema_path"], input_file))
    ai_messages = read_json(resolve_from_file(fixtures["ai_messages_path"], input_file))
    tool_messages = read_json(resolve_from_file(fixtures["tool_messages_path"], input_file))
    if not isinstance(selected_memory, dict):
        raise ValueError("preset memory must be a JSON object")
    if not isinstance(tools_schema, list):
        raise ValueError("preset tools_schema must be a JSON array")
    if not isinstance(ai_messages, list) or not ai_messages:
        raise ValueError("preset AI messages must be a non-empty JSON array")
    if not isinstance(tool_messages, dict):
        raise ValueError("preset ToolMessages must be an object keyed by tool_call_id")
    for message in ai_messages:
        validate_ai_message(message)
    return {
        "selected_memory": selected_memory,
        "tools_schema": tools_schema,
        "ai_messages": ai_messages,
        "tool_messages": tool_messages,
    }


def _fixture_tool_messages(tool_calls: list[dict], preset_messages: dict) -> list[dict]:
    results = []
    for call in tool_calls:
        call_id = call.get("id")
        message = deepcopy(preset_messages.get(call_id))
        if not isinstance(message, dict):
            raise ValueError(f"fixture ToolMessage does not exist for tool_call_id: {call_id}")
        if message.get("role") != "tool" or message.get("tool_call_id") != call_id:
            raise ValueError(f"invalid fixture ToolMessage for tool_call_id: {call_id}")
        if message.get("name") != call.get("name"):
            raise ValueError(f"fixture ToolMessage name does not match call: {call_id}")
        results.append(message)
    return results


def run_agent(
    input_path: str,
    tools_config: str | None,
    memory_config: str | None,
    model_config: str | None,
    outdir: str,
    llm_mode: str | None = None,
) -> dict:
    started = perf_counter()
    input_file = Path(input_path).resolve()
    output_dir = Path(outdir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    runtime = _validate_runtime_input(read_json(input_file))
    print(f"user_input: {runtime['user_input']}")
    execution_mode = runtime["execution_mode"]
    prompt_path = resolve_from_file(runtime["system_prompt_path"], input_file)
    system_prompt = read_text(prompt_path).strip()
    fixture_data = None
    tools_file = memory_file = model_file = None
    if execution_mode == "fixture":
        fixture_data = _load_fixture_inputs(input_file, runtime)
        selected_memory = fixture_data["selected_memory"]
        tools_schema = fixture_data["tools_schema"]
        mode = "fixture"
    else:
        if not tools_config or not memory_config or not model_config:
            raise ValueError("integrated mode requires tools_config, memory_config, and model_config")
        from b3_tool_layer import execute_tool_calls, get_tools_schema
        from b5_memory import load_memory

        tools_file = Path(tools_config).resolve()
        memory_file = Path(memory_config).resolve()
        model_file = Path(model_config).resolve()

        # ★ 记忆注入（初始化阶段）：决定用哪些记忆 + 加载 + 注入 system prompt
        # 优先级：用户显式选择 > 按 query 自动检索
        effective_ids = list(runtime["selected_memory_ids"])
        if not effective_ids and (runtime.get("user_input") or "").strip():
            try:
                # 用户没选记忆 → 根据问题自动检索相关记忆
                effective_ids = auto_select_memories(str(memory_file), runtime["user_input"])
            except Exception:
                pass  # 检索失败不阻塞主流程

        # 按 effective_ids 从 B5 加载记忆文档（同时加载全局记忆）
        selected_memory = load_memory(
            str(memory_file),
            effective_ids,
            runtime["use_global_memory"],
            runtime["user_input"],
            str(output_dir),
        )
        tools_schema = get_tools_schema(str(tools_file), runtime["toolset"], str(output_dir))
        mode = llm_mode or _default_llm_mode(model_file)
    # 把记忆文档拼成 <memory> 标签 → 拼接到 system prompt 末尾
    memory_context = _memory_context(selected_memory)
    if memory_context:
        system_prompt = f"{system_prompt}\n\n{memory_context}"
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": runtime["user_input"]},
    ]
    tool_rounds = 0
    llm_calls = 0
    turns = []
    all_tool_messages = []
    final_answer = ""
    status = "success"
    terminal_error = None
    warnings = []
    if selected_memory.get("status") in {"partial", "error"}:
        warnings.append("memory selection completed with errors")

    while True:
        llm_calls += 1
        turn_start = perf_counter()
        if execution_mode == "fixture":
            if llm_calls > len(fixture_data["ai_messages"]):
                raise ValueError("fixture AIMessage sequence ended before a final answer")
            ai_message = deepcopy(fixture_data["ai_messages"][llm_calls - 1])
            llm_status = "success"
            llm_error = None
        else:
            llm_result = generate_ai_message(
                str(model_file),
                messages,
                tools_schema,
                mode,
                str(output_dir / "llm_calls"),
                f"llm_call_{llm_calls:03d}",
            )
            if not isinstance(llm_result, dict) or not isinstance(llm_result.get("ai_message"), dict):
                raise ValueError("B4 result must contain an ai_message object")
            ai_message = llm_result["ai_message"]
            llm_status = llm_result.get("status")
            llm_error = llm_result.get("error")
        messages.append(ai_message)
        turn = {
            "turn_index": llm_calls,
            "ai_message": ai_message,
            "llm_status": llm_status,
            "llm_error": llm_error,
            "tool_messages": [],
            "latency_ms": None,
        }
        if llm_status != "success":
            status = "llm_parse_error"
            terminal_error = {
                "type": "LLMParseError",
                "message": "B4 failed to parse the model output as a valid AIMessage JSON object.",
                "llm_call_index": llm_calls,
                "cause": llm_error,
            }
            turn["latency_ms"] = round((perf_counter() - turn_start) * 1000, 3)
            turns.append(turn)
            break
        tool_calls = ai_message.get("tool_calls", [])
        if not tool_calls:
            final_answer = ai_message["content"]
            print(f"content: {final_answer}")
            turn["latency_ms"] = round((perf_counter() - turn_start) * 1000, 3)
            turns.append(turn)
            break
        if tool_rounds >= runtime["max_turns"]:
            requested = ", ".join(call.get("name", "unknown") for call in tool_calls)
            final_answer = (
                "任务因超过最大工具调用轮次而终止，"
                f"最后一次模型仍请求调用工具：{requested}。"
            )
            status = "max_turns_exceeded"
            terminal_error = {
                "type": "MaxTurnsExceeded",
                "message": final_answer,
                "unexecuted_tool_calls": tool_calls,
            }
            turn["latency_ms"] = round((perf_counter() - turn_start) * 1000, 3)
            turns.append(turn)
            break
        if execution_mode == "fixture":
            tool_messages = _fixture_tool_messages(
                tool_calls,
                fixture_data["tool_messages"],
            )
        else:
            tool_messages = execute_tool_calls(
                tool_calls,
                str(tools_file),
                runtime["toolset"],
                str(output_dir),
            )
        tool_rounds += 1
        messages.extend(tool_messages)
        all_tool_messages.extend(tool_messages)
        turn["tool_messages"] = tool_messages
        turn["latency_ms"] = round((perf_counter() - turn_start) * 1000, 3)
        turns.append(turn)

    write_json(messages, output_dir / "messages.json")
    if execution_mode == "integrated":
        write_json(all_tool_messages, output_dir / "tool_messages.json")
    write_text(final_answer.strip() + "\n", output_dir / "final_answer.md")
    memory_save = {"requested": runtime["save_memory"], "status": "not_requested"}
    if status != "success" and runtime["save_memory"] != "none":
        memory_save = {"requested": runtime["save_memory"], "status": "skipped", "reason": status}
    trace = {
        "conversation_id": runtime["conversation_id"],
        "execution_mode": execution_mode,
        "status": status,
        "toolset": runtime["toolset"],
        "max_turns": runtime["max_turns"],
        "tool_rounds_used": tool_rounds,
        "llm_call_count": llm_calls,
        "turns": turns,
        "final_answer_path": "final_answer.md",
        "memory_save": memory_save,
        "warnings": warnings,
        "error": terminal_error,
    }
    write_json(trace, output_dir / "trace.json")

    # ★ 对话成功后保存记忆：把 messages + trace + final_answer 持久化到 B5
    saved_memory = None
    if execution_mode == "integrated" and runtime["save_memory"] != "none" and trace["status"] == "success":
        try:
            from b5_memory import save_memory

            saved_memory = save_memory(
                str(memory_file),
                runtime["conversation_id"],
                runtime["save_memory"],
                str(output_dir / "messages.json"),
                str(output_dir / "trace.json"),
                str(output_dir / "final_answer.md"),
                str(output_dir),
            )
            trace["memory_save"] = {"requested": runtime["save_memory"], "status": "success"}
        except Exception as exc:
            trace["memory_save"] = {
                "requested": runtime["save_memory"],
                "status": "error",
                "error": {"type": type(exc).__name__, "message": str(exc)},
            }
            trace["warnings"].append("memory save failed")
            if trace["status"] == "success":
                trace["status"] = "partial"
        write_json(trace, output_dir / "trace.json")

    result = {
        "conversation_id": runtime["conversation_id"],
        "execution_mode": execution_mode,
        "status": trace["status"],
        "final_answer": final_answer,
        "messages_path": str(output_dir / "messages.json"),
        "trace_path": str(output_dir / "trace.json"),
        "final_answer_path": str(output_dir / "final_answer.md"),
        "selected_memory": selected_memory,
        "saved_memory": saved_memory,
        "elapsed_ms": round((perf_counter() - started) * 1000, 3),
    }
    if execution_mode == "integrated":
        append_jsonl(
            {
                "timestamp": now_iso(),
                "conversation_id": runtime["conversation_id"],
                "execution_mode": execution_mode,
                "status": trace["status"],
                "llm_mode": mode,
                "tool_rounds_used": tool_rounds,
                "llm_call_count": llm_calls,
                "elapsed_ms": result["elapsed_ms"],
            },
            output_dir / "runtime_log.jsonl",
        )
    return result



# ═══════════════════════════════════════════════════════════════════════════════
#  ADVANCED FEATURES (PPT Slide 14)
#  ═══════════════════════════════════════════════════════════════════════════
#   Baseline imports (top of file) already provide: argparse, json, sys, uuid,
#   copy.deepcopy, pathlib.Path, time.perf_counter, typing.Any, common.io_utils.*,
#   common.logging_utils.now_iso, common.path_utils.bootstrap_project_root /
#   resolve_cli_path / resolve_from_file, common.schemas.validate_ai_message /
#   validate_messages.
#   We add local re-imports only for names the advanced block needs standalone.
# ═══════════════════════════════════════════════════════════════════════════════

# Re-import everything the advanced block needs (baseline import block was de-duped).
from common.path_utils import bootstrap_project_root, resolve_cli_path, resolve_from_file
from common.schemas import validate_messages
from common.io_utils import read_json, read_text, read_yaml, write_json, write_text, append_jsonl
from common.logging_utils import now_iso
bootstrap_project_root()

# Project root is computed from this file's own location (code/ → parent = agent/).
PROJECT_ROOT = Path(__file__).resolve().parents[1]


TOOL_RULES_SUFFIX = (
    "\n\n[RULES] Use available tools when needed. Do not invent file contents. "
    "If a tool is needed, choose exactly one tool and wait for its ToolMessage "
    "before deciding whether another tool is needed. Never request multiple tools "
    "in the same response. If tool results are provided, answer based on the tool results."
)


# ---------------------------------------------------------------------------
# Configuration helpers
# ---------------------------------------------------------------------------


def _rel(path: str) -> str:
    """Resolve a repo-relative path regardless of the caller's cwd."""
    return str((PROJECT_ROOT / path).resolve())


def _default_model_cfg() -> str:
    return _rel("configs/model.yaml")


def _default_tools_cfg() -> str:
    return _rel("configs/tools.yaml")


def _default_memory_cfg() -> str:
    return _rel("configs/memory.yaml")


def _default_outdir() -> str:
    return _rel("outputs/B1_advanced")


def _default_prompts_dir() -> str:
    return _rel("prompts/advanced")


# ★ 批量任务默认目录
DEFAULT_BATCH_INPUT_DIR = PROJECT_ROOT / "data" / "batchTask"     # 存放批处理 JSON
DEFAULT_BATCH_OUTPUT_DIR = PROJECT_ROOT / "outputs" / "batch"     # 执行结果保存


# ---------------------------------------------------------------------------
# Shared single-turn agent loop (independent lightweight implementation)
# ---------------------------------------------------------------------------


def _step_generate_ai_message(model_cfg: str, messages: list[dict], tools_schema: list[dict],
                              mode: str, artifact_dir: Path | None,
                              call_index: int,
                              model_name: str | None = None) -> dict:
    """Thin wrapper over b4 that keeps the REPL decoupled from the base module."""
    from b4_local_agent_llm import generate_ai_message as b4_generate

    return b4_generate(
        model_cfg,
        list(messages),
        tools_schema,
        mode=mode,
        artifact_dir=str(artifact_dir) if artifact_dir else None,
        artifact_stem=f"llm_call_{call_index:03d}",
        model_name=model_name,
    )


def _step_execute_tool_calls(tool_calls: list[dict], tools_cfg: str, toolset: str,
                             outdir: Path | None) -> list[dict]:
    from b3_tool_layer import execute_tool_calls as b3_execute

    return b3_execute(tool_calls, tools_cfg, toolset, str(outdir) if outdir else None)


def _extract_tool_error_for_fallback(messages: list[dict]) -> str | None:
    """从 messages 中提取最近一次 tool 执行错误信息，作为 LLM 解析失败时的兜底回答。

    场景：tool → error → LLM 看到 error 后用自然语言回复 → 解析失败 → 重试 → 仍然失败
    此时直接把 tool 的 error 信息告诉用户，好过冷冰冰的"模型输出解析失败"。
    """
    import re
    for m in reversed(messages):
        if m.get("role") != "tool":
            continue
        raw = m.get("content", "")
        if not isinstance(raw, str):
            continue
        # 尝试解析 tool result JSON，提取 error/status
        try:
            result = json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            # JSON 解析失败时用正则兜底提取 error 字段
            match = re.search(r'"error"\s*:\s*"([^"]+)"', raw)
            if match:
                return f"⚠️ 工具执行出错：{match.group(1)}"
            continue
        status = result.get("status")
        if status in ("error", "timeout"):
            tool_name = m.get("name", m.get("tool", "工具"))
            error_msg = result.get("error") or result.get("stderr") or "未知错误"
            stdout = result.get("stdout", "")
            parts = [f"⚠️ {tool_name} 执行失败：{error_msg}"]
            if stdout:
                parts.append(f"输出：{stdout[:200]}")
            if result.get("traceback"):
                # 截取最后几行 traceback
                tb_lines = result["traceback"].strip().splitlines()
                parts.append("Traceback：\n" + "\n".join(tb_lines[-3:]))
            return "\n".join(parts)
    return None


def _refresh_memory_in_system(messages: list[dict], model_cfg: str) -> None:
    """每轮 LLM 调用前：根据最近对话重新检索记忆，刷新 system message 的 [MEMORY] 段。

    与初始化注入不同，这是"动态刷新"：
      - 初始化注入：对话前，按 selected_ids 或首次 query 加载
      - 动态刷新：每轮前，按最近几轮对话内容重新检索（关键词+向量）

    这样可以跟随对话进展自动切换相关记忆，而不是始终用同一批。
    """
    if not messages or messages[0].get("role") != "system":
        return  # 没有 system 消息 → 不操作
    try:
        # 定位 memory.yaml 路径（和 model.yaml 同级目录）
        from common.path_utils import resolve_from_file
        memory_cfg = str(resolve_from_file("../configs/model.yaml", Path(model_cfg)).parent / "memory.yaml")
        from b5_memory import _memory_paths
        if not _memory_paths(memory_cfg)["index"].exists():
            return  # 记忆索引不存在 → 跳过
        # 根据最近对话检索相关记忆（混合检索：关键词 + 向量）
        from b5_memory import retrieve_memories_for_turn, build_memory_block
        max_chars = _memory_paths(memory_cfg).get("max_chars", 2000)
        budget = int(max_chars * 0.4)  # 记忆预算占总字符预算的 40%
        docs = retrieve_memories_for_turn(memory_cfg, messages, top_k=5, budget_chars=budget)
        memory_block = build_memory_block(docs)
        # 替换 system message 的 [MEMORY]...[/MEMORY] 段（保留 TEMPLATE + SUMMARY）
        old_content = messages[0].get("content", "")
        import re
        if "[MEMORY]" in old_content:
            # 已有 MEMORY 段 → 替换内容（保留段落结构）
            new_content = re.sub(
                r"\[MEMORY\].*?\[/MEMORY\]",
                f"[MEMORY]\\n{memory_block}\\n[/MEMORY]" if memory_block else "",
                old_content,
                flags=re.DOTALL,
            )
        else:
            # 没有 MEMORY 段 → 在末尾追加
            new_content = old_content + f"\n\n[MEMORY]\n{memory_block}\n[/MEMORY]" if memory_block else old_content
        messages[0]["content"] = new_content
    except Exception:
        pass  # 检索失败不阻塞主流程（记忆是锦上添花，不是必须）


def run_single_turn(messages: list[dict], model_cfg: str, tools_cfg: str, toolset: str,
                    toolset_name: str, max_turns: int, mode: str,
                    outdir: Path | None = None,
                    event_callback: Any = None,
                    events_list: list | None = None,
                    model_name: str | None = None,
                    plan_state: dict | None = None) -> dict:
    """One user question -> answer loop with tool support.

    ``messages`` is mutated in place (assistant + tool messages are appended).
    Returns a trace fragment for this turn.

    事件系统:
    - event_callback(dict): 每阶段调用，前端据此流式渲染。
      事件类型: llm_start / llm_end / tool_call / tool_result / done
    - events_list (list | None): 非 None 时，每阶段事件被 append 到此列表，
      供前端的 polling 模式读取（与 event_callback 互斥或并存均可）。
    """
    def _emit(ev: dict) -> None:
        # ★ 事件双写：存列表（polling 模式）+ 调回调（兼容 CLI 调试）
        if events_list is not None:
            events_list.append(ev)
        if event_callback is not None:
            try:
                event_callback(ev)
            except Exception:
                pass  # 流事件不应影响主循环

    messages = validate_messages(messages)

    # ★ 每轮记忆注入：根据最近对话重新检索相关记忆，替换 system 的 MEMORY 段
    _refresh_memory_in_system(messages, model_cfg)

    # ★ 即使外部没传 outdir，也创建默认目录保存 LLM 原始输出（用于诊断解析失败）
    if outdir is None:
        outdir = PROJECT_ROOT / "outputs" / "B1_advanced" / "auto_save"
    outdir = Path(outdir)  # ★ 确保是 Path 类型（外部可能传 str）
    outdir.mkdir(parents=True, exist_ok=True)

    from b3_tool_layer import get_tools_schema

    tools_schema = get_tools_schema(tools_cfg, toolset, str(outdir))

    tool_rounds_used = 0
    llm_calls = 0
    turns: list[dict] = []
    final_answer = ""
    status = "success"
    terminal_error: dict | None = None
    # ★ 重试相关状态
    retried_after_parse_error = False
    last_raw_text = None
    # ★ 规划模式：每轮只暂停一次
    _plan_pause_done = False
    # ★ retry 消息索引（运行时返回前从 messages 移除，不存入 session）
    _retry_msg_indices: set[int] = set()

    while True:
        llm_calls += 1
        turn_start = perf_counter()
        step_dir = outdir / f"turn_{len(turns) + 1:03d}"
        step_dir.mkdir(parents=True, exist_ok=True)

        # ★ 通知前端：本轮 LLM 调用开始
        _emit({"type": "llm_start", "turn": llm_calls, "max_turns": max_turns})
        # ★ 调 B4 推理
        result = _step_generate_ai_message(
            model_cfg, messages, tools_schema, mode, step_dir, llm_calls, model_name,
        )   
        ai_message = result["ai_message"]
        token_stats = result.get("token_stats")  # ← 收集 token 统计
        messages.append(ai_message)

        # ★ 尝试从 B4 保存的 artifact 中读取 raw_text（用于诊断）
        raw_output_path = step_dir / f"llm_call_{llm_calls:03d}_raw_model_output.json"
        raw_text = None
        if raw_output_path.is_file():
            try:
                raw_record = read_json(raw_output_path)
                raw_text = raw_record.get("raw_text")
            except Exception:
                pass

        # ★ 通知前端：LLM 返回 ai_message（可能带 tool_calls 或 content）
        llm_end_event = {"type": "llm_end", "turn": llm_calls,
               "ai_message": ai_message,
               "has_tool_calls": bool(ai_message.get("tool_calls"))}
        if raw_text:
            llm_end_event["raw_text"] = raw_text
        if token_stats:
            llm_end_event["token_stats"] = token_stats
        _emit(llm_end_event)

        turn: dict[str, Any] = {
            "turn_index": llm_calls,
            "ai_message": ai_message,
            "llm_status": result.get("status", "success"),
            "llm_error": result.get("error"),
            "tool_messages": [],
            "latency_ms": None,
        }
        if token_stats:
            turn["token_stats"] = token_stats
        if result.get("status") != "success":
            # ★ 解析失败 — 如果没有重试过，注入提醒并重试一次
            if not retried_after_parse_error:
                retried_after_parse_error = True
                last_raw_text = raw_text
                # ★ 找到用户原始问题（messages 中最后一个 role=user 的消息）
                last_user_msg = ""
                for m in reversed(messages):
                    if m.get("role") == "user":
                        last_user_msg = m.get("content", "")
                        break
                retry_reminder = (
                    "[系统提醒] 上一次回复不是合法 JSON，请重试。\n"
                    f"用户的问题是：{last_user_msg}\n"
                    "请严格按以下格式回复（不要输出其他文字、不要markdown代码块）：\n"
                    '{"content":"你的最终回答","tool_calls":[]}\n'
                    '或\n'
                    '{"content":"","tool_calls":[{"id":"call_001","name":"<tool_name>","args":{...}}]}'
                )
                # ★ 记录 retry 消息索引（返回前移除，不存入 session）
                _retry_msg_indices.add(len(messages))
                messages.append({"role": "user", "content": retry_reminder})
                _retry_msg_indices.add(len(messages) - 2)
                # 移除前面append的非法 ai_message，用占位符替代
                messages[len(messages) - 2] = {
                    "role": "assistant",
                    "content": "（上一次解析失败，正在重试……）",
                    "tool_calls": [],
                }
                turn["llm_status"] = "retrying"
                turn["latency_ms"] = round((perf_counter() - turn_start) * 1000, 3)
                turns.append(turn)
                # ★ 不再 emit llm_parse_retry 事件（前端不显示 retry 过程）
                continue  # 重试一轮 LLM
            # ★ 已经重试过仍然失败 → 检查是否有 tool error 可兜底
            tool_error_content = _extract_tool_error_for_fallback(messages)
            if tool_error_content:
                # 兜底：用 tool 错误信息构造最终回答，而不是显示冰冷的"解析失败"
                final_answer = tool_error_content
                status = "success"
                _emit({"type": "final_answer", "turn": llm_calls, "content": final_answer})
                turn["latency_ms"] = round((perf_counter() - turn_start) * 1000, 3)
                turns.append(turn)
                break
            # 真正的解析失败
            status = "llm_parse_error"
            terminal_error = {
                "type": "LLMParseError",
                "message": "B4 could not parse the model output as a valid AIMessage.",
                "llm_call_index": llm_calls,
                "cause": result.get("error"),
            }
            turn["latency_ms"] = round((perf_counter() - turn_start) * 1000, 3)
            turns.append(turn)
            break

        tool_calls = ai_message.get("tool_calls", [])

        # ★ 规划模式：任何 phase 只要有 tool_calls 且未确认过，都暂停等待用户确认
        #   - planning/planning_fallback: 首轮生成计划
        #   - summarize: 多轮对话后模型仍可能请求工具调用，同样需要确认
        if (plan_state and mode == "plan_execute"
                and token_stats and token_stats.get("phase") in ("planning", "planning_fallback", "summarize")
                and tool_calls and not _plan_pause_done):
            _plan_pause_done = True
            _emit({"type": "plan_proposed", "turn": llm_calls,
                   "tool_calls": tool_calls, "token_stats": token_stats})
            confirmed = plan_state["event"].wait(timeout=300)  # 5 分钟超时
            if not confirmed:
                status = "cancelled"
                terminal_error = {"type": "PlanTimeout", "message": "执行计划确认超时"}
                _emit({"type": "plan_timeout", "message": "执行计划确认超时（5 分钟），已取消。"})
                turn["latency_ms"] = round((perf_counter() - turn_start) * 1000, 3)
                turns.append(turn)
                break
            if plan_state.get("cancelled"):
                status = "cancelled"
                terminal_error = {"type": "PlanCancelled", "message": "用户取消执行计划"}
                _emit({"type": "plan_cancelled", "message": "❌ 已取消执行计划"})
                turn["latency_ms"] = round((perf_counter() - turn_start) * 1000, 3)
                turns.append(turn)
                break
            # confirm → 继续执行

        if not tool_calls:
            # ★ 最终答案到达
            final_answer = ai_message.get("content", "")
            _emit({"type": "final_answer", "turn": llm_calls, "content": final_answer})
            turn["latency_ms"] = round((perf_counter() - turn_start) * 1000, 3)
            turns.append(turn)
            break

        if tool_rounds_used >= max_turns:
            requested = ", ".join(c.get("name", "unknown") for c in tool_calls)
            final_answer = (
                "任务因超过最大工具调用轮次而终止，"
                f"最后一次模型仍请求调用工具：{requested}。"
            )
            status = "max_turns_exceeded"
            terminal_error = {
                "type": "MaxTurnsExceeded",
                "message": final_answer,
                "unexecuted_tool_calls": tool_calls,
            }
            turn["latency_ms"] = round((perf_counter() - turn_start) * 1000, 3)
            turns.append(turn)
            _emit({"type": "max_turns_exceeded", "message": final_answer})
            break

        # ★ 通知前端：即将执行的工具调用
        _emit({"type": "tool_call_start", "turn": llm_calls,
               "tool_calls": tool_calls})

        tool_messages = _step_execute_tool_calls(tool_calls, tools_cfg, toolset_name,
                                                  step_dir)
        tool_rounds_used += 1
        messages.extend(tool_messages)
        turn["tool_messages"] = tool_messages
        turn["latency_ms"] = round((perf_counter() - turn_start) * 1000, 3)
        turns.append(turn)

        # ★ 通知前端：工具结果（逐条）
        for tmsg in tool_messages:
            _emit({"type": "tool_result", "turn": llm_calls,
                   "tool_call_id": tmsg.get("tool_call_id", ""),
                   "tool_name": tmsg.get("name", "?"),
                   "result_summary": (tmsg.get("content") or "")[:200]})


    _emit({"type": "done", "status": status, "final_answer": final_answer,
           "tool_rounds_used": tool_rounds_used, "llm_calls": llm_calls})

    # 汇总本轮 token 统计（所有 LLM 调用累加）
    total_input = 0
    total_output = 0
    # ★ 移除 retry 占位消息（不存入 session）
    if _retry_msg_indices:
        messages[:] = [m for i, m in enumerate(messages) if i not in _retry_msg_indices]
        _retry_msg_indices.clear()

    for turn in turns:
        ts = turn.get("token_stats", {})
        total_input += ts.get("input_tokens", 0) or 0
        total_output += ts.get("output_tokens", 0) or 0

    return {
        "status": status,
        "tool_rounds_used": tool_rounds_used,
        "llm_calls": llm_calls,
        "final_answer": final_answer,
        "turns": turns,
        "toolset": toolset_name,
        "max_turns": max_turns,
        "error": terminal_error,
        "token_stats": {"input_tokens": total_input, "output_tokens": total_output},
    }


# ---------------------------------------------------------------------------
# Feature 5 — template pool
# ---------------------------------------------------------------------------


def _load_template_pool(prompts_dir: str) -> dict[str, str]:
    directory = resolve_cli_path(prompts_dir)
    if not directory.is_dir():
        raise FileNotFoundError(f"prompts directory not found: {directory}")
    pool: dict[str, str] = {}
    for path in sorted(directory.glob("*.txt")):
        pool[path.stem] = read_text(path).strip()
    if not pool:
        raise ValueError(f"no .txt templates found in {directory}")
    return pool


def _compose_system_content(template_text: str) -> str:
    """Ensure every template carries the mandatory tool rules."""
    stripped = template_text.strip()
    if stripped.endswith(TOOL_RULES_SUFFIX.strip()):
        return stripped
    return stripped + TOOL_RULES_SUFFIX


def _ensure_system_placeholder(messages: list[dict]) -> None:
    if not messages or messages[0].get("role") != "system":
        messages.insert(0, {"role": "system", "content": ""})


# ---------------------------------------------------------------------------
# Feature 4 — message compression
# ---------------------------------------------------------------------------


def _build_compressible_text(messages: list[dict], keep_last_k_turns: int) -> str:
    """将旧消息（除最近 keep_last_k_turns 轮外）转为纯文本，供 LLM 做摘要。
    """
    # 找到所有 user 消息的索引，用于确定切割点
    user_indices = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    if len(user_indices) <= keep_last_k_turns:
        return ""  # 轮次不够 → 不压缩，返回空
    # 切割点：只取切割点之前的消息来压缩，之后的保留原样
    cut_until = user_indices[-keep_last_k_turns]
    segments: list[str] = []
    for message in messages[:cut_until]:
        role = message.get("role", "?")
        content = message.get("content", "") or ""
        if content.strip():
            segments.append(f"[{role}]\n{content.strip()}")
        # 工具调用转成单行可读文本（不保留原始 JSON）
        for call in message.get("tool_calls", []):
            name = call.get("name", "?")
            args = json.dumps(call.get("args", {}), ensure_ascii=False)
            segments.append(f"  tool_call -> {name}({args})")
    return "\n\n".join(segments)


def _summarise_messages_off(compressible_text: str) -> str:
    """⚠ 已废弃：禁止使用截断 fallback（即不调 LLM、直接截断旧消息）。

    之前版本在 LLM 不可用时走这条路，现在改为：LLM 失败就不压缩。
    保留此函数仅供外部兼容，_summarise_messages_with_model 不再调用它。
    如果意外走到这里，返回空字符串让上层感知失败。
    """
    return ""


def _clean_summary_text(text: str) -> str:
    """清理 LLM 生成的摘要文本，剥掉模板废话、标签行、空行，返回干净文本。

    LLM 摘要时常说废话，比如：
      "对话中未包含任何旧的摘要信息。"
      "本次对话仅包含用户问候。"
      "无工具调用、文件内容或计算结果。"
    这些对压缩毫无意义，必须删掉。
    """
    import re
    # 匹配 LLM 常用的废话模板句（正则覆盖多种措辞变体）
    junk_patterns = [
        r'对话中未?包含任何?旧的摘要信息[。，]?',
        r'本次对话仅包含[^。，]*[。，]?',
        r'无工具调用、文件内容或计算结果[。]?',
        r'对话中无旧摘要信息[。]?',
        r'^[\[【].*?[\]】]\s*$',  # 单独一行是标签（如 【对话摘要】）
    ]
    result = text.strip()
    for p in junk_patterns:
        result = re.sub(p, '', result, flags=re.MULTILINE)
    # 剥掉残留的 [user]/[assistant]/[tool]/[tool_call]/【 开头的行
    lines = [l for l in result.split('\n') if l.strip() and not l.strip().startswith(('[user]', '[assistant]', '[tool]', '[tool_call]', '【'))]
    return '\n'.join(lines).strip()

# Web compress 路径的专属函数
def _format_messages_for_summary(messages: list[dict]) -> str:
    """把消息列表格式化为易读的纯文本，给 LLM 做摘要用。

    ★ 关键设计：不保留原始 JSON，只提取每条消息的关键信息。
      原因：Qwen 看到大段 JSON 会被搞晕，输出质量下降。
      输出示例：
        用户：1+1=?
        工具调用：计算 1+1
        工具结果：calculator = 2
        助手：结果是 2
    """
    lines = []
    for msg in messages:
        role = msg.get("role", "?")
        if role == "system":
            continue  # system 消息不参与摘要
        content = (msg.get("content", "") or "").strip()

        # ★ 工具结果（role == "tool"）：不保留原始 JSON → 提取一句话摘要
        if role == "tool":
            summary = _summarize_tool_result(msg.get("content", ""))
            if summary:
                lines.append(f"工具结果：{summary}")
            continue

        # 用户 / 助手消息：截断到 100 字符（摘要不需要全文）
        if content:
            preview = content[:100].replace('\n', ' ')
            label = "用户" if role == "user" else "助手"
            lines.append(f"{label}：{preview}")
        # 工具调用转成中文描述（比 JSON 友好得多）
        for call in msg.get("tool_calls", []):
            name = call.get("name", "?")
            args = call.get("args", {})
            if name == "file_reader":
                lines.append(f"工具调用：读取 {args.get('path', '?')}")
            elif name == "calculator":
                lines.append(f"工具调用：计算 {args.get('expression', '?')}")
            elif name == "local_file_search":
                lines.append(f"工具调用：搜索 {args.get('query', '?')}")
            else:
                lines.append(f"工具调用：{name}")
    return "\n".join(lines)


def _summarize_tool_result(content: str) -> str:
    """从 B3 返回的 tool result JSON 中提取一句话摘要。

    B3 返回的 tool result 是 JSON 格式，直接给 LLM 看会很乱。
    这个函数把它转成人类可读的短句，例如：
        calculator → "calculator = 42"
        file_reader → "file_reader 读取1523字符"
        local_file_search → "local_file_search 找到3条/10文件"
    失败时返回：
        "calculator 失败: division by zero"
    """
    try:
        data = json.loads(content)
    except (json.JSONDecodeError, TypeError):
        # JSON 解析失败 → 截取前 50 字符兜底
        return content[:50].replace('\n', ' ') if content else ""
    if not isinstance(data, dict):
        return str(data)[:50]
    name = data.get("skill_name", "?")
    status = data.get("status", "?")
    # 错误状态 → 返回错误信息
    if status == "error":
        err = data.get("error") or {}
        msg = isinstance(err, dict) and err.get("message", "") or str(err)
        return f"{name} 失败: {msg}"[:50]
    output = data.get("output")
    # 按工具类型提取关键数字
    if name == "local_file_search":
        total = output.get("total_files", 0) if isinstance(output, dict) else 0
        results = output.get("results", []) if isinstance(output, dict) else []
        return f"{name} 找到{len(results)}条/{total}文件"
    if name == "file_reader":
        n = output.get("num_chars", 0) if isinstance(output, dict) else 0
        return f"{name} 读取{n}字符"
    if name == "calculator":
        r = output.get("result", "") if isinstance(output, dict) else ""
        return f"{name} = {r}"
    if name == "table_analyzer":
        rows = output.get("num_rows", 0) if isinstance(output, dict) else 0
        return f"{name} {rows}行数据"
    # 其他工具 → 返回 "工具名 状态"
    return f"{name} {status}"


def _extract_summary_from_raw(raw_text: str) -> str:
    """兜底：从 Qwen 的原始输出中正则提取摘要文本（即使 JSON 解析失败）。

    场景：LLM 返回的 JSON 格式错乱（多了引号、少了逗号等），
    _parse_model_output 解析失败时，用这个函数硬抠 content 字段。

    策略：
      1. 正则匹配 "content":"..." 的内部内容（兼容转义和未转义引号）
      2. 失败则从 content 之后取到 tool_calls 之前的所有内容
    """
    import re
    # 策略1：匹配 "content":"..." ,"tool_calls" 之间的内容
    match = re.search(r'"content"\s*:\s*"(.*?)"\s*,\s*"tool_calls"', raw_text, re.DOTALL)
    if match:
        content = match.group(1)
        # 反转义（\" → "，\n → 换行）
        content = content.replace('\\"', '"').replace("\\n", "\n").replace("\\t", "\t").strip()
        if content:
            return content[:500]
    # 策略2：取 content 之后的所有内容，截到 tool_calls 之前
    match2 = re.search(r'"content"\s*:\s*"(.*)', raw_text, re.DOTALL)
    if match2:
        tail = match2.group(1)
        # 取到 ,"tool_calls" 为止
        end = tail.rfind(',"tool_calls"')
        if end > 0:
            content = tail[:end].strip().strip('"')
        else:
            content = tail.strip().strip('"}')
        content = content.replace('\\"', '"').replace("\\n", "\n").strip()
        if content and len(content) > 10:
            return content[:500]
    return ""  # 都失败 → 返回空


def _summarise_messages_with_model(model_cfg: str, old_summary: str,
                                    messages_plain_text: str, mode: str) -> str:
    """调 Qwen（B4）把旧摘要 + 新对话合并为一条摘要。

    输入已经是干净的纯文本（程序预处理过），模型只做摘要合并。
    不给模型任何工具（tools_schema=[]），让它只能输出 content。

    流程：
      1. 清理旧摘要里的废话
      2. 构造 prompt（区分"有旧摘要"和"无旧摘要"两种情况）
      3. 调 B4 生成摘要，最多重试 3 次
      4. 都失败 → 兜底：从原始输出中正则提取 content
    """
    from b4_local_agent_llm import _load_model_config, _prompt_json_generate, _parse_model_output
    from common.io_utils import read_yaml

    config_path, config = _load_model_config(model_cfg)

    # ★ 清理旧摘要里的模板废话（如"对话中未包含任何旧的摘要信息"）
    clean_old = _clean_summary_text(old_summary)

    # ★ 构造 prompt：区分"有旧摘要"和"无旧摘要"两种情况
    if clean_old:
        # 有旧摘要 → 合并旧摘要 + 新对话
        user_content = (
            f"以下是之前的对话摘要：\n{clean_old}\n\n"
            f"以下是后续的新对话记录：\n{messages_plain_text}\n\n"
            f"请将旧摘要和新对话合并为一条连贯的中文段落摘要，"
            f"保留关键信息（工具调用结果中的具体数据、文件内容片段、计算结果）。"
            f"将摘要内容放入 content 字段。"
        )
    else:
        # 无旧摘要（首次压缩）→ 直接压缩新对话
        user_content = (
            f"以下是对话记录：\n{messages_plain_text}\n\n"
            f"请把上面的对话压缩为一条连贯的中文段落摘要，"
            f"保留关键信息（工具调用结果中的具体数据、文件内容片段、计算结果）。"
            f"将摘要内容放入 content 字段。"
        )

    # ★ 调 B4 生成摘要，最多重试 3 次
    last_error = ""
    last_raw = ""
    for attempt in range(3):
        gen_result = _prompt_json_generate(
            config_path, config,
            [{"role": "system", "content": "你是一个对话摘要助手。请将摘要内容放入 JSON 的 content 字段中返回。tool_calls 留空数组。注意：content 中的双引号必须转义为 \\\"。"},
             {"role": "user", "content": user_content}],
            tools_schema=[],  # ★ 不给工具 → 模型只能输出 content
        )
        raw_text = gen_result["text"]
        last_raw = raw_text
        try:
            parsed, ai_message = _parse_model_output(raw_text)
            result = ai_message.get("content", "").strip()
            if result:
                result = _clean_summary_text(result)  # 清理 LLM 的废话
                if result and len(result) > 10:       # 摘要太短视为失败
                    return result
            last_error = f"content too short or empty after parse"
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
        import time
        time.sleep(0.5 * (attempt + 1))  # 退避：0.5s, 1s, 1.5s
    # ★ 3 次都失败 → 兜底：从原始输出中正则硬抠 content（不抛异常）
    fallback = _extract_summary_from_raw(last_raw)
    if fallback:
        print(f"[compress] 使用兜底提取: {fallback[:80]}...", file=sys.stderr, flush=True)
        return fallback
    raise RuntimeError(f"LLM 摘要生成失败（重试 3 次）: {last_error}")


def _compress_messages(messages: list[dict], model_cfg: str | None, keep_last_k: int,
                        mode: str, turn_index: int, *,
                        _parse_segmented_system=None,
                        _build_segmented_system=None) -> dict[str, Any]:
    """压缩旧消息为一条摘要，原地修改 messages。

    ★ 对齐 Web (_run_compress) 的逻辑：
      - 解析 system 分段（template / rules / memory / summary）
      - 格式化旧消息 → 合并旧摘要 → 一个合并后的 system
      - 保留最近 k 轮不压缩

    Returns:
        dict: {"compressed": True/False, "original_count": ..., ...}
    """
    _ensure_system_placeholder(messages)

    # ① 按 user 消息计算切割点
    user_indices = [i for i, m in enumerate(messages) if m.get("role") == "user"]
    keep_last_k_turns = keep_last_k
    if len(user_indices) <= keep_last_k_turns:
        return {"compressed": False, "reason": "not_enough_history", "message_count": len(messages)}
    cut_until = user_indices[-keep_last_k_turns] if keep_last_k_turns > 0 else len(messages)

    # ② 解析 system 分段（外部注入，避免循环导入）
    old_segments = {"template": "", "rules": "", "memory": "", "summary": ""}
    if messages[0].get("role") == "system" and _parse_segmented_system is not None:
        old_segments = _parse_segmented_system(messages[0].get("content") or "")

    # ③ 格式化旧消息为纯文本
    messages_plain_text = _format_messages_for_summary(messages[:cut_until])
    if not messages_plain_text.strip():
        return {"compressed": False, "reason": "empty", "message_count": len(messages)}

    # ④ LLM 可用才压缩
    if mode == "off" or model_cfg is None:
        return {"compressed": False, "reason": "llm_not_available", "message_count": len(messages)}

    # ⑤ 调 LLM 合并旧摘要 + 新消息
    try:
        summary_text = _summarise_messages_with_model(
            model_cfg, old_segments["summary"], messages_plain_text, mode,
        )
    except Exception as exc:
        print(f"[compress] LLM 失败: {exc}", file=sys.stderr, flush=True)
        return {"compressed": False, "reason": "llm_failed", "error": str(exc),
                "message_count": len(messages)}

    # ⑥ 重建一个合并后的 system（template + memory 原样保留，只替换 summary）
    if _build_segmented_system is not None:
        merged_system_content = _build_segmented_system(
            template_text=old_segments["template"],
            rules_text=old_segments["rules"],
            memory_block=old_segments["memory"],
            summary_text=summary_text,
        )
    else:
        # 没有分段工具时（纯 CLI 独立调用）退化为简单格式
        merged_system_content = (
            f"{messages[0].get('content','')}\n\n"
            f"[历史对话摘要]\n{summary_text}"
        )

    new_messages: list[dict] = [{"role": "system", "content": merged_system_content}]

    # ⑦ 追加保留区间内的消息，过滤掉畸形 assistant
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

    # ⑧ 原地替换
    old_count = len(messages)
    messages.clear()
    messages.extend(new_messages)
    return {
        "compressed": True,
        "original_count": old_count,
        "compressed_count": len(messages),
        "cut_until": cut_until,
        "summary_text": summary_text,   # ★ 返回摘要供上层展示
        "summary_chars": len(summary_text),
        "mode": mode,
        "timestamp": now_iso(),
    }


def _maybe_auto_compress(state: dict[str, Any]) -> dict[str, Any] | None:
    """自动压缩检查：当用户轮次达到阈值时自动触发压缩。

    仅 CLI REPL 模式使用（Web 是用户手动点按钮）。
    阈值由 state["summary_threshold_turns"] 控制（默认 10 轮）。
    未到阈值返回 None，到了返回压缩结果。
    """
    threshold = int(state["summary_threshold_turns"])
    user_turns = sum(1 for m in state["messages"] if m.get("role") == "user")
    if user_turns < threshold:
        return None  # 轮次不够 → 不压缩
    return _run_compress(state)


def _run_compress(state: dict[str, Any]) -> dict[str, Any]:
    """从 REPL state 中提取参数，调用 _compress_messages。

    CLI 专用：注入分段解析函数，使 CLI 压缩与 Web 对齐。
    （Web 侧 agent_api._run_compress 直接用本地的同名函数）
    """
    from agent_api import _parse_segmented_system, _build_segmented_system
    return _compress_messages(
        state["messages"],
        state.get("summary_model_cfg"),
        int(state["compress_keep_last"]),   # 保留最近几轮不压缩
        state["summary_mode"],
        state["turn_index"],
        _parse_segmented_system=_parse_segmented_system,
        _build_segmented_system=_build_segmented_system,
    )


# ---------------------------------------------------------------------------
# Feature 3 — batch (shared core for CLI + Web)
# ---------------------------------------------------------------------------


def run_batch_tasks(tasks: list[dict], model_cfg: str, tools_cfg: str,
                    model_name: str | None = None) -> dict:
    """执行批量任务的核心函数（CLI 和 Web 共用）。

    每个任务独立执行（各自全新的 messages），串行遍历。
    返回结构化结果，不打印、不持久化——调用方决定怎么展示。

    Args:
        tasks: 任务列表，每项是一个 dict，至少包含 "user_input"。
               可选字段：task_id, toolset, max_turns, repl_mode, model_name。
               任务级 model_name 优先级高于全局 model_name。
        model_cfg: model.yaml 路径。
        tools_cfg: tools.yaml 路径。
        model_name: 全局默认模型名（任务未指定时使用）。

    Returns:
        {
            "batch_size": int,
            "all_ok": bool,
            "records": [
                {
                    "task_id": str,
                    "status": "success" | "error" | ...,
                    "tool_rounds": int,
                    "elapsed_ms": float,
                    "error": str | None,
                    "final_answer": str,
                },
                ...
            ]
        }
    """
    started = perf_counter()
    records: list[dict] = []

    # ★ 串行遍历每个任务（任务之间独立、互不污染）
    for idx, task_item in enumerate(tasks):
        item_start = perf_counter()
        task_id = task_item.get("task_id", f"task_{idx + 1:03d}")
        # 任务级 model_name 优先级高于全局 model_name
        task_model = task_item.get("model_name") or model_name
        try:
            # 构造独立的 messages（每个任务全新的对话，不共享历史）
            msgs: list[dict] = [
                {"role": "system",
                 "content": "You are a local tool-using agent. Use available tools when needed."},
                {"role": "user", "content": task_item.get("user_input", "").strip()},
            ]
            # ★ 调 run_single_turn 执行单个任务（同文件，直接调用）
            summary = run_single_turn(
                msgs,
                model_cfg,
                tools_cfg,
                task_item.get("toolset", "basic_tools"),
                task_item.get("toolset", "basic_tools"),
                int(task_item.get("max_turns", 3)),
                task_item.get("repl_mode", "prompt_json"),
                None,          # outdir=None → 不写诊断文件
                None,          # event_callback=None → 不走回调
                None,          # events_list=None → 不产事件流
                task_model,    # B4 model_name（任务级覆盖全局）
            )
            # 成功：收集结果
            records.append({
                "task_id": task_id,
                "status": summary.get("status", "unknown"),
                "tool_rounds": summary.get("tool_rounds_used"),
                "elapsed_ms": round((perf_counter() - item_start) * 1000, 1),
                "error": None,
                "final_answer": summary.get("final_answer") or "",
            })
        except Exception as exc:
            # 失败：记录错误信息，不中断其他任务
            records.append({
                "task_id": task_id,
                "status": "error",
                "tool_rounds": None,
                "elapsed_ms": round((perf_counter() - item_start) * 1000, 1),
                "error": f"{type(exc).__name__}: {exc}",
                "final_answer_preview": None,
            })

    # 汇总：all_ok = 没有任何任务出错
    return {
        "batch_size": len(records),
        "all_ok": all(r["status"] != "error" for r in records),
        "elapsed_ms": round((perf_counter() - started) * 1000, 1),
        "records": records,
    }


# ---------------------------------------------------------------------------
# Feature 4 — checkpoint （CLI & Web 共享）
# ---------------------------------------------------------------------------
#  session 落盘格式（Web / CLI 统一）：
#    outputs/sessions/{session_id}/session.json
#  内部状态字段（以 _ 开头，如 _plan_state）不序列化。
#  字段取 Web + CLI 两边超集，保证跨端恢复信息不丢。

SESSION_ROOT = PROJECT_ROOT / "outputs" / "sessions"


# ★ 运行时内部字段 — 不序列化到 session.json（不可 JSON 化或恢复时重建）
_INTERNAL_SESSION_FIELDS = {"template_pool"}


def _filter_internal_fields(d: dict[str, Any]) -> dict[str, Any]:
    """过滤内部状态字段（_ 开头 或 在 _INTERNAL_SESSION_FIELDS 中）。"""
    return {k: v for k, v in d.items()
            if not k.startswith("_") and k not in _INTERNAL_SESSION_FIELDS}


def save_session(state: dict[str, Any], root: Path = SESSION_ROOT) -> Path:
    """保存 session 到磁盘：root/{session_id}/session.json。

    自动过滤内部字段（_ 开头），更新 updated_at 时间戳。
    Web / CLI 共用，保证落盘格式一致、可跨端恢复。
    """
    sid = state.get("session_id")
    if not sid:
        raise ValueError("state has no session_id — cannot save")
    outdir = root / sid
    outdir.mkdir(parents=True, exist_ok=True)
    payload = _filter_internal_fields(state)
    payload["updated_at"] = now_iso()
    path = outdir / "session.json"
    write_json(payload, path)
    return path


def list_sessions(root: Path = SESSION_ROOT) -> list[dict[str, Any]]:
    """扫描 session 目录 → 按修改时间倒序返回摘要列表。

    每项包含：session_id, updated_at, message_count, model_name,
    turn_index, toolset, path。
    """
    if not root.is_dir():
        return []
    results: list[dict[str, Any]] = []
    for d in root.iterdir():
        if not d.is_dir():
            continue
        meta = d / "session.json"
        if not meta.is_file():
            continue
        try:
            rec = read_json(meta)
        except Exception:
            continue
        results.append({
            "session_id": rec.get("session_id", d.name),
            "updated_at": rec.get("updated_at"),
            "message_count": len(rec.get("messages", [])),
            "model_name": rec.get("model_name"),
            "turn_index": rec.get("turn_index"),
            "toolset": rec.get("toolset"),
            "path": str(meta),
        })
    results.sort(key=lambda r: r.get("updated_at") or "", reverse=True)
    return results


def load_session(sid: str, root: Path = SESSION_ROOT) -> dict[str, Any]:
    """读取指定 session → 补齐 Web/CLI 公共字段后返回。

    缺字段给默认值，保证返回的 dict 无论来自 Web 还是 CLI 都有完整字段。
    """
    meta = root / sid / "session.json"
    if not meta.is_file():
        raise FileNotFoundError(f"session '{sid}' not found at {meta}")
    payload = read_json(meta)
    if not isinstance(payload.get("messages"), list):
        raise ValueError(f"session '{sid}' has no valid messages")
    # ★ 补齐 Web 路由所需字段（旧 CLI session 可能没有）
    payload.setdefault("status", "idle")
    payload.setdefault("toolset", "basic_tools")
    payload.setdefault("max_turns", 5)
    payload.setdefault("repl_mode", "mock")
    payload.setdefault("token_stats", {"input": 0, "output": 0})
    payload.setdefault("created_at", now_iso())
    payload.setdefault("updated_at", payload.get("created_at"))
    payload.setdefault("model_name", None)
    payload.setdefault("template_key", None)
    return payload


def delete_session(sid: str, root: Path = SESSION_ROOT) -> bool:
    """删除 session 目录，返回是否成功。"""
    import shutil
    d = root / sid
    if d.is_dir():
        shutil.rmtree(d, ignore_errors=True)
        return True
    return False


# ---------------------------------------------------------------------------
# Feature 3 — batch
# ---------------------------------------------------------------------------


def run_batch(batch_input_path: str, model_cfg: str, tools_cfg: str, memory_cfg: str,
              outdir: str, parallel: int = 1) -> dict:
    """CLI 批量任务入口：读 JSON 文件 → 调 run_batch_tasks 执行 → 终端输出 + 落盘。

    CLI 专用：负责文件读取、打印进度、写 batch_summary.jsonl。
    核心执行逻辑已抽到 run_batch_tasks，本函数只做 CLI 包装。
    """

    # ① 读 JSON 文件，解析任务列表 + 共享默认配置
    batch_file = resolve_cli_path(batch_input_path)
    payload = read_json(batch_file)
    raw_tasks = payload.get("tasks") or []
    if not isinstance(raw_tasks, list) or not raw_tasks:
        raise ValueError("batch_input.json must contain a non-empty 'tasks' list")
    defaults = payload.get("shared_defaults") or {}
    if not isinstance(defaults, dict):
        defaults = {}

    output_dir = resolve_cli_path(outdir)
    output_dir.mkdir(parents=True, exist_ok=True)

    started = perf_counter()
    print(f"[batch] running {len(raw_tasks)} tasks (parallel={parallel})")

    # ② 调公共核心函数执行批量任务（和 Web 共用同一套逻辑）
    result = run_batch_tasks(
        tasks=raw_tasks,
        model_cfg=model_cfg,
        tools_cfg=tools_cfg,
        model_name=defaults.get("model_name"),
    )

    # ③ CLI 专属：打印每个任务的结果 + 写 batch_summary.jsonl
    for record in result["records"]:
        record["timestamp"] = now_iso()
        append_jsonl(record, output_dir / "batch_summary.jsonl")
        print(f"\n[batch] === {record['task_id']} ===")
        print(f"  status: {record['status']}")
        print(f"  tool_rounds: {record.get('tool_rounds')}")
        print(f"  elapsed_ms: {record.get('elapsed_ms')}")
        if record.get("final_answer"):
            print(f"  final_answer: {record['final_answer'][:80]}...")

    # ④ 返回汇总（status="success" 表示全部成功，"partial" 表示有失败）
    return {
        "batch_size": result["batch_size"],
        "status": "success" if result["all_ok"] else "partial",
        "elapsed_ms": round((perf_counter() - started) * 1000, 3),
        "summary_path": str(output_dir / "batch_summary.jsonl"),
        "records": result["records"],
    }


# ---------------------------------------------------------------------------
# Feature 1 + 4 + 5 — multi-turn REPL
# ---------------------------------------------------------------------------


REPL_BANNER = """
╔══════════════════════════════════════════════════════════════════════╗
║   B1 Advanced REPL — interactive multi-turn agent                  ║
║   Commands: /help  /switch <tpl>  /add <text>  /compress          ║
║             /batchtask  /resume  /status  /quit (/q)                ║
╚══════════════════════════════════════════════════════════════════════╝
"""


def _print_help(template_keys: list[str]) -> None:
    print("""
Inline commands:
  /help                      show this message
  /status                    current session info
  /quit       (/q)           exit the REPL
  /switch <template>         switch system prompt to one of: """ + ", ".join(template_keys) + """
  /add <extra text>          append extra text to current system prompt
  /compress                  compress history via LLM summary
  /save                      save a checkpoint immediately
Any other line is treated as a user question.
""")


def _init_repl_state(first_input_path: str, model_cfg: str, tools_cfg: str, memory_cfg: str,
                     prompts_dir: str, repl_mode: str, summary_mode: str,
                     compress_keep_last: int, summary_threshold_turns: int,
                     session_id: str | None, summary_model_cfg: str | None = None,
                     resume_payload: dict | None = None) -> dict[str, Any]:
    if resume_payload is not None:
        return _init_state_from_session(resume_payload, repl_mode, summary_mode,
                                        compress_keep_last, summary_threshold_turns,
                                        summary_model_cfg)
    pool = _load_template_pool(prompts_dir)

    # ★ 无 --input 时：直接启动空对话（用户从 stdin 输入第一问）
    if not first_input_path:
        default_key = "tool_master" if "tool_master" in pool else next(iter(pool))
        system_content = _compose_system_content(pool[default_key])
        messages: list[dict] = [{"role": "system", "content": system_content}]
        return {
            "session_id": session_id or f"repl_{uuid.uuid4().hex[:8]}",
            "conversation_id": "repl_session",
            "messages": messages,
            "system_prompt_key": default_key,
            "turn_index": 0,
            "toolset": "basic_tools",
            "max_turns": 5,
            "selected_memory_ids": [],
            "use_global_memory": False,
            "model_cfg": model_cfg, "tools_cfg": tools_cfg, "memory_cfg": memory_cfg,
            "summary_mode": summary_mode,
            "compress_keep_last": compress_keep_last,
            "summary_threshold_turns": summary_threshold_turns,
            "prompts_dir": prompts_dir,
            "summary_model_cfg": summary_model_cfg,
            "template_pool": pool,
            "model_name": None, "repl_mode": repl_mode, "status": "idle",
            "token_stats": {"input": 0, "output": 0},
            "template_key": None, "created_at": now_iso(),
        }

    # ★ 有 --input 时：从 runtime_input.json 预载第一问
    first_input = resolve_cli_path(first_input_path)
    runtime_input = read_json(first_input)

    from b5_memory import load_memory

    effective_ids = list(runtime_input.get("selected_memory_ids", []))
    if not effective_ids and (runtime_input.get("user_input") or "").strip():
        try:
            effective_ids = auto_select_memories(memory_cfg, runtime_input.get("user_input", ""))
        except Exception:
            pass

    selected_memory = load_memory(
        memory_cfg, effective_ids,
        runtime_input.get("use_global_memory", False),
        runtime_input.get("user_input", ""), None,
    )

    default_key = "tool_master" if "tool_master" in pool else next(iter(pool))
    base_template = pool[default_key]
    system_content = _compose_system_content(base_template)
    memory_docs = selected_memory.get("selected_memory_docs", [])
    if memory_docs:
        memory_block = "\n\n".join(
            f'<memory id="{d["memory_id"]}" type="{d["memory_type"]}">\n{d["content"].strip()}\n</memory>'
            for d in memory_docs
        )
        system_content = f"{system_content}\n\n{memory_block}"

    messages = [
        {"role": "system", "content": system_content},
        {"role": "user", "content": runtime_input.get("user_input", "").strip()},
    ]

    return {
        "session_id": session_id or f"repl_{uuid.uuid4().hex[:8]}",
        "conversation_id": runtime_input.get("conversation_id", "repl_session"),
        "messages": messages,
        "system_prompt_key": default_key,
        "turn_index": 0,
        "toolset": runtime_input.get("toolset", "basic_tools"),
        "max_turns": int(runtime_input.get("max_turns", 5)),
        "selected_memory_ids": runtime_input.get("selected_memory_ids", []),
        "use_global_memory": runtime_input.get("use_global_memory", False),
        "model_cfg": model_cfg,
        "tools_cfg": tools_cfg,
        "memory_cfg": memory_cfg,
        "summary_mode": summary_mode,
        "compress_keep_last": compress_keep_last,
        "summary_threshold_turns": summary_threshold_turns,
        "prompts_dir": prompts_dir,
        "summary_model_cfg": summary_model_cfg,
        "template_pool": pool,
        # ★ Web 超集字段 — 新建 session 时也初始化，保证 save_session 落盘完整
        #   注意：status / template_key / repl_mode / token_stats 是 Web 路由
        #   get_session / _run_turn 必须读取的字段，CLI 恢复后切到 Web 不能崩
        "model_name": None,
        "repl_mode": repl_mode,
        "status": "idle",
        "token_stats": {"input": 0, "output": 0},
        "template_key": None,
        "created_at": now_iso(),
    }


def _resume_session_interactive(root: Path = SESSION_ROOT) -> dict[str, Any]:
    """交互式选择历史 session → 返回加载的 payload（空 dict 表示新建）。"""
    sessions = list_sessions(root)
    if not sessions:
        print("  no previous sessions found — starting fresh.")
        return {}
    print("\n  ── previous sessions ──")
    for i, s in enumerate(sessions, 1):
        updated = s.get("updated_at", "?")[:19]
        model = s.get("model_name") or "-"
        print(f"    {i}. [{s['session_id'][:8]}]  "
              f"msgs={s.get('message_count', 0)}  "
              f"model={model}  "
              f"updated={updated}")
    print(f"    0. start a new session\n")
    while True:
        try:
            raw = input("  select [0-{}]: ".format(len(sessions))).strip()
        except (KeyboardInterrupt, EOFError):
            print()
            return {}
        if not raw:
            continue
        try:
            idx = int(raw)
        except ValueError:
            print("  please enter a number.")
            continue
        if idx == 0:
            return {}
        if 1 <= idx <= len(sessions):
            sid = sessions[idx - 1]["session_id"]
            print(f"  restoring session {sid[:8]} ...")
            return load_session(sid, root)
        print(f"  out of range — enter 0-{len(sessions)}.")
    return {}


def _init_state_from_session(payload: dict, repl_mode: str, summary_mode: str,
                              compress_keep_last: int, summary_threshold_turns: int,
                              summary_model_cfg: str | None) -> dict[str, Any]:
    """从 session.json payload 重建 REPL state（兼容 Web / CLI 两端格式）。"""
    pool = _load_template_pool(payload.get("prompts_dir", str(resolve_cli_path("../prompts/advanced"))))
    messages = payload.get("messages", [])
    if not messages:
        raise ValueError("session has no messages")
    validate_messages(messages)
    return {
        "session_id": payload.get("session_id", f"resumed_{uuid.uuid4().hex[:8]}"),
        "conversation_id": payload.get("conversation_id", "repl_resumed"),
        "messages": messages,
        "system_prompt_key": payload.get("system_prompt_key", ""),
        "turn_index": int(payload.get("turn_index", 0)),
        "toolset": payload.get("toolset", "basic_tools"),
        "max_turns": int(payload.get("max_turns", 5)),
        "selected_memory_ids": payload.get("selected_memory_ids", []),
        "use_global_memory": payload.get("use_global_memory", False),
        "model_cfg": payload.get("model_cfg", str(resolve_cli_path("../configs/model.yaml"))),
        "tools_cfg": payload.get("tools_cfg", str(resolve_cli_path("../configs/tools.yaml"))),
        "memory_cfg": payload.get("memory_cfg", str(resolve_cli_path("../configs/memory.yaml"))),
        "summary_mode": summary_mode,
        "compress_keep_last": compress_keep_last,
        "summary_threshold_turns": summary_threshold_turns,
        "prompts_dir": payload.get("prompts_dir", str(resolve_cli_path("../prompts/advanced"))),
        "summary_model_cfg": summary_model_cfg,
        "template_pool": pool,
        # ★ Web 超集字段（CLI 旧 checkpoint 没有这些，给出默认值）
        "model_name": payload.get("model_name"),
        "repl_mode": payload.get("repl_mode"),
        "status": payload.get("status", "idle"),
        "token_stats": payload.get("token_stats", {"input": 0, "output": 0}),
        "template_key": payload.get("template_key"),
        "created_at": payload.get("created_at", now_iso()),
    }


def _handle_command(state: dict[str, Any], raw: str, outdir: Path | None) -> tuple[str, bool] | dict | None:
    """处理 REPL 内置命令。

    返回值：
      (action, dirty) 元组：
        action = 'quit'  → 退出 REPL
        action = 'handled' → 已消费本行（不是问题）
        action = 'batch_handled' → /batchtask 专属
        dirty = True → 命令修改了 state，调用方需要 save_session
      None → 不是命令，当作用户问题继续
      dict → 恢复的 session payload（run_repl 用 _init_state_from_session 替换 state）
    """
    stripped = raw.strip()
    if not stripped.startswith("/") or stripped == "/":
        return None  # not a command

    parts = stripped.split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1] if len(parts) > 1 else ""

    if cmd in ("/quit", "/q"):
        return "quit"

    if cmd == "/help":
        _print_help(sorted(state["template_pool"].keys()))
        return ("handled", False)

    if cmd == "/status":
        print(f"  session  : {state['session_id']}")
        print(f"  template : {state['system_prompt_key']}")
        print(f"  turn     : {state['turn_index']}")
        print(f"  messages : {len(state['messages'])} entries")
        print(f"  toolset  : {state['toolset']}")
        print(f"  templates: {', '.join(sorted(state['template_pool'].keys()))}")
        return ("handled", False)

    if cmd == "/switch":
        target = arg.strip()
        if not target:
            print("  usage: /switch <template_name>")
            return ("handled", False)
        if target not in state["template_pool"]:
            print(f"  unknown template '{target}'. Available: {', '.join(sorted(state['template_pool'].keys()))}")
            return ("handled", False)
        if state["messages"] and state["messages"][0].get("role") == "system":
            state["messages"][0]["content"] = _compose_system_content(state["template_pool"][target])
        else:
            state["messages"].insert(0, {
                "role": "system",
                "content": _compose_system_content(state["template_pool"][target]),
            })
        state["system_prompt_key"] = target
        print(f"  switched system prompt to [{target}]")
        return ("handled", True)

    if cmd == "/add":
        if not arg.strip():
            print("  usage: /add <extra text>")
            return ("handled", False)
        _ensure_system_placeholder(state["messages"])
        state["messages"][0]["content"] = state["messages"][0].get("content", "") + "\n\n" + arg.strip()
        print("  appended extra text to current system prompt")
        return ("handled", True)

    if cmd == "/compress":
        print("  compressing history ...")
        record = _run_compress(state)
        if record.get("compressed"):
            print(f"  compressed: {record['original_count']} -> {record['compressed_count']} messages")
            # ★ 显示压缩后的摘要内容
            summary = record.get("summary_text", "")
            if summary:
                # 只取前两行或前 200 字符作为预览，终端不刷屏
                preview = summary[:300].replace("\n", " ")
                print(f"  summary: {preview}{'…' if len(summary) > 300 else ''}")
        else:
            print(f"  skipped: {record.get('reason')}")
        return ("handled", True)

    # ★ /batchtask — 独立的批量任务，不碰 session state
    if cmd == "/batchtask":
        _handle_batchtask_command(state)
        return "batch_handled"  # 直接返回，不调 or

    # ★ /resume — 在 REPL 内交互式切换到历史 session
    if cmd == "/resume":
        sessions = list_sessions()
        if not sessions:
            print("  no previous sessions found.")
            return ("handled", False)
        print("\n  ── previous sessions ──")
        for i, s in enumerate(sessions, 1):
            updated = s.get("updated_at", "?")[:19]
            model = s.get("model_name") or "-"
            # 标记当前 session
            marker = " (*)" if s["session_id"] == state["session_id"] else ""
            print(f"    {i}. [{s['session_id'][:8]}]  "
                  f"msgs={s['message_count']}  model={model}  "
                  f"updated={updated}{marker}")
        print()
        while True:
            try:
                raw_idx = input("  select (empty to cancel): ").strip()
            except (KeyboardInterrupt, EOFError):
                print()
                return ("handled", False)
            if not raw_idx:
                print("  cancelled.")
                return ("handled", False)
            try:
                idx = int(raw_idx)
            except ValueError:
                print("  please enter a number.")
                continue
            if 1 <= idx <= len(sessions):
                sid = sessions[idx - 1]["session_id"]
                print(f"  restoring session {sid[:8]} ...")
                return load_session(sid)  # ← dict → run_repl 替换 state
            print(f"  out of range — enter 1-{len(sessions)}.")

    print(f"  unknown command '{cmd}'. Type /help for the list.")
    return ("handled", False)


def _handle_batchtask_command(state: dict[str, Any]) -> str:
    """处理 /batchtask 命令：列文件 → 选 → 执行 → 保存结果。

    完全独立，不修改 state/session，结果保存到 DEFAULT_BATCH_OUTPUT_DIR。
    """
    import glob

    input_dir = Path(DEFAULT_BATCH_INPUT_DIR)
    output_dir = Path(DEFAULT_BATCH_OUTPUT_DIR)
    output_dir.mkdir(parents=True, exist_ok=True)

    # ① 列输入目录所有 .json 文件
    files = sorted(input_dir.glob("*.json"))
    if not files:
        print(f"  no batch JSON files found in {input_dir}")
        return ("handled", True)

    print(f"\n  ── batch task files ({input_dir}) ──")
    for i, fp in enumerate(files, 1):
        # 显示任务数
        try:
            raw = read_json(fp)
            task_count = len(raw.get("tasks") or [])
        except Exception:
            task_count = "?"
        print(f"    {i}. {fp.name}  ({task_count} tasks)")
    print()

    # ② 用户选择
    while True:
        try:
            raw_idx = input("  select (empty to cancel): ").strip()
        except (KeyboardInterrupt, EOFError):
            print()
            return ("handled", True)
        if not raw_idx:
            print("  cancelled.")
            return ("handled", True)
        try:
            idx = int(raw_idx)
        except ValueError:
            print("  please enter a number.")
            continue
        if 1 <= idx <= len(files):
            break
        print(f"  out of range — enter 1-{len(files)}.")

    chosen = files[idx - 1]

    # ③ 读任务列表
    try:
        payload = read_json(chosen)
        tasks = payload.get("tasks") or []
        if not tasks:
            print(f"  no tasks in {chosen.name}")
            return ("handled", True)
    except Exception as exc:
        print(f"  failed to read {chosen.name}: {exc}")
        return ("handled", True)

    defaults = payload.get("shared_defaults") or {}
    print(f"\n[batch] running {len(tasks)} tasks from {chosen.name}")

    # ④ 直接调公共核心（不经过 run_batch 的 CLI 包装）
    model_cfg = str(PROJECT_ROOT / "configs" / "model.yaml")
    tools_cfg = str(PROJECT_ROOT / "configs" / "tools.yaml")
    result = run_batch_tasks(tasks, model_cfg, tools_cfg,
                             model_name=defaults.get("model_name"))

    # ⑤ 终端打印
    for rec in result["records"]:
        print(f"\n[batch] === {rec['task_id']} ===")
        print(f"  status: {rec['status']}")
        print(f"  tool_rounds: {rec.get('tool_rounds')}")
        print(f"  elapsed_ms: {rec.get('elapsed_ms')}")
        if rec.get("final_answer"):
            print(f"  final_answer: {rec['final_answer'][:120]}")
        if rec.get("error"):
            print(f"  error: {rec['error']}")

    # ⑥ 保存 JSON 结果到输出目录
    timestamp = now_iso().replace(":", "-").replace("+", "_")[:19]
    out_path = output_dir / f"{chosen.stem}_{timestamp}.json"
    write_json({
        "source_file": chosen.name,
        "batch_size": result["batch_size"],
        "all_ok": result["all_ok"],
        "elapsed_ms": result["elapsed_ms"],
        "records": result["records"],
    }, out_path)
    print(f"\n  result saved to {out_path}")
    return ("handled", True)


def _run_one_turn(state: dict[str, Any], user_input: str, repl_mode: str,
                  outdir: Path | None) -> str:
    if outdir is not None:
        outdir.mkdir(parents=True, exist_ok=True)
        (outdir / "repl_history").mkdir(parents=True, exist_ok=True)

    state["messages"].append({"role": "user", "content": user_input})

    turn_start = perf_counter()
    summary = run_single_turn(
        state["messages"],
        state["model_cfg"],
        state["tools_cfg"],
        state["toolset"],
        state["toolset"],
        state["max_turns"],
        repl_mode,
        None,
    )
    latency_ms = round((perf_counter() - turn_start) * 1000, 3)
    final_answer = summary.get("final_answer", "")

    state["turn_index"] += 1

    # ★ 累积 token_stats（基于本轮 summary 的 token 用量增量）
    ts = summary.get("token_stats") or {}
    if isinstance(ts, dict) and ts:
        prev = state.get("token_stats") or {}
        state["token_stats"] = {
            "input": int(prev.get("input", 0)) + int(ts.get("input", 0)),
            "output": int(prev.get("output", 0)) + int(ts.get("output", 0)),
        }

    if outdir is not None:
        turn_dir = outdir / f"turn_{state['turn_index']:03d}"
        write_json(state["messages"], turn_dir / "messages.json")
        write_text(final_answer.strip() + "\n", turn_dir / "final_answer.md")
        write_json(summary, turn_dir / "trace.json")
        append_jsonl({
            "timestamp": now_iso(),
            "session_id": state["session_id"],
            "turn_index": state["turn_index"],
            "latency_ms": latency_ms,
            "status": summary["status"],
            "tool_rounds": summary["tool_rounds_used"],
            "llm_calls": summary["llm_calls"],
            "final_answer_preview": final_answer[:120],
        }, outdir / "session_log.jsonl")

    print(f"\n[agent]\n{final_answer}\n")
    return final_answer


def _print_history(state: dict[str, Any]) -> None:
    """恢复 session 后回显历史消息摘要。"""
    print("\n  ── conversation history ──")
    for m in state["messages"]:
        role = m.get("role", "?")
        content = (m.get("content") or "").strip()
        if not content and role == "assistant":
            content = "(tool_call)"
        elif not content:
            continue
        # 只显示 user / assistant 的概要，跳过 system
        if role == "system":
            continue
        preview = content[:80].replace("\n", " ")
        tag = "user" if role == "user" else "agent"
        print(f"    [{tag}] {preview}{'…' if len(content) > 80 else ''}")
    print()


def run_repl(first_input_path: str, model_cfg: str, tools_cfg: str, memory_cfg: str,
             prompts_dir: str, repl_mode: str, summary_mode: str,
             compress_keep_last: int, summary_threshold_turns: int,
             outdir: str | None, session_id: str | None = None,
             summary_model_cfg: str | None = None,
             resume: bool = False,
             resume_session_id: str | None = None) -> dict:
    """多轮 REPL 主循环。

    --resume            → 交互式选择历史 session 恢复
    --resume-session-id → 直接指定 sid 恢复（跳过交互选择）
    否则 → 新建 session（每轮自动保存到 SESSION_ROOT/{sid}/session.json）。
    """
    out = resolve_cli_path(outdir) if outdir else None

    # ① 恢复 or 新建
    payload: dict | None = None
    if resume_session_id:
        # 直接指定 sid
        payload = load_session(resume_session_id)
        print(f"[restore] loaded session {resume_session_id[:8]}")
    elif resume:
        # 交互式选择
        payload = _resume_session_interactive()

    state = _init_repl_state(
        first_input_path, model_cfg, tools_cfg, memory_cfg,
        prompts_dir, repl_mode, summary_mode, compress_keep_last,
        summary_threshold_turns, session_id, summary_model_cfg,
        resume_payload=payload,
    )

    is_resumed = payload is not None
    if is_resumed:
        print(f"[restore] resumed — turn {state['turn_index']}, {len(state['messages'])} messages")
        _print_history(state)
    else:
        print(REPL_BANNER)

    # First question: consume the pre-seeded user message from runtime_input.json (if any)
    first_turn_done = False
    if state["messages"] and state["messages"][-1].get("role") == "user":
        first_question = state["messages"][-1].get("content", "").strip()
        if first_question:
            print(f"[user] {first_question}")
            state["messages"].pop()
            try:
                _run_one_turn(state, first_question, repl_mode, out)
                _maybe_auto_compress(state)
                save_session(state)  # 每轮结束自动保存
            except KeyboardInterrupt:
                save_session(state)
                raise
            first_turn_done = True

    while True:
        try:
            raw = input("\n[you] ").strip()
        except KeyboardInterrupt:
            path = save_session(state)
            print(f"\n  session saved to {path} (resume with --resume)")
            raise
        except EOFError:
            raw = "/quit"

        if not raw:
            continue
        _cmd_result = _handle_command(state, raw, out)
        if isinstance(_cmd_result, dict):
            # ★ /resume → payload dict → 替换 state
            state = _init_state_from_session(
                _cmd_result, repl_mode, summary_mode,
                compress_keep_last, summary_threshold_turns, summary_model_cfg,
            )
            print(f"[restore] switched to session {state['session_id'][:8]} — "
                  f"turn {state['turn_index']}, {len(state['messages'])} messages")
            _print_history(state)
            save_session(state)
            continue
        if _cmd_result == "batch_handled":
            # ★ /batchtask → 完全独立，不保存 session
            continue
        # ★ 普通命令 → 元组 (action, dirty)
        action, dirty = _cmd_result
        if action == "quit":
            path = save_session(state)
            print(f"  session saved to {path}")
            return {"status": "quit", "session_id": state["session_id"], "turns": state["turn_index"]}
        if action == "handled" and dirty:
            # ★ 命令修改了 state（如 /compress /switch /add）→ 落盘
            save_session(state)
            continue
        # ("handled", False) → 没改 state（/help /status）→ 不保存

        try:
            _run_one_turn(state, raw, repl_mode, out)
            _maybe_auto_compress(state)
            save_session(state)  # 每轮结束自动保存
        except KeyboardInterrupt:
            path = save_session(state)
            print(f"\n  session saved to {path}")
            return {"status": "interrupted", "session_id": state["session_id"], "turns": state["turn_index"]}

    return {"status": "done", "session_id": state["session_id"], "turns": state["turn_index"]}



def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local Agent message and tool loop.",
                                     formatter_class=argparse.RawDescriptionHelpFormatter,
                                     epilog="""\
examples:
  # 多轮对话（mock 模式，全部默认配置）
  python %(prog)s --mode advanced_repl

  # 接上次继续（交互式选择 session）
  python %(prog)s --mode advanced_repl --resume

  # 用真实 LLM 对话
  python %(prog)s --mode advanced_repl --repl_mode prompt_json

  # 批量任务
  python %(prog)s --mode advanced_batch --batch_input ../cli_io/batch_input.json
""")
    parser.add_argument("--input", required=False, default=None,
                        help="baseline: runtime_input.json (required); advanced_repl: optional base config")
    # ★ config 参数全部可选 — 默认走 configs/ 目录（和 Web 共享配置）
    parser.add_argument("--tools_config", default=None,
                        help="default: configs/tools.yaml")
    parser.add_argument("--memory_config", default=None,
                        help="default: configs/memory.yaml")
    parser.add_argument("--model_config", default=None,
                        help="default: configs/model.yaml")
    parser.add_argument("--outdir", default=None,
                        help="default: outputs/B1_advanced")
    parser.add_argument("--llm_mode", choices=["mock", "prompt_json"], default=None,
                        help="baseline integrated mode only")

    # ── Advanced modes ─────────────────────────────────────────────────────────
    parser.add_argument("--mode", default="baseline", required=False,
                        choices=["baseline", "advanced_repl", "advanced_batch"],
                        help="baseline = single-question; advanced_repl = multi-turn; advanced_batch = batch tasks")
    parser.add_argument("--session_id", default=None,
                        help="advanced_repl: stable id used for new session naming")
    parser.add_argument("--resume", action="store_true", default=False,
                        help="advanced_repl: 交互式选择历史 session 恢复")
    parser.add_argument("--resume-session-id", default=None,
                        help="advanced_repl: 直接指定 sid 恢复（跳过交互选择）")
    parser.add_argument("--batch_input", default=None,
                        help='advanced_batch: path to JSON {"tasks": [...]}')
    parser.add_argument("--parallel", default=1, type=int,
                        help="advanced_batch: number of concurrent tasks (default 1)")
    parser.add_argument("--repl_mode", default="prompt_json",
                        choices=["mock", "prompt_json"],
                        help="advanced_repl: per-turn LLM mode (default prompt_json = 真模型)")
    parser.add_argument("--summary_mode", default="prompt_json",
                        choices=["off", "mock", "prompt_json"],
                        help="advanced_repl: history-compression mode (default prompt_json)")
    parser.add_argument("--summary_model_cfg", default=None,
                        help="advanced_repl: separate model config for summarisation")
    parser.add_argument("--prompts_dir", default=None,
                        help="advanced_repl: system-prompt templates dir (default prompts/advanced)")
    parser.add_argument("--compress_keep_last", default=0, type=int,
                        help="advanced_repl: turns to preserve verbatim when compressing (default 0 = 全部压成一条摘要)")
    parser.add_argument("--summary_threshold_turns", default=6, type=int,
                        help="advanced_repl: auto-compress once user turns >= this value (default 6)")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        # ── Baseline (Slide 8) ───────────────────────────────────────────────────
        if args.mode == "baseline":
            if not args.input:
                raise ValueError("--input is required for baseline mode")
            result = run_agent(
                str(resolve_cli_path(args.input)),
                str(resolve_cli_path(args.tools_config)) if args.tools_config else None,
                str(resolve_cli_path(args.memory_config)) if args.memory_config else None,
                str(resolve_cli_path(args.model_config)) if args.model_config else None,
                str(resolve_cli_path(args.outdir)),
                args.llm_mode,
            )
            print(result["final_answer_path"])
            return 0

        # ── Advanced modes (Slide 14) ────────────────────────────────────────────
        model_cfg = args.model_config or _default_model_cfg()
        tools_cfg = args.tools_config or _default_tools_cfg()
        memory_cfg = args.memory_config or _default_memory_cfg()
        outdir = args.outdir or _default_outdir()
        prompts_dir = args.prompts_dir or _default_prompts_dir()

        if args.mode == "advanced_repl":
            summary_model_cfg = None
            if args.summary_mode == "prompt_json":
                summary_model_cfg = args.summary_model_cfg or model_cfg
            result = run_repl(
                args.input,
                model_cfg, tools_cfg, memory_cfg, prompts_dir,
                args.repl_mode, args.summary_mode,
                args.compress_keep_last, args.summary_threshold_turns,
                outdir, session_id=args.session_id,
                summary_model_cfg=summary_model_cfg,
                resume=args.resume,
                resume_session_id=args.resume_session_id,
            )
            print(f"\n[done] session finished — turns={result['turns']}")
            return 0

        if args.mode == "advanced_batch":
            if not args.batch_input:
                raise ValueError("--batch_input is required for advanced_batch mode")
            result = run_batch(
                args.batch_input, model_cfg, tools_cfg, memory_cfg, outdir,
                parallel=args.parallel,
            )
            print(f"\n[done] batch finished — {result['batch_size']} tasks in {result['elapsed_ms']}ms")
            return 0

        print(f"fatal: unknown mode {args.mode}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n  interrupted.", file=sys.stderr)
        return 130
    except Exception as exc:
        print(f"fatal: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
