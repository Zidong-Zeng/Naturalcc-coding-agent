from __future__ import annotations

import argparse
import json
import re
import sys
from copy import deepcopy
from pathlib import Path
from time import perf_counter
from typing import Any

from common.io_utils import append_jsonl, read_json, read_yaml, write_json
from common.logging_utils import now_iso
from common.path_utils import resolve_cli_path, resolve_from_file
from common.schemas import make_ai_message, validate_ai_message, validate_messages


PARSE_ERROR_CONTENT = "模型输出解析失败，无法生成有效工具调用或最终回答。"
_MODEL_CACHE: dict[tuple[str, ...], tuple[Any, Any]] = {}


def _load_model_config(model_config: str | Path) -> tuple[Path, dict]:
    path = Path(model_config).resolve()
    config = read_yaml(path)
    if not isinstance(config, dict):
        raise ValueError("model.yaml must contain an object")
    config = _resolve_env_vars(config)
    return path, config


def _resolve_env_vars(obj: Any) -> Any:
    if isinstance(obj, str):
        import os
        return os.path.expandvars(obj)
    elif isinstance(obj, dict):
        return {k: _resolve_env_vars(v) for k, v in obj.items()}
    elif isinstance(obj, list):
        return [_resolve_env_vars(item) for item in obj]
    return obj


def _artifact_paths(artifact_dir: str | Path, stem: str | None) -> tuple[Path, Path, Path]:
    directory = Path(artifact_dir)
    prefix = f"{stem}_" if stem else ""
    return (
        directory / f"{prefix}raw_model_output.json",
        directory / f"{prefix}ai_message.json",
        directory / "llm_run_log.jsonl",
    )


def _extract_tool_result(message: dict) -> dict:
    try:
        result = json.loads(message["content"])
    except (KeyError, json.JSONDecodeError, TypeError) as exc:
        raise ValueError("ToolMessage content is not a SkillResult JSON string") from exc
    if not isinstance(result, dict):
        raise ValueError("ToolMessage content must decode to an object")
    return result


def _three_points(text: str) -> list[str]:
    parts = [part.strip(" \t\r\n。") for part in re.split(r"\n+|(?<=[。！？!?])", text) if part.strip()]
    points = []
    for part in parts:
        if part not in points:
            points.append(part)
        if len(points) == 3:
            break
    while len(points) < 3:
        points.append("工具结果未提供更多可提取内容")
    return points


def _mock_generate(messages: list[dict]) -> dict:
    tool_messages = [message for message in messages if message.get("role") == "tool"]
    if not tool_messages:
        return make_ai_message(
            "",
            [
                {
                    "id": "call_001",
                    "name": "file_reader",
                    "args": {"path": "docs/agent_intro.txt", "max_chars": 2000},
                },
                {
                    "id": "call_002",
                    "name": "local_file_search",
                    "args": {"query": "agent", "directory": "docs"},
                },
            ],
        )
    failed_calls = []
    all_results = []
    for tm in tool_messages:
        result = _extract_tool_result(tm)
        if tm.get("status") != "success" or result.get("status") != "success":
            error = result.get("error") or {}
            detail = error.get("message", "未知工具错误") if isinstance(error, dict) else str(error)
            failed_calls.append(f"{tm.get('name', 'unknown')}: {detail}")
        else:
            output = result.get("output") or {}
            content = output.get("content") if isinstance(output, dict) else None
            if isinstance(content, str) and content.strip():
                all_results.append(content)
    if failed_calls:
        return make_ai_message(f"部分工具调用失败：{'; '.join(failed_calls)}", [])
    combined_content = "\n\n".join(all_results)
    if not combined_content.strip():
        combined_content = "工具执行结果为空"
    points = _three_points(combined_content)
    answer = "综合分析结果如下：\n" + "\n".join(f"{index}. {point}" for index, point in enumerate(points, 1))
    return make_ai_message(answer, [])


def _parse_tool_calls_fragment(raw_text: str, original_error: json.JSONDecodeError) -> dict:
    markers = ['"tool_calls":[', '\\"tool_calls\\":[']
    marker_index = -1
    marker = ""
    for item in markers:
        marker_index = raw_text.find(item)
        if marker_index != -1:
            marker = item
            break
    if marker_index == -1:
        raise original_error
    array_start = marker_index + marker.index("[")
    array_end = raw_text.rfind("]")
    if array_end < array_start:
        raise ValueError("model output contains tool_calls marker but no closing array")
    array_text = raw_text[array_start : array_end + 1]
    try:
        tool_calls = json.loads(array_text)
    except json.JSONDecodeError:
        tool_calls = json.loads(array_text.replace('\\"', '"'))
    if not isinstance(tool_calls, list):
        raise original_error
    return {"content": "", "tool_calls": tool_calls}


def _parse_markdown_code_block(raw_text: str) -> dict:
    """从 Markdown 代码块中提取 JSON（如 ```json {...} ```）。"""
    match = re.search(r"```(?:json)?\s*\n?(.*?)\n?```", raw_text, re.DOTALL)
    if not match:
        raise ValueError("no markdown code block found")
    return json.loads(match.group(1).strip())


def _parse_json_with_backtick_tail(raw_text: str, original_error: json.JSONDecodeError) -> dict:
    text = raw_text.strip()
    try:
        candidate, end_index = json.JSONDecoder().raw_decode(text)
    except json.JSONDecodeError:
        raise original_error
    trailing = text[end_index:].strip()
    if trailing and set(trailing) <= {"`"}:
        return candidate
    raise original_error


def _parse_qwen_xml_tool_call(raw_text: str) -> dict:
    text = raw_text.strip()
    xml_match = re.search(r"<tool_call>.*?</tool_call>", text, re.DOTALL)
    if not xml_match:
        raise ValueError("not a Qwen tool call XML format")
    xml_content = xml_match.group()
    matches = re.findall(r"<function=(\w+)>(.*?)</function>", xml_content, re.DOTALL)
    if not matches:
        raise ValueError("no function found in XML")
    tool_calls = []
    call_id = 1
    for func_name, params_content in matches:
        params = {}
        param_matches = re.findall(r"<parameter=(\w+)>(.*?)</parameter>", params_content, re.DOTALL)
        for param_name, param_value in param_matches:
            params[param_name] = param_value.strip()
        tool_calls.append({
            "id": f"call_{call_id:03d}",
            "name": func_name,
            "args": params,
        })
        call_id += 1
    return {"content": "", "tool_calls": tool_calls}


def _candidate_to_message(candidate: dict) -> tuple[dict, dict]:
    if not isinstance(candidate, dict):
        raise ValueError("model output JSON must be an object")
    expected_keys = {"content", "tool_calls"}
    unknown_keys = set[Any](candidate) - expected_keys
    if unknown_keys:
        raise ValueError(f"model output JSON contains unknown keys: {', '.join(sorted(unknown_keys))}")
    message = {
        "role": "assistant",
        "content": candidate.get("content", ""),
        "tool_calls": candidate.get("tool_calls", []),
    }
    validate_ai_message(message)
    # ★ 允许同时有 content 和 tool_calls（模型可能在调工具时附带说明文字）
    parsed_candidate = {"content": message["content"], "tool_calls": message["tool_calls"]}
    return parsed_candidate, message


def _parse_model_output(raw_text: str) -> tuple[dict, dict]:
    try:
        candidate = json.loads(raw_text.strip())
    except json.JSONDecodeError as exc:
        try:
            candidate = _parse_json_with_backtick_tail(raw_text, exc)
        except json.JSONDecodeError:
            try:
                candidate = _parse_tool_calls_fragment(raw_text, exc)
            except (json.JSONDecodeError, ValueError):
                try:
                    candidate = _parse_qwen_xml_tool_call(raw_text)
                except ValueError:
                    try:
                        candidate = _parse_markdown_code_block(raw_text)
                    except (json.JSONDecodeError, ValueError):
                        return {"content": raw_text.strip(), "tool_calls": []}, make_ai_message(raw_text.strip(), [])
    return _candidate_to_message(candidate)


def _dtype_value(torch_module: Any, configured: str) -> Any:
    if configured == "auto":
        return "auto"
    mapping = {
        "bfloat16": torch_module.bfloat16,
        "float16": torch_module.float16,
        "float32": torch_module.float32,
    }
    if configured not in mapping:
        raise ValueError(f"unsupported torch_dtype: {configured}")
    return mapping[configured]


def _model_cache_key(
    model_path: Path,
    tokenizer_path: Path,
    local_only: bool,
    trust_remote_code: bool,
    dtype: Any,
    device_map: Any,
    max_memory: Any,
) -> tuple[str, ...]:
    try:
        device_map_key = json.dumps(device_map, sort_keys=True, separators=(",", ":"))
    except TypeError:
        device_map_key = repr(device_map)
    try:
        max_memory_key = json.dumps(max_memory, sort_keys=True, separators=(",", ":"))
    except TypeError:
        max_memory_key = repr(max_memory)
    return (
        str(model_path),
        str(tokenizer_path),
        str(local_only),
        str(trust_remote_code),
        str(dtype),
        device_map_key,
        max_memory_key,
    )


def _load_model_bundle(
    auto_model: Any,
    auto_tokenizer: Any,
    model_path: Path,
    tokenizer_path: Path,
    local_only: bool,
    trust_remote_code: bool,
    dtype: Any,
    device_map: Any,
    max_memory: Any,
) -> tuple[Any, Any]:
    cache_key = _model_cache_key(
        model_path,
        tokenizer_path,
        local_only,
        trust_remote_code,
        dtype,
        device_map,
        max_memory,
    )
    cached = _MODEL_CACHE.get(cache_key)
    if cached is not None:
        print("model_cache=hit", file=sys.stderr, flush=True)
        return cached

    print("model_cache=miss", file=sys.stderr, flush=True)
    tokenizer = auto_tokenizer.from_pretrained(
        str(tokenizer_path),
        local_files_only=local_only,
        trust_remote_code=trust_remote_code,
    )
    model = auto_model.from_pretrained(
        str(model_path),
        local_files_only=local_only,
        trust_remote_code=trust_remote_code,
        dtype=dtype,
        device_map=device_map,
        max_memory=max_memory,
    )
    _MODEL_CACHE[cache_key] = (tokenizer, model)
    return tokenizer, model


def _get_model_config_by_name(config: dict, model_name: str | None) -> dict:
    if model_name is None:
        return config.get("model", {})
    models = config.get("models", {})
    if model_name not in models:
        raise ValueError(f"model '{model_name}' not found in config")
    return models[model_name]


def _build_prompt_messages(messages: list[dict], tools_schema: list[dict], tool_calling_mode: str = "prompt_json") -> list[dict]:
    prompt_messages = deepcopy(messages)
    if tool_calling_mode == "native_tool":
        for message in reversed(prompt_messages):
            if message.get("role") == "user":
                message["content"] += "\n\nPlease generate appropriate tool calls based on available tools."
                break
        return prompt_messages
    if tool_calling_mode == "prompt_json":
        format_instruction = (
            "IMPORTANT OUTPUT FORMAT:\n"
            "You must return exactly one valid JSON object.\n"
            "Do not output markdown.\n"
            "Do not output explanations.\n"
            "Do not output code fences or backticks.\n"
            'The first output character must be "{" and the last output character must be "}".\n\n'
            "Valid schema A:\n"
            '{"content":"final answer text","tool_calls":[]}\n\n'
            "Valid schema B (single tool call):\n"
            '{"content":"","tool_calls":[{"id":"call_001","name":"file_reader",'
            '"args":{"path":"docs/agent_intro.txt","max_chars":2000}}]}\n\n'
            "Valid schema C (multiple tool calls):\n"
            '{"content":"","tool_calls":[{"id":"call_001","name":"file_reader",'
            '"args":{"path":"docs/agent_intro.txt","max_chars":2000}},{"id":"call_002",'
            '"name":"local_file_search","args":{"query":"agent","directory":"docs"}}]}\n\n'
            "The top-level keys must be exactly:\n"
            "- content: string\n"
            "- tool_calls: array\n\n"
            "You can include multiple tool calls in a single response.\n"
            "Never put tool_calls inside content.\n"
            'Never output {"content":"tool_calls": ...}.'
        )
        envelope_reminder = (
            "IMPORTANT OUTPUT FORMAT: Output the JSON object now. "
            'Your first output character must be "{" and your last output character must be "}". '
            "Never output a backtick, Markdown, a code block, an explanation, or text outside the JSON. "
            'Use exactly the top-level keys "content" (string) and "tool_calls" (array). '
            "Choose exactly one schema: final content with an empty tool_calls array, or empty content with tool calls. "
            "You can include multiple tool calls in a single response. "
            'Never put tool_calls inside content. Never output {"content":"tool_calls": ...}.'
        )
        system_instruction = (
            "\n\nAvailable tools JSON schema:\n"
            + json.dumps(tools_schema, ensure_ascii=False)
            + "\n"
            + format_instruction
        )
        if prompt_messages and prompt_messages[0].get("role") == "system":
            prompt_messages[0]["content"] += system_instruction
        else:
            prompt_messages.insert(0, {"role": "system", "content": system_instruction.strip()})

        for message in reversed(prompt_messages):
            if message.get("role") == "user":
                message["content"] += "\n\n" + envelope_reminder
                break
        last_is_tool = prompt_messages[-1].get("role") == "tool"
        if last_is_tool:
            tool_messages = [m for m in prompt_messages if m.get("role") == "tool"]
            completed_tools = set(tm.get("name") for tm in tool_messages)
            envelope_reminder += (
                f" The latest messages contain {len(tool_messages)} tool results. "
                f"Completed tools: {', '.join(completed_tools)}. "
                'If all requested information is available, answer with schema A now and set "tool_calls" to exactly []. '
                "Do not repeat completed tool calls."
            )
            prompt_messages.append(
                {
                    "role": "user",
                    "content": envelope_reminder,
                }
            )
    return prompt_messages


def _prompt_json_generate(config_path: Path, config: dict, messages: list[dict], tools_schema: list[dict], model_name: str | None = None) -> dict:
    try:
        import torch
        from transformers import AutoModelForCausalLM, AutoTokenizer
    except ImportError as exc:
        raise RuntimeError("prompt_json mode requires requirements-llm.txt") from exc
    model_config = _get_model_config_by_name(config, model_name)
    generation_config = config.get("generation", {})
    tool_calling_mode = config.get("tool_calling", {}).get("mode", "prompt_json")
    model_setting = model_config.get("model_name_or_path")
    tokenizer_setting = model_config.get("tokenizer_name_or_path", model_setting)
    if not isinstance(model_setting, str) or not isinstance(tokenizer_setting, str):
        raise ValueError("model_name_or_path and tokenizer_name_or_path are required")
    model_path = resolve_from_file(model_setting, config_path)
    tokenizer_path = resolve_from_file(tokenizer_setting, config_path)
    if not model_path.exists() or not tokenizer_path.exists():
        raise FileNotFoundError(f"local model path does not exist: {model_path}")
    local_only = bool(model_config.get("local_files_only", True))
    trust_remote_code = bool(model_config.get("trust_remote_code", False))
    dtype = _dtype_value(torch, str(model_config.get("torch_dtype", "auto")))
    tokenizer, model = _load_model_bundle(
        AutoModelForCausalLM,
        AutoTokenizer,
        model_path,
        tokenizer_path,
        local_only,
        trust_remote_code,
        dtype,
        model_config.get("device_map", "auto"),
        model_config.get("max_memory"),
    )
    prompt_messages = _build_prompt_messages(messages, tools_schema, tool_calling_mode)
    qwen_tools = None
    if tool_calling_mode == "native_tool":
        qwen_tools = []
        for tool in tools_schema:
            if tool.get("type") == "function" and "function" in tool:
                func = tool["function"]
                qwen_tools.append({
                    "name": func["name"],
                    "description": func["description"],
                    "parameters": func.get("parameters", {}),
                })
    inputs = tokenizer.apply_chat_template(
        prompt_messages,
        tokenize=True,
        add_generation_prompt=True,
        return_tensors="pt",
        return_dict=True,
        enable_thinking=False,
        tools=qwen_tools,
    )
    device = next(model.parameters()).device
    inputs = inputs.to(device)
    input_length = inputs["input_ids"].shape[-1]
    options = {
        "max_new_tokens": int(generation_config.get("max_new_tokens", 1024)),
        "do_sample": bool(generation_config.get("do_sample", False)),
        "temperature": float(generation_config.get("temperature", 0)),
        "top_p": float(generation_config.get("top_p", 1)),
    }
    with torch.no_grad():
        generated = model.generate(**inputs, **options)
    new_tokens = generated[0][input_length:]
    output_text = tokenizer.decode(new_tokens, skip_special_tokens=True)
    return {
        "text": output_text,
        "input_tokens": input_length,
        "output_tokens": len(new_tokens),
        "model_name": model_setting,
    }


def _openai_generate(config_path: Path, model_config: dict, messages: list[dict], tools_schema: list[dict]) -> dict:
    api_base = model_config.get("api_base", "https://api.openai.com/v1")
    api_key = model_config.get("api_key")
    model_setting = model_config.get("model", "gpt-4o-mini")

    if not api_key:
        raise ValueError("api_key is required for openai backend")

    try:
        from openai import OpenAI
    except ImportError as exc:
        raise RuntimeError("openai backend requires openai package") from exc

    client = OpenAI(
        api_key=api_key,
        base_url=api_base,
    )

    # ★ 转换 tool_calls 为 OpenAI function 格式
    def _convert_tool_calls_to_openai(tcs):
        result = []
        for tc in tcs:
            if "function" in tc:
                result.append(tc)  # 已经是 OpenAI 格式
            else:
                args = tc.get("args", {})
                if isinstance(args, dict):
                    import json as _json
                    args = _json.dumps(args, ensure_ascii=False)
                result.append({
                    "id": tc.get("id", "call_001"),
                    "type": "function",
                    "function": {
                        "name": tc.get("name", ""),
                        "arguments": args,
                    }
                })
        return result

    tools = []
    for tool in tools_schema:
        if tool.get("type") == "function" and "function" in tool:
            func = tool["function"]
            tools.append({
                "type": "function",
                "function": {
                    "name": func["name"],
                    "description": func["description"],
                    "parameters": func.get("parameters", {}),
                }
            })

    # ★ 转换为 OpenAI 标准格式（DeepSeek 兼容）
    # - tool 消息：去掉 name / status 字段（只保留 role, tool_call_id, content）
    # - assistant 消息：tool_calls=[] → 省略字段；tool_calls 非空 → 转为 function 格式
    clean_messages = []
    for msg in messages:
        m = dict(msg)
        role = m.get("role")
        if role == "tool":
            # OpenAI 标准 tool 消息只保留 3 个字段
            clean = {"role": "tool", "tool_call_id": m.get("tool_call_id", "")}
            if m.get("content") is not None:
                clean["content"] = m["content"]
            clean_messages.append(clean)
        elif role == "assistant":
            tcs = m.get("tool_calls", [])
            if not tcs:
                # 空 tool_calls → 省略字段
                m.pop("tool_calls", None)
            else:
                # 转为 OpenAI function 格式
                m["tool_calls"] = _convert_tool_calls_to_openai(tcs)
            clean_messages.append(m)
        else:
            clean_messages.append(m)

    response = client.chat.completions.create(
        model=model_setting,
        messages=clean_messages,
        tools=tools if tools else None,
        tool_choice="auto",
        max_tokens=1024,
    )

    choice = response.choices[0]
    message = choice.message

    content = message.content or ""
    tool_calls = []

    if message.tool_calls:
        # 模型返回了原生 OpenAI tool_calls
        for tool_call in message.tool_calls:
            tool_calls.append({
                "id": tool_call.id,
                "name": tool_call.function.name,
                "args": json.loads(tool_call.function.arguments),
            })
    else:
        # ★ 模型可能把 tool_calls 以 JSON 格式嵌入在 content 中（如 plan_execute 模式）
        # 尝试从 content 中提取（支持纯 JSON 或 Markdown 代码块）
        inner = None
        try:
            inner = json.loads(content)
        except (json.JSONDecodeError, TypeError):
            try:
                inner = _parse_markdown_code_block(content)
            except (json.JSONDecodeError, ValueError):
                pass
        if isinstance(inner, dict) and "content" in inner:
            # 提取 content 和 tool_calls（无论 tool_calls 是否为空）
            content = inner.get("content", "")
            if inner.get("tool_calls"):
                for tc in inner["tool_calls"]:
                    tool_calls.append({
                        "id": tc.get("id", "call_001"),
                        "name": tc.get("name", ""),
                        "args": tc.get("args", {}),
                    })

    return {
        "text": json.dumps({"content": content, "tool_calls": tool_calls}, ensure_ascii=False),
        "input_tokens": response.usage.prompt_tokens,
        "output_tokens": response.usage.completion_tokens,
        "model_name": model_setting,
    }


def _plan_execute_generate(config_path: Path, config: dict, messages: list[dict], tools_schema: list[dict], model_name: str | None = None, backend: str = "transformers") -> dict:
    """规划执行模式：先规划多工具调用 → 执行 → 汇总。

    backend="transformers" → 本地 Qwen（_prompt_json_generate）
    backend="openai"      → DeepSeek/GPT（_openai_generate）
    """
    plan_messages = deepcopy(messages)

    # ★ 根据 backend 选择生成函数
    if backend == "openai":
        generate_fn = lambda msgs: _openai_generate(config_path, _get_model_config_by_name(config, model_name), msgs, tools_schema)
    else:
        generate_fn = lambda msgs: _prompt_json_generate(config_path, config, msgs, tools_schema, model_name)

    has_tool_results = any(m.get("role") == "tool" for m in plan_messages)
    if has_tool_results:
        # ★ summarize phase：让模型自由输出（_openai_generate 会自动包装成JSON）
        plan_messages.append({
            "role": "user",
            "content": "请根据以上工具执行结果，给出最终回答。"
        })
        result = generate_fn(plan_messages)
        return {
            "text": result["text"],
            "input_tokens": result["input_tokens"],
            "output_tokens": result["output_tokens"],
            "model_name": result["model_name"],
            "phase": "summarize",
        }

    plan_messages.append(
        {
            "role": "user",
            "content": "请分析当前任务，制定一个详细的执行计划。请输出需要调用的工具列表（可能有多个），以及每个工具的调用参数。输出格式：JSON对象，包含tool_calls数组，每个元素包含id、name和args。",
        }
    )

    plan_result = generate_fn(plan_messages)

    try:
        parsed_candidate, _ = _parse_model_output(plan_result["text"])
        if parsed_candidate.get("tool_calls"):
            return {
                "text": json.dumps({"content": "", "tool_calls": parsed_candidate["tool_calls"]}, ensure_ascii=False),
                "input_tokens": plan_result["input_tokens"],
                "output_tokens": plan_result["output_tokens"],
                "model_name": plan_result["model_name"],
                "phase": "planning",
            }
    except Exception:
        pass

    return {
        "text": plan_result["text"],
        "input_tokens": plan_result["input_tokens"],
        "output_tokens": plan_result["output_tokens"],
        "model_name": plan_result["model_name"],
        "phase": "planning_fallback",
    }


def generate_ai_message(
    model_config: str,
    messages: list[dict],
    tools_schema: list[dict],
    mode: str = "prompt_json",
    artifact_dir: str | None = None,
    artifact_stem: str | None = None,
    model_name: str | None = None,
) -> dict:
    config_path, config = _load_model_config(model_config)
    messages = validate_messages(deepcopy(messages))
    if not isinstance(tools_schema, list):
        raise ValueError("tools_schema must be an array")
    generated_at = now_iso()

    if mode == "mock":
        backend = "mock"
        ai_message = _mock_generate(messages)
        raw_text = json.dumps({"content": ai_message["content"], "tool_calls": ai_message["tool_calls"]}, ensure_ascii=False)
        parsed_candidate = {"content": ai_message["content"], "tool_calls": ai_message["tool_calls"]}
        status = "success"
        error = None
        token_stats = None
    elif mode == "prompt_json":
        selected_model_config = _get_model_config_by_name(config, model_name)
        backend = selected_model_config.get("backend", "transformers")
        if backend == "openai":
            generation_result = _openai_generate(config_path, selected_model_config, messages, tools_schema)
        else:
            generation_result = _prompt_json_generate(config_path, config, messages, tools_schema, model_name)
        raw_text = generation_result["text"]
        token_stats = {
            "input_tokens": generation_result["input_tokens"],
            "output_tokens": generation_result["output_tokens"],
            "model_name": generation_result["model_name"],
        }
        try:
            parsed_candidate, ai_message = _parse_model_output(raw_text)
            status = "success"
            error = None
        except Exception as exc:
            parsed_candidate = None
            ai_message = make_ai_message(PARSE_ERROR_CONTENT, [])
            status = "error"
            error = {"type": type(exc).__name__, "message": str(exc)}
    elif mode == "plan_execute":
        selected_model_config = _get_model_config_by_name(config, model_name)
        backend = selected_model_config.get("backend", "transformers")
        generation_result = _plan_execute_generate(config_path, config, messages, tools_schema, model_name, backend)
        raw_text = generation_result["text"]
        token_stats = {
            "input_tokens": generation_result["input_tokens"],
            "output_tokens": generation_result["output_tokens"],
            "model_name": generation_result["model_name"],
            "phase": generation_result["phase"],
        }
        try:
            parsed_candidate, ai_message = _parse_model_output(raw_text)
            status = "success"
            error = None
        except Exception as exc:
            parsed_candidate = None
            ai_message = make_ai_message(PARSE_ERROR_CONTENT, [])
            status = "error"
            error = {"type": type(exc).__name__, "message": str(exc)}
    else:
        raise ValueError("mode must be mock, prompt_json, or plan_execute")

    raw_record = {
        "mode": mode,
        "backend": backend,
        "raw_text": raw_text,
        "parsed_candidate": parsed_candidate,
        "status": status,
        "error": error,
        "generated_at": generated_at,
        "token_stats": token_stats,
    }

    if artifact_dir:
        raw_path, message_path, log_path = _artifact_paths(artifact_dir, artifact_stem)
        write_json(raw_record, raw_path)
        write_json(ai_message, message_path)
        append_jsonl(
            {
                "timestamp": generated_at,
                "mode": mode,
                "status": status,
                "raw_output_path": str(raw_path),
                "ai_message_path": str(message_path),
                "error": error,
                "token_stats": token_stats,
            },
            log_path,
        )

    result = {
        "ai_message": ai_message,
        "status": status,
        "error": error,
    }
    if token_stats:
        result["token_stats"] = token_stats
    return result


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate one AIMessage with a local or mock LLM.")
    parser.add_argument("--model_config", required=True)
    parser.add_argument("--messages", required=True)
    parser.add_argument("--tools_schema", required=True)
    parser.add_argument("--mode", choices=["mock", "prompt_json", "plan_execute"], required=True)
    parser.add_argument("--outdir", required=True)
    parser.add_argument("--model_name", default=None)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        outdir = resolve_cli_path(args.outdir)
        result = generate_ai_message(
            str(resolve_cli_path(args.model_config)),
            read_json(resolve_cli_path(args.messages)),
            read_json(resolve_cli_path(args.tools_schema)),
            args.mode,
            str(outdir),
            model_name=args.model_name,
        )
        print(outdir / "ai_message.json")
        if "token_stats" in result:
            print(f"Token stats: input={result['token_stats']['input_tokens']}, output={result['token_stats']['output_tokens']}", file=sys.stderr)
        return 0
    except Exception as exc:
        print(f"fatal: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


def batch_test(
    model_config: str,
    test_cases: list[dict],
    tools_schema: list[dict],
    modes: list[str],
    model_names: list[str] | None = None,
    outdir: str | None = None,
) -> dict:
    config_path, config = _load_model_config(model_config)
    results = []
    model_names = model_names or [None]

    for case in test_cases:
        case_id = case.get("id", f"case_{len(results)}")
        case_messages = case["messages"]
        expected_type = case.get("expected_type", "tool_call")

        for mode in modes:
            for model_name in model_names:
                try:
                    start_time = perf_counter()
                    result = generate_ai_message(
                        str(config_path),
                        case_messages,
                        tools_schema,
                        mode=mode,
                        model_name=model_name,
                    )
                    elapsed_ms = round((perf_counter() - start_time) * 1000, 3)

                    ai_message = result["ai_message"]
                    has_tool_calls = bool(ai_message["tool_calls"])
                    has_content = bool(ai_message["content"].strip())
                    tool_call_success = False

                    if expected_type == "tool_call" and has_tool_calls:
                        tool_call_success = True
                    elif expected_type == "final_answer" and has_content and not has_tool_calls:
                        tool_call_success = True

                    result_entry = {
                        "case_id": case_id,
                        "mode": mode,
                        "model_name": model_name or "default",
                        "status": result["status"],
                        "has_tool_calls": has_tool_calls,
                        "tool_call_count": len(ai_message["tool_calls"]),
                        "has_content": has_content,
                        "expected_type": expected_type,
                        "tool_call_success": tool_call_success,
                        "elapsed_ms": elapsed_ms,
                    }
                    if "token_stats" in result:
                        result_entry.update(result["token_stats"])
                    results.append(result_entry)
                except Exception as exc:
                    results.append({
                        "case_id": case_id,
                        "mode": mode,
                        "model_name": model_name or "default",
                        "status": "error",
                        "error": str(exc),
                        "tool_call_success": False,
                    })

    summary = {
        "total_cases": len(test_cases),
        "total_runs": len(results),
        "by_mode": {},
        "by_model": {},
    }

    for mode in modes:
        mode_results = [r for r in results if r["mode"] == mode]
        success_count = sum(1 for r in mode_results if r.get("tool_call_success"))
        summary["by_mode"][mode] = {
            "runs": len(mode_results),
            "success": success_count,
            "success_rate": round(success_count / len(mode_results) * 100, 2) if mode_results else 0,
            "avg_input_tokens": round(sum(r.get("input_tokens", 0) for r in mode_results if "input_tokens" in r) / len(mode_results), 2) if mode_results else 0,
            "avg_output_tokens": round(sum(r.get("output_tokens", 0) for r in mode_results if "output_tokens" in r) / len(mode_results), 2) if mode_results else 0,
            "avg_elapsed_ms": round(sum(r.get("elapsed_ms", 0) for r in mode_results) / len(mode_results), 2) if mode_results else 0,
        }

    for model_name in model_names:
        name_key = model_name or "default"
        model_results = [r for r in results if r["model_name"] == name_key]
        success_count = sum(1 for r in model_results if r.get("tool_call_success"))
        summary["by_model"][name_key] = {
            "runs": len(model_results),
            "success": success_count,
            "success_rate": round(success_count / len(model_results) * 100, 2) if model_results else 0,
        }

    if outdir:
        output_path = Path(outdir)
        output_path.mkdir(parents=True, exist_ok=True)
        write_json(results, output_path / "batch_test_results.json")
        write_json(summary, output_path / "batch_test_summary.json")

    return {"results": results, "summary": summary}


if __name__ == "__main__":
    raise SystemExit(main())
