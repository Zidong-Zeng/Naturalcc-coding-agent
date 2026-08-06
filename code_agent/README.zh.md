# NaturalCC Code Agent

[English README](README.md)

`code_agent` 是一个本地代码编辑代理，把静态项目理解能力和 Aider 的文件修改能力组合在一起。

它会先解析目标项目，收集函数、变量、类型、成员、include 和符号关系；再根据用户任务生成语义增强 prompt；最后把 prompt 交给 Aider，由 Aider 修改选中的目标文件。

项目提供两种使用方式：

- 图形界面：FastAPI 后端 + React/Vite 前端。
- 命令行：`aider_runner.py`。

注意：第一个目标文件始终是 NaturalCC 构造 prompt 时使用的主解析文件。Aider 仍然可以接收并修改多个目标文件。

## 项目是什么

这不是通用聊天机器人，而是面向项目上下文的代码补全和代码修改代理。

典型任务：

- 补全函数体
- 补全函数签名
- 补全变量、成员或类型
- 按项目现有风格做小范围代码修改
- 检测潜在漏洞并按需自动修复
- 在执行 Aider 前预览最终语义 prompt

主要文件：

- `agent_web_api.py`：FastAPI 后端，同时可服务打包后的前端。
- `webui/`：React + Vite 图形界面。
- `aider_runner.py`：CLI 入口和 Aider 调度逻辑。
- `completion_prompt_agent.py`：语义 prompt 构造逻辑。
- `rag/c/`：C/C++ 解析和上下文检索。
- `rag/java/`：Java 解析和 prompt 路径。
- `test_api.py`：OpenRouter key / 连通性检查脚本。

## 环境搭建

### 前置条件

- [uv](https://docs.astral.sh/uv/getting-started/installation/)（Python 包管理器）
- Node.js & npm（前端构建）

### 1. 安装系统依赖

C/C++ 解析需要系统级 `libclang` 库。在运行 `uv sync` 之前，通过系统包管理器安装：

- **Ubuntu/Debian**: `sudo apt install libclang1`
- **macOS**: `brew install libclang`
- **其他发行版**: 在系统包仓库中搜索 `libclang` 并安装

### 2. 创建 Python 环境

在 `code_agent/` 目录下执行：

```bash
uv sync
```

此命令会自动创建 `.venv` 虚拟环境并安装所有锁定的 Python 依赖，无需手动配置 conda 或执行 pip。

为 Agent 模式的 token 硬预算安装固定版本的 DeepSeek V3 官方 tokenizer：

```bash
uv run python scripts/install_deepseek_tokenizer.py
```

安装器从 `cdn.deepseek.com` 下载官方压缩包，校验固定的压缩包和文件 SHA-256，只把 `tokenizer.json` 与 `tokenizer_config.json` 安装到 `resources/deepseek_v3_tokenizer/`。本地文件缺失或校验/加载失败时 Agent 模式会明确失败，不会退回字符估算。

项目运行至少需要以下能力（均由 `uv sync` 自动处理）：

- `fastapi`
- `uvicorn`
- `clang` Python bindings
- `aider` 可执行命令在 `PATH` 中

如需 GPU 支持（例如运行基于 vLLM 的离线评估），可手动安装：

```bash
uv pip install vllm
```

调用 OpenRouter / OpenAI 时，可以在界面或 CLI 中传入 API Key，也可以设置环境变量：

```bash
export OPENROUTER_API_KEY=sk-or-v1-...
export OPENAI_API_KEY=sk-...
```

### 3. 安装前端依赖

在 `code_agent/` 目录下执行：

```bash
cd webui
npm install
```

## 使用图形界面

图形界面由 FastAPI 后端和 React 前端组成。

### 一键启动

如果你已安装图形终端模拟器（gnome-terminal、konsole、alacritty 等），或已安装 `tmux` 作为回退：

```bash
./start.sh
```

该脚本会自动使用 `.venv` 中的 Python，并打开两个终端窗口（或 tmux 分屏）：
- 一个运行 FastAPI 后端
- 一个运行 Vite 前端开发服务器

使用 tmux 时，session 名称为 `ncc-agent`。按 `Ctrl+B` 再按 `D` 可分离会话；重新 attach 用 `tmux attach -t ncc-agent`。

### 开发模式

适合修改前端代码时使用，支持 Vite 热更新。

终端 1：

```bash
uv run python agent_web_api.py --host 127.0.0.1 --port 7860
```

终端 2：

```bash
cd webui
npm run dev
```

打开：

```text
http://127.0.0.1:5173/
```

开发模式下，Vite 在 `5173` 提供前端页面，并把 `/api/*` 请求代理到 `7860` 的 FastAPI 后端。

### 本地打包模式

适合只启动一个服务来同时提供 UI 和 API。

```bash
cd webui
npm run build
cd ..
uv run python agent_web_api.py --host 127.0.0.1 --port 7860
```

打开：

```text
http://127.0.0.1:7860/
```

打包模式下，FastAPI 会服务 `webui/dist`，并在同一端口提供所有后端 API。

### UI 使用流程

1. 在左侧新建或打开一个历史会话；重新打开时会恢复聊天、workspace、文件上下文、模型、预算和最后一次 Run。
2. 在顶部设置 workspace 和模型；API Key 只在 Settings 中临时输入，不写入会话数据库。
3. 在输入框键入 `@文件名` 搜索 workspace 文件，或粘贴绝对路径后点击 **Add context**。显式加入的外部绝对路径具有读写权限，并显示 External 标记。
4. 点击 **Budget** 设置 LLM calls 和 Tool calls；顶部进度条显示本轮已用量，运行中只能把上限调整到不低于已使用量。
5. 输入开发指令并发送。每条用户消息创建一个独立 Run；同一会话继承已提交的 ThreadCheckpoint，以及其水位线之后的连续原始聊天。
6. 点击 **Run details** 查看审批、暂停/恢复/取消、事件时间线、修改文件、验证结果和项目记忆。
7. 将鼠标移到历史会话上并点击删除图标，可永久删除该会话及其 Run 记录。排队、运行、暂停或等待审批的任务需要先点击确认框中的“Cancel task first”，取消成功后再确认永久删除。
7. 如需旧版 NaturalCC Prompt → Aider 流程，在顶部运行模式中选择 **Pipeline**。

## 使用 CLI

在 `code_agent/` 目录下执行命令。

### 仅预览 Prompt

```bash
uv run python aider_runner.py \
  -dir /path/to/project \
  -f src/foo.c include/foo.h \
  -i "补全 foo 函数实现" \
  --preview
```

### 执行 Aider 修改文件

```bash
uv run python aider_runner.py \
  -dir /path/to/project \
  -f src/foo.c include/foo.h \
  -i "根据现有风格完善 foo 函数实现" \
  -m openrouter/deepseek/deepseek-chat
```

### 常用 CLI 参数

```bash
-dir /path/to/project
-f src/foo.c include/foo.h
-i "你的修改或补全需求"
-m openrouter/deepseek/deepseek-chat
-key sk-...
-s parse_flags
-t function_body
--prefix parse_
--preview
```

`-t` 支持：

```text
member
variable
function
function_body
type
```

## API 接口

`agent_web_api.py` 提供：

- `GET /api/health`
- `GET /api/bootstrap`
- `GET /api/models`
- `GET|POST /api/workspace/scan`
- `GET /api/browse`
- `POST /api/command-preview`
- `POST /api/prompt/preview`
- `POST /api/run`

`/api/run` 会返回按行分隔的 JSON 事件，前端用它实时显示 Aider 日志。

## Feature Plugin 系统

**Advanced** 面板现在由插件架构驱动。每个功能都是 `plugins/` 下的一个 `FeaturePlugin`。前端根据每个插件的 `config_schema` 动态渲染表单，因此添加新功能**不需要**修改任何前端代码。

### 架构

- `plugins/base.py` — `FeaturePlugin` 抽象基类、`ExecutionMode`（`aider`/`direct`/`hybrid`）、`ConfigField` 表单定义、`ExecutionContext`。
- `plugins/registry.py` — `@register_plugin` 类装饰器；插件在导入时自动注册。
- `plugins/dispatcher.py` — 将执行路由到 AIDER、DIRECT 或 HYBRID 模式。
- `plugins/code_completion.py` — 原有的 `symbol`/`completion_type`/`prefix` 逻辑，已迁移为插件。
- `plugins/code_summary.py` — NaturalCC + Aider dry-run 代码总结。
- `plugins/code_repair.py` — AIDER 模式的代码修复提示词，用于 bug、编译错误和测试失败。
- `plugins/vulnerability_detection.py` — 漏洞分析插件，支持可选的 Aider 自动修复。

### 执行模式

| 模式 | 行为 | 示例 |
|------|------|------|
| `aider` | 生成 prompt → 调用 Aider → 修改代码文件或输出 dry-run 报告 | 代码补全、代码修复、代码总结 |
| `direct` | 直接分析 → 返回报告 / 写入文件 | 静态报告 |
| `hybrid` | 通过 API 分析 → 生成修复 prompt → Aider 修复 | 漏洞检测 |

### 内置代码总结功能

功能名：`code_summary`（AIDER 模式）

执行方式：
- 对选中的目标文件，或项目下的源码文件，构造正常的 NaturalCC 语义 prompt。
- 使用 `--dry-run` 调用 Aider，因此总结过程不会修改文件。
- 使用所选模型生成更深入的代码理解报告。

主要配置项：
- `summary_scope`：`targets`（仅目标文件）或 `project`（全项目源码）
- `detail_level`：`brief` / `standard` / `detailed`
- `include_symbols`：要求 Aider 包含关键符号和数据流
- `max_files`：发送给 NaturalCC 和 Aider 的文件数量上限

### NaturalCC / libclang 版本对齐

NaturalCC 要求 Python `clang` bindings 与系统安装的 `libclang` 版本匹配。本项目固定 `clang==18.1.8`，对应 Ubuntu LLVM 18 / `libclang1-18` 系列。如果系统使用其他 LLVM 主版本，需要把 `clang` 依赖和锁文件调整为与 `libclang.so` 相同的主版本。

### 内置代码修复功能

功能名：`code_repair`（AIDER 模式）

执行方式：
- 根据用户指令、修复类型、可选错误日志和可选额外上下文生成聚焦修复提示词。
- 复用现有 NaturalCC 语义 prompt 路径，然后交给 Aider 修改目标文件。
- 默认偏向最小修复，并尽量保持现有接口不变。

主要配置项：
- `repair_type`：`bug_fix` / `compile_error` / `test_failure` / `safe_refactor`
- `failure_log`：编译错误、测试失败、堆栈或运行时报错
- `extra_context`：约束、期望行为或复现说明
- `allow_refactor`：必要时允许小范围辅助重构

### 内置漏洞检测功能

功能名：`vulnerability_detection`（HYBRID 模式）

执行方式：
- 阶段 1：进行基于规则的静态漏洞扫描并生成报告。
- 阶段 2（可选）：当 `auto_fix=true` 时，生成修复指令并调用 Aider 对目标文件进行修复。

主要配置项：
- `scan_scope`：`targets`（仅目标文件）或 `project`（全项目）
- `severity_threshold`：`low` / `medium` / `high` / `critical`
- `rule_profile`：`default` / `c_cpp` / `web`
- `auto_fix`：是否执行自动修复阶段
- `max_findings`：报告中最大告警条数
- `extra_instruction`：额外修复约束

使用建议：
- 如果要开启 `auto_fix`，先选择好目标文件。
- 建议先用 `auto_fix=false` 查看扫描结果，再决定是否自动修复。

### 如何添加新功能插件

1. 在 `plugins/` 下创建新文件，例如 `plugins/my_feature.py`。
2. 继承 `FeaturePlugin`，实现 `metadata`、`config_schema` 和 `execute`。
3. 用 `@register_plugin` 装饰类。
4. 重启后端。前端会自动显示新功能并渲染其表单。

示例：

```python
# plugins/my_feature.py
from typing import Any, Dict, Generator, List, Optional
from code_agent.plugins.base import (
    FeaturePlugin, FeatureMetadata, ExecutionMode,
    ConfigField, ConfigFieldType, ExecutionContext, PluginResult,
)
from code_agent.plugins.registry import register_plugin


@register_plugin
class MyFeaturePlugin(FeaturePlugin):

    @property
    def metadata(self) -> FeatureMetadata:
        return FeatureMetadata(
            name="my_feature",           # 唯一标识
            label="My Feature",          # 显示名称
            description="功能描述",
            execution_mode=ExecutionMode.DIRECT,  # 或 AIDER / HYBRID
        )

    @property
    def config_schema(self) -> List[ConfigField]:
        return [
            ConfigField(
                name="my_param",
                label="My Parameter",
                type=ConfigFieldType.TEXT,   # text / textarea / select / switch / file
                required=True,
                default="",
                placeholder="输入值",
                help_text="显示在字段下方的帮助文本",
            ),
        ]

    def execute(self, context: ExecutionContext) -> Generator[str, None, None]:
        # yield 字符串作为日志输出
        yield "开始执行...\n"
        # ... 你的业务逻辑 ...
        # 完成后 yield PluginResult（用于 DIRECT / HYBRID 模式）
        yield PluginResult(success=True, message="完成！")
```

### 配置字段类型

| 类型 | 渲染为 | 额外属性 |
|------|--------|---------|
| `text` | `<input type="text">` | `placeholder`, `default` |
| `textarea` | `<textarea>` | `placeholder`, `default` |
| `select` | `<select>` | `options: [{value, label}]`, `default` |
| `switch` | `<input type="checkbox">` | `default` (bool) |
| `file` | `<input type="file">` | `accept`, `multiple` |

### 文件上传

如果插件配置包含 `file` 类型字段，前端会自动以 `multipart/form-data` 发送请求。上传的文件在 `context.uploaded_files` 中以 `{field_name: UploadFile}` 的形式提供。

### 插件相关的 API 变更

`/api/bootstrap` 现在返回：

```json
{
  "features": [{"name": "...", "label": "...", "execution_mode": "..."}],
  "schemas": {"feature_name": [{"name": "...", "type": "...", ...}]},
  "default_feature": "code_completion"
}
```

`/api/run` 同时接受 JSON（向后兼容）和 `multipart/form-data`（用于文件上传）。请求体应包含：

```json
{
  "feature": "my_feature",
  "feature_config": {"my_param": "value"}
}
```

## 注意事项和限制

- C/C++ 解析依赖 `libclang`。
- 部分解析路径仍使用偏 C 语言的 libclang 参数，C++ 语法覆盖可能不完整。
- `rag/` 中包含离线研究和评测脚本，其中部分脚本带有本地路径假设。
- 持久化 Agent Runtime 已在 `tests/` 下建立确定性的单元、契约、安全、API 和 UI reducer 测试；真实模型与 Aider 调用仍属于手工验收。
- `test_api.py` 只用于 API 连通性检查，不是 parser 或 UI 测试。

## VS Code 插件

本地 VS Code 插件会启动同一个 FastAPI 服务，并在编辑器标签页中打开已构建的 Web
界面。插件只包含应用源码与前端构建产物；不会打包 Python 依赖、Aider、`libclang`
或本地模型。

在 `code_agent/` 中构建本地可安装包：

```bash
cd webui && npm run build
cd ..
npm run package
```

随后在 VS Code 中执行 **Extensions: Install from VSIX...**，选择生成的
`naturalcc-code-agent-0.1.0.vsix`，再运行 **NaturalCC: Open Code Agent**。
如果插件未能自动发现运行环境，请将 `naturalccCodeAgent.pythonPath` 配置为 `uv
sync` 创建的 Python 解释器，例如 `/path/to/code_agent/.venv/bin/python`。服务只监听
`127.0.0.1`，并会在插件停用时结束。

### 用户环境与 API Key

插件包含应用源码，但不包含 Python 运行依赖。用户应先克隆仓库并创建运行环境：

```bash
git clone --branch ncc3 --single-branch https://github.com/CGCL-codes/naturalcc.git
cd naturalcc/code_agent
uv sync
```

随后在 VS Code 设置中，将 `naturalccCodeAgent.pythonPath` 指向该环境的 Python
绝对路径，例如：

```json
"naturalccCodeAgent.pythonPath": "/absolute/path/naturalcc/code_agent/.venv/bin/python"
```

运行 **NaturalCC: Open Code Agent** 后，选择模型，并在界面的 **API Key** 字段填入
自己的 OpenRouter 或 OpenAI Key。该 Key 仅会发送给本次本地 Agent 请求，不会写入
VS Code 设置。也可以在启动 VS Code 的环境中设置 `OPENROUTER_API_KEY` 或
`OPENAI_API_KEY`，然后重启扩展主机。
## 持久化 Agent 模式

项目现在包含两套互相独立的运行方式：

- **Pipeline**：保留原有“选择功能 → NaturalCC Prompt → Aider”的 `/api/run` 流程。
- **Agent**：通过 `/api/agent/*` 使用可持久化状态机。模型可以自主选择只读工具，写文件或执行命令前请求授权；Run 支持重启恢复、预算限制、事件审计、暂停/取消，并能基于用户选中的证据生成受治理的长期记忆建议。

Agent 默认把会话、聊天消息、事件、快照、workspace 租约、审批和记忆保存到 `outputs/agent_runtime.db`。可以通过 `CODE_AGENT_DB` 修改路径。模型配置使用环境变量，API Key 不写入 Run 或会话：

```bash
export CODE_AGENT_MODEL=deepseek-chat
export CODE_AGENT_API_BASE=https://api.deepseek.com/v1
export DEEPSEEK_API_KEY=...
uv run python agent_web_api.py --host 127.0.0.1 --port 7860
```

Agent 使用同一个 DeepSeek 请求序列化器完成离线计数和 API 提交。可通过以下环境变量显式配置模型级 token 硬预算：

```bash
export CODE_AGENT_TOKENIZER_DIR=/absolute/path/to/resources/deepseek_v3_tokenizer
export CODE_AGENT_CONTEXT_WINDOW_TOKENS=65536
export CODE_AGENT_OUTPUT_RESERVE_TOKENS=4096
export CODE_AGENT_CONTEXT_SAFETY_MARGIN_TOKENS=512
export CODE_AGENT_PROVIDER_FRAMING_TOKENS=256
export CODE_AGENT_COMPACTION_TRIGGER_RATIO=0.72
export CODE_AGENT_COMPACTION_TARGET_RATIO=0.50
export CODE_AGENT_ANALYZER_OUTPUT_TOKENS=4096
export CODE_AGENT_SUMMARIZER_OUTPUT_TOKENS=2048
```

达到软阈值且处于安全点时，运行时冻结一个连续、完整闭合的历史前缀，先调用禁用工具且仅输出 JSON 的 `CompactionAnalyzer`，再调用 `CheckpointSummarizer`。只有通过校验并 committed 的 checkpoint 会进入后续主模型上下文；结构化 analysis 只用于审计，原始消息和事件仍是权威数据，API/JSON 失败时使用有界 deterministic fallback。维护调用受 `max_compaction_calls` 限制，不消耗 `max_llm_calls`，但其 token、成本和耗时仍计入 Run 总预算。如果 system rules、goal、WorkingState、checkpoint/recent tail、工具 schema、输出预留与安全余量无法共同放入窗口，请求会在调用模型 API 前以 `reason=context_hard_limit` 终止。

active memory 按照提示缓存特性分为两层。用户偏好、项目约束、架构决策、仓库规范以及兼容的旧 `constraint`/`decision` 记录，组成与当前 goal 无关、顺序确定的 pinned memory；其他 active memory 根据当前 goal 通过 FTS5 检索（包含匹配的 Run 级记忆），形成动态记忆。提示词顺序为：稳定 system rules -> pinned memory -> committed checkpoint -> runtime authorization -> current goal -> retrieved memory -> WorkingState -> recent message tail。这使可复用前缀保持稳定，而路径、计数器、检索结果等动态数据位于后部；pinned ID 会从动态检索中排除，不会重复注入。如果上游服务返回 DeepSeek 兼容的 `prompt_cache_hit_tokens` 和 `prompt_cache_miss_tokens`，Run 会累计两者，并在 **Run details -> Usage** 中展示实测命中率；**Not reported** 表示上游 API 未返回这两个字段。

默认权限：

- workspace 列表/读取/搜索、NaturalCC 解析/符号搜索、Git 状态/diff：自动允许；
- 新建目录、新建 UTF-8 文件、修改文件、调用 Aider、运行项目命令：当前 Run 授权后执行。`workspace.create_directory` 可显式创建父目录链；`workspace.create_file` 要求父目录已存在，且绝不覆盖已有路径；
- shell 解释器、危险 Git 子命令、workspace 越界、push 和 commit：内置工具拒绝。

Web UI 采用 Codex 风格工作台：左侧保存多轮会话，输入框用 `@` 或绝对路径管理文件上下文，顶部预算条显示 LLM/Tool/Input token 用量，Budget 弹窗可在 Run 开始前或运行中调整 `max_input_tokens`；如果 Run 已因预算耗尽而终止，新上限从下一条消息生效。Run details 抽屉承载审批、事件、变更、验证、提示缓存用量和记忆。ThreadCheckpoint 会按 token 压力自动生成并参与后续 Run。生成长期记忆时，先在聊天消息上选择 **Use as memory evidence**，再点击 **Create memory suggestions**。系统会依次执行禁用工具的证据分析和 Proposal 生成，并沿用 DeepSeek 官方离线 tokenizer 的模型级硬上限检查；若某一阶段返回无效结构化结果，该阶段最多执行一次禁用工具的 Schema 修复调用。内部 JSON 经校验后会被确定性投影为可读审核卡片，展示范围、类型、证据、影响和警告，并允许编辑。只有点击 **Accept and remember** 后才会创建 active FTS5 记忆；未审核、被拒绝、已延后或失败的 Proposal 绝不会注入模型上下文。

用户显式加入的外部绝对路径会成为该会话的授权路径，模型工具可以读写该文件或目录；模型自行生成的其他越界路径仍会被拒绝。常见密钥文件仍受敏感路径保护。

历史会话删除是不可恢复的事务操作，会同步清理该会话的消息、Run、事件、快照、审批和 workspace lease。只要会话仍包含活动 Run，后端就会拒绝删除；项目级长期记忆由独立的记忆治理界面管理，不随会话自动删除。

命令执行器采用 argv 白名单、清理后的环境变量、workspace 内 cwd、输出上限、超时、进程组取消和显式审批；白名单包含 C/C++ 编译器入口 `gcc`、`g++`、`c++` 和 `clang++`。但它不是操作系统沙箱：用户批准的编译器、Python/Node 进程、包脚本或测试仍可能以服务进程权限访问主机或网络。处理不可信仓库时，应在容器或受限系统账号中运行。

### Agent API

```text
POST /api/agent/threads
GET  /api/agent/threads
GET  /api/agent/threads/{thread_id}
PATCH /api/agent/threads/{thread_id}
GET  /api/agent/threads/{thread_id}/messages
POST /api/agent/threads/{thread_id}/messages
POST /api/agent/context/resolve
POST /api/agent/runs
GET  /api/agent/runs
GET  /api/agent/runs/{run_id}
PATCH /api/agent/runs/{run_id}/budget
POST /api/agent/runs/{run_id}/run
POST /api/agent/runs/{run_id}/step
POST /api/agent/runs/{run_id}/approve
POST /api/agent/runs/{run_id}/reject
POST /api/agent/runs/{run_id}/pause
POST /api/agent/runs/{run_id}/resume
POST /api/agent/runs/{run_id}/cancel
GET  /api/agent/runs/{run_id}/events
GET  /api/agent/runs/{run_id}/events.ndjson
POST /api/agent/memory-proposals/from-selection
GET  /api/agent/memory-proposals
GET  /api/agent/memory-proposals/{proposal_id}/review
GET  /api/agent/memory-proposals/{proposal_id}/evidence
PATCH /api/agent/memory-proposals/{proposal_id}
POST /api/agent/memory-proposals/{proposal_id}/approve
POST /api/agent/memory-proposals/{proposal_id}/reject
POST /api/agent/memory-proposals/{proposal_id}/defer
GET  /api/agent/memories
POST /api/agent/memories
PUT  /api/agent/memories/{memory_id}
POST /api/agent/memories/{memory_id}/activate
POST /api/agent/memories/{memory_id}/reject
DELETE /api/agent/memories/{memory_id}
```

### 测试

```bash
uv run --project code_agent pytest code_agent/tests code_agent/test_vulnerability_detection.py -q
npm --prefix code_agent/webui test
npm --prefix code_agent/webui run build
```

必跑测试使用 scripted model 和临时 fixture workspace，不需要 API Key，也不会真实调用 Aider。
