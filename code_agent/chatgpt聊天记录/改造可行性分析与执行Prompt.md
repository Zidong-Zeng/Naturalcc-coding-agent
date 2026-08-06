# code_agent × NEUAgent 融合：可行性分析与执行 Prompt

> 本文档供 coding agent（如 Claude Code / Cursor / Aider）执行改造时使用。
> 包含 6 个改造点的可行性分析、架构决策、以及可直接执行的实现指令。

---

## 前置阅读

执行前请先完整阅读以下文件以建立上下文：
- `D:\桌面2\科研训练\HUST时期\naturalcc-ncc3\code_agent\架构梳理文档.md`
- `D:\桌面2\科研训练\HUST时期\naturalcc-ncc3\code_agent\code_agent_vs_NEUAgent_对比与借鉴.md`
- `D:\桌面2\科研训练\HUST时期\naturalcc-ncc3\NEUAgent\README.md`
- `D:\桌面2\科研训练\HUST时期\naturalcc-ncc3\NEUAgent\code\b1_agent_runtime.py`（Agent Loop 参考实现）
- `D:\桌面2\科研训练\HUST时期\naturalcc-ncc3\NEUAgent\code\b3_tool_layer.py`（Tool Schema 生成参考实现）
- `D:\桌面2\科研训练\HUST时期\naturalcc-ncc3\NEUAgent\code\b4_local_agent_llm.py`（LLM 推理参考实现）
- `D:\桌面2\科研训练\HUST时期\naturalcc-ncc3\NEUAgent\code\b5_memory.py`（记忆系统参考实现）

---

## 改造点 1：Agent Loop — 从 Pipeline 到自主推理循环

### 可行性分析

**当前架构**：
```
用户选模块 → NaturalCC 生成 Prompt → Aider --message-file 单次执行 → 结束
```

**目标架构**：
```
用户发送需求 → LLM 推理 → 自主选择工具 → 执行工具 → 结果回传 → LLM 再推理 → ... → 最终回答
```

**关于 Aider 的决策：保留，但降级为工具**

| 方案 | 描述 | 评估 |
|------|------|------|
| A: 在 Aider 外包装循环 | 多次调用 `aider --message-file`，每次传入上轮结果 | ❌ 每轮都是独立进程+独立LLM调用，成本高、上下文断裂 |
| B: 用 Aider 的交互模式 | 通过 stdin 与 Aider REPL 多轮对话 | ⚠️ 可行但难以精确控制，Aider 不受我们调度 |
| **C: Agent Loop 直调 LLM + Aider 作为编辑工具** | Agent Loop 用 LLM API 做推理和工具选择；需要编辑代码时，调用 Aider 作为子工具 | ✅ **推荐** — 保留现有 Aider 集成，但 Aider 不再主导流程 |

**最终架构决策**：

```
┌────────────────────────────────────────────────────────────────┐
│                     Agent Loop（新增：agent_loop.py）            │
│                                                                │
│  while tool_rounds < max_turns:                                │
│    │                                                           │
│    ├─→ Step 1: LLM 推理（直调 API）                             │
│    │    messages + tools_schema → LLM → ai_message              │
│    │                                                           │
│    ├─→ Step 2: 判断                                            │
│    │    tool_calls == []? → 最终回答 → break                    │
│    │                                                           │
│    └─→ Step 3: 执行工具                                        │
│         for each tool_call:                                    │
│           ├─ naturalcc_parse   → CProjectParser.parse_dir()    │
│           ├─ naturalcc_search  → searcher.get_prompt4names()   │
│           ├─ code_edit         → run_aider_stream()  ← ★ 保留  │
│           ├─ code_summary      → run_aider_stream(dry_run=True) │
│           ├─ code_repair       → run_aider_stream()            │
│           ├─ vuln_detect       → VulnerabilityDetectionPlugin  │
│           ├─ calculator        → NEUAgent skills               │
│           ├─ file_reader       → NEUAgent skills               │
│           └─ ...                                               │
│         结果注入 messages → 回到 Step 1                         │
└────────────────────────────────────────────────────────────────┘
```

**Aider 的新角色**：从"流程主导者"变为"代码编辑工具"。Agent Loop 决定**何时**编辑代码、**编辑哪些文件**、**编辑什么内容**，然后将这些参数传给 Aider 单次执行。Aider 执行完毕后，Agent Loop 检查结果，决定是否需要进一步修改。

---

## 改造点 2：Skill 注册 — 插件 + NEUAgent Skills 统一管理

### 可行性分析

**现状**：
- code_agent 有 6 个插件（code_completion / code_summary / code_repair / vulnerability_detection / design_to_code / knowledge_graph），通过 `@register_plugin` 装饰器注册
- NEUAgent 有 7 个 Skill（calculator / file_reader / local_file_search / table_analyzer / format_converter / code_executor / read_and_convert），通过 `SKILL_MODULES` 字典 + YAML 配置注册

**融合策略**：采用**分层注册**，不破坏现有插件系统：

```
Skill 注册层（新增：skill_registry.py）
├── code_agent 插件（通过 @register_plugin 自动注册）
│   ├── code_completion     ← 代码补全
│   ├── code_summary        ← 代码总结
│   ├── code_repair         ← 代码修复
│   ├── vulnerability_detection ← 漏洞检测
│   ├── design_to_code      ← 设计稿转代码
│   └── knowledge_graph     ← 知识图谱
│
├── NEUAgent 技能（直接导入 Python 函数）
│   ├── calculator          ← 安全数学计算
│   ├── file_reader         ← 文件读取
│   ├── local_file_search   ← 本地文件搜索
│   ├── table_analyzer      ← 表格分析
│   ├── format_converter    ← 格式转换
│   ├── code_executor       ← 代码执行沙箱
│   └── read_and_convert    ← 读取并转换
│
└── naturalcc 核心能力（封装现有模块）
    ├── naturalcc_parse     ← 项目语义解析
    └── naturalcc_search    ← 符号搜索/上下文检索
```

**直接复制 NEUAgent 的 7 个 Skill**：`skills/` 文件夹下的 Python 文件是纯函数实现，无外部依赖（除了 code_executor 需要 subprocess 沙箱），可以直接复制到 code_agent 项目中使用。只需注意 `data_root` 路径的适配。

---

## 改造点 3：模型自主决策 — Tool Definition + Prompt 设计

### 可行性分析

这是改造的核心——让模型"看到"所有可用工具，并根据用户意图自主选择。

**关键技术点**：

1. **Tool Definition 生成**：将每个 Skill 的元信息转换为 OpenAI function-calling 格式
2. **System Prompt 设计**：明确告诉模型它的角色、可用工具、使用规则
3. **输出解析**：模型返回 JSON（含 `content` + `tool_calls`），需要健壮的解析器

**复用 NEUAgent 的关键代码**：
- `b3_tool_layer.py::get_tools_schema()` → 从 YAML 配置生成 tools_schema
- `b3_tool_layer.py::execute_tool_calls()` → 执行工具调用并构造 ToolMessage
- `b4_local_agent_llm.py::generate_ai_message()` → LLM 推理（支持 OpenAI function calling）
- `b4_local_agent_llm.py::_parse_model_output()` → 多级 JSON 解析 + 兜底策略

---

## 改造点 4：多轮对话 + Session 持久化

### 可行性分析

**当前状态**：完全无状态，每次 HTTP 请求独立。

**目标状态**：
- 用户在一个 session 内可以连续发送多轮消息
- Session 持久化到磁盘（JSON 文件）
- 支持恢复历史 session 继续对话
- 前端无刷新时 session 不丢失

**Session 数据结构**：
```json
{
  "session_id": "abc123def456",
  "project_dir": "/path/to/project",
  "target_files": ["src/main.c", "src/utils.c"],
  "messages": [
    {"role": "system", "content": "..."},
    {"role": "user", "content": "帮我修复登录bug"},
    {"role": "assistant", "content": null, "tool_calls": [...]},
    {"role": "tool", "tool_call_id": "call_001", "content": "..."},
    {"role": "assistant", "content": "已修复，修改了..."}
  ],
  "model": "deepseek/deepseek-chat",
  "turn_index": 13,
  "token_stats": {"input": 45230, "output": 12890},
  "created_at": "2026-07-28T10:00:00",
  "updated_at": "2026-07-28T10:15:00"
}
```

**存储路径**：`outputs/sessions/{session_id}/session.json`

---

## 改造点 5：记忆系统 — KV 混合检索 + 向量 + 摘要 + CRUD

### 可行性分析

**从最简单的部分开始**：

| 阶段 | 内容 | 依赖 | 复杂度 |
|------|------|------|--------|
| Phase 1 | 关键词检索 + 文件存储 + CRUD | 无外部依赖 | 🟢 低 |
| Phase 2 | 纯 Python TF-IDF 向量化 + 混合检索 | 无外部依赖 | 🟡 中 |
| Phase 3 | 自动摘要 + LLM 辅助摘要 | 需要 LLM 调用 | 🟡 中 |

NEUAgent 的 `b5_memory_advanced.py::_text_to_vector()` 使用纯 Python 实现 TF-IDF 向量化（hash + MD5），完全不需要下载任何模型。可以直接复用。

**记忆存储结构**：
```
code_agent/outputs/memory/
├── memory_index.json          ← 记忆元数据索引
├── vectors.json               ← 向量嵌入
├── global/                    ← 全局记忆
│   └── mem_global_xxx.md
└── conversations/             ← 对话记忆
    └── mem_conv_xxx.md
```

**记忆注入时机**：
- **对话开始时**：根据用户 query 检索相关记忆 → 注入 system prompt
- **每轮 LLM 调用前**：根据最近对话内容动态刷新记忆

---

## 改造点 6：上下文压缩 — LLM 摘要 + 保留最近 K 轮

### 可行性分析

**触发条件**：当消息列表超过阈值（如 20 条消息或 10 轮对话）时自动触发。

**压缩流程**（复用 NEUAgent 的 `_compress_messages`）：
```
1. 找到最近 K 轮（保留不压缩）
2. 旧消息 → _format_messages_for_summary() → 纯文本
   - 工具结果 → 一句话摘要（如 "calculator = 900"）
   - 用户/助手消息 → 截断到 100 字符
   - System 消息 → 跳过
3. 调 LLM 生成合并摘要（旧摘要 + 新对话 → 一条新摘要）
4. 重建 system message:
   [TEMPLATE] + [RULES] + [MEMORY] + [SUMMARY]
5. 保留最近 K 条消息 + 新的 system message
```

**关键设计原则**：不保留原始 JSON（大段 JSON 会干扰模型摘要质量），而是转成人类可读的短文本描述。

---

## 执行 Prompt：6 个 Phase 的完整实现指令

> **以下指令供 coding agent 逐步执行。每个 Phase 相对独立，完成后应可独立测试验证。**

---

### Phase 1: Skill 统一注册层

**目标**：建立统一的 Skill 注册表，包含 code_agent 原有 6 个插件 + NEUAgent 7 个 Skill + 2 个 NaturalCC 核心能力。

**具体任务**：

1. **复制 NEUAgent Skills 到 code_agent**

```
将 D:\...\NEUAgent\skills\ 下的以下文件复制到 code_agent\skills\：
  - calculator.py
  - file_reader.py
  - local_file_search.py
  - table_analyzer.py
  - format_converter.py
  - code_executor.py
  - read_and_convert.py
  - __init__.py

同时复制 NEUAgent\code\common\ 下的依赖文件到 code_agent\skills\common\：
  - schemas.py  (或提取 make_skill_result 函数)
  - error_codes.py
  - io_utils.py 中 read_text / read_json / write_json / write_text
  - path_utils.py 中 DEFAULT_DATA_ROOT / resolve_cli_path / resolve_from_file

注意：修改所有 import 路径，从 from common.xxx 改为 from .common.xxx
```

2. **创建 `code_agent/skill_registry.py`**

```python
"""
统一的 Skill 注册表。

设计原则：
- 每个 Skill 有三个要素：元信息、参数 Schema、执行函数
- 支持三种来源：code_agent 插件 / NEUAgent Skills / NaturalCC 核心能力
- 对外提供统一的 get_tool_definitions() → OpenAI function-calling 格式
"""

from dataclasses import dataclass, field
from typing import Dict, List, Any, Callable, Optional
import importlib


@dataclass
class SkillDefinition:
    """一个 Skill 的完整定义"""
    name: str                          # 唯一标识，如 "code_completion"
    description: str                   # 描述，给 LLM 看
    parameters: Dict[str, Any]         # JSON Schema 格式的参数定义
    required: List[str] = field(default_factory=list)
    execute_fn: Optional[Callable] = None  # 执行函数
    source: str = "code_agent"         # "code_agent" | "neuagent" | "naturalcc"


class SkillRegistry:
    """统一 Skill 注册表（单例）"""

    _instance = None
    _skills: Dict[str, SkillDefinition] = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._skills = {}
        return cls._instance

    def register(self, skill: SkillDefinition):
        self._skills[skill.name] = skill

    def get(self, name: str) -> Optional[SkillDefinition]:
        return self._skills.get(name)

    def list_all(self) -> List[SkillDefinition]:
        return list(self._skills.values())

    def get_tool_definitions(self) -> List[dict]:
        """生成 OpenAI function-calling 格式的 tool definitions。

        这是整个改造的关键输出——模型通过这个列表"看到"所有可用工具。
        """
        tools = []
        for skill in self._skills.values():
            tools.append({
                "type": "function",
                "function": {
                    "name": skill.name,
                    "description": skill.description,
                    "parameters": {
                        "type": "object",
                        "properties": skill.parameters,
                        "required": skill.required,
                    },
                },
            })
        return tools

    def execute(self, name: str, args: dict, context: dict = None) -> dict:
        """执行一个 Skill。

        Args:
            name: Skill 名称
            args: 参数（来自 LLM 的 tool_call arguments）
            context: 额外上下文（project_dir, target_files 等）

        Returns:
            {"status": "success"|"error", "output": ..., "error": ...}
        """
        skill = self._skills.get(name)
        if skill is None:
            return {"status": "error", "error": f"Unknown skill: {name}"}

        if skill.execute_fn is None:
            return {"status": "error", "error": f"Skill {name} has no execute function"}

        try:
            result = skill.execute_fn(args, context or {})
            return {"status": "success", "output": result}
        except Exception as e:
            return {"status": "error", "error": str(e)}


# 全局单例
skill_registry = SkillRegistry()
```

3. **注册所有 Skill**

创建 `code_agent/skill_registry_init.py`，在模块导入时自动注册所有 Skill：

```python
"""
自动注册所有 Skill 到 skill_registry。
导入此模块即完成注册。
"""
from code_agent.skill_registry import skill_registry, SkillDefinition

# ===== code_agent 插件 → Skill =====
def _register_code_agent_plugins():
    from code_agent.plugins.registry import registry as plugin_registry

    # code_completion
    plugin = plugin_registry.get("code_completion")
    if plugin:
        skill_registry.register(SkillDefinition(
            name="code_completion",
            description="基于项目语义图谱的智能代码补全。根据符号名、补全类型（member/variable/function/function_body/type）在目标文件中完成代码补全或修正。",
            parameters={
                "symbol": {"type": "string", "description": "目标符号名称，如函数名、变量名。留空则自动推断。"},
                "completion_type": {"type": "string", "enum": ["member", "variable", "function", "function_body", "type"], "description": "补全类型"},
                "prefix": {"type": "string", "description": "前缀过滤，用于缩小候选范围"},
            },
            required=[],
            execute_fn=lambda args, ctx: _execute_plugin("code_completion", args, ctx),
            source="code_agent",
        ))

    # code_repair
    plugin = plugin_registry.get("code_repair")
    if plugin:
        skill_registry.register(SkillDefinition(
            name="code_repair",
            description="代码修复工具。根据错误日志和上下文定位根因，做最小必要修改。支持 bug_fix / compile_error / test_failure / safe_refactor 四种修复类型。",
            parameters={
                "repair_type": {"type": "string", "enum": ["bug_fix", "compile_error", "test_failure", "safe_refactor"], "description": "修复类型"},
                "failure_log": {"type": "string", "description": "编译错误、测试失败、堆栈或运行时报错信息"},
                "extra_context": {"type": "string", "description": "额外上下文：约束条件、期望行为、复现步骤"},
            },
            required=[],
            execute_fn=lambda args, ctx: _execute_plugin("code_repair", args, ctx),
            source="code_agent",
        ))

    # code_summary
    plugin = plugin_registry.get("code_summary")
    if plugin:
        skill_registry.register(SkillDefinition(
            name="code_summary",
            description="代码总结工具。对选中文件或整个项目做结构化代码总结，覆盖职责、关键流程、重要符号和依赖关系。不会修改任何文件。",
            parameters={
                "summary_scope": {"type": "string", "enum": ["targets", "project"], "description": "总结范围：targets=仅选中文件，project=全项目源码"},
                "detail_level": {"type": "string", "enum": ["brief", "standard", "detailed"], "description": "详细程度"},
                "include_symbols": {"type": "boolean", "description": "是否包含关键函数、类型、数据流"},
            },
            required=[],
            execute_fn=lambda args, ctx: _execute_plugin("code_summary", args, ctx),
            source="code_agent",
        ))

    # vulnerability_detection
    plugin = plugin_registry.get("vulnerability_detection")
    if plugin:
        skill_registry.register(SkillDefinition(
            name="vulnerability_detection",
            description="漏洞检测工具。对目标文件做静态模式扫描，检测缓冲区溢出、SQL注入、命令注入、路径穿越、格式化字符串等安全漏洞。返回结构化报告。",
            parameters={
                "scan_scope": {"type": "string", "enum": ["targets", "project"], "description": "扫描范围"},
                "severity_threshold": {"type": "string", "enum": ["low", "medium", "high", "critical"], "description": "最低告警级别"},
                "auto_fix": {"type": "boolean", "description": "是否自动修复发现的漏洞"},
            },
            required=[],
            execute_fn=lambda args, ctx: _execute_plugin("vulnerability_detection", args, ctx),
            source="code_agent",
        ))

    # knowledge_graph
    plugin = plugin_registry.get("knowledge_graph")
    if plugin:
        skill_registry.register(SkillDefinition(
            name="knowledge_graph",
            description="知识图谱生成工具。解析项目代码结构，生成交互式可视化知识图谱HTML，展示模块、符号之间的依赖关系。",
            parameters={
                "language": {"type": "string", "enum": ["c", "java"], "description": "项目语言"},
            },
            required=["language"],
            execute_fn=lambda args, ctx: _execute_plugin("knowledge_graph", args, ctx),
            source="code_agent",
        ))


def _execute_plugin(plugin_name: str, args: dict, context: dict) -> dict:
    """将插件执行包装为 Skill 调用"""
    from code_agent.plugins.registry import registry as plugin_registry
    from code_agent.plugins.base import ExecutionContext

    plugin = plugin_registry.get(plugin_name)
    if plugin is None:
        return {"status": "error", "message": f"Plugin {plugin_name} not found"}

    ctx = ExecutionContext(
        project_dir=context.get("project_dir", ""),
        target_files=context.get("target_files", []),
        instruction=context.get("instruction", ""),
        model=context.get("model", "deepseek/deepseek-chat"),
        api_key=context.get("api_key"),
        feature_config={"feature": plugin_name, **args},
        symbol=args.get("symbol"),
        completion_type=args.get("completion_type"),
        prefix=args.get("prefix", ""),
    )

    outputs = []
    for item in plugin.execute(ctx):
        if hasattr(item, 'log'):
            outputs.append(item.log)
        elif isinstance(item, str):
            outputs.append(item)

    return {"status": "success", "output": "\n".join(outputs)}


# ===== NEUAgent Skills =====
def _register_neuagent_skills():
    """注册 NEUAgent 的 7 个通用技能"""
    from skills.calculator import calculator
    from skills.file_reader import file_reader
    from skills.local_file_search import local_file_search
    from skills.table_analyzer import table_analyzer
    from skills.format_converter import format_converter
    from skills.code_executor import code_executor
    from skills.read_and_convert import read_and_convert

    skill_registry.register(SkillDefinition(
        name="calculator",
        description="安全地计算数学表达式。支持加减乘除、幂运算、三角函数、对数等。",
        parameters={
            "expression": {"type": "string", "description": "算术表达式，如 '(100+200)*3'"},
        },
        required=["expression"],
        execute_fn=lambda args, ctx: calculator(**args),
        source="neuagent",
    ))

    skill_registry.register(SkillDefinition(
        name="file_reader",
        description="读取本地UTF-8文本文件（txt/md）。返回文件内容、字符数和截断状态。",
        parameters={
            "path": {"type": "string", "description": "文件路径（相对或绝对）"},
            "max_chars": {"type": "integer", "description": "最大返回字符数，默认2000"},
        },
        required=["path"],
        execute_fn=lambda args, ctx: file_reader(**args, data_root=ctx.get("data_root")),
        source="neuagent",
    ))

    skill_registry.register(SkillDefinition(
        name="local_file_search",
        description="在本地文件中搜索关键词。支持指定目录和文件类型过滤。",
        parameters={
            "query": {"type": "string", "description": "搜索关键词"},
            "root_dir": {"type": "string", "description": "搜索根目录，默认为data目录"},
            "file_types": {"type": "array", "items": {"type": "string"}, "description": "文件扩展名过滤，如 ['txt', 'md']"},
            "top_k": {"type": "integer", "description": "最多返回条数，默认5"},
        },
        required=["query"],
        execute_fn=lambda args, ctx: local_file_search(**args, data_root=ctx.get("data_root")),
        source="neuagent",
    ))

    skill_registry.register(SkillDefinition(
        name="table_analyzer",
        description="分析CSV/TSV表格文件。返回行数、列数、列名、预览行和数值统计。",
        parameters={
            "path": {"type": "string", "description": "表格文件路径"},
            "max_rows_preview": {"type": "integer", "description": "预览行数，默认5"},
            "describe": {"type": "boolean", "description": "是否计算数值统计，默认true"},
        },
        required=["path"],
        execute_fn=lambda args, ctx: table_analyzer(**args, data_root=ctx.get("data_root")),
        source="neuagent",
    ))

    skill_registry.register(SkillDefinition(
        name="format_converter",
        description="将文本转换为 Markdown 列表或结构化 JSON，并写入文件。",
        parameters={
            "text": {"type": "string", "description": "要转换的文本"},
            "target_format": {"type": "string", "enum": ["markdown", "json"], "description": "目标格式"},
            "output_filename": {"type": "string", "description": "输出文件名（可选）"},
        },
        required=["text", "target_format"],
        execute_fn=lambda args, ctx: format_converter(**args, output_dir=ctx.get("output_dir")),
        source="neuagent",
    ))

    skill_registry.register(SkillDefinition(
        name="code_executor",
        description="在受限沙箱中执行 Python 代码，带超时和资源限制。用于验证代码逻辑或运行计算。",
        parameters={
            "code": {"type": "string", "description": "要执行的 Python 源代码"},
            "timeout_seconds": {"type": "integer", "description": "最大执行秒数（1-30），默认5"},
        },
        required=["code"],
        execute_fn=lambda args, ctx: code_executor(**args),
        source="neuagent",
    ))

    skill_registry.register(SkillDefinition(
        name="read_and_convert",
        description="读取文本/markdown文件并将其转换为 Markdown 列表或结构化 JSON。",
        parameters={
            "path": {"type": "string", "description": "源文件路径"},
            "target_format": {"type": "string", "enum": ["markdown", "json"], "description": "目标格式"},
            "max_chars": {"type": "integer", "description": "最大读取字符数，默认5000"},
            "output_filename": {"type": "string", "description": "输出文件名（可选）"},
        },
        required=["path", "target_format"],
        execute_fn=lambda args, ctx: read_and_convert(**args, data_root=ctx.get("data_root"), output_dir=ctx.get("output_dir")),
        source="neuagent",
    ))


# ===== NaturalCC 核心能力 =====
def _register_naturalcc_skills():
    """注册 NaturalCC 的项目解析和搜索能力"""
    skill_registry.register(SkillDefinition(
        name="naturalcc_parse",
        description="解析项目代码，构建语义图谱。提取函数、变量、结构体、类型、导入关系等。这是代码分析和补全的前置步骤。",
        parameters={
            "project_dir": {"type": "string", "description": "项目根目录路径"},
            "language": {"type": "string", "enum": ["c", "java"], "description": "项目语言，默认从文件后缀推断"},
        },
        required=["project_dir"],
        execute_fn=lambda args, ctx: _execute_naturalcc_parse(args),
        source="naturalcc",
    ))

    skill_registry.register(SkillDefinition(
        name="naturalcc_search",
        description="在项目的语义图谱中搜索符号信息。可以查找函数定义、类型声明、成员变量、调用关系等。",
        parameters={
            "symbol": {"type": "string", "description": "要搜索的符号名称"},
            "file_path": {"type": "string", "description": "限定搜索的文件路径（可选）"},
            "search_type": {"type": "string", "enum": ["definition", "usages", "members", "type_info"], "description": "搜索类型"},
        },
        required=["symbol"],
        execute_fn=lambda args, ctx: _execute_naturalcc_search(args, ctx),
        source="naturalcc",
    ))

    # ★ Aider 代码编辑——作为 Skill
    skill_registry.register(SkillDefinition(
        name="code_edit",
        description="使用 Aider 在目标文件中执行代码编辑。这是代码修改的核心工具——当需要修改、补全或重构代码时使用此工具。",
        parameters={
            "instruction": {"type": "string", "description": "具体的编辑指令，描述需要做什么修改"},
            "target_files": {"type": "array", "items": {"type": "string"}, "description": "要编辑的文件路径列表"},
            "project_dir": {"type": "string", "description": "项目根目录"},
        },
        required=["instruction", "target_files"],
        execute_fn=lambda args, ctx: _execute_aider_edit(args, ctx),
        source="naturalcc",
    ))


def _execute_naturalcc_parse(args: dict) -> dict:
    from code_agent.completion_prompt_agent import CompletionPromptAgent
    agent = CompletionPromptAgent()
    agent.load_project(args["project_dir"], args.get("language", "c"))
    return {
        "status": "success",
        "project_dir": args["project_dir"],
        "language": agent.language,
        "file_count": len(agent.parse_res) if agent.parse_res else 0,
    }


def _execute_naturalcc_search(args: dict, context: dict) -> dict:
    from code_agent.completion_prompt_agent import CompletionPromptAgent
    agent = CompletionPromptAgent()
    proj_dir = context.get("project_dir", args.get("project_dir", ""))
    agent.load_project(proj_dir, args.get("language", "c"))

    # 搜索符号
    results = []
    symbol = args["symbol"]
    for fpath, finfo in (agent.parse_res or {}).items():
        if symbol in finfo:
            results.append({
                "file": fpath,
                "name": symbol,
                "type": finfo[symbol].get("type"),
                "def": finfo[symbol].get("def", "")[:500],
            })
    return {"status": "success", "symbol": symbol, "matches": len(results), "results": results[:20]}


def _execute_aider_edit(args: dict, context: dict) -> dict:
    """通过 Aider 执行代码编辑"""
    from code_agent.aider_runner import run_aider_stream

    outputs = []
    for log in run_aider_stream(
        target_files=args.get("target_files", context.get("target_files", [])),
        user_instruction=args["instruction"],
        model=context.get("model", "deepseek/deepseek-chat"),
        api_key=context.get("api_key"),
        project_dir=args.get("project_dir", context.get("project_dir", "")),
    ):
        outputs.append(log)

    return {"status": "success", "output": "\n".join(outputs)}


# ★ 模块导入时自动注册所有 Skill
_register_code_agent_plugins()
_register_neuagent_skills()
_register_naturalcc_skills()
```

---

### Phase 2: Agent Loop 核心引擎

**目标**：实现 `while tool_calls:` 循环，替代原有的一次性 pipeline。

**具体任务**：

1. **创建 `code_agent/agent_loop.py`**

```python
"""
Agent Loop —— 模型自主推理 + 工具调用的核心引擎。

设计原则：
- 模型通过 OpenAI function-calling 自主决定调用哪些工具
- 工具结果回传给模型，模型可以继续调用工具或给出最终回答
- Aider 降级为一个可选的代码编辑工具（code_edit skill）
- NaturalCC 解析和搜索也作为工具供模型调用
"""

import json
import sys
from pathlib import Path
from typing import Generator, List, Dict, Any, Optional
from datetime import datetime


# 导入 skill 注册表（自动注册所有 Skill）
import code_agent.skill_registry_init  # noqa: F401 — side-effect import
from code_agent.skill_registry import skill_registry


MAX_TOOL_ROUNDS = 8        # 最多工具调用轮次
MAX_RETRY_PARSE = 2        # JSON 解析失败重试次数


def _call_llm(
    messages: List[dict],
    tools: List[dict],
    model: str,
    api_key: str,
    api_base: str = "https://api.deepseek.com/v1",
) -> dict:
    """
    调用 LLM API（支持 function calling）。

    当前使用 DeepSeek API（OpenAI 兼容），后续可扩展其他后端。
    参考 NEUAgent b4_local_agent_llm.py 的 _openai_generate()。
    """
    from openai import OpenAI

    client = OpenAI(api_key=api_key, base_url=api_base)

    # 清理消息格式（与 NEUAgent _openai_generate 保持一致）
    clean_messages = []
    for msg in messages:
        m = dict(msg)
        role = m.get("role")
        if role == "tool":
            clean = {"role": "tool", "tool_call_id": m.get("tool_call_id", "")}
            if m.get("content") is not None:
                clean["content"] = str(m["content"])
            clean_messages.append(clean)
        elif role == "assistant":
            tcs = m.get("tool_calls", [])
            if not tcs:
                m.pop("tool_calls", None)
            else:
                # 转换为 OpenAI function 格式
                m["tool_calls"] = [
                    {
                        "id": tc.get("id", f"call_{i:03d}"),
                        "type": "function",
                        "function": {
                            "name": tc["name"],
                            "arguments": json.dumps(tc.get("args", {}), ensure_ascii=False),
                        },
                    }
                    for i, tc in enumerate(tcs)
                ]
            clean_messages.append(m)
        else:
            clean_messages.append(m)

    response = client.chat.completions.create(
        model=model,
        messages=clean_messages,
        tools=tools if tools else None,
        tool_choice="auto" if tools else None,
        max_tokens=4096,
    )

    choice = response.choices[0]
    msg = choice.message

    tool_calls = []
    if msg.tool_calls:
        for tc in msg.tool_calls:
            try:
                args = json.loads(tc.function.arguments)
            except (json.JSONDecodeError, TypeError):
                args = {}
            tool_calls.append({
                "id": tc.id,
                "name": tc.function.name,
                "args": args,
            })

    return {
        "content": msg.content or "",
        "tool_calls": tool_calls,
        "input_tokens": response.usage.prompt_tokens if response.usage else 0,
        "output_tokens": response.usage.completion_tokens if response.usage else 0,
    }


def _build_system_prompt(project_dir: str, target_files: List[str],
                          memory_context: str = "") -> str:
    """构造 system prompt，包含工具使用规则。"""
    prompt = f"""你是一个由 NaturalCC 驱动的智能代码助手（Coding Agent）。

## 你的能力
你可以使用以下工具来帮助用户完成编程任务：
- **naturalcc_parse**: 解析项目代码，构建语义图谱
- **naturalcc_search**: 搜索符号定义、类型信息、调用关系
- **code_edit**: 使用 Aider 在文件中执行代码编辑（补全、修复、重构）
- **code_summary**: 对代码做结构化总结（不修改文件）
- **code_repair**: 根据错误日志做最小化修复
- **vulnerability_detection**: 扫描安全漏洞
- **knowledge_graph**: 生成项目知识图谱可视化
- **calculator**: 数学计算
- **file_reader**: 读取文件内容
- **local_file_search**: 在文件中搜索关键词
- **code_executor**: 在沙箱中执行 Python 代码

## 工作流程
1. 分析用户需求，确定需要哪些步骤
2. 如果需要了解代码结构，先调用 naturalcc_parse 解析项目
3. 如果需要查看代码细节，调用 naturalcc_search 或 file_reader
4. 如果需要修改代码，调用 code_edit
5. 如果需要验证修改，调用 code_executor 运行测试
6. 逐步完成所有步骤后，给出最终总结

## 规则
- 每次可以调用一个或多个工具
- 工具结果会返回给你，你可以据此决定下一步
- 如果当前信息足够完成任务，就不要重复调用相同工具
- 编辑代码前，确保已经充分理解代码结构
- 使用 code_edit 时，给出明确、具体的修改指令

## 当前项目信息
- 项目目录: {project_dir}
- 目标文件: {', '.join(target_files) if target_files else '未指定'}
"""

    if memory_context:
        prompt += f"\n## 相关历史记忆\n{memory_context}\n"

    return prompt


def run_agent_loop(
    user_instruction: str,
    model: str,
    api_key: str,
    project_dir: str,
    target_files: List[str] = None,
    max_turns: int = MAX_TOOL_ROUNDS,
    session_messages: List[dict] = None,
    memory_context: str = "",
    event_callback = None,
) -> Generator[str, None, None]:
    """
    Agent 主循环 —— 模型自主推理 + 工具调用。

    这是整个改造的核心函数，替代原有的 pipeline 模式。

    Args:
        user_instruction: 用户需求
        model: LLM 模型名
        api_key: API 密钥
        project_dir: 项目根目录
        target_files: 目标文件列表
        max_turns: 最大工具调用轮次
        session_messages: 已有的对话历史（续写场景）
        memory_context: 记忆系统注入的上下文
        event_callback: 事件回调（可选，用于流式推送）

    Yields:
        NDJSON 事件字符串
    """
    target_files = target_files or []
    tools = skill_registry.get_tool_definitions()

    # 初始化消息列表
    if session_messages:
        messages = list(session_messages)
    else:
        system_prompt = _build_system_prompt(project_dir, target_files, memory_context)
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_instruction},
        ]

    tool_rounds = 0
    total_input_tokens = 0
    total_output_tokens = 0

    # 事件辅助函数
    def _emit(event_type: str, **kwargs):
        event = {"type": event_type, "timestamp": datetime.now().isoformat(), **kwargs}
        if event_callback:
            event_callback(event)
        return json.dumps(event, ensure_ascii=False) + "\n"

    yield _emit("start", status="running", tools=[t["function"]["name"] for t in tools])

    # ★ 核心循环
    while tool_rounds < max_turns:
        yield _emit("llm_start", round=tool_rounds + 1, max_turns=max_turns)

        # Step 1: LLM 推理
        try:
            llm_result = _call_llm(messages, tools, model, api_key)
        except Exception as e:
            yield _emit("error", message=f"LLM调用失败: {e}")
            break

        total_input_tokens += llm_result.get("input_tokens", 0)
        total_output_tokens += llm_result.get("output_tokens", 0)

        # Step 2: 判断是否结束
        if not llm_result["tool_calls"]:
            # ★ 模型给出了最终回答
            assistant_msg = {
                "role": "assistant",
                "content": llm_result["content"],
                "tool_calls": [],
            }
            messages.append(assistant_msg)

            yield _emit("done", status="success",
                       content=llm_result["content"],
                       tool_rounds=tool_rounds,
                       input_tokens=total_input_tokens,
                       output_tokens=total_output_tokens)
            return

        # 追加 assistant 消息
        assistant_msg = {
            "role": "assistant",
            "content": llm_result["content"],
            "tool_calls": llm_result["tool_calls"],
        }
        messages.append(assistant_msg)

        yield _emit("llm_end", round=tool_rounds + 1,
                   tool_calls=[tc["name"] for tc in llm_result["tool_calls"]])

        # Step 3: 执行工具
        for tc in llm_result["tool_calls"]:
            tool_name = tc["name"]
            tool_args = tc["args"]

            yield _emit("tool_call_start", name=tool_name, args=tool_args)

            # 构造执行上下文
            exec_context = {
                "project_dir": project_dir,
                "target_files": target_files,
                "instruction": user_instruction,
                "model": model,
                "api_key": api_key,
                "data_root": str(Path(project_dir) / "data"),
                "output_dir": str(Path(project_dir) / "outputs" / "skills"),
            }

            # 执行
            result = skill_registry.execute(tool_name, tool_args, exec_context)

            # 构造 tool 消息
            tool_msg = {
                "role": "tool",
                "tool_call_id": tc["id"],
                "name": tool_name,
                "content": json.dumps(result, ensure_ascii=False),
            }
            messages.append(tool_msg)

            yield _emit("tool_result", name=tool_name,
                       status=result.get("status"),
                       summary=str(result)[:500])

        tool_rounds += 1

    # 超过最大轮次
    yield _emit("done", status="max_turns_exceeded",
               message=f"超过最大工具调用轮次({max_turns})",
               tool_rounds=tool_rounds,
               input_tokens=total_input_tokens,
               output_tokens=total_output_tokens)
```

2. **在 Web API 中集成 Agent Loop**

修改 `agent_web_api.py`，新增 Agent 模式端点：

```python
# 新增导入
from code_agent.agent_loop import run_agent_loop
from code_agent.session_manager import get_or_create_session  # Phase 4

@app.post("/api/agent/run")
async def agent_run(request: AgentRequest):
    """
    新版 Agent 模式端点——模型自主决策，多轮工具调用。

    与旧版 /api/run（用户手动选模块）并存，逐步迁移。
    """
    project_dir = normalize_project_dir(request.project_dir)
    target_files = sanitize_target_files(request.target_files)

    # 获取或创建 session
    session = get_or_create_session(
        session_id=request.session_id,
        project_dir=project_dir,
        target_files=target_files,
        model=request.model or DEFAULT_MODEL,
    )

    # 添加用户消息
    session.add_message("user", request.instruction)

    async def _stream():
        for event in run_agent_loop(
            user_instruction=request.instruction,
            model=request.model or DEFAULT_MODEL,
            api_key=request.api_key,
            project_dir=project_dir,
            target_files=target_files,
            session_messages=session.messages,
        ):
            yield event
            await asyncio.sleep(0)

        # 自动保存 session
        session.save()

    return StreamingResponse(
        _stream(),
        media_type="application/x-ndjson",
        headers=STREAM_HEADERS,
    )
```

---

### Phase 3: 模型决策 Prompt 设计

**目标**：让模型清楚地理解每个工具的用途、参数和使用时机。

**核心原则**：
1. Tool definition 中的 `description` 是模型决策的关键依据，必须准确、具体
2. System prompt 中的使用规则要明确"什么时候用什么工具"
3. 支持模型在单轮中调用多个工具（并行执行独立任务）

**System Prompt 模板**（已内置在 Phase 2 的 `_build_system_prompt()` 中，此处独立展示）：

```
你是一个由 NaturalCC 驱动的智能代码助手（Coding Agent）。

## 你的能力
你可以使用以下工具来帮助用户完成编程任务：
[TOOL_DEFINITIONS]  ← 运行时自动注入

## 工作流程
1. 分析用户需求，确定需要哪些步骤
2. 如果需要了解代码结构，先调用 naturalcc_parse 解析项目
3. 如果需要查看代码细节，调用 naturalcc_search 或 file_reader
4. 如果需要修改代码，调用 code_edit
5. 如果需要验证修改，调用 code_executor 运行测试
6. 逐步完成所有步骤后，给出最终总结

## 规则
- 每次可以调用一个或多个工具
- 工具结果会返回给你，你可以据此决定下一步
- 如果当前信息足够完成任务，就不要重复调用相同工具
- 编辑代码前，确保已经充分理解代码结构
- 使用 code_edit 时，给出明确、具体的修改指令
```

**工具描述设计原则**（已在 Phase 1 的 SkillDefinition 中落实）：
- `code_edit` 的描述强调"需要修改代码时使用"
- `naturalcc_parse` 的描述强调"这是代码分析的前置步骤"
- `code_summary` 的描述强调"不修改文件"
- 每个描述都给出了典型使用场景

---

### Phase 4: Session 持久化

**目标**：支持多轮对话，Session 可持久化和恢复。

1. **创建 `code_agent/session_manager.py`**

```python
"""
Session 管理器 —— 多轮对话的状态持久化。

每个 session 以 JSON 文件存储在 outputs/sessions/{session_id}/session.json。
支持创建、读取、更新、删除、列表。
"""
import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional


SESSION_ROOT = Path(__file__).resolve().parent / "outputs" / "sessions"
SESSION_ROOT.mkdir(parents=True, exist_ok=True)


class Session:
    def __init__(self, session_id: str = None, **kwargs):
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.messages: List[dict] = kwargs.get("messages", [])
        self.project_dir: str = kwargs.get("project_dir", "")
        self.target_files: List[str] = kwargs.get("target_files", [])
        self.model: str = kwargs.get("model", "deepseek/deepseek-chat")
        self.turn_index: int = kwargs.get("turn_index", 0)
        self.token_stats: Dict[str, int] = kwargs.get("token_stats", {"input": 0, "output": 0})
        self.status: str = kwargs.get("status", "idle")
        self.created_at: str = kwargs.get("created_at", datetime.now().isoformat())
        self.updated_at: str = kwargs.get("updated_at", self.created_at)

    def add_message(self, role: str, content: str, **extra):
        msg = {"role": role, "content": content, **extra}
        self.messages.append(msg)
        self.turn_index += 1
        self.updated_at = datetime.now().isoformat()

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "messages": self.messages,
            "project_dir": self.project_dir,
            "target_files": self.target_files,
            "model": self.model,
            "turn_index": self.turn_index,
            "token_stats": self.token_stats,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        return cls(**{k: v for k, v in data.items()
                      if k in cls.__init__.__code__.co_varnames})

    def save(self):
        outdir = SESSION_ROOT / self.session_id
        outdir.mkdir(parents=True, exist_ok=True)
        payload = self.to_dict()
        payload["updated_at"] = datetime.now().isoformat()
        with open(outdir / "session.json", "w", encoding="utf-8") as f:
            json.dump(payload, f, ensure_ascii=False, indent=2)

    @classmethod
    def load(cls, session_id: str) -> Optional["Session"]:
        path = SESSION_ROOT / session_id / "session.json"
        if not path.is_file():
            return None
        with open(path, "r", encoding="utf-8") as f:
            return cls.from_dict(json.load(f))

    @classmethod
    def list_all(cls) -> List[Dict[str, Any]]:
        if not SESSION_ROOT.is_dir():
            return []
        results = []
        for d in sorted(SESSION_ROOT.iterdir(),
                       key=lambda p: p.stat().st_mtime, reverse=True):
            if not d.is_dir():
                continue
            meta = d / "session.json"
            if not meta.is_file():
                continue
            with open(meta, "r", encoding="utf-8") as f:
                data = json.load(f)
            results.append({
                "session_id": data.get("session_id"),
                "updated_at": data.get("updated_at"),
                "message_count": len(data.get("messages", [])),
                "model": data.get("model"),
                "turn_index": data.get("turn_index"),
            })
        return results

    @classmethod
    def delete(cls, session_id: str) -> bool:
        import shutil
        d = SESSION_ROOT / session_id
        if d.is_dir():
            shutil.rmtree(d, ignore_errors=True)
            return True
        return False


# 内存缓存
_active_sessions: Dict[str, Session] = {}


def get_or_create_session(session_id: str = None, **kwargs) -> Session:
    if session_id and session_id in _active_sessions:
        return _active_sessions[session_id]
    if session_id:
        disk = Session.load(session_id)
        if disk:
            _active_sessions[session_id] = disk
            return disk
    s = Session(session_id=session_id, **kwargs)
    _active_sessions[s.session_id] = s
    return s
```

2. **在 agent_web_api.py 中添加 Session 管理路由**

```python
@app.get("/api/sessions")
async def list_sessions():
    return {"sessions": Session.list_all()}

@app.get("/api/sessions/{sid}")
async def get_session(sid: str):
    session = Session.load(sid)
    if session is None:
        raise HTTPException(404, f"Session {sid} not found")
    return session.to_dict()

@app.post("/api/sessions/{sid}/compress")
async def compress_session(sid: str):
    """手动触发上下文压缩"""
    session = Session.load(sid)
    if session is None:
        raise HTTPException(404, f"Session {sid} not found")
    from code_agent.context_compressor import compress_history
    # ... 调用压缩逻辑
    return {"status": "compressed"}

@app.delete("/api/sessions/{sid}")
async def delete_session(sid: str):
    if Session.delete(sid):
        return {"status": "deleted"}
    raise HTTPException(404, f"Session {sid} not found")
```

---

### Phase 5: 记忆系统

**目标**：KV 混合检索 + 文件存储 + 自动注入 system prompt。

**注意**：从最简单实现开始——先用关键词检索 + 文件存储，向量化后续再加。

**参考实现**：直接复用 NEUAgent 的 `b5_memory.py` 和 `b5_memory_advanced.py` 中的关键函数。

**创建 `code_agent/memory_manager.py`**，核心函数：

```python
class MemoryIndex:
    """记忆索引管理器"""

    def search(self, query: str, top_k: int = 5) -> List[Dict]:
        """Phase 1: 纯关键词检索
        遍历 memory_index.json → 对每条记忆做关键词匹配 → 排序取 top_k
        """

    def save(self, memory_type, conversation_id, messages, final_answer) -> str:
        """保存记忆为 Markdown → 更新索引"""

    def inject_memory_context(self, system_prompt: str, query: str, max_chars=2000) -> str:
        """检索相关记忆 → 以 <memory> 标签注入 system prompt"""

    def get_content(self, memory_id) -> str:
        """读取记忆全文"""

    def delete(self, memory_id) -> bool:
        """删除记忆（文件 + 索引）"""

    # Phase 2 再加:
    # def search_hybrid(self, query, top_k, keyword_weight=0.4) -> List[Dict]:
    # def _text_to_vector(self, text, dim=384) -> List[float]:
```

**关键实现细节**（从 NEUAgent 移植）：
- `_extract_keywords(text)`: 中英文关键词提取 + 停用词过滤（代码在 b5_memory_advanced.py:68-95）
- `_keyword_score(query, content)`: Jaccard 相似度（b5_memory_advanced.py:97-104）
- 记忆 `.md` 文件格式（b5_memory.py:166-177）
- `memory_index.json` 结构（b5_memory.py:182-191）
- 记忆注入 system prompt 的方式（b1_agent_runtime.py:62-82 的 `_memory_context()`）

**文件组织结构**：
```
code_agent/memory/
├── memory_index.json
├── global/
│   └── mem_global_xxx.md
└── conversations/
    └── mem_conv_xxx.md
```

---

### Phase 6: 上下文压缩

**目标**：当对话轮次过多时，使用 LLM 将旧消息压缩为摘要。

**创建 `code_agent/context_compressor.py`**，核心函数（从 NEUAgent b1_agent_runtime.py 移植）：

```python
def compress_history(
    messages: List[dict],
    model: str,
    api_key: str,
    keep_last_k: int = 4,
) -> List[dict]:
    """
    压缩对话历史。

    流程:
    1. 保留最近 K 条消息不压缩
    2. 旧消息 → _format_messages_for_summary() → 纯文本
    3. 调 LLM 生成合并摘要（旧摘要 + 新对话 → 新摘要）
    4. 重建 system message: [TEMPLATE] + [SUMMARY]
    5. 返回压缩后的消息列表

    压缩前: 30条消息 → 压缩后: system(含摘要) + 最近4条消息
    """
```

**关键移植函数**：
- `_format_messages_for_summary()`: b1_agent_runtime.py:950-992
- `_summarize_tool_result()`: b1_agent_runtime.py:995-1036，工具结果 → 一句话摘要
- `_summarise_messages_with_model()`: b1_agent_runtime.py:1074-1143
- `_clean_summary_text()`: b1_agent_runtime.py:924-947

**自动触发条件**：在 `run_agent_loop()` 中，每次 LLM 调用前检查消息数量，超过阈值（如 20 条）则自动调用 `compress_history()`。

---

## 总结：改造后的完整调用链路

```
用户发送需求
    │
    ▼
POST /api/agent/run
    │
    ├── 获取/创建 Session
    ├── 记忆检索 → 注入 system prompt
    │
    ▼
┌──────────────────────────────────────────────────────────────────┐
│ run_agent_loop()                                                 │
│                                                                  │
│  messages = [                                                    │
│    {role: "system", content: system_prompt + memory_context},    │
│    {role: "user", content: user_instruction}                     │
│  ]                                                               │
│                                                                  │
│  while tool_rounds < max_turns:                                  │
│    │                                                             │
│    ├─ _call_llm(messages, tools)                                 │
│    │   └── OpenAI API: tools=tool_definitions, tool_choice=auto  │
│    │                                                             │
│    ├─ tool_calls == []? → 最终回答 → break                       │
│    │                                                             │
│    └─ for each tool_call:                                        │
│        ├─ naturalcc_parse    → CProjectParser.parse_dir()        │
│        ├─ naturalcc_search   → CompletionPromptAgent 搜索        │
│        ├─ code_edit          → aider_runner.run_aider_stream()   │
│        ├─ code_summary       → CodeSummaryPlugin                 │
│        ├─ code_repair        → CodeRepairPlugin                  │
│        ├─ vulnerability_*    → VulnerabilityDetectionPlugin      │
│        ├─ calculator         → skills.calculator.calculator()    │
│        ├─ file_reader        → skills.file_reader.file_reader()  │
│        └─ ...                                                    │
│                                                                  │
│  自动压缩检查 → 消息超过阈值 → compress_history()                 │
│                                                                  │
│  ▼                                                               │
│  返回最终结果 → Session.save() → 记忆保存                         │
└──────────────────────────────────────────────────────────────────┘
```

---

## 实施检查清单

| Phase | 新文件 | 修改文件 | 完成标志 |
|-------|--------|---------|---------|
| Phase 1 | `skill_registry.py`, `skill_registry_init.py`, `skills/` (复制) | 无 | 导入 `skill_registry_init` 后 `skill_registry.list_all()` 返回 15 个 Skill |
| Phase 2 | `agent_loop.py` | `agent_web_api.py` (新增端点) | 发送需求 "修复登录 bug 并检查漏洞" → 模型自动调用 code_repair + vulnerability_detection |
| Phase 3 | 无 (内嵌在 Phase 1/2) | 无 | 模型对 "计算性能优化效果" 调 calculator；对 "读取文档" 调 file_reader |
| Phase 4 | `session_manager.py` | `agent_web_api.py` (新增路由) | 发送 5 轮对话 → 检查 `outputs/sessions/` 下有持久化文件 → 重启后恢复 |
| Phase 5 | `memory_manager.py` | `agent_loop.py` (注入记忆) | 对话后自动保存记忆 → 新对话自动检索相关记忆注入 |
| Phase 6 | `context_compressor.py` | `agent_loop.py` (自动压缩) | 20 轮长对话 → 自动压缩 → 上下文不超限 |

---

## 兼容性注意事项

1. **旧版 API 不删除**：`/api/run` 保持原样，新版使用 `/api/agent/run`
2. **Aider 保留**：作为 `code_edit` skill 的底层实现，代码零修改
3. **NaturalCC 解析保留**：`CompletionPromptAgent` 和 `CProjectParser` 完全不改
4. **插件系统保留**：`@register_plugin` 装饰器继续工作，Phase 1 的新增代码通过适配层桥接
5. **前端渐进迁移**：新增 "Agent 模式" 开关，默认仍走旧版 pipeline
