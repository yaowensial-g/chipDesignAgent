# 组件与接口说明

> Tool Catalog 同时包含编排 Tool 和工程 Tool，并提供适用条件、JSON Schema、前置条件、副作用与执行类型。完整清单见 [动态编排架构](dynamic-orchestration.md)。

## 1. 组件

| 组件 | 输入 | 输出 | 持久化 |
|---|---|---|---|
| React Workspace | 用户输入、上传文件 | 对话、DS、Diff、Run、Issue | 浏览器内存 |
| API Router | HTTP | JSON/结构化错误 | 无 |
| ConversationService | 消息、历史、文档和工程上下文 | 普通回答或统一 Run | 间接 |
| SessionService | session、项目、用户工作角色 | Design/Verification 主 Agent | SQLite |
| OrchestratorService | 消息、主 Agent、候选 Tool | TaskIntent、Tool Call、Observation、Run | SQLite（经 RunService） |
| ToolSelectorService | 已授权 Tool Metadata、Intent | 有限 Tool Shortlist 和选择轨迹 | Run result |
| DelegationService | 专家任务、裁剪上下文 | 结构化专家结果 | SQLite |
| RunService | Intent、Tool 执行结果、用户指令 | Run、事件、专业执行记录关联 | SQLite |
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
| `orchestrator` | 历史兼容定义；控制面本身不是业务主 Agent | 不作为默认主 Agent |
| `chat` | 普通技术问答 | `reply_to_user` Tool |
| `architecture_advisor` | 解释和质疑规格，不修改 FS/AS/ICD | DS Workflow |
| `design` | 设计用户的主 Agent；DS/RTL、设计 Tool、结果整合 | Planner、DS Workflow、RTLService |
| `verification` | 验证用户的主 Agent；验证分析、已有用例、结果整合 | Planner、Tool 权限 |
| `spec_reviewer` | 规格一致性 | DS Review |
| `microarchitecture_reviewer` | 微架构完整性 | DS Review |
| `verification_reviewer` | 可验证性 | DS Review |
| `interface_reviewer` | 接口一致性 | DS Review |
| `specialist_reviewer` | CDC/低功耗/安全/DFT/PPA/时序专项 | 由任务标签和用户提示按需选择 |

主 Agent 当前共享一个有效模型 Provider；Selector 可单独配置模型名称。角色隔离依靠会话身份、Tool 白名单、Delegate 契约和执行前权限复核，不是独立模型进程。

## 3. Tool Catalog

| Tool | 允许角色 | Mutability | 需批准 | 行为 |
|---|---|---|---|---|
| `reply_to_user` | Design、Verification | analysis | 否 | 普通问答、解释和总结；始终进入候选集 |
| `ask_user` | Design、Verification | analysis | 否 | 缺少关键输入时暂停 Run；始终进入候选集 |
| `consult_architecture_agent` | Design、Verification | analysis | 否 | 创建只读 Architecture DelegationTask |
| `consult_design_agent` | Verification | analysis | 否 | 创建 Design DelegationTask |
| `consult_verification_agent` | Design | analysis | 否 | 创建 Verification DelegationTask |
| `create_ds_candidate` | Design | candidate_write | 否 | 启动 DS 候选与评审子流程 |
| `create_rtl_candidate` | Design | candidate_write | 否 | 生成 RTL Candidate 与 Patch |
| `report_capability_gap` | Design、Verification | analysis | 否 | 明确报告 Adapter 未接入并暂停 |
| `discover_testcases` | Design、Verification | read | 否 | 扫描 Workspace 的已有验证文件 |
| `run_mock_eda` | Design、Verification | analysis | 否 | 占位返回 Compile/Sim/Lint/CDC/综合/STA 字符串，不启动进程 |
| `verilator_lint` | Design、Verification | execution | 是 | 保留的真实 Adapter，当前 `enabled=false` |

注册契约还包括 `when_to_use`、`when_not_to_use`、输入/输出 JSON Schema、前置条件、副作用、执行类型、幂等性和超时。新增 Sim/CDC/综合/STA 应沿用同一接口，不应把工具决策硬编码在 Agent 提示词中。

## 4. HTTP API

前缀 `/api/v1`。普通网页以 Conversation 和 Run 接口为主；专用 Task、RTL、Patch、ToolRun 接口作为兼容、测试和系统集成入口保留。

### 系统、模型和文档

| 方法 | 路径 | 说明 |
|---|---|---|
| GET | `/health` | 服务状态与运行信息 |
| GET | `/diagnostics` | 本机无费用自检 |
| GET/PUT | `/settings/model` | 读取/保存公开模型设置 |
| POST | `/settings/model/test` | 真实连接探针 |
| POST | `/documents/extract` | PDF/DOCX/MD/TXT 抽取 |

### 对话、统一 Run 和 DS 兼容接口

| 方法 | 路径 | 说明 |
|---|---|---|
| POST | `/chat` | 无 session 时的兼容聊天入口；可创建统一 Run |
| POST | `/conversations/{session_id}/messages` | 网页主入口，session 由路径绑定 |
| GET | `/conversations/{session_id}` | 查询工作角色和主 Agent |
| PUT | `/conversations/{session_id}/role` | 切换会话工作角色 |
| GET | `/runs/{run_id}` | 查询统一 Run，并刷新关联 ToolRun 终态 |
| GET | `/runs/{run_id}/events` | 查询按序持久化的 RunEvent |
| GET | `/runs/{run_id}/delegations` | 查询结构化专家委派结果 |
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
| GET | `/tools?role=design` | 全量或按主 Agent 权限过滤的 Tool 元数据 |
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

`approve` 记录用户对建议的决策；文件修改必须另行生成候选 Patch 并经过 Diff 审批，不能由 Issue 决策直接写入。

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

统一控制面表：

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

## 9. 非功能特性

| 维度 | 当前状态 |
|---|---|
| 可恢复 | Task/结果持久化；运行中 Tool 和 LangGraph Checkpoint 不完整恢复 |
| 安全 | DPAPI、Workspace 隔离、无 Shell 拼接、Patch 审批；无用户 ACL |
| 可观测 | 请求编号、事件、Tool 日志、自检；无指标和 Trace |
| 扩展 | Registry/Adapter 提供商业 EDA、问题单和 Git 的部署接入边界 |
| 测试 | 51 项后端测试 + TypeScript/Vite 构建 |
| 审计 | 来源、Diff、日志、用户决策已保存；缺少统一 Actor/时间线 |
