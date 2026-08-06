from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import traceback
from pathlib import Path


class TimeoutError(Exception):
    pass


def _safe_builtins() -> dict:
    """沙箱内允许的最小 builtin 集合。"""
    safe = {
        "print": print,
        "len": len,
        "range": range,
        "int": int,
        "float": float,
        "str": str,
        "list": list,
        "dict": dict,
        "tuple": tuple,
        "set": set,
        "bool": bool,
        "sum": sum,
        "min": min,
        "max": max,
        "abs": abs,
        "round": round,
        "sorted": sorted,
        "reversed": reversed,
        "enumerate": enumerate,
        "zip": zip,
        "map": map,
        "filter": filter,
        "isinstance": isinstance,
        "type": type,
        "True": True,
        "False": False,
        "None": None,
    }
    return safe


def _static_check(code: str) -> None:
    """词法 + 语法双层沙箱检查。"""
    forbidden = [
        "import os", "import sys", "import subprocess",
        "open(", "__import__", "exec(", "eval(",
    ]
    lowered = code.lower()
    for pattern in forbidden:
        if pattern in lowered:
            raise ValueError(f"forbidden pattern detected: {pattern}")

    try:
        tree = ast.parse(code)
    except SyntaxError as exc:
        raise ValueError(f"syntax error: {exc}") from exc

    # 词法黑名单已覆盖常见形式；AST 层完全禁止 import（沙箱 builtin 不含 __import__）
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            raise ValueError("import statements are disabled in sandbox")


# 子进程入口脚本（字符串，写入临时目录后由 python -m 执行）
_WORKER_SCRIPT = '''
import json
import sys
from io import StringIO

# 沙箱 builtin
__safe_builtins = {
    "print": print, "len": len, "range": range, "int": int,
    "float": float, "str": str, "list": list, "dict": dict,
    "tuple": tuple, "set": set, "bool": bool, "sum": sum,
    "min": min, "max": max, "abs": abs, "round": round,
    "sorted": sorted, "reversed": reversed, "enumerate": enumerate,
    "zip": zip, "map": map, "filter": filter,
    "isinstance": isinstance, "type": type,
    "True": True, "False": False, "None": None,
}

stdout_cap, stderr_cap = StringIO(), StringIO()
old_out, old_err = sys.stdout, sys.stderr
sys.stdout, sys.stderr = stdout_cap, stderr_cap

status = "success"
error_msg = None
tb = None

try:
    with open("user_code.py", encoding="utf-8") as f:
        user_code = f.read()
    exec(compile(user_code, "user_code.py", "exec"), {"__builtins__": __safe_builtins})
except Exception as exc:
    status = "error"
    error_msg = f"{type(exc).__name__}: {exc}"
    tb = __import__("traceback").format_exc()
finally:
    sys.stdout, sys.stderr = old_out, old_err

result = {
    "status": status,
    "stdout": stdout_cap.getvalue(),
    "stderr": stderr_cap.getvalue(),
    "error": error_msg,
    "traceback": tb,
}
print("__RESULT__" + json.dumps(result, ensure_ascii=False))
'''


def _safe_exec_subprocess(code: str, timeout_seconds: int) -> dict:
    """子进程方案 —— 可在任意线程中调用。"""
    with tempfile.TemporaryDirectory(prefix="code_exec_") as tmpdir:
        tmp = Path(tmpdir)
        (tmp / "user_code.py").write_text(code, encoding="utf-8")
        (tmp / "worker.py").write_text(_WORKER_SCRIPT, encoding="utf-8")

        try:
            proc = subprocess.run(
                [sys.executable, "worker.py"],
                capture_output=True,
                text=True,
                timeout=timeout_seconds,
                cwd=tmp,
                env={"PATH": os.environ.get("PATH", "")},
            )
        except subprocess.TimeoutExpired as exc:
            return {
                "status": "timeout",
                "stdout": exc.stdout or "",
                "stderr": exc.stderr or "",
                "error": "code execution timed out",
                "traceback": None,
            }

        stdout_text = proc.stdout or ""
        stderr_text = proc.stderr or ""

        for line in reversed(stdout_text.splitlines()):
            if line.startswith("__RESULT__"):
                try:
                    return json.loads(line[len("__RESULT__"):])
                except json.JSONDecodeError:
                    pass

        return {
            "status": "error",
            "stdout": stdout_text,
            "stderr": stderr_text,
            "error": f"subprocess exited with code {proc.returncode}",
            "traceback": stderr_text,
        }


def code_executor(
    code: str,
    timeout_seconds: int | None = None,
    *,
    output_dir: str | None = None,
) -> dict:
    """在受限沙箱中执行 Python 代码（子进程方案，线程安全）。"""
    if timeout_seconds is None:
        timeout_seconds = 5
    if not isinstance(code, str) or not code.strip():
        raise ValueError("code must be a non-empty string")
    if not isinstance(timeout_seconds, int) or isinstance(timeout_seconds, bool) or timeout_seconds <= 0:
        raise ValueError("timeout_seconds must be a positive integer")
    if timeout_seconds > 30:
        raise ValueError("timeout_seconds must not exceed 30 seconds")
    if len(code) > 10000:
        raise ValueError("code is too long (max 10000 characters)")

    _static_check(code)

    result = _safe_exec_subprocess(code, timeout_seconds)
    result["code_length"] = len(code)
    result["timeout_limit"] = timeout_seconds
    return result
