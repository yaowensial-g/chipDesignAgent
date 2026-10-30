# 运行逻辑说明

> 当前对话主链为 Backend Message → ContextBuilder/RAG → Role Resolution → Intent Enricher → Permission Filter → Tool Selector → Primary Agent → Tool/Delegate → Observation。

## 1. 启动

```powershell
.\scripts\setup.ps1
.\scripts\run-web.ps1
```

`run-web.ps1` 隐藏启动 Uvicorn `127.0.0.1:8000`，以前台方式启动 Vite `127.0.0.1:5173`；Vite 把 `/api` 代理到后端。

首次导入应用时，SQLite 会自动建表，Tool Registry 会注册内置 Tool，Workspace 根目录在 `data/workspaces`。

## 2. 页面初始化

1. 初始化示例项目和模块，并从后端加载该项目的会话列表；
2. 读取公开模型设置；
3. 默认选择设计工程师工作角色，并允许切换验证工程师；
4. 显示真实模型或模拟运行时；
5. 用户可以直接聊天，不要求先上传 FS/AS；
6. 右上角提供工程工作台、系统自检和模型设置。

浏览器刷新后可以选择历史会话，网页会分页恢复后端消息，并通过 `active_run_id` 重新绑定
当前 Run。上传控件的本地文件对象不会恢复，但已入库的文档仍可由后端 RAG 检索。

## 3. 对话、角色主 Agent 和统一 Run

```mermaid
sequenceDiagram
    participant U as User
    participant W as Web
    participant C as ConversationService
    participant S as SessionService
    participant CB as ContextBuilder
    participant RAG as Document/Memory Retrieval
    participant O as Control Plane
    participant TS as Tool Selector
    participant P as Primary Agent
    participant T as Tool Registry
    participant D as DelegationService
    participant E as Tool Handler
    participant DB as SQLite
    U->>W: 消息 + 可选文档
    W->>C: POST /conversations/{id}/messages
    C->>S: session + user_role
    S-->>O: Design / Verification 主 Agent
    S->>DB: 持久化 user message
    O->>CB: 当前消息 + project/module/role
    CB->>RAG: 关键词 + 语义混合检索
    RAG-->>CB: active memory + document chunks
    CB->>DB: Context Manifest
    CB-->>O: 有 Token 预算的上下文快照
    O->>O: enrich TaskIntent
    O->>DB: create Unified Run
    loop 最多 6 个 Planner Step
        O->>T: 按主 Agent 权限过滤
        T-->>TS: 轻量 Tool Metadata
        TS-->>O: Tool Shortlist
        O->>P: Intent + 候选 Tool Schema + Observations
        P-->>O: ToolSelection
        O->>T: 解析 Tool + Policy 校验
        alt 跨专业判断
            T->>D: 创建受控专家任务
            D-->>O: 结构化专家 Observation
        else 普通 Tool
            T->>E: 调用受控 Handler
        end
        E-->>O: Observation / 专业对象 ID
        O->>DB: RunEvent + result/status
    end
    O-->>C: orchestrated ChatResponse
    C->>DB: 持久化 assistant message
    C-->>W: assistant + RunView + 可选 TaskView
```

上传文件只建立版本化证据，不自动触发流程。用户决定任务目标和研发阶段；模型不会直接
接收全部原文，而是接收与当前问题相关的分块。Intent Enricher 只整理有证据的信息，
当前主 Agent 从权限安全的候选集中选择 Tool。明确 DS 任务必须能够检索到 AS，否则
`ask_user` 让 Run 进入 `waiting_for_user`。

## 3.1 上下文选择顺序

```text
保存本轮用户消息
→ 首次文档幂等入库、分块、索引
→ 读取当前角色的近期消息
→ 较早消息形成可追踪抽取摘要
→ 检索 active 长期记忆
→ 对 active 文档分块执行混合 RAG
→ 按区段 Token 预算组装 effective_request
→ 保存 Context Manifest
→ Intent / Selector / Primary Agent / Delegate 复用同一快照
```

Runtime 只保留按 Token 计数的最后安全阀，不再使用 120,000 字符和上传顺序截断。

统一 Run 和专业执行记录分层：

```text
Run（用户任务控制面）
├─ DelegationTask（只读专家协作）
├─ DS Task（LangGraph 专业状态）
├─ RTL Patch（候选修改与审批）
└─ ToolRun（一次外部进程执行）
```

因此 `runs.run_id` 和 `tool_runs.run_id` 含义不同，前者面向用户任务，后者面向工具进程。

DS 图仍为：生产角色分析 → 规格冲突 → 最多 1～3 轮内部讨论 → 必要时等待用户 → DS → 四类默认评审和自定义评审 → 交付。

## 4. 等待真实架构结论与重启恢复

正常运行时使用 LangGraph `interrupt` 和相同 `thread_id` 恢复。TaskService 同时把去除不可序列化 Interrupt 对象后的状态和 `pending_user_request` 写入 SQLite。

后端重启后：

1. `GET /tasks/{id}` 从 SQLite 恢复 TaskView；
2. 若快照处于等待用户，`POST /resume` 走兼容恢复路径；
3. 保存用户结论/问题单引用；
4. 重新生成 DS 和评审；
5. 写回 delivered/cancelled 快照。

局限：这条路径不是从 LangGraph 精确节点 Checkpoint 恢复，已完成的模型节点不会按持久化图语义逐节点重放。

## 5. RTL 候选和 Patch

```mermaid
sequenceDiagram
    participant U as User
    participant W as Engineering Modal
    participant RTL as RTLService
    participant D as Design Runtime
    participant P as PatchService
    participant FS as Workspace
    U->>W: 生成 RTL 候选
    W->>RTL: DS + 文档 + 目标路径
    RTL->>D: generate_rtl
    D-->>RTL: SystemVerilog candidate
    RTL->>P: before + after + reason + sources
    P-->>W: unified diff / patch_ready
    alt 用户批准
        W->>P: approve
        P->>FS: 校验 before 未变化并原子写入
        P-->>W: applied
    else 拒绝或自定义
        W->>P: reject/custom
        P-->>W: 保留决策，不修改目标
    end
```

确定性运行时只生成安全骨架和 TODO；真实模型基于 DS 生成候选，但均受同一 Diff Review 保护。

## 6. ToolRun

### 启动

1. 前端选择 Workspace、Tool、Role 和参数；
2. 对需要授权的 Tool，用户必须明确点击执行；
3. 后端校验 Workspace、Tool、角色和批准位；
4. 创建 `queued` Run 并立即返回 Run ID；
5. 线程池执行，状态进入 `running`；
6. 收集 stdout/stderr，状态短暂进入 `parsing`；
7. 日志写入 `logs/<run_id>.*.log`；
8. 生成 Issue 后进入 `completed`、`failed` 或 `timed_out`。

### 本地 EDA 仿真执行配置

- `run_mock_eda` 接收 `stage`、`target` 和 `options`；
- stage 支持 compilation、simulation、lint、cdc、synthesis、sta；
- 不读取 PATH，不要求安装 Verilator 或商业 EDA；
- 不启动子进程、不生成真实日志和 Issue；
- 返回带 `[MOCK EDA]` 的明确非真实执行字符串；
- Run.result 同时保存 `mock=true` 与 `real_execution=false`。

## 7. 单 Issue 闭环

日志解析后每条 Issue 包含：工具、严重度、错误码、文件/行号、原始日志引用、指纹、根因判断、证据、影响、修改建议、1～10 置信度和建议复验。

`GET /tool-runs/{run_id}/issues/next` 只把队列中第一条未处理问题设为 `awaiting_user`。用户动作：

- `approve`：记录接受建议；只有生成候选 Patch 并获得单独批准后才能修改文件；
- `skip`：标记跳过并进入下一条；
- `custom`：保存用户自定义处理原则，作为后续候选 Patch 的输入；
- `explain`：保持当前问题活跃。

系统提供单问题展示和决策记录。自动修复只有在生成候选 Patch、获得用户批准并完成最小复验后，才能进入 `resolved`；否则保持为开放问题。

## 8. 模型选择

```text
CHIP_AGENT_MODEL_ENABLED 显式 false
    → Deterministic Runtime
否则 effective settings.ready
    → LLM Runtime
否则
    → Deterministic Runtime
```

配置优先级：环境变量 > `data/model_settings.json` > `config/model.toml`。网页保存的 Key 使用 Windows DPAPI；测试强制关闭真实模型。

## 9. 状态序列

统一 Run：

```text
planned → running → completed / failed / cancelled
                  ↘ waiting_for_user
                  ↘ waiting_for_approval
```

Run 等待原因不是只有人工架构结论，还可能是缺少 AS、已确认 DS、Workspace、Tool 授权或真实 Tool Adapter。

DS：

```text
requested → executing → internal_discussion? → waiting_for_user?
→ reviewing → delivered
```

ToolRun：

```text
queued → running → parsing → completed / failed / timed_out
```

EngineeringIssue：

```text
detected → awaiting_user → approved / skipped / custom_requested
```

`patch_ready/applied/verifying/resolved/reopened` 定义完整的修复状态语义；只有实际创建 Patch、应用修改并执行复验的 Adapter 才能推进这些状态。

## 10. 失败行为

| 场景 | 行为 |
|---|---|
| 文件解析失败 | 422，前端显示原因 |
| DS 缺少 AS | 创建 waiting_for_user Run，不创建专业 DS Task |
| 模型调用/JSON 失败 | 结构化错误、请求编号和诊断建议 |
| Workspace 路径穿越 | 422 |
| 无权限或未授权 Tool | 403 |
| Tool 不存在/Workspace 不存在 | 404 |
| Mock EDA | Run completed，但明确标记未真实执行 |
| Patch Review 期间目标变化 | 409，不覆盖文件 |
| 后端重启 | 已落库结果可查；运行中 Tool 不自动续跑 |

### 10.1 各任务的当前编排行为

| 用户明确请求 | RunType | 当前动作 |
|---|---|---|
| 完成/评审 DS | `ds_design` | 调用 DS Workflow，Task 与 Run 双向关联 |
| 生成/实现 RTL | `rtl_implementation` | 要求已确认 DS，生成 Candidate/Patch，等待审批 |
| 执行编译/Sim/Lint/CDC/综合/STA | 对应 RunType | 调用统一 `run_mock_eda`，返回 Mock 字符串 |
| 执行已有用例 | `test_execution` | 调用 `discover_testcases`，不自动生成验证计划 |

直接 `/tasks/ds`、`/rtl/candidates` 和 `/tool-runs` 仍可用于测试与系统集成，但正式网页的主路径是 Conversation → RunService。

## 11. 自检

`GET /diagnostics` 不调用外部模型，不产生费用。检查后端、默认配置、本地设置、模型就绪状态、数据目录、SQLite、Tool Registry 和 Mock EDA 状态。

## 12. 进程启动与组件初始化顺序

### 12.1 后端导入阶段

Uvicorn 导入 `app.main:app` 时发生以下初始化：

```mermaid
sequenceDiagram
    participant U as Uvicorn
    participant Main as app.main
    participant DB as Database
    participant Tools as Tool Registry
    participant Graph as LangGraph
    participant API as FastAPI

    U->>Main: import app.main
    Main->>DB: import global database
    DB->>DB: create data directory
    DB->>DB: create/migrate SQLite tables
    Main->>Tools: import ToolRunService
    Tools->>Tools: register built-in tools
    Main->>Graph: import compiled DS graph
    Graph->>Graph: compile with MemorySaver
    Main->>API: register middleware/exception handlers/routes
    API-->>U: application ready
```

如果 SQLite Schema 初始化失败，应用导入会失败。当前 EDA 路径不检查任何外部可执行文件，因此开发机不需要安装 Verilator。模型设置也不会在启动时发出外部请求。

### 12.2 前端启动阶段

Vite 加载 React 后，`App` 只读取公开模型配置，不自动读取 Workspace、Task 或运行历史，也不自动调用真实模型。用户必须显式打开工程工作台或发送消息。

## 13. 请求公共处理链

所有 `/api/v1` 请求先经过请求编号中间件：

```text
接收 X-Request-ID（若有）或生成随机 ID
→ 执行 FastAPI Route
→ 成功：把 X-Request-ID 写入响应
→ 未处理异常：分类错误、记录同 ID 日志、返回结构化 error
```

结构化错误示例：

```json
{
  "error": {
    "code": "MODEL_INVOCATION_FAILED",
    "message": "模型没有返回有效 JSON",
    "request_id": "68c7b50cb863",
    "suggestions": ["运行系统自检", "检查模型名称、Base URL、API Key 和网络连接"]
  }
}
```

前端 `parseResponse` 优先显示结构化错误；若只有 FastAPI `detail` 则降级显示 detail；未知 500 会引导用户运行自检。

## 14. 文档抽取详细流程

```mermaid
flowchart TD
    Select[用户选择/拖入文件] --> Multipart[POST documents/extract]
    Multipart --> Size{空文件或超过 10 MB?}
    Size -->|是| Reject[422]
    Size -->|否| Ext{扩展名}
    Ext -->|txt/md| Decode[UTF-8 BOM → UTF-8 → GB18030]
    Ext -->|pdf| PDF[PyPDF 逐页抽取]
    Ext -->|docx| DOCX[段落 + 表格单元格]
    Ext -->|其他| Reject
    Decode --> Empty{抽取文本为空?}
    PDF --> Empty
    DOCX --> Empty
    Empty -->|是| Reject
    Empty -->|否| Infer[根据独立文件名 Token 推断 FS/AS/ICD]
    Infer --> Store[后端创建文档版本与检索分块]
```

后端保存抽取后的文档正文、文档版本和检索分块；原始二进制是否作为 Artifact 保留由上传策略决定。浏览器刷新后，前端通过会话和文档查询接口恢复可见上下文，不依赖浏览器内存保存工程历史。

## 15. 普通聊天详细流程

1. 前端发送会话标识、项目、模块和本轮消息；
2. ConversationService 将请求统一交给 OrchestratorService；
3. ContextBuilder 检索会话摘要、近期消息、长期记忆和相关文档 Chunk，并形成 Context Manifest；
4. Runtime 形成 TaskIntent，Tool Selector 从角色已授权的 Tool 元数据中选择候选，Planner 只接收候选 Schema；
5. 普通问答选择 `reply_to_user`，不校验 AS，也不创建 DS Task；
6. Chat Tool 接收受预算控制的上下文并形成 Observation；
7. Run 以 `run_type=chat` 完成，返回 `ChatResponse`，其中 `route=orchestrated`、`task=null`。

聊天正文和会话元数据保存在后端。前端进入会话时分页读取历史消息，并查询 active Run 恢复任务状态；早期消息由确定性摘要压缩，原文仍保留在存储层。

## 16. DS 工作流逐节点说明

| 节点 | 读取 | 写入 | 失败影响 |
|---|---|---|---|
| `prepare_context` | request、disabled_agents | status、quality_warnings、discussion_round、event | Task 失败 |
| `architecture_analysis` | 文档、模块 | architecture_analysis、event | Task 失败 |
| `design_analysis` | AS/文档、模块 | design_analysis/submodules、event | Task 失败 |
| `verification_analysis` | 文档、模块 | verification_analysis、event | Task 失败 |
| `detect_conflicts` | request | issues、status、event | Task 失败 |
| `discussion` | issues、round | 增加轮次、更新 Issue、event | Task 失败 |
| `await_user` | issues | Interrupt；恢复后写 user_resolution | 进入等待状态 |
| `generate_ds` | request、design_analysis、resolution | ds_markdown、reviewing、event | 不产生候选 |
| 四类 Review | request、ds_markdown | 各自 Finding 列表、event | 当前整体调用失败 |
| `custom_review` | custom_reviewers | custom_findings、event | 当前整体调用失败 |
| `aggregate_findings` | 所有 Finding | findings、delivered、event | Task 不交付 |

### 16.1 Agent 禁用

被禁用的分析角色使用最小占位结果继续；被禁用的 Reviewer 返回空列表。`prepare_context` 对每个禁用的默认角色增加质量警告。禁用只影响可选角色，不绕过路径、Patch 或 Tool 权限。

### 16.2 并行汇合

Architecture、Design、Verification 从 `prepare_context` 扇出，三者全部到达后才进行冲突检测；DS 生成后多个 Review 扇出，并在 Aggregate 节点汇合。`events` 使用列表追加 Reducer，防止并行分支覆盖彼此事件。

### 16.3 模型调用数量

真实模型模式下，一次无冲突、无自定义 Reviewer 的 DS 通常包括 Intent Enricher、Planner、三个分析、冲突检测、DS 生成和四个 Review。实际调用数会随重新规划、讨论和格式修复增加，因此生产化需要按步骤记录 Token、耗时和费用。

## 17. DS 正常路径时序

```mermaid
sequenceDiagram
    participant UI
    participant Conv as ConversationService
    participant Task as TaskService
    participant Graph as LangGraph
    participant Orch as OrchestratorService
    participant Planner as LLM Planner
    participant DB as SQLite

    UI->>Conv: POST /chat（明确生成 DS）
    Conv->>Orch: orchestrate(message, documents)
    Orch->>Orch: enrich TaskIntent + create Unified Run
    Orch->>Planner: Tool Catalog + 当前 Observation
    Planner-->>Orch: create_ds_candidate(args)
    Orch->>Orch: Policy / 前置条件校验
    Orch->>Task: create_ds_task
    Task->>Graph: ainvoke(initial_state, thread_id)
    par 角色分析
        Graph->>Runtime: architecture_analysis
        Graph->>Runtime: design_analysis
        Graph->>Runtime: verification_analysis
    end
    Graph->>Runtime: detect_issues
    Runtime-->>Graph: []
    Graph->>Runtime: generate_ds
    par 独立评审
        Graph->>Runtime: spec review
        Graph->>Runtime: microarchitecture review
        Graph->>Runtime: verification review
        Graph->>Runtime: interface review
    end
    Graph-->>Task: delivered state
    Task->>DB: upsert task snapshot
    Task-->>UI: TaskView
```

如果外部集成直接调用 `/tasks/ds`，则绕过统一对话编排并直接创建兼容 DS Task；正式网页主入口始终通过 Orchestrator。

## 18. 冲突、Interrupt 与恢复细节

### 18.1 进程内恢复

1. `detect_conflicts` 返回非空 Issue；
2. `discussion` 增加轮次，直到请求中的上限；
3. `await_user` 调用 LangGraph `interrupt(payload)`；
4. `ainvoke` 返回包含 `__interrupt__` 的状态；
5. TaskService 保存热状态，并把 Interrupt Payload 单独序列化到 SQLite；
6. 用户提交 Resolution；
7. TaskService 使用同一 thread_id 调用 `Command(resume=...)`；
8. Graph 从 await_user 节点继续到取消或 DS 生成。

### 18.2 重启后兼容恢复

内存中的 `__interrupt__` 和 MemorySaver 已丢失，但 SQLite 仍有 request、分析结果、Issue、事件和 `_pending_user_request`。新 TaskService 加载快照后：

- cancel：直接写 cancelled；
- 其他动作：保存 Resolution，调用 Runtime 重新生成 DS，并重新运行默认/自定义 Review，最后写 delivered。

这条路径不会恢复原始 LangGraph 执行栈，也可能再次产生模型费用。它保证业务可以继续，但不是严格的 exactly-once 工作流恢复。

## 19. RTL Candidate 与 Patch 状态机

```mermaid
stateDiagram-v2
    [*] --> patch_ready: create candidate
    patch_ready --> applied: approve + before unchanged
    patch_ready --> rejected: reject
    patch_ready --> custom_requested: custom
    custom_requested --> custom_requested: 保存用户自定义处理原则
    patch_ready --> patch_ready: approve 发生冲突并返回 409
    applied --> [*]
    rejected --> [*]
```

### 19.1 新文件

目标不存在时 before 为空，Diff 从空文件生成。批准前文件仍不存在；批准时若目标仍不存在则创建。

### 19.2 修改已有文件

创建 Patch 时读取完整 before。批准时再次读取；任何差异都会拒绝应用，即使变化发生在 Patch 不涉及的其他行。这种策略保守但安全。

### 19.3 Apply 顺序

```text
读取数据库 Patch
→ 检查状态是否允许决策
→ 解析并校验 Workspace 路径
→ 比较当前目标与 before
→ 临时文件写入 after
→ atomic replace
→ 更新 status/applied_at
```

当前没有回滚 API；若应用后用户要撤销，应基于当前内容创建反向候选 Patch，而不是直接篡改历史状态。

## 20. ToolRun 详细状态转换

```mermaid
stateDiagram-v2
    [*] --> queued: POST tool-runs
    queued --> running: worker starts
    running --> parsing: executor returns
    running --> parsing: executor raises → TOOL_INTERNAL
    parsing --> completed: exit_code = 0
    parsing --> timed_out: exit_code = 124
    parsing --> failed: other nonzero exit
```

枚举同时定义 `preparing`、`collecting`、`cancelled`，供具备准备、结果收集和取消能力的外部 Adapter 使用；本地执行器只进入与实际动作对应的状态。

### 20.1 创建阶段

- 验证 Workspace 存在；
- 从 Registry 查找 Tool；
- 检查 Role 是否在 allowed_roles；
- 若 requires_user_approval=true，检查请求批准位；
- 写入 queued Run；
- 提交线程池；
- 返回 202。

### 20.2 执行阶段

线程将状态改为 running，调用 Tool Executor。Executor 返回实际命令数组、exit_code、stdout 和 stderr。未捕获异常被转换为 `TOOL_INTERNAL`，而不是让 Run 永久停留 running。

### 20.3 收集与解析

Service 把状态改为 parsing，保存最大约一百万字符的 stdout/stderr，并把日志写到 Workspace。随后解析标准化 Issue，最后根据 exit_code 写终态。

## 21. 保留但默认禁用的 Verilator Adapter

以下是工程环境接入外部工具时使用的 Adapter 契约。默认配置中 `verilator_lint.enabled=false`，
聊天不会选择它，直接 ToolRun 接口也会拒绝执行；日常测试应使用 `run_mock_eda`。

请求示例：

```json
{
  "workspace_id": "...",
  "tool_name": "verilator_lint",
  "role": "design",
  "arguments": {
    "files": ["rtl/dma_ctrl.sv", "rtl/dma_pkg.sv"],
    "top_module": "dma_ctrl"
  },
  "user_approved": true
}
```

校验顺序：

1. PATH 中查找 Verilator；
2. `files` 必须为非空数组；
3. 每个路径必须在 Workspace；
4. 后缀只能为 `.sv/.svh/.v/.vh`；
5. 文件必须存在；
6. top module 必须匹配 HDL 标识符格式；
7. 构造 `verilator --lint-only -Wall ... --top-module ...`；
8. 以 Workspace 为 cwd、`shell=False` 执行；
9. 超时返回 124 和 `TOOL_TIMEOUT`。

工具不存在时不会验证后续文件参数，而是优先返回环境错误。这表示当前最先阻断的是执行环境。

## 22. 日志标准化和 Issue 排队

### 22.1 解析

当前解析器识别：

- `%Error-CODE: file:line:column: message`；
- `%Warning-CODE: ...`；
- `TOOL_ENVIRONMENT`；
- `TOOL_ARGUMENT`；
- `TOOL_INTERNAL`；
- `TOOL_TIMEOUT`。

每条结果产生 stage、severity、code、message、file、line 和 fingerprint。当前 stage 主要区分 lint 与通用 tool_execution。

### 22.2 启发式分析

| Code 类型 | 根因模板 | 默认置信度 |
|---|---|---:|
| TOOL_ENVIRONMENT | 工具环境不满足，不是 RTL 错误 | 10 |
| WIDTH | 位宽、signed 或截断不一致 | 8 |
| MULTIDR* | 多过程或连续赋值驱动同一信号 | 8 |
| LATCH | 组合分支未完整赋值 | 8 |
| 其他 | 需结合源码和 DS 判断 | 6 |

这些是确定性解析建议，不是最终根因。需要语义分析时，主 Agent 通过日志分析 Tool 读取原始证据并给出带置信度的判断。

### 22.3 队列推进

```mermaid
stateDiagram-v2
    [*] --> detected
    detected --> awaiting_user: next_issue
    awaiting_user --> approved: approve
    awaiting_user --> skipped: skip
    awaiting_user --> custom_requested: custom
    awaiting_user --> awaiting_user: explain
```

每次 `next_issue` 按 queue_index 返回第一条仍活跃的问题。`approve` 当前只保存决策，不会自动进入 patch_ready；`skip` 保留风险；`custom` 不会自动生成新 Patch；`explain` 不推进队列。

## 23. Workspace 和 Artifact 运行逻辑

创建 Workspace 时：

1. 用 project_id 生成安全 slug；
2. 追加 Workspace UUID 前缀避免同名冲突；
3. 在配置根目录内解析绝对路径；
4. 创建 `rtl`、`dv`、`constraints`、`candidates`、`artifacts`、`logs`；
5. 写入 SQLite；
6. 返回 Workspace 的绝对根路径。

创建 Artifact 时：

1. 校验相对路径；
2. 原子写入内容；
3. 查询同路径最大版本并加一；
4. 计算 SHA-256；
5. 保存类型、状态、来源和时间。

当前顺序是先写文件再插入 Artifact；若数据库写入失败，可能留下未登记文件。生产化需要补偿逻辑或暂存区。

## 24. 并发、幂等与重复请求

| 操作 | 当前幂等性 | 重复调用结果 |
|---|---|---|
| 创建 Workspace | 非幂等 | 生成新的 Workspace |
| 创建 DS Task | 非幂等 | 新 Task、新模型调用 |
| Resume Task | 条件幂等 | 首次后不再等待，再次调用返回 409 |
| 创建 RTL Candidate | 非幂等 | 新 Patch，可能再次调用模型 |
| Approve Patch | 条件幂等 | 应用后再次批准返回 409 |
| Start ToolRun | 非幂等 | 创建并执行新 Run |
| Issue explain | 近似幂等 | 保持 awaiting_user，但更新决策内容 |

当前 API 没有 Idempotency-Key。网络重试可能重复创建 Task、Patch 或 Run。生产环境应为高成本 POST 支持客户端幂等键，并将其与用户、项目和请求 Payload Hash 绑定。

## 25. 配置热更新运行逻辑

保存模型设置时：

1. 前端提交公开字段和可选新 Key；
2. ModelSettingsService 读取现有 Provider Key；
3. 新 Key 使用 DPAPI 加密；
4. 使用临时文件替换 `data/model_settings.json`；
5. 返回公开掩码视图；
6. 前端清空 Key 输入；
7. 后续每次 Runtime 调用重新读取 effective settings，无需重启。

环境变量始终优先于网页设置，因此用户可能在页面保存新值但实际仍由环境变量覆盖。公开视图中的 `api_key_source` 用于解释这一情况。

## 26. 端到端示例：从 DS 到 Lint

```text
用户上传 FS/AS/ICD
→ 发送“生成 dma_ctrl DS”
→ Intent Enricher 形成 TaskIntent
→ Planner 选择 create_ds_candidate
→ Orchestrator 创建并更新 ds_design Run
→ 三角色分析、冲突检测
→ 无冲突：生成 DS、四类评审、保存 Task
→ 用户打开工程工作台
→ 创建/复用 Workspace
→ Design Runtime 生成 rtl/dma_ctrl.sv Candidate
→ PatchService 返回 Diff
→ 用户批准
→ 校验 before、原子写入 RTL
→ 用户明确授权 Verilator Lint
→ ToolRun queued/running/parsing/failed 或 completed
→ 保存原始日志、生成标准化 Issue
→ 前端一次展示一条 Issue
→ 用户 approve/skip/custom/explain
```

当前链路在 Issue 决策后停止。目标链路还应包括“生成修复 Patch → 再次批准 → 应用 → 最小 Lint 复验 → resolved/reopened”。

## 27. 运行时数据落点总表

| 数据 | 产生位置 | 保存位置 | 前端刷新后 |
|---|---|---|---|
| 当前聊天消息 | React | 浏览器内存 | 丢失 |
| 上传抽取文本 | React/API 响应 | 浏览器内存 | 丢失 |
| 模型设置 | 设置页面 | JSON + DPAPI/环境变量 | 保留 |
| DS Task 快照 | TaskService | SQLite | 保留，可按 ID 查询 |
| LangGraph Checkpoint | Workflow | MemorySaver | 丢失 |
| Workspace 目录 | WorkspaceService | 文件系统 + SQLite | 保留 |
| Artifact 内容 | ArtifactService | 文件系统 + SQLite 元数据 | 保留 |
| Patch before/after | PatchService | SQLite | 保留 |
| ToolRun | ToolRunService | SQLite | 保留 |
| 原始工具日志 | ToolRunService | SQLite 截断副本 + Workspace 文件 | 保留 |
| EngineeringIssue/决策 | ToolRunService | SQLite | 保留 |

## 28. 测试时运行逻辑

`tests/conftest.py` 在测试收集阶段设置 `CHIP_AGENT_MODEL_ENABLED=false`。因此：

- Intent Enricher、Planner、Chat、DS、RTL 都走 Deterministic Runtime；
- 不读取用户保存的启用状态发起模型请求；
- 测试结果不受网络、配额或模型版本影响；
- Provider JSON 测试通过 Mock 单独验证格式修复；
- Phase 2 测试使用真实临时 SQLite 或受控全局 Workspace 验证路径和审批不变量。

前端通过 `tsc -b && vite build` 验证类型和生产打包。页面交互采用人工验收；自动化测试覆盖后端契约与前端编译边界。

## 29. 恢复语义总结

| 对象 | 可查询 | 可继续 | 精确恢复 |
|---|---:|---:|---:|
| 已交付 DS Task | 是 | 不适用 | 保存最终投影 |
| 等待用户 DS Task | 是 | 是，兼容路径 | 否 |
| MemorySaver 节点状态 | 进程内 | 进程内可以 | 重启后否 |
| 已完成 ToolRun | 是 | 不适用 | 日志/结果保留 |
| 运行中 ToolRun | 数据行可见 | 否 | 否 |
| Patch Ready | 是 | 可批准/拒绝 | 是，但依赖目标未变化 |
| 已应用 Patch | 是 | 不自动回滚 | 历史保留 |

“可查询”“可继续”和“精确恢复”是三个不同能力。系统对专业对象提供持久化查询，并为等待用户的 DS、RTL 和 Tool 审批提供受控恢复；未纳入恢复协议的外部进程不能宣称精确接管。
