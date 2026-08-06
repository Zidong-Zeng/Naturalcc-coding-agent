"""Test DeepSeek API - actual code completion on StudentManager.java"""
import json, urllib.request, os, sys

BASE = "http://127.0.0.1:7860"
API_KEY = "sk-17e9c304a4c249128b6eac01acee6ea8"
PROJ_DIR = os.path.abspath(".")

# First make a backup
TARGET = os.path.join(PROJ_DIR, "test", "StudentManager.java")
backup = open(TARGET, "r", encoding="utf-8").read()

print("=" * 60)
print("DeepSeek 实时代码补全测试")
print(f"Target: test/StudentManager.java")
print("=" * 60)

body = json.dumps({
    "project_dir": PROJ_DIR,
    "target_files": ["test/StudentManager.java"],
    "instruction": "补全 addStudent 方法的实现，检查 name 不为 null 且 age 大于 0，成功返回 true，失败返回 false",
    "model": "deepseek/deepseek-chat",
    "api_key": API_KEY,
    "feature": "code_completion",
}, ensure_ascii=False).encode()

print("\nSending request to /api/run ...")
req = urllib.request.Request(
    f"{BASE}/api/run",
    data=body,
    headers={"Content-Type": "application/json; charset=utf-8"}
)

try:
    with urllib.request.urlopen(req, timeout=180) as r:
        buffer = ""
        while True:
            chunk = r.read(4096)
            if not chunk:
                break
            buffer += chunk.decode("utf-8", errors="replace")
            lines = buffer.split("\n")
            buffer = lines.pop() or ""
            for line in lines:
                if not line.strip():
                    continue
                try:
                    evt = json.loads(line)
                    t = evt.get("type", "")
                    if t == "log":
                        log = evt.get("log", "")
                        # Print last few chars for progress
                        if len(log) > 200:
                            print(log[-200:], end="", flush=True)
                        else:
                            print(log, end="", flush=True)
                    elif t == "done":
                        print(f"\n\n--- DONE: {evt.get('status', '?')} ---")
                        if evt.get("report"):
                            print(evt["report"])
                except json.JSONDecodeError:
                    pass

    # Show diff
    print("\n" + "=" * 60)
    print("File changes:")
    current = open(TARGET, "r", encoding="utf-8").read()
    if current != backup:
        print("✓ File was modified successfully!")
        for i, (old, new) in enumerate(zip(backup.split("\n"), current.split("\n"))):
            if old != new:
                print(f"  Line {i+1}:")
                print(f"    - {old.strip()}")
                print(f"    + {new.strip()}")
    else:
        print("File was not modified (this may be normal for dry-run or error)")

except urllib.error.HTTPError as e:
    print(f"HTTP Error: {e.code}")
    print(e.read().decode()[:500])
except Exception as e:
    print(f"Error: {e}")
