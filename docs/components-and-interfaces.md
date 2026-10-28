# 组件与接口说明（v0.4 动态编排）

> Tool Catalog 现在同时包含编排 Tool 和工程 Tool，并提供适用/禁用条件、JSON Schema、前置条件、副作用与执行类型。完整清单见 [dynamic-orchestration-v0.4.md](dynamic-orchestration-v0.4.md)。

## 1. 组件

| 组件 | 输入 | 输出 | 持久化 |
|---|---|---|---|
| React Workspace | 用户输入、上传文件 | 对话、DS、Diff、Run、Issue | 浏览器内存 |
| API Router | HTTP | JSON/结构化错误 | 无 |
| ConversationService | 消息、历史、文档和工程上下文 | 普通回答或统一 Run | 间接 |
| RunService | 结构化路由、用户指令 | Run、事件、专业执行记录关联 | SQLite |
| TaskService | DS 请求/用户结论 | TaskView | SQLite 快照 + 热内存 |
| DS Workflow | DSWorkflowState | delivered/interrupt | MemorySaver |
| Agent Runtime | 角色请求 | 文本或结构化输出 | 无 |
| Skill Catalog | Skill 名称 | 版本化专业 Prompt 片段 | Markdown 文件 |
| WorkspaceService | 项目、相对路径 | 隔离目录/安全路径 | 文件系统 + SQLite |
| ArtifactService | 内容、类型、来源 | 版本、Hash | 文件系统 + SQLite |
| RTLService | DS、文档、目标路径 | RTL candidate + Patch | SQLite Patch |
| PatchService | before/after/决策 | Diff/应用状态 | SQLite + 目标文件 |
| Tool Registry | Tool 定义 | 元数据/执行器 | 进程注册 |
| ToolRunService | Role、Tool、参数、授权 | Run、日志、Issue | SQLite + 日志文件 |
| Model Settings | TOML、JSON、环境变量 | Effective Settings | JSON/环境变量 |
| Secret Protection | Key | DPAPI 密文 | JSON |

## 2. Agent 角色

| ID | 职责 | 当前使用位置 |
|---|---|---|
| `orchestrator` | 根据用户明确目标选择 Workflow/Tool，不决定研发阶段 | ConversationService + RunService |
| `conversation_router` | chat/DS/RTL/EDA/用例任务分类 | `/chat`、`/conversations/{id}/messages` |
| `chat` | 普通技术问答 | `/chat` |
| `architecture_advisor` | 解释和质疑规格，不修改 FS/AS/ICD | DS Workflow |
| `design` | DS/RTL 候选及设计侧工具判断 | DS Workflow、RTLService |
| `verification` | 可验证性分析、已有用例与验证侧工具 | DS Workflow、Tool 权限 |
| `spec_reviewer` | 规格一致性 | DS Review |
| `microarchitecture_reviewer` | 微架构完整性 | DS Review |
| `verification_reviewer` | 可验证性 | DS Review |
| `interface_reviewer` | 接口一致性 | DS Review |
| `specialist_reviewer` | CDC/低功耗/安全/DFT/PPA/时序专项 | 元数据已定义，按需编排尚未接入 |

所有真实角色当前共享一个有效模型配置；角色隔离依靠契约和提示词，不是独立模型进程。

## 3. Tool Catalog

| Tool | 允许角色 | Mutability | 需批准 | 行为 |
|---|---|---|---|---|
| `discover_testcases` | Design、Verification | read | 否 | 扫描 Workspace 的已有验证文件 |
| `verilator_lint` | Design、Verification | execution | 是 | 受控执行 Verilator `--lint-only -Wall` |

注册契约还包括幂等性和超时。新增 Sim/CDC/综合/STA 应沿用同一接口，不应把工具决策硬编码在 Agent 提示词中。

## 4. HTTP API

前缀 `/api/v1`。普通网页以 Conversation 和 Run 接口为主；专用 Task、RTL、Patch、ToolRun 接口作为兼容、测试和系统集成入口保留。

### 系统、模型和文档

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 状态和 v0.4.0 |
| GET | `/diagnostics` | 本机无费用自检 |
| GET/PUT | `/settings/model` | 读取/保存公开模型设置 |
| POST | `/settings/model/test` | 真实连接探针 |
| POST | `/documents/extract` | PDF/DOCX/MD/TXT 抽取 |

### 对话、统一 Run 和 DS 兼容接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/chat` | 无 session 时的兼容聊天入口；可创建统一 Run |
| POST | `/conversations/{session_id}/messages` | 网页主入口，session 由路径绑定 |
| GET | `/runs/{run_id}` | 查询统一 Run，并刷新关联 ToolRun 终态 |
| GET | `/runs/{run_id}/events` | 查询按序持久化的 RunEvent |
| POST | `/runs/{run_id}/resume` | 提交架构结论、Tool 授权或 Patch 决策 |
| POST | `/runs/{run_id}/cancel` | 取消非终态 Run |
| POST | `/tasks/ds` | 兼容/测试：直接创建 DS Task，跳过编排 |
| GET | `/tasks/{task_id}` | 内存或 SQLite 快照 |
| POST | `/tasks/{task_id}/resume` | 提交真实架构结论/假设/取消 |

### Workspace 与 Artifact

| 方法 | 路径 | 说明 |
|---|---|---|
| GET/POST | `/workspaces` | 按项目查询或创建隔离工作区 |
| GET | `/workspaces/{workspace_id}` | Workspace 信息 |
| GET/POST | `/workspaces/{workspace_id}/artifacts` | 查询或创建带版本制品 |

### RTL 与 Patch

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/rtl/candidates` | Design Runtime 生成 RTL candidate 和 Patch |
| POST | `/patches` | 对任意 Workspace 文件创建候选 Diff |
| GET | `/patches/{patch_id}` | 查询 Patch |
| POST | `/patches/{patch_id}/decision` | approve/reject/custom |

### ToolRun 与 Issue

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/tools` | Tool 元数据 |
| POST | `/tool-runs` | 授权检查后异步创建 Run |
| GET | `/tool-runs/{run_id}` | Run 状态、命令和输出 |
| GET | `/tool-runs/{run_id}/issues` | 全部标准化问题 |
| GET | `/tool-runs/{run_id}/issues/next` | 下一条 Active Issue |
| POST | `/issues/{issue_id}/decision` | approve/skip/custom/explain |

## 5. 关键请求

### RTL candidate

```json
{
  "project_id": "demo-project",
  "workspace_id": "optional-existing-id",
  "module_name": "dma_ctrl",
  "target_path": "rtl/dma_ctrl.sv",
  "ds_markdown": "# confirmed DS ...",
  "documents": [],
  "instruction": "根据已确认 DS 生成 RTL 候选"
}
```

响应同时包含 Workspace、候选内容和 `patch_ready` Patch。候选内容未写入目标文件。

### ToolRun

```json
{
  "workspace_id": "...",
  "tool_name": "verilator_lint",
  "role": "design",
  "arguments": {
    "files": ["rtl/dma_ctrl.sv"],
    "top_module": "dma_ctrl"
  },
  "user_approved": true
}
```

返回 202 和 Run ID。前端轮询 Run，再请求下一条 Issue。

### Issue decision

```json
{
  "action": "approve",
  "comment": "同意该判断",
  "patch_id": null
}
```

当前 `approve` 只记录决策，不会自动修改代码；自动修复闭环尚未实现。

## 6. 领域对象

### Workspace / Artifact

- Workspace：ID、项目、显示名、绝对根目录、创建时间；
- Artifact：类型、相对路径、递增版本、SHA-256、状态、derived_from。

### Patch

- 目标相对路径；
- 原因和来源；
- before/after 内容在数据库中保存；
- API 返回 unified diff、状态和应用时间；
- 状态：`patch_ready`、`custom_requested`、`rejected`、`applied`。

### ToolRun

- Tool、Workspace、参数、实际命令数组；
- 状态、退出码、stdout/stderr、时间；
- 状态枚举包含 queued/preparing/running/collecting/parsing/completed/failed/cancelled/timed_out；当前实现使用其中 queued/running/parsing/终态。

### EngineeringIssue

- 日志定位：stage/tool/severity/code/message/file/line/raw_log_ref；
- 去重：fingerprint；
- 分析：root_cause、evidence、impact、suggestion、confidence、verification_scope；
- 状态和用户决策。

### TaskView

包含项目、模块、状态、规格 Issue、评审 Finding、质量警告、事件、待用户请求和 DS Markdown。

### RunView

统一 Run 是控制面对象，不替代专业执行记录。主要字段包括：

- `run_type`：`ds_design`、`rtl_implementation`、`simulation`、`lint`、`cdc`、`synthesis`、`sta`、`test_execution`；
- `status`：`planned/running/waiting_for_user/waiting_for_approval/completed/failed/cancelled`；
- `legacy_task_id/tool_run_id/patch_id`：关联 DS Task、工程 ToolRun 和候选 Patch；
- `pending_user_request`：缺少 AS/DS/Workspace、审批或 Tool Adapter 等阻塞原因；
- `result`：当前专业执行结果快照；
- `events`：可审计的编排事件。

注意：统一 Run 与 `tool_runs` 不是同一个对象。前者记录用户任务全生命周期，后者只记录一次外部 Tool 执行。

## 7. SQLite Schema

v0.3 新增：

| 表 | 作用 |
|---|---|
| `runs` | 统一保存用户任务、路由原因、关联执行记录、等待项和结果快照 |
| `run_events` | 保存 Run 的有序事件，便于进度展示和审计 |

原有 `task_snapshots`、`tool_runs`、`patches`、`engineering_issues` 等表继续承担专业执行数据，不被统一 Run 重复实现。

| 表 | 作用 |
|---|---|
| `workspaces` | 项目隔离目录 |
| `artifacts` | 制品版本和 Hash |
| `patches` | 候选修改和审批状态 |
| `tool_runs` | 工具运行状态与输出 |
| `engineering_issues` | 问题队列、分析和用户决策 |
| `task_snapshots` | DS 应用快照 |

## 8. 配置

优先级：环境变量 > `data/model_settings.json` > `config/model.toml`。

环境变量：

```text
CHIP_AGENT_MODEL_ENABLED
CHIP_AGENT_MODEL_PROVIDER
CHIP_AGENT_API_STYLE
CHIP_AGENT_MODEL
CHIP_AGENT_MODEL_TIMEOUT
CHIP_AGENT_MAX_OUTPUT_TOKENS
DEEPSEEK_API_KEY / DEEPSEEK_BASE_URL
OPENAI_API_KEY / OPENAI_BASE_URL
```

## 9. 非功能现状

| 维度 | 当前状态 |
|---|---|
| 可恢复 | Task/结果持久化；运行中 Tool 和 LangGraph Checkpoint 不完整恢复 |
| 安全 | DPAPI、Workspace 隔离、无 Shell 拼接、Patch 审批；无用户 ACL |
| 可观测 | 请求编号、事件、Tool 日志、自检；无指标和 Trace |
| 扩展 | Registry/Adapter 结构已具备；商业 EDA 尚未接入 |
| 测试 | 40 项后端测试 + TypeScript/Vite 构建 |
| 审计 | 来源、Diff、日志、用户决策已保存；缺少统一 Actor/时间线 |
