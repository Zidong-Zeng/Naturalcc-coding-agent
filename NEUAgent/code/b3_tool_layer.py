from __future__ import annotations

import argparse
import hashlib
import importlib
import inspect
import json
import sys
import time
from time import perf_counter
from pathlib import Path

from common.error_codes import classify_error, make_error_result as make_b2_error_result
from common.io_utils import append_jsonl, read_json, read_yaml, write_json
from common.logging_utils import now_iso
from common.path_utils import bootstrap_project_root, resolve_cli_path, resolve_from_file
from common.schemas import make_skill_result, make_tool_message, normalize_tool_call


bootstrap_project_root()


JSON_TYPES = {
    "string": str,
    "integer": int,
    "number": (int, float),
    "boolean": bool,
    "object": dict,
    "array": list,
}

PYTHON_TYPE_TO_JSON_TYPE = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    dict: "object",
    list: "array",
}


def _load_tools_config(tools_config: str | Path) -> tuple[Path, dict]:
    config_path = Path(tools_config).resolve()
    config = read_yaml(config_path)
    if not isinstance(config, dict):
        raise ValueError("tools.yaml must contain an object")
    if not isinstance(config.get("tools"), dict) or not isinstance(config.get("toolsets"), dict):
        raise ValueError("tools.yaml must define tools and toolsets")
    return config_path, config


def _resolve_toolset(config: dict, toolset: str | None) -> tuple[str, list[str]]:
    selected = toolset or config.get("default_toolset")
    if not isinstance(selected, str) or selected not in config["toolsets"]:
        raise ValueError(f"toolset does not exist: {selected}")
    names = config["toolsets"][selected]
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise ValueError(f"toolset {selected} must be a list of tool names")
    return selected, names


def _parameter_schema(tool: dict) -> dict:
    raw_parameters = tool.get("parameters", {})
    if not isinstance(raw_parameters, dict):
        raise ValueError("tool parameters must be an object")
    properties = {}
    for name, definition in raw_parameters.items():
        if not isinstance(definition, dict) or definition.get("type") not in JSON_TYPES:
            raise ValueError(f"invalid parameter schema for {name}")
        properties[name] = dict(definition)
    required = tool.get("required", [])
    if not isinstance(required, list) or not all(name in properties for name in required):
        raise ValueError("required parameters must reference declared properties")
    return {
        "type": "object",
        "properties": properties,
        "required": required,
        "additionalProperties": False,
    }


def get_tools_schema(
    tools_config: str,
    toolset: str,
    outdir: str | None = None,
) -> list[dict]:
    _, config = _load_tools_config(tools_config)
    selected, tool_names = _resolve_toolset(config, toolset)
    schema = []
    for name in tool_names:
        tool = config["tools"].get(name)
        if not isinstance(tool, dict):
            raise ValueError(f"toolset references missing tool: {name}")
        for field in ("module", "function", "description", "returns"):
            if field not in tool:
                raise ValueError(f"tool {name} missing {field}")
        returns = tool["returns"]
        if not isinstance(returns, dict):
            raise ValueError(f"tool {name} returns must be an object")
        schema.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool["description"],
                    "parameters": _parameter_schema(tool),
                    "x-returns": {"type": "object", "properties": returns},
                },
            }
        )
    if outdir:
        output_dir = Path(outdir)
        write_json(schema, output_dir / "tools_schema.json")
        write_json(
            {"status": "success", "toolset": selected, "tool_count": len(schema), "tools": tool_names},
            output_dir / "tool_schema_report.json",
        )
    return schema


def _validate_args(args: dict, definition: dict) -> None:
    parameter_schema = _parameter_schema(definition)
    properties = parameter_schema["properties"]
    required = set(parameter_schema["required"])
    missing = [name for name in required if name not in args]
    if missing:
        raise ValueError(f"missing required parameters: {', '.join(missing)}")
    unknown = sorted(set(args) - set(properties))
    if unknown:
        raise ValueError(f"unknown parameters: {', '.join(unknown)}")
    for name, value in args.items():
        # ★ 可选参数传 null → 跳过验证（让函数默认值生效）
        if name not in required and value is None:
            continue
        expected_name = properties[name]["type"]
        expected = JSON_TYPES[expected_name]
        if expected_name in {"integer", "number"} and isinstance(value, bool):
            valid = False
        else:
            valid = isinstance(value, expected)
        if not valid:
            raise ValueError(f"parameter {name} must be {expected_name}")
        if expected_name == "array" and "items" in properties[name]:
            item_type = properties[name]["items"].get("type")
            if item_type in JSON_TYPES and not all(isinstance(item, JSON_TYPES[item_type]) for item in value):
                raise ValueError(f"parameter {name} contains invalid items")


def _error_result(name: str, args: dict, exc: Exception, latency_ms: float = 0.0) -> dict:
    error_code, error_msg = classify_error(exc)
    b2_error = make_b2_error_result(error_code, error_msg, name)
    return make_skill_result(
        name,
        "error",
        args,
        None,
        {
            "type": type(exc).__name__,
            "message": str(exc),
            "error_code": error_code,
            "error_category": b2_error["error_category"],
            "recoverable": b2_error["recoverable"],
        },
        latency_ms,
    )


def _parse_docstring(doc: str) -> dict:
    """解析 docstring，提取描述和参数说明"""
    if not doc:
        return {"description": "No description", "params": {}}
    
    lines = doc.strip().split("\n")
    description_lines = []
    params = {}
    current_section = None
    current_param = None
    
    for line in lines:
        stripped = line.strip()
        
        # 检测段落标题
        if stripped.lower() in ("args:", "arguments:", "parameters:", "params:"):
            current_section = "params"
            continue
        elif stripped.lower() in ("returns:", "return:", "yields:", "yield:"):
            current_section = "returns"
            continue
        elif stripped.startswith("Raises:") or stripped.startswith("Exceptions:"):
            current_section = "raises"
            continue
        
        # 解析参数说明
        if current_section == "params":
            # 匹配 "param_name: description" 或 "param_name (type): description"
            if ":" in stripped and not stripped.startswith(" "):
                parts = stripped.split(":", 1)
                param_part = parts[0].strip()
                desc_part = parts[1].strip() if len(parts) > 1 else ""
                
                # 提取参数名（去掉类型注解）
                param_name = param_part.split("(")[0].strip()
                if param_name:
                    current_param = param_name
                    params[param_name] = desc_part
            elif current_param and stripped:
                # 续行
                params[current_param] += " " + stripped
        elif current_section is None and stripped:
            # 主描述部分
            description_lines.append(stripped)
    
    description = " ".join(description_lines) if description_lines else "No description"
    return {"description": description, "params": params}


def _python_type_to_json_schema(annotation) -> dict:
    """将 Python 类型注解转换为 JSON Schema"""
    if annotation == inspect.Parameter.empty:
        return {"type": "string"}
    
    # 处理字符串形式的类型注解（from __future__ import annotations）
    if isinstance(annotation, str):
        annotation = annotation.strip()
        
        # 基本类型映射
        type_map = {
            "str": "string",
            "int": "integer",
            "float": "number",
            "bool": "boolean",
            "dict": "object",
            "list": "array",
            "None": "null",
        }
        
        # 检查是否是基本类型
        if annotation in type_map:
            return {"type": type_map[annotation]}
        
        # 处理 Optional[X]
        if annotation.startswith("Optional[") and annotation.endswith("]"):
            inner = annotation[9:-1]
            return _python_type_to_json_schema(inner)
        
        # 处理 list[X] 或 List[X]
        if annotation.lower().startswith("list[") and annotation.endswith("]"):
            inner = annotation[5:-1]
            item_type = _python_type_to_json_schema(inner)
            return {"type": "array", "items": item_type}
        
        # 处理 dict[K, V] 或 Dict[K, V]
        if annotation.lower().startswith("dict[") and annotation.endswith("]"):
            inner = annotation[5:-1]
            # 简单处理：取第二个类型参数作为 value 类型
            parts = inner.split(",", 1)
            if len(parts) == 2:
                value_type = _python_type_to_json_schema(parts[1].strip())
                return {"type": "object", "additionalProperties": value_type}
        
        # 处理 tuple[X, Y, ...]
        if annotation.lower().startswith("tuple[") and annotation.endswith("]"):
            inner = annotation[6:-1]
            parts = [p.strip() for p in inner.split(",")]
            items = [_python_type_to_json_schema(p) for p in parts]
            return {"type": "array", "items": items}
        
        # 处理 Union[X, None] 形式
        if annotation.startswith("Union[") and annotation.endswith("]"):
            inner = annotation[6:-1]
            parts = [p.strip() for p in inner.split(",")]
            non_none = [p for p in parts if p != "None"]
            if len(non_none) == 1:
                return _python_type_to_json_schema(non_none[0])
        
        # 默认回退
        return {"type": "string"}
    
    # 处理实际类型对象
    if annotation in PYTHON_TYPE_TO_JSON_TYPE:
        return {"type": PYTHON_TYPE_TO_JSON_TYPE[annotation]}
    
    if annotation is type(None):
        return {"type": "null"}
    
    # 处理 typing 模块的类型
    type_str = str(annotation)
    
    # 处理 Optional[X] = Union[X, None]
    if "Union" in type_str and "None" in type_str:
        if hasattr(annotation, "__args__"):
            non_none_args = [arg for arg in annotation.__args__ if arg is not type(None)]
            if len(non_none_args) == 1:
                return _python_type_to_json_schema(non_none_args[0])
    
    # 处理 List[X], list[X]
    if "list" in type_str.lower() and hasattr(annotation, "__args__"):
        item_type = _python_type_to_json_schema(annotation.__args__[0])
        return {"type": "array", "items": item_type}
    
    # 处理 Dict[K, V], dict[K, V]
    if "dict" in type_str.lower() and hasattr(annotation, "__args__"):
        value_type = _python_type_to_json_schema(annotation.__args__[1])
        return {"type": "object", "additionalProperties": value_type}
    
    # 处理 Tuple[X, Y, ...]
    if "tuple" in type_str.lower() and hasattr(annotation, "__args__"):
        items = [_python_type_to_json_schema(arg) for arg in annotation.__args__]
        return {"type": "array", "items": items}
    
    # 默认回退
    return {"type": "string"}


def _auto_generate_schema_from_function(module_path: str, function_name: str) -> dict:
    """自动从Python函数源码解析生成tools_schema"""
    try:
        module = importlib.import_module(module_path)
        function = getattr(module, function_name)
        signature = inspect.signature(function)
        doc = inspect.getdoc(function) or ""
        
        # 解析文档字符串
        doc_info = _parse_docstring(doc)
        description = doc_info["description"]
        param_docs = doc_info["params"]

        parameters = {}
        required = []

        for param_name, param in signature.parameters.items():
            # 跳过内部参数
            if param_name in ("data_root", "output_dir", "self", "cls"):
                continue

            # 推断类型
            param_schema = _python_type_to_json_schema(param.annotation)
            
            # 添加描述（优先从 docstring 获取）
            if param_name in param_docs:
                param_schema["description"] = param_docs[param_name]
            else:
                param_schema["description"] = f"Parameter {param_name}"
            
            # 如果有默认值，添加到 schema
            if param.default != inspect.Parameter.empty:
                if param.default is not None:
                    try:
                        param_schema["default"] = param.default
                    except (TypeError, ValueError):
                        pass
            
            parameters[param_name] = param_schema

            # 判断是否必需
            if param.default == inspect.Parameter.empty and param.kind not in (
                inspect.Parameter.VAR_POSITIONAL,
                inspect.Parameter.VAR_KEYWORD
            ):
                required.append(param_name)

        # 推断返回类型
        return_schema = {"type": "object", "description": "Function return value"}
        if signature.return_annotation != inspect.Signature.empty:
            return_schema = _python_type_to_json_schema(signature.return_annotation)
            return_schema["description"] = "Function return value"

        return {
            "type": "function",
            "function": {
                "name": function_name,
                "description": description,
                "parameters": {
                    "type": "object",
                    "properties": parameters,
                    "required": required,
                    "additionalProperties": False
                },
                "x-returns": return_schema
            }
        }
    except Exception as exc:
        raise ValueError(f"Failed to auto-generate schema for {module_path}.{function_name}: {exc}")


def _get_cache_key(name: str, args: dict) -> str:
    """生成缓存键"""
    cache_data = json.dumps({"name": name, "args": args}, sort_keys=True, ensure_ascii=False)
    return hashlib.sha256(cache_data.encode()).hexdigest()


def _load_cache(cache_file: Path) -> dict:
    """加载缓存"""
    if cache_file.exists():
        return read_json(cache_file)
    return {}


def _save_cache(cache: dict, cache_file: Path) -> None:
    """保存缓存"""
    write_json(cache, cache_file)


def _cache_set(cache: dict, key: str, value: dict, ttl_seconds: int = 3600) -> None:
    """设置缓存条目（带TTL）"""
    import time
    cache[key] = {
        "value": value,
        "expires_at": time.time() + ttl_seconds
    }


def _cache_get(cache: dict, key: str) -> dict | None:
    """获取缓存条目（检查TTL）"""
    import time
    if key not in cache:
        return None
    entry = cache[key]
    if time.time() > entry.get("expires_at", 0):
        del cache[key]
        return None
    return entry["value"]


def _cache_cleanup(cache: dict, max_size: int = 1000) -> None:
    """清理过期和超限的缓存"""
    import time
    now = time.time()
    expired = [k for k, v in cache.items() if now > v.get("expires_at", 0)]
    for k in expired:
        del cache[k]
    
    if len(cache) > max_size:
        sorted_keys = sorted(cache.keys(), key=lambda k: cache[k].get("expires_at", 0))
        for k in sorted_keys[:len(cache) - max_size]:
            del cache[k]


def execute_tool_calls(
    tool_calls: list[dict],
    tools_config: str,
    toolset: str | None = None,
    outdir: str | None = None,
    enable_retry: bool = False,
    enable_cache: bool = False,
) -> list[dict]:
    config_path, config = _load_tools_config(tools_config)
    selected, allowed_tools = _resolve_toolset(config, toolset)
    if not isinstance(tool_calls, list):
        raise ValueError("tool_calls must be a list")
    data_root_setting = config.get("settings", {}).get("data_root", "../data")
    resolved_data_root = resolve_from_file(data_root_setting, config_path)
    tool_messages = []
    log_records = []
    output_dir = Path(outdir) if outdir else None

    cache_file = output_dir / "tool_call_cache.json" if output_dir and enable_cache else None
    cache = _load_cache(cache_file) if cache_file else {}
    cache_dirty = False

    stats = {
        "total_calls": 0,
        "success_count": 0,
        "error_count": 0,
        "retry_count": 0,
        "cache_hits": 0,
        "total_latency_ms": 0.0,
        "latencies_ms": [],
        "tool_stats": {}
    }

    for index, raw_call in enumerate(tool_calls):
        stats["total_calls"] += 1
        start = perf_counter()
        try:
            call = normalize_tool_call(raw_call, index)
        except Exception as exc:
            call = {"id": f"call_{index + 1:03d}", "name": "unknown", "args": {}}
            result = _error_result(call["name"], call["args"], exc)
        else:
            name = call["name"]
            args = call["args"]

            if name not in stats["tool_stats"]:
                stats["tool_stats"][name] = {
                    "calls": 0,
                    "success": 0,
                    "errors": 0,
                    "total_latency_ms": 0.0
                }
            stats["tool_stats"][name]["calls"] += 1

            if name not in allowed_tools or name not in config["tools"]:
                result = _error_result(name, args, ValueError(f"tool is not available in {selected}: {name}"))
            else:
                cache_key = _get_cache_key(name, args) if enable_cache else None
                cached_result = _cache_get(cache, cache_key) if cache_key else None
                
                if cached_result is not None:
                    result = cached_result
                    stats["cache_hits"] += 1
                    stats["success_count"] += 1
                    stats["tool_stats"][name]["success"] += 1
                else:
                    definition = config["tools"][name]
                    try:
                        _validate_args(args, definition)

                        # 使用 B2 的 run_skill 执行（复用错误分类和参数注入）
                        from b2_run_skill import run_skill as _run_skill

                        exec_start = perf_counter()
                        b2_result = _run_skill(
                            name, args,
                            data_root=str(resolved_data_root),
                            output_dir=str(output_dir) if output_dir else None,
                        )
                        latency_ms = round((perf_counter() - exec_start) * 1000, 3)
                        b2_result["latency_ms"] = latency_ms

                        if b2_result["status"] == "error" and enable_retry:
                            error_info = b2_result.get("error", {})
                            if error_info.get("recoverable", False):
                                for attempt in range(3):
                                    time.sleep(min(0.1 * (2 ** attempt), 2.0))
                                    exec_start = perf_counter()
                                    b2_result = _run_skill(
                                        name, args,
                                        data_root=str(resolved_data_root),
                                        output_dir=str(output_dir) if output_dir else None,
                                    )
                                    latency_ms = round((perf_counter() - exec_start) * 1000, 3)
                                    b2_result["latency_ms"] = latency_ms
                                    if b2_result["status"] == "success":
                                        stats["retry_count"] += attempt + 1
                                        break
                                    error_info = b2_result.get("error", {})
                                    if not error_info.get("recoverable", False):
                                        break

                        result = b2_result

                        if result["status"] == "success":
                            stats["success_count"] += 1
                            stats["tool_stats"][name]["success"] += 1
                        else:
                            stats["error_count"] += 1
                            stats["tool_stats"][name]["errors"] += 1
                        stats["total_latency_ms"] += latency_ms
                        stats["tool_stats"][name]["total_latency_ms"] += latency_ms
                        stats["latencies_ms"].append(latency_ms)

                        if enable_cache and cache_key and result["status"] == "success":
                            _cache_set(cache, cache_key, result, ttl_seconds=3600)
                            cache_dirty = True

                    except (ImportError, AttributeError) as exc:
                        raise RuntimeError(f"cannot load configured tool {name}: {exc}") from exc
                    except Exception as exc:
                        latency_ms = round((perf_counter() - start) * 1000, 3)
                        result = _error_result(name, args, exc, latency_ms)
                        stats["error_count"] += 1
                        stats["tool_stats"][name]["errors"] += 1
                        stats["total_latency_ms"] += latency_ms
                        stats["tool_stats"][name]["total_latency_ms"] += latency_ms
                        stats["latencies_ms"].append(latency_ms)

        content = json.dumps(result, ensure_ascii=False, separators=(",", ":"))
        message = make_tool_message(call["id"], call["name"], content, result["status"])
        tool_messages.append(message)
        log_records.append(
            {
                "timestamp": now_iso(),
                "toolset": selected,
                "tool_call_id": call["id"],
                "name": call["name"],
                "status": result["status"],
                "args": call["args"],
                "skill_result": result,
                "latency_ms": result["latency_ms"],
            }
        )
    if outdir:
        write_json(tool_messages, output_dir / "tool_messages.json")
        for record in log_records:
            append_jsonl(record, output_dir / "tool_call_log.jsonl")

        if stats["total_calls"] > 0:
            stats["success_rate"] = stats["success_count"] / stats["total_calls"]
            stats["error_rate"] = stats["error_count"] / stats["total_calls"]
            stats["avg_latency_ms"] = stats["total_latency_ms"] / stats["total_calls"]
            
            # 计算延迟百分位
            if stats["latencies_ms"]:
                sorted_latencies = sorted(stats["latencies_ms"])
                stats["p50_latency_ms"] = sorted_latencies[len(sorted_latencies) // 2]
                stats["p90_latency_ms"] = sorted_latencies[int(len(sorted_latencies) * 0.9)]
                stats["p99_latency_ms"] = sorted_latencies[int(len(sorted_latencies) * 0.99)]
                stats["max_latency_ms"] = max(sorted_latencies)
                stats["min_latency_ms"] = min(sorted_latencies)

            for tool_name, tool_stat in stats["tool_stats"].items():
                if tool_stat["calls"] > 0:
                    tool_stat["avg_latency_ms"] = tool_stat["total_latency_ms"] / tool_stat["calls"]
                    tool_stat["success_rate"] = tool_stat["success"] / tool_stat["calls"]
                    tool_stat["error_rate"] = tool_stat["errors"] / tool_stat["calls"]

            # 清理 latencies_ms 避免输出过大
            del stats["latencies_ms"]

            write_json(stats, output_dir / "tool_call_stats.json")
        
        # 保存缓存（如果有更新）
        if enable_cache and cache_dirty and cache_file:
            _cache_cleanup(cache)
            _save_cache(cache, cache_file)

    return tool_messages


def auto_generate_tools_schema(
    tools_config: str,
    toolset: str,
    outdir: str | None = None,
) -> list[dict]:
    """自动从Python函数源码解析生成tools_schema"""
    _, config = _load_tools_config(tools_config)
    selected, tool_names = _resolve_toolset(config, toolset)
    schema = []

    for name in tool_names:
        tool = config["tools"].get(name)
        if not isinstance(tool, dict):
            raise ValueError(f"toolset references missing tool: {name}")

        module_path = tool.get("module")
        function_name = tool.get("function")

        if not module_path or not function_name:
            raise ValueError(f"tool {name} missing module or function")

        try:
            tool_schema = _auto_generate_schema_from_function(module_path, function_name)
            schema.append(tool_schema)
        except Exception as exc:
            raise ValueError(f"Failed to auto-generate schema for {name}: {exc}")

    if outdir:
        output_dir = Path(outdir)
        write_json(schema, output_dir / "tools_schema_auto.json")
        write_json(
            {"status": "success", "toolset": selected, "tool_count": len(schema), "tools": tool_names, "mode": "auto"},
            output_dir / "tool_schema_auto_report.json",
        )

    return schema


def compare_schema_descriptions(
    tools_config: str,
    toolset: str,
    test_cases: list[dict],
    outdir: str | None = None,
) -> dict:
    """对比不同工具schema描述方式对模型工具调用准确率的影响
    
    Args:
        tools_config: 工具配置文件路径
        toolset: 工具集名称
        test_cases: 测试用例列表，每个用例包含 {"input": "...", "expected_tool": "...", "expected_args": {...}}
        outdir: 输出目录
    
    Returns:
        包含不同描述方式的准确率统计
    """
    _, config = _load_tools_config(tools_config)
    selected, tool_names = _resolve_toolset(config, toolset)
    
    # 生成两种描述方式
    schemas = {
        "detailed": [],  # 详细版（带参数描述）
        "brief": []      # 简略版（仅函数名和描述）
    }
    
    for name in tool_names:
        tool = config["tools"].get(name)
        if not isinstance(tool, dict):
            raise ValueError(f"toolset references missing tool: {name}")
        
        module_path = tool.get("module")
        function_name = tool.get("function")
        
        if not module_path or not function_name:
            raise ValueError(f"tool {name} missing module or function")
        
        try:
            # 详细版
            detailed_schema = _auto_generate_schema_from_function(module_path, function_name)
            schemas["detailed"].append(detailed_schema)
            
            # 简略版
            brief_schema = {
                "type": "function",
                "function": {
                    "name": name,
                    "description": tool.get("description", "No description"),
                    "parameters": {
                        "type": "object",
                        "properties": {
                            k: {"type": v.get("type", "string")}
                            for k, v in tool.get("parameters", {}).items()
                        },
                        "required": tool.get("required", []),
                        "additionalProperties": False
                    }
                }
            }
            schemas["brief"].append(brief_schema)
        except Exception as exc:
            raise ValueError(f"Failed to generate schema for {name}: {exc}")
    
    # 模拟模型调用（改进的匹配算法）
    # 定义高优先级关键词（精确匹配）和低优先级关键词（模糊匹配）
    keyword_mapping = {
        "calculator": {
            "high": ["计算", "calc", "math", "加减乘除", "求和", "运算", "sqrt", "log", "sin", "cos"],
            "low": ["数字", "数值", "公式"]
        },
        "file_reader": {
            "high": ["读取文件", "read file", "打开文件", "查看文件内容"],
            "low": ["读取", "read"]  # 移除单独的"文件"，避免与搜索冲突
        },
        "local_file_search": {
            "high": ["搜索文件", "查找文件", "search file", "find file", "文件搜索", "搜索包含"],
            "low": ["搜索", "查找", "search", "find", "查询"]
        },
        "table_analyzer": {
            "high": ["分析表格", "analyze table", "表格分析", "CSV分析", "数据分析"],
            "low": ["表格", "分析", "table", "analyze", "CSV", "数据"]
        },
        "format_converter": {
            "high": ["格式转换", "convert format", "JSON转换", "YAML转换", "CSV转换"],
            "low": ["转换", "格式", "convert", "format"]
        },
        "code_executor": {
            "high": ["执行代码", "运行代码", "执行 Python", "沙箱", "code execution", "execute code"],
            "low": ["代码", "执行", "运行", "python", "sandbox"]
        },
        "read_and_convert": {
            "high": ["读取并转换", "读取文件并转换", "读取后转换", "文件读取转换"],
            "low": ["读取", "转换", "read_and_convert", "read convert"]
        },
    }
    
    results = {
        "detailed": {"correct": 0, "total": 0, "accuracy": 0.0, "cases": []},
        "brief": {"correct": 0, "total": 0, "accuracy": 0.0, "cases": []}
    }
    
    for case in test_cases:
        input_text = case.get("input", "")
        expected_tool = case.get("expected_tool", "")
        expected_args = case.get("expected_args", {})
        
        for desc_type in ["detailed", "brief"]:
            results[desc_type]["total"] += 1
            
            # 改进的匹配算法：优先匹配高优先级关键词
            predicted_tool = None
            best_score = 0
            
            for schema in schemas[desc_type]:
                tool_name = schema["function"]["name"]
                keywords = keyword_mapping.get(tool_name, {"high": [], "low": []})
                
                # 计算匹配分数（高优先级权重10，低优先级权重1）
                high_score = sum(10 for kw in keywords["high"] if kw.lower() in input_text.lower())
                low_score = sum(1 for kw in keywords["low"] if kw.lower() in input_text.lower())
                score = high_score + low_score
                
                # 详细版：额外检查参数描述（但权重较低）
                if desc_type == "detailed" and score > 0:
                    # 只有在已有匹配的基础上才加分，避免引入噪声
                    param_desc = str(schema["function"].get("parameters", {})).lower()
                    bonus = sum(0.5 for kw in keywords["high"] if kw.lower() in param_desc)
                    score += bonus
                
                if score > best_score:
                    best_score = score
                    predicted_tool = tool_name
            
            if predicted_tool == expected_tool:
                results[desc_type]["correct"] += 1
                results[desc_type]["cases"].append({
                    "input": input_text,
                    "expected": expected_tool,
                    "predicted": predicted_tool,
                    "score": best_score,
                    "correct": True
                })
            else:
                results[desc_type]["cases"].append({
                    "input": input_text,
                    "expected": expected_tool,
                    "predicted": predicted_tool,
                    "score": best_score,
                    "correct": False
                })
    
    # 计算准确率
    for desc_type in results:
        if results[desc_type]["total"] > 0:
            results[desc_type]["accuracy"] = results[desc_type]["correct"] / results[desc_type]["total"]
    
    comparison = {
        "toolset": selected,
        "test_count": len(test_cases),
        "detailed_accuracy": results["detailed"]["accuracy"],
        "brief_accuracy": results["brief"]["accuracy"],
        "improvement": results["detailed"]["accuracy"] - results["brief"]["accuracy"],
        "details": results
    }
    
    if outdir:
        output_dir = Path(outdir)
        write_json(comparison, output_dir / "schema_comparison.json")
        write_json(schemas["detailed"], output_dir / "schemas_detailed.json")
        write_json(schemas["brief"], output_dir / "schemas_brief.json")
    
    return comparison


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Generate tool schema or execute tool calls.")
    parser.add_argument("--tools_config", required=True)
    parser.add_argument("--toolset", default=None)
    parser.add_argument("--tool_calls")
    parser.add_argument("--test_cases", help="Test cases file for schema comparison")
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--export_schema", action="store_true")
    action.add_argument("--export_schema_auto", action="store_true", help="Auto-generate schema from function signatures")
    action.add_argument("--execute", action="store_true")
    action.add_argument("--compare_schemas", action="store_true", help="Compare different schema description styles")
    parser.add_argument("--outdir", required=True)
    parser.add_argument("--enable_retry", action="store_true", help="Enable automatic retry for recoverable errors")
    parser.add_argument("--enable_cache", action="store_true", help="Enable tool call result caching")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        config_path = resolve_cli_path(args.tools_config)
        outdir = resolve_cli_path(args.outdir)
        if args.export_schema:
            if not args.toolset:
                _, config = _load_tools_config(config_path)
                args.toolset = config.get("default_toolset")
            get_tools_schema(str(config_path), args.toolset, str(outdir))
            print(outdir / "tools_schema.json")
        elif args.export_schema_auto:
            if not args.toolset:
                _, config = _load_tools_config(config_path)
                args.toolset = config.get("default_toolset")
            auto_generate_tools_schema(str(config_path), args.toolset, str(outdir))
            print(outdir / "tools_schema_auto.json")
        elif args.compare_schemas:
            if not args.toolset:
                _, config = _load_tools_config(config_path)
                args.toolset = config.get("default_toolset")
            if not args.test_cases:
                raise ValueError("--test_cases is required with --compare_schemas")
            test_cases_data = read_json(resolve_cli_path(args.test_cases))
            test_cases = test_cases_data.get("test_cases") if isinstance(test_cases_data, dict) else test_cases_data
            compare_schema_descriptions(
                str(config_path),
                args.toolset,
                test_cases,
                str(outdir),
            )
            print(outdir / "schema_comparison.json")
        else:
            if not args.tool_calls:
                raise ValueError("--tool_calls is required with --execute")
            payload = read_json(resolve_cli_path(args.tool_calls))
            tool_calls = payload.get("tool_calls") if isinstance(payload, dict) else payload
            execute_tool_calls(
                tool_calls,
                str(config_path),
                args.toolset,
                str(outdir),
                enable_retry=args.enable_retry,
                enable_cache=args.enable_cache,
            )
            print(outdir / "tool_messages.json")
        return 0
    except Exception as exc:
        print(f"fatal: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
