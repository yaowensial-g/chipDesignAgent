# 编排、Workflow、Skill 与 Tool 设计

> 用户工作角色决定 Design 或 Verification 主 Agent，专家协作使用 Delegate，Tool 采用权限过滤后的两阶段选择。实现位置见 [角色驱动 Multi-Agent 架构](multi-agent-architecture.md)。

## 1. 核心分工

| 概念 | 解决的问题 | 当前实现 |
|---|---|---|
| Session + Primary Agent | 谁负责本轮判断和最终答复 | `SessionService`、Design/Verification |
| Control Plane + Intent | 用户想做什么、还缺什么信息 | `OrchestratorService`、`TaskIntent` |
| Tool Selector | 哪些已授权 Tool 值得给主 Agent | `ToolSelectorService` |
| Delegate | 如何获得跨专业判断而不切换主 Agent | `DelegationService`、`DelegationTask` |
| Run | 一次用户任务现在处于什么状态 | `RunService`、`runs`、`run_events` |
| Workflow | 多步骤任务怎样推进和中断 | DS 使用 LangGraph；工程任务由 RunService 协调 ToolRun |
| Role Agent | 由哪个生产角色判断 | Architecture、Design、Verification、Reviewer |
| Skill | 该角色应该按什么专业方法判断 | `skills/**/*.md` + `SkillCatalog` |
| Tool/Function | 实际执行哪个确定性动作 | Tool Registry、Workspace/Patch/ToolRun Service |

结论：DS 不是一个巨大的 Function Call。它是 Workflow；Design Agent 加载 DS Skill，并通过 Function/Service 检索资料、保存 Candidate 和创建问题。Lint 则以 Tool 执行为主，Agent/Skill 负责解释日志和建议。

## 2. 主交互边界

正式网页通过聊天表达意图：

```text
POST /conversations/{session_id}/messages
→ SessionService 解析 Design / Verification 主 Agent
→ 控制平面形成 TaskIntent 并创建 Run
→ Registry 先按主 Agent 权限过滤
→ Selector Model 读取 Metadata 并形成候选集
→ 主 Agent通过原生 Function Calling 选择 Tool
→ Registry / Policy 校验权限、参数和审批
→ Delegate 或 Tool Handler 执行并返回 Observation
→ 重新规划、等待用户或完成
```

`/tasks/ds`、`/rtl/candidates`、`/tool-runs` 继续保留，用于自动化测试、调试和其他系统集成；它们不是普通用户需要理解的主要入口。

## 3. 为什么模型不能直接执行

Selector 只返回候选名称，主 Agent 只返回 ToolSelection，都不直接执行动作。真正执行前，由确定性代码检查：

- 用户是否明确要求动作；
- 是否有 AS、已确认 DS、Workspace 等必需输入；
- Tool 是否注册；
- 角色是否有权限；
- 是否需要用户审批；
- 候选修改是否必须经过 Diff Review。

模型无法直接写 SQLite、Workspace 或启动任意 Shell。

## 4. Skill Catalog

项目 Skill 位于根目录 `skills/`：

```text
skills/
├─ architecture/specification-analysis.md
├─ design/ds-authoring.md
├─ design/rtl-implementation.md
└─ review/independent-review.md
```

`app/skills/registry.py` 只允许安全名称并限制读取在 Skill 根目录内。真实模型 Runtime 在对应角色 Prompt 后附加 Skill。Skill 只增加知识和约束，不授予文件、进程或数据库权限。

Skill 使用受控 Markdown 保存，并可通过 metadata 声明适用模块类型、输入要求、输出 Schema 和评估集；API Key、工程路径和执行命令不得写入 Skill。

## 5. Run 与专业记录

统一 Run 保存控制面状态：

```text
planned
→ running
→ waiting_for_user / waiting_for_approval
→ completed / failed / cancelled
```

专业记录继续独立存在：

- DS Task 保存 LangGraph 状态、Issue、Finding 和 DS；
- Patch 保存 before/after/Diff 和审批结果；
- ToolRun 保存命令、stdout/stderr、退出码和工程 Issue。

这种设计避免用一个万能 JSON 取代各领域对象，同时允许前端用一套 Run UI 展示不同任务。

## 6. 能力矩阵

| RunType | 聊天触发 | 执行能力 |
|---|---|---|
| `ds_design` | 是 | 完整 DS Multi-Agent Workflow |
| `rtl_implementation` | 是 | RTL Candidate + Patch 审批 |
| `compilation` | 是 | 本地仿真执行；工程环境通过 Adapter 接入 |
| `lint` | 是 | 本地仿真执行；工程环境通过 Adapter 接入 |
| `test_execution` | 是 | 发现已有用例，不替验证团队生成计划 |
| `simulation` | 是 | 本地仿真执行；工程环境通过 Adapter 接入 |
| `cdc` | 是 | 本地仿真执行；工程环境通过 Adapter 接入 |
| `synthesis` | 是 | 本地仿真执行；工程环境通过 Adapter 接入 |
| `sta` | 是 | 本地仿真执行；工程环境通过 Adapter 接入 |

## 7. 扩展契约

系统通过稳定契约扩展能力，而不是修改主编排入口：

1. 多目标任务可使用 RunEvent 或 DAG 投影展示依赖关系；
2. 仿真、CDC、综合和 STA 通过 Tool Adapter 接入命令、许可证和结果解析；
3. 工程日志分析方法以 Tool-specific Skill 提供；
4. Run 事件可由轮询替换为 SSE，持久化 Run ID 保持不变；
5. Skill 通过元数据、测试样例和效果评估管理；
6. Issue、Candidate Patch、用户审批和最小复验使用同一可恢复状态协议；
7. Selector 与 Delegate 的 token、成本、限流和熔断由 Provider 观测接口提供。
