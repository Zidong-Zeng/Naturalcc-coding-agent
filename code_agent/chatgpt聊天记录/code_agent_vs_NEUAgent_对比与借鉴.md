# code_agent vs NEUAgent 对比分析 & 借鉴方案

> 深度对比两个项目的架构差异，提炼 NEUAgent 在模型决策、长程编程、记忆管理方面的核心技术，规划 code_agent 的进化路径。

---

## 一、项目定位对比

| 维度 | code_agent | NEUAgent |
|------|-----------|----------|
| **本质定位** | 语义增强 Prompt 生成器 + Aider 封装层 | 完整的 Tool-Calling Agent 系统 |
| **核心闭环** | 用户选模块 → NaturalCC 生成 Prompt → Aider 单次执行 | LLM 推理 → 工具调用 → 结果回传 → LLM 再推理（循环） |
| **模型角色** | 被动接收 Prompt，无决策权 | 主动分析意图，自主选择工具/Skill |
| **执行模式** | Aider `--message-file` one-shot | Agent Loop：最多 N 轮工具调用，直到模型输出最终回答 |
| **多轮对话** | 不支持（无状态，每次请求独立） | 支持（session 持久化 + checkpoint 恢复） |
| **长程任务** | 不支持 | 支持批量任务 + 规划-执行模式 |
| **记忆系统** | 无 | KV 混合检索 + 向量存储 + 自动摘要 + CRUD |
| **上下文管理** | 无运行时压缩 | LLM 摘要压缩 + 保留最近 K 轮 |
| **Skill/Tool 体系** | 6 个插件（用户手动选择） | 7 个 Skill（模型通过 function calling 自主选择） |

---

## 二、NEUAgent Skill 注册与模型决策机制（核心精华）

### 2.1 三层 Skill 注册架构

```
┌─────────────────────────────────────────────────────────────────┐
│ Layer 1: Skill 函数定义 (skills/*.py)                            │
│                                                                  │
│   纯 Python 函数，每个 Skill 是一个独立模块：                       │
│   skills/calculator.py  → def calculator(expression: str) -> dict │
│   skills/file_reader.py → def file_reader(path: str, ...) -> dict │
│   ...                                                            │
│                                                                  │
│   关键设计：                                                       │
│   - 每个函数签名 = Skill 的参数 Schema                             │
│   - 函数 docstring = Skill 的描述文档                             │
│   - 函数返回值 dict = Skill 的标准化输出（含 status / output / error）│
│   - 支持 data_root 和 output_dir 注入（B2→B3 自动注入）            │
└─────────────────────────────────────────────────────────────────┘
                              ↑
┌─────────────────────────────────────────────────────────────────┐
│ Layer 2: YAML 配置声明 (configs/tools.yaml)                      │
│                                                                  │
│   toolsets:                                                      │
│     basic_tools:                                                 │
│       - calculator                                               │
│       - file_reader                                              │
│       - local_file_search                                        │
│       - table_analyzer                                           │
│       - format_converter                                         │
│       - code_executor                                            │
│       - read_and_convert                                         │
│                                                                  │
│   tools:                                                         │
│     calculator:                                                  │
│       module: skills.calculator     ← Python 模块路径             │
│       function: calculator          ← 函数名                      │
│       description: Calculate a safe arithmetic expression.       │
│       parameters:                   ← 参数 Schema                │
│         expression:                                              │
│           type: string                                           │
│           description: Arithmetic expression                     │
│       required: [expression]                                     │
│       returns:                                                   │
│         result:                                                  │
│           type: number                                           │
│                                                                  │
│   关键设计：                                                       │
│   - 新增 Skill 只需加一个 YAML 定义 + 注册到 toolsets              │
│   - tools.yaml 是 Skill 的"注册表"和"能力清单"                     │
│   - 完全声明式，不改代码即可增减 Skill                              │
└─────────────────────────────────────────────────────────────────┘
                              ↑
┌─────────────────────────────────────────────────────────────────┐
│ Layer 3: OpenAI Function Calling Schema 自动生成 (b3_tool_layer)  │
│                                                                  │
│   get_tools_schema(tools_config, toolset) → List[dict]            │
│                                                                  │
│   从 YAML → OpenAI function-calling 格式的自动转换：               │
│   [                                                              │
│     {                                                            │
│       "type": "function",                                        │
│       "function": {                                              │
│         "name": "calculator",                                    │
│         "description": "Calculate a safe arithmetic expression.",│
│         "parameters": {                                          │
│           "type": "object",                                      │
│           "properties": {                                        │
│             "expression": {"type": "string", "description": "..."}│
│           },                                                     │
│           "required": ["expression"]                             │
│         }                                                        │
│       }                                                          │
│     },                                                           │
│     ...                                                          │
│   ]                                                              │
│                                                                  │
│   同时支持 auto_generate_tools_schema()：                          │
│   - 从 Python 函数签名 + docstring 自动推断 Schema                 │
│   - _parse_docstring() 解析 Args:/Returns: 段落                   │
│   - _python_type_to_json_schema() 类型映射                        │
│   - 对用户透明——写一个 Python 函数 = 自动生成 tool definition      │
└─────────────────────────────────────────────────────────────────┘
```

### 2.2 模型决策的完整闭环（Agent Loop）

```
B1 Agent Runtime (b1_agent_runtime.py) — run_single_turn()

┌─────────────────────────────────────────────────────────────────────────┐
│  while True:   ← ★ 核心循环：模型可以多轮推理，直到给出最终回答           │
│                                                                         │
│    ┌──────────────────────────────────────────┐                        │
│    │ Step 1: 记忆注入                          │                        │
│    │ _refresh_memory_in_system(messages)       │                        │
│    │   → 每轮前根据最近对话重新检索相关记忆      │                        │
│    │   → 替换 system message 的 [MEMORY] 段    │                        │
│    └────────────────┬─────────────────────────┘                        │
│                     ▼                                                   │
│    ┌──────────────────────────────────────────┐                        │
│    │ Step 2: LLM 推理 (B4)                     │                        │
│    │ generate_ai_message(model_cfg, messages,  │                        │
│    │                     tools_schema, mode)    │                        │
│    │                                          │                        │
│    │  ★ 关键：tools_schema 作为 OpenAI 格式     │                        │
│    │    的 functions 参数传给 LLM               │                        │
│    │                                          │                        │
│    │  后端分支：                                │                        │
│    │  ├── backend="openai"                     │                        │
│    │  │   → OpenAI client.chat.completions.create(                       │
│    │  │       tools=tools, tool_choice="auto") │                        │
│    │  │   → 模型自主决定是否调用工具、调用哪个   │                        │
│    │  │                                        │                        │
│    │  └── backend="transformers"               │                        │
│    │      → Qwen apply_chat_template(          │                        │
│    │          tools=qwen_tools)                │                        │
│    │      → _build_prompt_messages() 注入格式指令│                       │
│    │                                          │                        │
│    │  返回: {"ai_message": {"content": ...,    │                        │
│    │           "tool_calls": [...]}}            │                        │
│    └────────────────┬─────────────────────────┘                        │
│                     ▼                                                   │
│    ┌──────────────────────────────────────────┐                        │
│    │ Step 3: 判断是否继续                       │                        │
│    │                                          │                        │
│    │  if tool_calls == []:                     │                        │
│    │      → content 非空 → 最终回答 → break    │                        │
│    │                                          │                        │
│    │  if tool_rounds >= max_turns:             │                        │
│    │      → 超过最大轮次 → 终止 → break         │                        │
│    │                                          │                        │
│    │  else:                                    │                        │
│    │      → 继续执行工具                        │                        │
│    └────────────────┬─────────────────────────┘                        │
│                     ▼                                                   │
│    ┌──────────────────────────────────────────┐                        │
│    │ Step 4: 工具执行 (B3)                      │                        │
│    │ execute_tool_calls(tool_calls,             │                        │
│    │                    tools_config, toolset)  │                        │
│    │                                          │                        │
│    │  for each tool_call:                      │                        │
│    │    ├── _validate_args(args, definition)   │  参数校验              │
│    │    ├── b2_run_skill.run_skill(name, args) │  执行 Skill 函数        │
│    │    │   → importlib.import_module(module)  │                        │
│    │    │   → function(**args)                 │                        │
│    │    │   → 返回标准化 SkillResult            │                        │
│    │    └── make_tool_message(...)              │  构造 ToolMessage      │
│    │                                          │                        │
│    │  返回: tool_messages → 追加到 messages     │                        │
│    └────────────────┬─────────────────────────┘                        │
│                     │                                                   │
│                     ▼  回到 while 循环（Step 2 再次 LLM 推理）           │
│                                                                         │
│    Step 5: 后处理                                                        │
│      - 累积 token_stats（所有 LLM 调用累加）                              │
│      - 清除 retry 占位消息                                               │
│      - 返回最终结果                                                      │
└─────────────────────────────────────────────────────────────────────────┘
```

### 2.3 模型如何"看到"和"选择" Skill

这是 NEUAgent 最核心的设计——**让模型看到所有可用的 Skill，并自主决定调用哪个**：

```
用户输入: "计算 (100+200)*3 的结果，并搜索 docs 目录中和 agent 相关的文件"

    │
    ▼
┌─────────────────────────────────────────────────────────────────────┐
│ B4 构造发给 LLM 的消息                                               │
│                                                                     │
│ system message:                                                      │
│   "You are a local tool-using agent..."                             │
│   + 模板内容 (tool_master.txt)                                       │
│   + 记忆注入: <memory id="mem_001" type="conversation">...</memory>  │
│   + tools_schema (OpenAI function-calling 格式):                     │
│     [                                                               │
│       {"type":"function", "function":{"name":"calculator", ...}},    │
│       {"type":"function", "function":{"name":"file_reader", ...}},   │
│       {"type":"function", "function":{"name":"local_file_search",...}}│
│       ...共7个                                                       │
│     ]                                                               │
│   + 输出格式指令 (prompt_json 模式)                                   │
│                                                                     │
│ user message:                                                        │
│   "计算 (100+200)*3 的结果，并搜索 docs 目录中和 agent 相关的文件"     │
│   + envelope_reminder (强调 JSON 输出格式)                            │
└─────────────────────────────────────────────────────────────────────┘
    │
    ▼  LLM 推理
┌─────────────────────────────────────────────────────────────────────┐
│ LLM 输出:                                                            │
│ {                                                                   │
│   "content": "",                                                    │
│   "tool_calls": [                                                   │
│     {"id": "call_001", "name": "calculator",                        │
│      "args": {"expression": "(100+200)*3"}},                        │
│     {"id": "call_002", "name": "local_file_search",                 │
│      "args": {"query": "agent", "root_dir": "docs"}}                │
│   ]                                                                 │
│ }                                                                   │
│                                                                     │
│ ★ 模型自主选择了 2 个工具，并正确填充了参数                           │
└─────────────────────────────────────────────────────────────────────┘
    │
    ▼  B3 执行所有 tool_calls
    │
    ▼  结果回传给 LLM
┌─────────────────────────────────────────────────────────────────────┐
│ LLM 再次推理（看到工具结果后）:                                       │
│ {                                                                   │
│   "content": "计算结果为 900。在 docs 目录中找到 3 个相关文件：       │
│    agent_intro.txt、tool_calling.md、search_skill_demo.md。",       │
│   "tool_calls": []                                                  │
│ }                                                                   │
│                                                                     │
│ ★ tool_calls 为空 → 这是最终答案 → 循环结束                          │
└─────────────────────────────────────────────────────────────────────┘
```

### 2.4 Skill 模块映射表（B2 调度层）

```python
# b2_run_skill.py — Hard-coded skill registry
SKILL_MODULES = {
    "calculator":          "skills.calculator",
    "file_reader":         "skills.file_reader",
    "local_file_search":   "skills.local_file_search",
    "table_analyzer":      "skills.table_analyzer",
    "format_converter":    "skills.format_converter",
    "code_executor":       "skills.code_executor",
    "read_and_convert":    "skills.read_and_convert",
}

def run_skill(skill_name, input_data, data_root=None, output_dir=None) -> dict:
    # 1. 查表 → import 模块
    module = importlib.import_module(SKILL_MODULES[skill_name])
    function = getattr(module, skill_name)
    # 2. 自动注入 data_root / output_dir（如果函数签名有这些参数）
    # 3. 执行 → 捕获异常 → 标准化返回 SkillResult
    output = function(**kwargs)
    return make_skill_result(skill_name, "success", input_data, output, None, latency_ms)
```

### 2.5 OpenAI Function Calling 的原生支持

```python
# b4_local_agent_llm.py — _openai_generate()
def _openai_generate(config_path, model_config, messages, tools_schema):
    # ★ 将 tools_schema 转换为 OpenAI 原生 tools 参数
    tools = []
    for tool in tools_schema:
        tools.append({
            "type": "function",
            "function": {
                "name": tool["function"]["name"],
                "description": tool["function"]["description"],
                "parameters": tool["function"]["parameters"],
            }
        })

    response = client.chat.completions.create(
        model=model_setting,
        messages=clean_messages,
        tools=tools,               # ★ 原生 function calling
        tool_choice="auto",        # ★ 模型自主选择
        max_tokens=1024,
    )

    # 解析模型返回的 tool_calls
    if message.tool_calls:
        for tool_call in message.tool_calls:
            tool_calls.append({
                "id": tool_call.id,
                "name": tool_call.function.name,
                "args": json.loads(tool_call.function.arguments),
            })
```

---

## 三、NEUAgent 长程编程能力分析

### 3.1 多轮对话与 Session 管理

```
Session 生命周期:
┌──────────────────────────────────────────────────────────────────┐
│                                                                  │
│  POST /api/sessions              → CreateSessionReq → 新建 session│
│  POST /api/sessions/{sid}/run    → 异步执行 + polling 获取事件    │
│  POST /api/sessions/{sid}/compress → 历史压缩（LLM 摘要）         │
│  GET  /api/sessions              → 列出所有 session               │
│  GET  /api/sessions/{sid}        → 加载 session 状态              │
│  DELETE /api/sessions/{sid}      → 删除 session                   │
│                                                                  │
│  持久化格式: outputs/sessions/{sid}/session.json                  │
│  {                                                               │
│    "session_id": "...",                                          │
│    "messages": [...],           ← 完整对话历史                    │
│    "turn_index": 5,                                              │
│    "token_stats": {"input": ..., "output": ...},                 │
│    "model_name": "deepseek-v4-flash",                            │
│    "toolset": "basic_tools",                                     │
│    ...                                                           │
│  }                                                               │
│                                                                  │
│  CLI 恢复:                                                        │
│  python b1_agent_runtime.py --mode advanced_repl --resume         │
│    → 交互式选择历史 session → 继续对话                             │
└──────────────────────────────────────────────────────────────────┘
```

### 3.2 上下文压缩（历史摘要）

```python
# b1_agent_runtime.py — _compress_messages()

def _compress_messages(messages, model_cfg, keep_last_k, mode, ...):
    """
    压缩流程:
    1. 解析 system 分段: template / rules / memory / summary
    2. _format_messages_for_summary() → 旧消息转纯文本（去掉 JSON）
       - 工具结果 → 一句话摘要（如 "calculator = 42"）
       - 用户/助手消息 → 截断到 100 字符
       - System 消息 → 跳过（不参与摘要）
    3. _summarise_messages_with_model() → 调 LLM 生成合并摘要
       - 有旧摘要 → 合并旧摘要 + 新对话
       - 无旧摘要 → 直接压缩新对话
       - 最多重试 3 次
       - 失败兜底: _extract_summary_from_raw() 正则硬提取
    4. 重建 system message:
       [TEMPLATE] + [RULES] + [MEMORY] + [SUMMARY]
    5. 保留最近 K 轮不压缩，其余用摘要替代
    """

    # 压缩前: 30 条消息（15 轮对话）
    # 压缩后: system(含摘要) + 最近 4 条消息（2 轮）
```

**摘要质量保证**:
- `_clean_summary_text()`: 清理 LLM 生成的废话模板句
- `_extract_summary_from_raw()`: JSON 解析失败的兜底正则提取
- `_summarize_tool_result()`: 工具结果 → 人类可读的一句话

### 3.3 批量任务

```python
# b1_agent_runtime.py — run_batch_tasks()

def run_batch_tasks(tasks, model_cfg, tools_cfg, model_name=None):
    """
    每个任务:
      - 全新的 messages（互不污染）
      - 独立的 run_single_turn() 调用
      - 独立的模型选择（任务级 model_name 覆盖全局）
      - 异常隔离：一个任务失败不影响其他
    """
```

### 3.4 规划-执行模式

```
plan_execute 模式:
  用户输入 →
    Phase 1 (planning):
      LLM 分析任务 → 制定执行计划（多工具调用列表）
      → 前端展示计划卡片 → 等待用户确认
    Phase 2 (execution):
      用户确认后 → 执行所有工具调用
    Phase 3 (summarize):
      工具结果汇总 → LLM 生成最终回答
```

---

## 四、NEUAgent 记忆管理系统

### 4.1 三层记忆架构

```
┌─────────────────────────────────────────────────────────────────┐
│ Layer 1: 存储层                                                  │
│                                                                  │
│ memory/                                                          │
│ ├── memory_index.json           ← 记忆索引（元数据）              │
│ │   {                                                            │
│ │     "mem_conversation_abc": {                                  │
│ │       "memory_id": "...",                                      │
│ │       "memory_type": "conversation",                           │
│ │       "title": "Conversation abc",                             │
│ │       "summary": "讨论了用户认证方案...",                        │
│ │       "path": "conversations/abc.md",                          │
│ │       "created_at": "...",                                     │
│ │       "updated_at": "..."                                      │
│ │     }                                                          │
│ │   }                                                            │
│ ├── vectors.json                ← 向量嵌入（纯 Python TF-IDF）    │
│ │   {                                                            │
│ │     "mem_conversation_abc": [0.12, -0.34, 0.56, ...]           │
│ │   }                                                            │
│ ├── global/                     ← 全局记忆（长期知识）             │
│ │   └── mem_course_001.md                                        │
│ └── conversations/              ← 对话记忆（按会话存储）           │
│     └── 38027b933338.md                                          │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ Layer 2: 检索层 (KV 混合检索)                                     │
│                                                                  │
│ search_memory_by_keywords(query, top_k)                          │
│   → 关键词提取 (_extract_keywords)                                │
│     - 英文: 3 字母以上，排除停用词 (100+ stop words)               │
│     - 中文: 2 字以上                                              │
│   → Jaccard 相似度: |query ∩ content| / |query|                  │
│                                                                  │
│ search_memory_by_vector(query, top_k)                            │
│   → _text_to_vector(text, dim=384)                               │
│     - 纯 Python TF-IDF 加权（不需要模型下载）                      │
│     - L2 归一化                                                   │
│   → _cosine_similarity(query_vec, memory_vec)                    │
│                                                                  │
│ auto_select_memories(config_path, query, top_k=5)                │
│   → 关键词检索 top_k*2 + 向量检索 top_k*2                         │
│   → 加权融合: keyword_weight * kw_score                           │
│              + (1-keyword_weight) * vec_score                     │
│   → 排序取 top_k                                                  │
└─────────────────────────────────────────────────────────────────┘

┌─────────────────────────────────────────────────────────────────┐
│ Layer 3: 注入层                                                  │
│                                                                  │
│ 初始化注入（对话开始时）:                                          │
│   load_memory() → selected_memory_docs                           │
│   _memory_context() → <memory> 标签包裹                           │
│   拼接到 system prompt 末尾                                       │
│                                                                  │
│ 动态刷新（每轮 LLM 调用前）:                                       │
│   _refresh_memory_in_system()                                    │
│   → retrieve_memories_for_turn(messages, top_k=5, budget=...)    │
│     - 取最近 2 轮非 system 消息作为 query                          │
│     - 混合检索 → 按 token 预算截断                                 │
│   → build_memory_block(docs) → <memory> 标签块                    │
│   → 替换 system message 的 [MEMORY]...[/MEMORY] 段               │
└─────────────────────────────────────────────────────────────────┘
```

### 4.2 记忆持久化与更新

```
save_memory / save_memory_advanced:
  对话结束后:
    1. 生成 .md 文件（结构化 Markdown）
       # Conversation abc
       - memory_id: `mem_conversation_abc`
       ...
       ## Final Answer
       ...
       ## Messages
       ```json ... ```
       ## Trace
       ```json ... ```
    2. 更新 memory_index.json
    3. 更新 vectors.json
    4. 自动摘要 (auto_summarize=True):
       - 长文本 → _call_b4_for_summary() 调 LLM 生成摘要
       - LLM 不可用 → _simple_summarize() 抽取式摘要

update_memory:
  更新已有记忆:
    1. 冲突检测: _detect_conflicts(old, new) → 句子级相似度比对
    2. 智能合并: _merge_contents(old, new, strategy="smart")
       - Markdown 结构解析
       - 按 Section 合并: 相似度>0.9 用新版, 0.6-0.9 智能合并, <0.6 保留两份
    3. 更新向量

CRUD 支持:
  - get_memory_content(memory_id) → 读取完整内容
  - update_memory_content(memory_id, title, content) → 覆写
  - delete_memory(memory_id) → 删 .md + vectors + index

记忆检索 API（前端）:
  - GET /api/memory/search?query=xxx → 混合检索结果
  - POST /api/sessions/{sid}/memory → 切换选中记忆
```

### 4.3 Token 预算机制

```python
# 记忆注入有严格的 token 预算控制
max_memory_chars: 2000       # 单条记忆最大字符数（可配置）
budget_chars: 2000           # 每轮记忆注入的总字符预算

# 加载记忆时按预算截断
remaining = max_memory_chars
for memory_id in ordered_ids:
    included = original[:remaining]
    remaining -= len(included)
    if remaining <= 0: break
```

---

## 五、code_agent 可借鉴的技术方案

### 5.1 模型决策能力改造

#### 当前问题

```python
# code_agent 当前: 模型完全不知道有哪些模块可用
# agent_web_api.py
feature: str = Field(default="code_completion")  # ← 用户/前端决定

# dispatcher.py
feature_name = context.feature_config.get("feature")  # ← 被动读取
plugin = registry.get(feature_name)                    # ← 硬路由
```

#### 改造方案：引入 Tool Definition 层

**Step 1: 新增 `tool_schema.py` — 将插件转为 OpenAI Function Calling 格式**

```python
# 新增文件: code_agent/tool_schema.py

import json
from typing import Dict, List, Any
from code_agent.plugins.registry import registry
from code_agent.plugins.base import ConfigFieldType


JSON_TYPE_MAP = {
    ConfigFieldType.TEXT: "string",
    ConfigFieldType.TEXTAREA: "string",
    ConfigFieldType.SELECT: "string",
    ConfigFieldType.SWITCH: "boolean",
    ConfigFieldType.FILE: "string",
}


def plugin_to_tool_definition(plugin_name: str) -> dict | None:
    """将一个插件转换为 OpenAI function-calling 格式的 tool definition。

    这是 code_agent 从"用户选模块"到"模型选模块"的关键桥梁。
    """
    plugin = registry.get(plugin_name)
    if plugin is None:
        return None

    properties = {}
    required = []

    for field in plugin.config_schema:
        json_type = JSON_TYPE_MAP.get(field.type, "string")
        prop = {
            "type": json_type,
            "description": field.help_text or field.label,
        }
        if field.options:
            prop["enum"] = [opt["value"] for opt in field.options]
        if field.placeholder:
            prop["description"] += f" (e.g. {field.placeholder})"

        properties[field.name] = prop
        if field.required:
            required.append(field.name)

    return {
        "type": "function",
        "function": {
            "name": plugin.metadata.name,
            "description": plugin.metadata.description,
            "parameters": {
                "type": "object",
                "properties": properties,
                "required": required,
            },
        },
    }


def get_all_tool_definitions() -> List[dict]:
    """获取所有已注册插件的 tool definitions，供 LLM function calling 使用。"""
    tools = []
    for metadata in registry.list_plugins():
        tool_def = plugin_to_tool_definition(metadata.name)
        if tool_def:
            tools.append(tool_def)
    return tools
```

**Step 2: 引入 Agent Loop — 模型自主多轮决策**

```python
# 新增文件: code_agent/agent_loop.py

import json
import subprocess
from typing import Generator, List, Dict, Any
from code_agent.tool_schema import get_all_tool_definitions
from code_agent.plugins.registry import registry
from code_agent.plugins.base import ExecutionContext, PluginResult


MAX_TOOL_ROUNDS = 5  # 最多工具调用轮次

# ★ 简化的 LLM 调用（后续可接入 Aider 或自己的 LLM 客户端）
def _call_llm_with_tools(messages: List[dict], tools: List[dict],
                         model: str, api_key: str) -> dict:
    """调用 LLM API（支持 OpenAI function calling）。

    这里展示核心逻辑——实际实现可选择:
    - 复用 Aider 的 LLM 调用
    - 直接调用 OpenAI/DeepSeek API
    - 调用 B4 的 generate_ai_message
    """
    from openai import OpenAI

    client = OpenAI(api_key=api_key)
    response = client.chat.completions.create(
        model=model,
        messages=messages,
        tools=tools,
        tool_choice="auto",
    )
    choice = response.choices[0].message

    tool_calls = []
    if choice.tool_calls:
        for tc in choice.tool_calls:
            tool_calls.append({
                "id": tc.id,
                "name": tc.function.name,
                "args": json.loads(tc.function.arguments),
            })

    return {
        "content": choice.content or "",
        "tool_calls": tool_calls,
    }


def run_agent_loop(
    user_instruction: str,
    model: str,
    api_key: str,
    project_dir: str,
    target_files: List[str],
    extra_context: Dict[str, Any] = None,
) -> Generator[str, None, None]:
    """Agent 主循环：模型自主决策 → 工具执行 → 结果回传 → 再决策。

    这是 NEUAgent 的 run_single_turn 思想在 code_agent 中的实现。

    Yields:
        NDJSON 事件流 (与现有 agent_web_api.py 兼容)
    """
    tools = get_all_tool_definitions()
    tool_names = [t["function"]["name"] for t in tools]

    system_prompt = f"""你是一个由 NaturalCC 驱动的代码智能体。
你可以使用以下工具来完成用户的编程任务：

{json.dumps(tools, ensure_ascii=False, indent=2)}

使用规则：
1. 分析用户意图，选择最合适的工具
2. 如果任务需要多步骤，可以多次调用工具
3. 每次可以调用一个或多个工具
4. 工具结果会返回给你，你可以据此决定下一步
5. 当任务完成时，输出最终回答（不调用工具）
"""

    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": user_instruction},
    ]

    yield json.dumps({"type": "start", "status": "running", "tools": tool_names},
                     ensure_ascii=False) + "\n"

    tool_rounds = 0

    while tool_rounds < MAX_TOOL_ROUNDS:
        yield json.dumps({"type": "llm_start", "round": tool_rounds + 1},
                         ensure_ascii=False) + "\n"

        # ★ Step 1: LLM 推理（带 tool definitions）
        ai_message = _call_llm_with_tools(messages, tools, model, api_key)

        # ★ Step 2: 判断是否继续
        if not ai_message["tool_calls"]:
            # 模型给出最终回答
            yield json.dumps({
                "type": "done",
                "status": "success",
                "log": ai_message["content"],
                "content": ai_message["content"],
            }, ensure_ascii=False) + "\n"
            return

        # 追加 assistant 消息
        messages.append({
            "role": "assistant",
            "content": ai_message["content"],
            "tool_calls": ai_message["tool_calls"],
        })

        # ★ Step 3: 执行工具
        for tc in ai_message["tool_calls"]:
            tool_name = tc["name"]
            tool_args = tc["args"]

            yield json.dumps({
                "type": "tool_call",
                "name": tool_name,
                "args": tool_args,
            }, ensure_ascii=False) + "\n"

            plugin = registry.get(tool_name)
            if plugin is None:
                tool_result = f"Error: unknown tool '{tool_name}'"
            else:
                try:
                    # 构造 ExecutionContext → 调用插件
                    context = ExecutionContext(
                        project_dir=project_dir,
                        target_files=target_files,
                        instruction=user_instruction,
                        model=model,
                        api_key=api_key,
                        feature_config={"feature": tool_name, **tool_args},
                    )
                    # 收集插件输出
                    outputs = []
                    for item in plugin.execute(context):
                        if isinstance(item, PluginResult):
                            tool_result = item.log or item.message
                        else:
                            outputs.append(str(item))
                    if outputs:
                        tool_result = "\n".join(outputs)
                except Exception as e:
                    tool_result = f"Error executing {tool_name}: {e}"

            # 追加 tool 结果消息
            messages.append({
                "role": "tool",
                "tool_call_id": tc["id"],
                "name": tool_name,
                "content": tool_result,
            })

            yield json.dumps({
                "type": "tool_result",
                "name": tool_name,
                "result": tool_result[:500],
            }, ensure_ascii=False) + "\n"

        tool_rounds += 1

    # 超过最大轮次
    yield json.dumps({
        "type": "done",
        "status": "max_turns",
        "log": f"任务超过最大工具调用轮次 ({MAX_TOOL_ROUNDS})",
    }, ensure_ascii=False) + "\n"
```

**Step 3: 在 Web API 中集成 Agent Loop**

```python
# 修改 agent_web_api.py — 新增 /api/agent/run 端点

@app.post("/api/agent/run")
async def run_agent_loop_endpoint(request: AgentRequest):
    """
    新增端点：模型自主决策的 Agent 模式。
    与现有 /api/run（用户选模块）并存，逐步迁移。
    """
    context = ExecutionContext(
        project_dir=normalize_project_dir(request.project_dir),
        target_files=sanitize_target_files(request.target_files),
        instruction=request.instruction,
        model=request.model or DEFAULT_MODEL,
        api_key=request.api_key,
    )

    return StreamingResponse(
        _stream_agent_loop(context),
        media_type="application/x-ndjson",
        headers=STREAM_HEADERS,
    )


async def _stream_agent_loop(context: ExecutionContext):
    """流式输出 Agent Loop 的 NDJSON 事件。"""
    for event in run_agent_loop(
        user_instruction=context.instruction,
        model=context.model,
        api_key=context.api_key,
        project_dir=context.project_dir,
        target_files=context.target_files,
    ):
        yield event
        await asyncio.sleep(0)
```

### 5.2 长程编程能力改造

#### 5.2.1 Session 管理

```python
# 新增文件: code_agent/session_manager.py

import json
import uuid
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional


SESSION_ROOT = Path("outputs/sessions")


class Session:
    """NEUAgent 风格的 Session 管理。

    每个 session 包含:
    - messages: 完整对话历史（OpenAI 格式）
    - metadata: 项目路径、模型、工具集等
    - token_stats: 累计 token 使用量
    - turn_index: 当前轮次
    """

    def __init__(self, session_id: str = None, **kwargs):
        self.session_id = session_id or uuid.uuid4().hex[:12]
        self.messages: List[dict] = kwargs.get("messages", [])
        self.project_dir: str = kwargs.get("project_dir", "")
        self.target_files: List[str] = kwargs.get("target_files", [])
        self.model: str = kwargs.get("model", "deepseek/deepseek-chat")
        self.toolset: str = kwargs.get("toolset", "all")
        self.max_turns: int = kwargs.get("max_turns", 5)
        self.turn_index: int = kwargs.get("turn_index", 0)
        self.token_stats: Dict[str, int] = kwargs.get("token_stats", {"input": 0, "output": 0})
        self.status: str = kwargs.get("status", "idle")
        self.created_at: str = kwargs.get("created_at", datetime.now().isoformat())
        self.updated_at: str = kwargs.get("updated_at", self.created_at)

    def add_message(self, role: str, content: str, **extra):
        msg = {"role": role, "content": content, **extra}
        self.messages.append(msg)
        self.updated_at = datetime.now().isoformat()

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "messages": self.messages,
            "project_dir": self.project_dir,
            "target_files": self.target_files,
            "model": self.model,
            "toolset": self.toolset,
            "max_turns": self.max_turns,
            "turn_index": self.turn_index,
            "token_stats": self.token_stats,
            "status": self.status,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @classmethod
    def from_dict(cls, data: dict) -> "Session":
        return cls(**data)

    def save(self):
        """持久化到磁盘。"""
        outdir = SESSION_ROOT / self.session_id
        outdir.mkdir(parents=True, exist_ok=True)
        payload = self.to_dict()
        payload["updated_at"] = datetime.now().isoformat()
        (outdir / "session.json").write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    @classmethod
    def load(cls, session_id: str) -> Optional["Session"]:
        """从磁盘恢复 session。"""
        path = SESSION_ROOT / session_id / "session.json"
        if not path.is_file():
            return None
        data = json.loads(path.read_text(encoding="utf-8"))
        return cls.from_dict(data)

    @classmethod
    def list_all(cls) -> List[Dict[str, Any]]:
        """列出所有 session 及其摘要。"""
        if not SESSION_ROOT.is_dir():
            return []
        results = []
        for d in sorted(SESSION_ROOT.iterdir(), key=lambda p: p.name, reverse=True):
            if not d.is_dir():
                continue
            meta = d / "session.json"
            if not meta.is_file():
                continue
            data = json.loads(meta.read_text(encoding="utf-8"))
            results.append({
                "session_id": data.get("session_id"),
                "updated_at": data.get("updated_at"),
                "message_count": len(data.get("messages", [])),
                "model": data.get("model"),
                "turn_index": data.get("turn_index"),
                "project_dir": data.get("project_dir"),
            })
        return results


# 内存缓存（当前会话）
_active_sessions: Dict[str, Session] = {}


def get_or_create_session(session_id: str = None, **kwargs) -> Session:
    """获取或创建 session（先查内存 → 再查磁盘 → 新建）。"""
    if session_id and session_id in _active_sessions:
        return _active_sessions[session_id]

    if session_id:
        disk_session = Session.load(session_id)
        if disk_session:
            _active_sessions[session_id] = disk_session
            return disk_session

    new_session = Session(session_id=session_id, **kwargs)
    _active_sessions[new_session.session_id] = new_session
    return new_session
```

#### 5.2.2 上下文压缩

```python
# 新增文件: code_agent/context_compressor.py

import json
from typing import List, Dict


def _format_messages_for_summary(messages: List[dict]) -> str:
    """将旧消息转为纯文本，供 LLM 做摘要（借鉴 NEUAgent 的设计）。

    关键设计原则:
    - 不保留原始 JSON（模型被大段 JSON 干扰）
    - 工具结果 → 一句话摘要
    - 用户/助手消息 → 截断到 100 字符
    - System 消息 → 跳过
    """
    lines = []
    for msg in messages:
        role = msg.get("role", "?")
        if role == "system":
            continue
        content = (msg.get("content", "") or "").strip()

        if role == "tool":
            # 尝试从 tool result JSON 中提取一句话摘要
            try:
                data = json.loads(content)
                if isinstance(data, dict):
                    name = data.get("skill_name", data.get("tool", "?"))
                    status = data.get("status", "?")
                    lines.append(f"工具结果：{name} → {status}")
                else:
                    lines.append(f"工具结果：{str(data)[:80]}")
            except (json.JSONDecodeError, TypeError):
                lines.append(f"工具结果：{content[:80]}")
            continue

        if content:
            preview = content[:100].replace('\n', ' ')
            label = "用户" if role == "user" else "助手"
            lines.append(f"{label}：{preview}")

        for call in msg.get("tool_calls", []):
            name = call.get("name", "?")
            args = call.get("args", {})
            lines.append(f"工具调用：{name}({json.dumps(args, ensure_ascii=False)})")

    return "\n".join(lines)


def compress_history(messages: List[dict], model: str, api_key: str,
                     keep_last_k: int = 4) -> List[dict]:
    """压缩对话历史：旧消息 → LLM 摘要，保留最近 K 条消息。

    Args:
        messages: 完整消息列表
        model: 用于摘要的 LLM 模型
        api_key: API 密钥
        keep_last_k: 保留最近 K 条消息不被压缩

    Returns:
        压缩后的消息列表
    """
    if len(messages) <= keep_last_k + 4:
        return messages  # 消息太少，不需要压缩

    old_messages = messages[:-keep_last_k]
    recent_messages = messages[-keep_last_k:]

    # 提取旧摘要
    old_summary = ""
    sys_content = messages[0].get("content", "") if messages[0].get("role") == "system" else ""
    if "[SUMMARY]" in sys_content:
        import re
        match = re.search(r"\[SUMMARY\](.*?)\[/SUMMARY\]", sys_content, re.DOTALL)
        if match:
            old_summary = match.group(1).strip()

    # 格式化旧消息为纯文本
    plain_text = _format_messages_for_summary(old_messages)

    # 调 LLM 合并摘要
    from openai import OpenAI
    client = OpenAI(api_key=api_key)

    compress_prompt = (
        f"以下是之前的对话摘要：\n{old_summary}\n\n"
        f"以下是后续的新对话记录：\n{plain_text}\n\n"
        f"请将旧摘要和新对话合并为一条连贯的中文段落摘要（200字以内），"
        f"保留关键信息（文件修改、代码变更、错误修复等）。"
    ) if old_summary else (
        f"请将以下对话压缩为一条中文段落摘要（200字以内），"
        f"保留关键信息（文件修改、代码变更、错误修复等）：\n{plain_text}"
    )

    response = client.chat.completions.create(
        model=model,
        messages=[
            {"role": "system", "content": "你是对话摘要助手。输出简洁的中文摘要。"},
            {"role": "user", "content": compress_prompt},
        ],
        max_tokens=500,
    )
    new_summary = response.choices[0].message.content.strip()

    # 重建 system message（保留 template，替换 summary）
    if messages[0].get("role") == "system":
        import re
        old_content = messages[0]["content"]
        if "[SUMMARY]" in old_content:
            new_content = re.sub(
                r"\[SUMMARY\].*?\[/SUMMARY\]",
                f"[SUMMARY]\n{new_summary}\n[/SUMMARY]",
                old_content,
                flags=re.DOTALL,
            )
        else:
            new_content = old_content + f"\n\n[SUMMARY]\n{new_summary}\n[/SUMMARY]"

        new_messages = [{"role": "system", "content": new_content}] + recent_messages
    else:
        new_messages = [
            {"role": "system", "content": f"[SUMMARY]\n{new_summary}\n[/SUMMARY]"}
        ] + recent_messages

    return new_messages
```

### 5.3 记忆管理改造

```python
# 新增文件: code_agent/memory_manager.py

import json
import re
import hashlib
import math
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Any, Optional


MEMORY_ROOT = Path("outputs/memory")
MEMORY_ROOT.mkdir(parents=True, exist_ok=True)


# ============================================================================
# Part 1: 文本向量化（纯 Python TF-IDF，无需外部模型）
# ============================================================================

def _extract_keywords(text: str) -> List[str]:
    """提取关键词：中文2字以上 + 英文3字母以上，过滤停用词。"""
    text = text.lower()
    tokens = []

    # 中文单字
    cn_chars = re.findall(r'[一-鿿]', text)
    tokens.extend(cn_chars)
    # 英文单词
    en_words = re.findall(r'[a-z]{3,}', text)
    tokens.extend(en_words)
    # 中英文混合（如变量名）
    mixed = re.findall(r'[a-z_][a-z0-9_]{2,}', text)
    tokens.extend(mixed)

    STOP_WORDS = {
        'the', 'and', 'for', 'are', 'with', 'this', 'that', 'from',
        'have', 'will', 'would', 'their', 'what', 'when', 'where',
        'which', 'while', 'some', 'these', 'them', 'than', 'then',
        'also', 'after', 'other', 'many', 'time', 'very', 'just',
        'even', 'well', 'only', 'over', 'think', 'know', 'take',
        'make', 'like', 'use', 'see', 'way', 'who', 'its', 'may',
        'say', 'try', 'ask', 'end', 'why', 'let', 'put', 'own',
        'too', 'old', 'each', 'first', 'never', 'every', 'still',
        'most', 'long', 'last', 'find', 'give', 'does', 'made',
        'part', 'such', 'keep', 'call', 'need', 'name', 'done',
        'open', 'case', 'show', 'live', 'play', 'read', 'stop',
    }
    return [t for t in tokens if t not in STOP_WORDS]


def text_to_vector(text: str, dim: int = 384) -> List[float]:
    """纯 Python TF-IDF 向量化（借鉴 NEUAgent 设计）。

    不需要下载任何模型，轻量、离线、跨平台。
    """
    tokens = _extract_keywords(text)
    if not tokens:
        tokens = [text[i:i+2] for i in range(len(text)-1)] or ["_"]

    word_freq = {}
    for token in tokens:
        word_freq[token] = word_freq.get(token, 0) + 1

    total = len(tokens)
    vector = [0.0] * dim

    for word, freq in word_freq.items():
        tf = freq / total
        idx = int(hashlib.md5(word.encode()).hexdigest(), 16) % dim
        weight = tf * (1.0 / (1 + math.log(1 + len(word))))
        vector[idx] += weight

    # L2 归一化
    magnitude = math.sqrt(sum(x**2 for x in vector))
    if magnitude > 0:
        vector = [x / magnitude for x in vector]

    return vector


def cosine_similarity(v1: List[float], v2: List[float]) -> float:
    if len(v1) != len(v2):
        return 0.0
    dot = sum(a * b for a, b in zip(v1, v2))
    n1 = math.sqrt(sum(a**2 for a in v1))
    n2 = math.sqrt(sum(b**2 for b in v2))
    return dot / (n1 * n2) if n1 and n2 else 0.0


# ============================================================================
# Part 2: 记忆索引
# ============================================================================

class MemoryIndex:
    """记忆索引管理器。

    文件结构:
    memory/
    ├── memory_index.json     ← 元数据索引
    ├── vectors.json          ← 向量嵌入
    ├── global/               ← 全局记忆
    │   └── mem_global_001.md
    └── conversations/        ← 对话记忆
        └── mem_conv_abc.md
    """

    def __init__(self, root: Path = MEMORY_ROOT):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.index_path = self.root / "memory_index.json"
        self.vectors_path = self.root / "vectors.json"
        self.global_dir = self.root / "global"
        self.conv_dir = self.root / "conversations"
        self.global_dir.mkdir(exist_ok=True)
        self.conv_dir.mkdir(exist_ok=True)

    def _read_index(self) -> dict:
        if not self.index_path.is_file():
            return {}
        return json.loads(self.index_path.read_text(encoding="utf-8"))

    def _write_index(self, index: dict):
        self.index_path.write_text(
            json.dumps(index, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def _read_vectors(self) -> dict:
        if not self.vectors_path.is_file():
            return {}
        return json.loads(self.vectors_path.read_text(encoding="utf-8"))

    def _write_vectors(self, vectors: dict):
        self.vectors_path.write_text(
            json.dumps(vectors, ensure_ascii=False),
            encoding="utf-8",
        )

    def save(self, memory_type: str, conversation_id: str,
             messages: List[dict], final_answer: str, trace: dict = None) -> str:
        """保存记忆为 Markdown 文件，更新索引和向量。"""
        now = datetime.now().isoformat()
        memory_id = f"mem_{memory_type}_{conversation_id[:8]}"
        target_dir = self.conv_dir if memory_type == "conversation" else self.global_dir
        rel_dir = "conversations" if memory_type == "conversation" else "global"

        # 生成 Markdown
        md_content = (
            f"# {memory_type.title()} Memory: {conversation_id}\n\n"
            f"- memory_id: `{memory_id}`\n"
            f"- created_at: `{now}`\n"
            f"- conversation_id: `{conversation_id}`\n\n"
            f"## Final Answer\n\n{final_answer}\n\n"
            f"## Messages\n\n```json\n"
            f"{json.dumps(messages, ensure_ascii=False, indent=2)}\n```\n"
        )
        if trace:
            md_content += f"\n## Trace\n\n```json\n{json.dumps(trace, ensure_ascii=False, indent=2)}\n```\n"

        file_path = target_dir / f"{conversation_id[:8]}.md"
        file_path.write_text(md_content, encoding="utf-8")

        # 更新索引
        index = self._read_index()
        index[memory_id] = {
            "memory_id": memory_id,
            "memory_type": memory_type,
            "title": f"{memory_type.title()} {conversation_id[:8]}",
            "summary": final_answer[:200],
            "path": f"{rel_dir}/{conversation_id[:8]}.md",
            "conversation_id": conversation_id,
            "created_at": now,
            "updated_at": now,
        }
        self._write_index(index)

        # 更新向量
        vectors = self._read_vectors()
        vectors[memory_id] = text_to_vector(md_content)
        self._write_vectors(vectors)

        return memory_id

    def search(self, query: str, top_k: int = 5,
               keyword_weight: float = 0.4) -> List[Dict[str, Any]]:
        """KV 混合检索（借鉴 NEUAgent 的 auto_select_memories）。

        关键词检索 + 向量检索 → 加权融合 → 排序取 top_k。
        """
        index = self._read_index()
        vectors = self._read_vectors()
        if not index:
            return []

        query_vec = text_to_vector(query)
        query_keywords = set(_extract_keywords(query))

        scored = []
        for memory_id, meta in index.items():
            # 关键词分数
            kw_score = 0.0
            if query_keywords:
                content_keywords = set(_extract_keywords(
                    meta.get("summary", "") + " " + meta.get("title", "")
                ))
                intersection = query_keywords & content_keywords
                kw_score = len(intersection) / len(query_keywords) if query_keywords else 0.0

            # 向量相似度
            vec_score = 0.0
            if memory_id in vectors:
                vec_score = cosine_similarity(query_vec, vectors[memory_id])

            # 加权融合
            final_score = keyword_weight * kw_score + (1 - keyword_weight) * vec_score
            if final_score > 0:
                scored.append((memory_id, meta, final_score))

        scored.sort(key=lambda x: x[2], reverse=True)

        results = []
        for memory_id, meta, score in scored[:top_k]:
            # 读取文件内容
            file_path = self.root / meta["path"]
            content = ""
            if file_path.is_file():
                content = file_path.read_text(encoding="utf-8")[:2000]

            results.append({
                "memory_id": memory_id,
                "memory_type": meta.get("memory_type"),
                "title": meta.get("title"),
                "summary": meta.get("summary"),
                "score": round(score, 4),
                "content": content,
                "created_at": meta.get("created_at"),
            })

        return results

    def get_content(self, memory_id: str) -> Optional[str]:
        """读取记忆完整内容。"""
        index = self._read_index()
        meta = index.get(memory_id)
        if not meta:
            return None
        file_path = self.root / meta["path"]
        if file_path.is_file():
            return file_path.read_text(encoding="utf-8")
        return None

    def delete(self, memory_id: str) -> bool:
        """删除记忆（文件 + 索引 + 向量）。"""
        index = self._read_index()
        meta = index.pop(memory_id, None)
        if meta is None:
            return False

        file_path = self.root / meta["path"]
        if file_path.is_file():
            file_path.unlink()

        vectors = self._read_vectors()
        vectors.pop(memory_id, None)

        self._write_index(index)
        self._write_vectors(vectors)
        return True

    def inject_memory_context(self, system_prompt: str, query: str,
                               max_chars: int = 2000) -> str:
        """检索相关记忆并注入 system prompt（借鉴 NEUAgent 设计）。

        返回增强后的 system prompt，记忆以 <memory> 标签包裹。
        """
        memories = self.search(query, top_k=3)

        if not memories:
            return system_prompt

        parts = []
        remaining = max_chars
        for mem in memories:
            content = mem["content"][:remaining]
            if not content.strip():
                continue
            parts.append(
                f'<memory id="{mem["memory_id"]}" type="{mem["memory_type"]}">\n'
                f'{content.strip()}\n</memory>'
            )
            remaining -= len(content)
            if remaining <= 0:
                break

        if parts:
            return system_prompt + "\n\n" + "\n\n".join(parts)
        return system_prompt


# 全局单例
memory_index = MemoryIndex()
```

---

## 六、改造优先级路线图

```
Phase 1 (基础): 让模型"看见"模块                   预计工作量: 2-3天
├── 新增 tool_schema.py (插件 → OpenAI tool definition)
├── 修改 /api/bootstrap → 返回 tool definitions
└── 验证：前端展示模块列表 vs 之前一致

Phase 2 (核心): 模型自主决策 Agent Loop             预计工作量: 5-7天
├── 新增 agent_loop.py (while 循环 + function calling)
├── 新增 /api/agent/run 端点（与旧 /api/run 并存）
├── 前端新增 "Agent 模式" 开关
└── 验证：用户说 "修复 bug 并检查漏洞"，模型自动调 code_repair + vulnerability_detection

Phase 3 (长程): Session + 上下文压缩                 预计工作量: 3-5天
├── 新增 session_manager.py
├── 新增 context_compressor.py
├── 修改 /api/run → 支持 session_id 参数
└── 验证：10 轮对话后，上下文自动压缩不超限

Phase 4 (记忆): 记忆系统                             预计工作量: 3-5天
├── 新增 memory_manager.py
├── 新增 /api/memory 相关端点
├── 对话结束自动保存记忆 → 下次对话自动检索
└── 验证：跨 session 记忆持久化，相关记忆准确注入

Phase 5 (高级): 规划-执行 + 批量任务                  预计工作量: 5-7天
├── Agent Loop 增加 plan_execute 模式
├── 批量任务并行执行
└── 验证：复杂多步骤任务自动规划并执行
```

---

## 七、核心差异一图概览

```
                    code_agent (当前)              NEUAgent (目标)
                    ════════════════              ═══════════════

用户意图分析:        用户自己判断选哪个模块    →    LLM 分析意图，自主选 Skill

Skill 注册:          Python @register_plugin    →    YAML 声明 + Python 函数
                     (用户不可见)                     (对 LLM 可见)

Skill 暴露:          前端表单 label/desc        →    OpenAI function-calling
                     (给人类看)                       tools 参数 (给 LLM 看)

执行循环:            单次 Aider --message-file  →    while tool_calls:
                     执行完即退出                       LLM推理→工具执行→再推理

参数填充:            用户手动填写表单            →    LLM 从用户意图中提取参数

多工具组合:          不支持（一次一个插件）       →    模型可并行调用多个 Skill

任务规划:            不支持                      →    plan_execute 模式
                                                     规划→确认→执行→汇总

会话状态:            无（每次请求全新 Context）   →    Session 持久化 + 恢复

上下文管理:          无压缩，超长直接失败          →    LLM 摘要压缩 + 保留 K 轮

记忆系统:            无                          →    KV 混合检索 + 向量 + 摘要
                                                     自动注入 + 动态刷新

代码修改闭环:        Aider 闭环 (单次)           →    Agent 闭环 (多轮)
                     无自我检查机制                    模型看到修改结果后可再决策
```

---

> **总结**: NEUAgent 的核心价值不在于单个 Skill 的实现（calculator、file_reader 等都很简单），而在于**让模型拥有"看到所有可用工具、自主选择、多轮执行、记住上下文"的能力**。code_agent 在 NaturalCC 语义解析方面领先，若能融合 NEUAgent 的 Agent Loop + Skill 暴露 + 记忆管理，将从一个"增强型 Prompt 工具"进化为真正的"自主 Coding Agent"。
