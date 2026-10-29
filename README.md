# ChipFlow 芯片设计 Multi-Agent 工作台

完整技术文档入口：[docs/README.md](docs/README.md)，包含总体架构、代码结构、运行逻辑及组件与接口说明。

当前代码已升级到 v0.5，产品基线见 [PRD_v0.2.md](PRD_v0.2.md)。主交互采用“工作角色决定主 Agent + Delegate 专家协作 + 两阶段动态 Tool 选择”：

- FastAPI 服务；
- LangGraph DS 工作流；
- 架构、设计、验证三角色分析；
- 两轮规格冲突讨论；
- 等待用户输入与恢复；
- 四类独立评审；
- DS Markdown 生成；
- Agent 禁用和质量降级提示；
- 可切换的模拟运行时与真实模型运行时；
- OpenAI Responses API 和 OpenAI 兼容 Chat Completions；
- 网页模型配置、连接测试和 API Key 持久化；
- 网页系统自检、结构化错误分类和请求编号；
- React 正式前端工作台；
- 任务意图完善、原生 Function Calling 和 Planner–Executor Loop；
- 普通聊天作为 `reply_to_user` Tool，不再是独立路由分支；
- Design/Verification 可作为主 Agent，Architecture 作为只读专家；
- 跨专业协作持久化为有次数和超时边界的 DelegationTask；
- Tool 先按主 Agent 权限过滤，再由轻量 Selector 选择有限候选；
- PDF、DOCX、Markdown 和文本文件上传；
- DS、评审意见和执行过程展示。
- Architecture、Design、Verification 和独立 Reviewer 角色定义；
- SQLite 任务快照，等待架构结论的任务可在后端重启后恢复；
- 隔离 Workspace 与安全相对路径解析，阻止路径穿越；
- Artifact 版本、Hash 与来源记录；
- Design Agent 基于已交付 DS 生成 SystemVerilog 候选；
- Patch/Diff 审批，用户确认前不写入正式工作文件；
- 带角色权限、修改等级和审批策略的 Tool Registry；
- 异步 ToolRun、原始日志落盘、统一 Issue 和指纹去重；
- 一次一个 Active Issue，以及“按建议/跳过/自定义/解释”决策；
- Verilator Lint 真实适配器和已有验证用例发现 Tool；
- 工程工作台网页：RTL Diff Review、Lint 执行和逐条 Issue 处理；
- 自检覆盖 SQLite、Tool Registry 与 Verilator 环境。

当前版本已经具备真实模型调用能力；未配置或未启用模型时自动使用确定性模拟运行时。当前真实 EDA 适配器为 Verilator Lint；商业 Sim、CDC、综合、STA、生产问题单系统以及自动最小复验仍需要按目标工具和项目环境继续接入。系统不会把“工具未安装”伪装成执行成功。

## Windows 环境启动

在 PowerShell 中执行：

```powershell
.\scripts\setup.ps1
.\scripts\run-web.ps1
```

打开正式前端：

- http://127.0.0.1:5173

页面使用方式：

1. 在左侧填写项目和目标模块；
2. 可以直接提问或讨论芯片架构、RTL、验证等话题，无需先上传文档；
3. 选择“设计工程师”或“验证工程师”；对应主 Agent 会把普通问答编排为 `reply_to_user` Tool，并在需要时 Delegate 给其他专家；
4. 需要生成 DS 时，再上传 FS、AS、ICD（支持 PDF、DOCX、MD、TXT）并输入“请生成该模块 DS”；
5. 检查 Agent 推断的文档类型和版本，在右侧查看 DS、评审意见和执行过程；
6. 如果任务因规格歧义暂停，在对话区提交真实架构结论或问题单编号后继续。
7. DS 交付后打开右上角“工程工作台”，生成 RTL 候选并 Review Diff；批准后才会写入 Workspace。
8. 在工程工作台明确授权执行 Lint；结果按单条 Issue 展示根因、影响、建议、置信度与复验范围。

上传文档本身不会触发 Multi-Agent 工作流；只有明确要求生成、修改或正式评审 DS 时才进入当前的 DS 工作流。“DS 是什么”或针对文档内容的普通提问仍由聊天 Agent 回答。

仓库的 `samples` 目录提供了可直接上传的演示文档。

## 配置真实模型

1. 点击页面右上角“模型设置”；
2. 选择 OpenAI 或 DeepSeek；
3. 选择 Responses API 或 Chat Completions；
4. 填写模型名称、Base URL 和 API Key；
5. 打开“启用真实模型”；
6. 点击“保存并测试”。

选择 DeepSeek 时，页面自动设置：

```text
API 类型：Responses API
模型：deepseek-flash
Base URL：https://api.deepseek.com
```

也可以改为 `deepseek-v4-pro`，或根据 DeepSeek 后续发布的模型更新名称。

默认设置位于 `config/model.toml`，本项目当前默认使用 DeepSeek。网页覆盖设置位于 `data/model_settings.json`，API Key 使用 Windows DPAPI 绑定当前用户加密，该文件已被 `.gitignore` 排除。后端查询接口只返回 Key 掩码，不返回完整 Key。

也可以使用环境变量，且环境变量优先级高于网页保存的设置：

```powershell
$env:OPENAI_API_KEY = "..."
$env:OPENAI_BASE_URL = "https://api.openai.com/v1"
$env:CHIP_AGENT_MODEL = "gpt-5.5"
$env:CHIP_AGENT_MODEL_ENABLED = "true"
```

DeepSeek 环境变量示例：

```powershell
$env:DEEPSEEK_API_KEY = "..."
$env:DEEPSEEK_BASE_URL = "https://api.deepseek.com"
$env:CHIP_AGENT_MODEL_PROVIDER = "deepseek"
$env:CHIP_AGENT_MODEL = "deepseek-flash"
$env:CHIP_AGENT_MODEL_ENABLED = "true"
```

真实模型启用后，架构分析、设计分析、验证分析、规格质疑、DS 生成和所有评审 Agent 都会调用模型。一次完整 DS 流程会产生多次模型请求。

后端调试入口：

- API 文档：http://127.0.0.1:8000/docs
- 健康检查：http://127.0.0.1:8000/api/v1/health
- 系统自检：http://127.0.0.1:8000/api/v1/diagnostics

页面右上角提供“系统自检”。它检查后端、默认配置、本地设置、DPAPI 密钥可读性、模型运行状态、数据目录、SQLite、Tool Registry 和 Verilator 环境，不会连接外部模型或产生 API 费用。发生错误时，对话区错误条也可以直接打开自检面板。

## 运行测试

```powershell
.\scripts\test.ps1
```

## 正常 DS 任务示例

```powershell
$body = @{
  project_id = "demo-project"
  module_name = "dma_ctrl"
  documents = @(
    @{ kind = "FS"; name = "DMA FS"; version = "1.0"; content = "支持 DMA 命令处理。" },
    @{ kind = "AS"; name = "DMA AS"; version = "1.0"; content = "SUBMODULE: command_queue`nSUBMODULE: scheduler" },
    @{ kind = "ICD"; name = "DMA ICD"; version = "1.0"; content = "ready/valid 接口。" }
  )
} | ConvertTo-Json -Depth 8

Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/api/v1/tasks/ds `
  -ContentType "application/json" `
  -Body $body
```

## 冲突和恢复示例

在任一输入文档中加入 `[CONFLICT]`，创建任务后状态会变为 `waiting_for_user`。随后执行：

```powershell
$resolution = @{
  action = "provide_resolution"
  content = "真实架构师确认以 ICD 定义为准。"
  evidence_reference = "ISSUE-123"
} | ConvertTo-Json

Invoke-RestMethod `
  -Method Post `
  -Uri http://127.0.0.1:8000/api/v1/tasks/<task-id>/resume `
  -ContentType "application/json" `
  -Body $resolution
```

