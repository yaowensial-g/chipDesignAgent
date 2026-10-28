# 代码结构说明（v0.4 动态编排）

> v0.4 新增 `app/services/orchestrator_service.py` 和 `app/tools/orchestration.py`。`ConversationService` 已成为兼容门面，主链不再调用固定 Conversation Router；详见 [dynamic-orchestration-v0.4.md](dynamic-orchestration-v0.4.md)。

## 1. 目录

```text
chipDesignAgent/
├─ app/
│  ├─ agents/       角色目录、确定性/真实模型运行时
│  ├─ api/          FastAPI 路由
│  ├─ config/       模型设置和 DPAPI
│  ├─ documents/    上传文档抽取
│  ├─ domain/       Pydantic 契约和枚举
│  ├─ models/       外部模型协议适配
│  ├─ services/     对话、任务、工程服务
│  ├─ skills/       项目 Skill 的安全加载器
│  ├─ storage/      SQLite Schema 和访问层
│  ├─ tools/        Tool Registry 与执行器
│  ├─ workflows/    LangGraph DS 工作流
│  └─ main.py       应用入口和统一异常响应
├─ config/model.toml
├─ data/             运行数据，关键文件均被 Git 忽略
│  ├─ chipflow.db
│  ├─ model_settings.json
│  └─ workspaces/
├─ frontend/src/
├─ samples/
├─ scripts/
├─ skills/           架构、设计和评审的版本化专业指南
├─ tests/
├─ docs/
├─ PRD_v0.1.md       历史需求
├─ PRD_v0.2.md       当前需求基线
├─ SELF_VALIDATION_v0.1.md
├─ SELF_VALIDATION_v0.2.md
└─ pyproject.toml
```

`.venv`、`node_modules`、`dist`、`__pycache__`、SQLite 文件、日志和测试 Workspace 都是运行生成物。

## 2. 后端入口与领域契约

### `app/main.py`

- FastAPI v0.2.0；
- CORS；
- 请求编号；
- 模型、配置和未知异常的结构化错误；
- `/api/v1` 路由挂载。

### `app/api/routes.py`

包含系统/模型、文档、Conversation/Run、DS 兼容任务、Workspace/Artifact、RTL、Patch、ToolRun 和 Issue 决策接口。路由只处理 HTTP 边界，编排和工程逻辑位于 Service。

### `app/domain/models.py` 与 `enums.py`

除 v0.1 的文档、Task、Issue、Finding、模型设置外，新增：

- `WorkspaceView`、`ArtifactView`；
- `RTLCandidateRequest/Response`；
- `PatchView`；
- `ToolDefinitionView`、`ToolRunView`；
- `EngineeringIssueView`、Issue/Patch 决策；
- `RunView`、`RunEventView`、`RunResumeRequest`；
- `RunType` 与 `RunStatus`；
- AgentRole、Mutability、Artifact/ToolRun/EngineeringIssue 状态。

Pydantic 是后端 HTTP 契约的事实来源；前端类型目前手工同步。

## 3. 编排、Agent 与 DS 工作流

### `app/services/run_service.py`

统一控制面服务，负责：

- 将 Router 输出映射为 `RunType`；
- 先落库 Run，再调用 DS Workflow、RTLService 或 ToolRunService；
- 把 `Task/ToolRun/Patch` ID 关联到 Run，而不是复制专业执行逻辑；
- 保存等待用户、等待审批、能力缺失和执行完成等状态；
- 持久化 RunEvent；
- 查询 Run 时把 ToolRun 终态同步到统一 Run；
- 处理 DS Resume、RTL Patch 决策、Tool 授权和通用取消。

当前聊天可编排 DS、RTL、Lint 和已有用例发现；Sim/CDC/综合/STA 在缺少 Adapter 时生成诚实的能力缺失等待项。

### `app/agents/definitions.py`

角色目录包括 Orchestrator、Chat、Architecture、Design、Verification、四类默认 Reviewer 和 Specialist Reviewer。旧 Conversation Router 已退出主链；Agent 是职责边界，不是独立进程。

### `app/agents/runtime.py`

- `DeterministicAgentRuntime`：离线测试和演示；
- `LLMChipDesignRuntime`：真实提示词和结果归一化；
- `ConfigurableAgentRuntime`：按当前设置动态选择；
- 支持聊天、DS 分析/生成/评审和 RTL 候选生成。

真实模型 Runtime 会通过 `app/skills/registry.py` 加载根目录 `skills/**/*.md`。Skill 提供专业分析步骤和禁止事项，但没有 Workspace、数据库或进程执行权限；确定性测试 Runtime 不依赖模型 Skill 才能运行。

### `app/workflows/ds_workflow.py`

保留 v0.1 的 LangGraph DS 流程：三个生产角色分析、冲突检测、固定轮次讨论、用户 Interrupt、DS 生成及并行评审。内部 Checkpointer 仍是 `MemorySaver`。

### `app/services/task_service.py`

- 调用 LangGraph；
- 在内存保留热状态；
- 把可序列化状态写入 SQLite `task_snapshots`；
- 从 SQLite 读取历史 TaskView；
- 服务重启后，对等待用户的任务执行兼容恢复路径并完成 DS/评审。

注意：这不是把 LangGraph Checkpoint 本身写入 SQLite。

## 4. 工程执行组件

### `app/storage/database.py`

初始化 `data/chipflow.db`，当前表：

- `workspaces`；
- `artifacts`；
- `patches`；
- `tool_runs`；
- `engineering_issues`；
- `task_snapshots`。
- `runs`；
- `run_events`。

使用 WAL、外键、线程锁和短连接。当前没有 Alembic；新增列使用小型兼容迁移。

### `app/services/workspace_service.py`

创建隔离目录及 `rtl/dv/constraints/candidates/artifacts/logs` 子目录；所有文件路径必须是 Workspace 内相对路径。Artifact 写入时记录版本、SHA-256、状态和来源。

### `app/services/rtl_service.py`

调用当前 Agent Runtime 生成 SystemVerilog 候选，然后交给 PatchService；不会直接写目标 RTL。

### `app/services/patch_service.py`

记录 before/after、来源、原因和 unified diff。批准时再次比较当前目标与 before 内容，匹配后才原子写入；支持 reject/custom 状态。

### `app/tools/registry.py`

每个 Tool 注册名称、说明、允许角色、修改等级、是否需要批准、幂等性、超时和执行函数。

### `app/tools/builtin.py`

- `discover_testcases`：扫描已有验证资产，不生成用例；
- `verilator_lint`：验证输入路径和后缀，使用 `shell=False` 执行 Verilator；工具不存在、参数错误和超时都返回可解析错误。

### `app/services/tool_run_service.py`

- 异步线程池执行；
- Tool 角色权限和批准检查；
- Run 状态持久化；
- stdout/stderr 写入 Workspace；
- 解析 Verilator 和环境错误；
- 指纹去重；
- 产生根因、影响、建议、置信度和复验范围；
- 一次只激活一条 Issue，并保存用户决策。

当前没有外部进程取消和崩溃后的运行中进程接管。

## 5. 前端

### `frontend/src/App.tsx`

主页面：项目/模块、文档上传、聊天、统一 Run 状态及事件、真实架构反馈、RTL Patch 审批、DS/Finding 视图，以及模型设置、自检、工程工作台兼容入口。运行中的 Tool Run 会按统一 Run ID 轮询。

### `frontend/src/EngineeringModal.tsx`

- 读取/创建项目 Workspace；
- 基于当前 DS 生成 RTL 候选；
- 展示 Diff，批准/拒绝/自定义；
- 明确授权执行 Lint；
- 轮询 ToolRun；
- 逐条展示和决策 Issue。

### 其他文件

- `api.ts`：唯一 HTTP Client；
- `types.ts`：手工维护的 TypeScript 契约；
- `ModelSettingsModal.tsx`：模型及密钥；
- `DiagnosticsModal.tsx`：本机自检；
- `styles.css`：三栏主界面和 Modal 响应式样式。

## 6. 测试

| 文件 | 覆盖范围 |
|---|---|
| `test_api.py` | 聊天、DS、暂停恢复、上传、自检和错误响应 |
| `test_model_settings.py` | DPAPI、配置优先级、供应商 Key 和运行时切换 |
| `test_provider_json.py` | JSON 提取、修复和模型输出错误 |
| `test_phase2_platform.py` | Workspace 越界、Patch 审批、RTL 候选、Tool 授权、日志解析和 SQLite 恢复 |
| `test_run_orchestration.py` | Conversation 主入口、统一 Run、DS 关联、RTL 审批、Lint 状态回写和缺失 Adapter |

`tests/conftest.py` 强制 `CHIP_AGENT_MODEL_ENABLED=false`，测试不会消耗真实模型额度。测试数量以 `pytest -q` 的最新结果为准。

## 7. 扩展方式

### 增加 EDA Tool

1. 在 `app/tools` 实现参数验证和执行器；
2. 用参数数组运行，不拼接 Shell 字符串；
3. 注册角色、Mutability、批准策略、超时；
4. 在 `parse_tool_issues` 增加该工具解析器；
5. 保存原始日志并增加成功、失败、缺环境和超时测试；
6. 如需 UI，在 `EngineeringModal` 增加工具参数表单。

### 增加 Agent 能力

1. 先判断它属于现有生产角色、Reviewer 还是新 Tool；
2. 在角色目录和 Runtime 增加契约；
3. 必要时更新编排而非把判断塞入 API；
4. 对任何写操作沿用 Patch Review；
5. 增加事件、持久化和权限测试。

### 增加模型供应商

协议差异集中在 `app/models/provider.py`，配置合并放在 `model_settings.py`，前端只增加预设和公开字段，不保存明文 Key。
