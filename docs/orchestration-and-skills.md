# 编排、Workflow、Skill 与 Tool 设计

## 1. 核心分工

| 概念 | 解决的问题 | 当前实现 |
|---|---|---|
| Orchestrator/Router | 用户明确要求做什么 | `runtime.route_conversation()` |
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
→ Router 输出结构化 route
→ RunService 进行确定性校验和落库
→ DS Workflow / RTLService / ToolRunService
```

`/tasks/ds`、`/rtl/candidates`、`/tool-runs` 继续保留，用于自动化测试、调试和其他系统集成；它们不是普通用户需要理解的主要入口。

## 3. 为什么模型不能直接执行

路由模型只返回任务类别和理由。真正执行前，由确定性代码检查：

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

当前 Skill 是版本化 Markdown。后续可增加 metadata，例如适用模块类型、版本、输入要求、输出 Schema 和评估集，但不应把 API Key、工程路径或执行命令写入 Skill。

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

## 6. 当前支持矩阵

| RunType | 聊天触发 | 当前执行能力 |
|---|---|---|
| `ds_design` | 是 | 完整 DS Multi-Agent Workflow |
| `rtl_implementation` | 是 | RTL Candidate + Patch 审批 |
| `lint` | 是 | Verilator Lint ToolRun |
| `test_execution` | 是 | 当前先发现已有用例，不生成验证计划 |
| `simulation` | 是 | 未注册 Adapter，诚实阻塞 |
| `cdc` | 是 | 未注册 Adapter，诚实阻塞 |
| `synthesis` | 是 | 未注册 Adapter，诚实阻塞 |
| `sta` | 是 | 未注册 Adapter，诚实阻塞 |

## 7. 下一步

1. 将多目标请求拆为用户确认的 Run DAG；
2. 为仿真、CDC、综合和 STA 实现统一 Tool Adapter；
3. 把工程日志分析方法外置为 Tool-specific Skill；
4. 增加 Run 的 SSE 事件推送和最近会话恢复；
5. 为 Skill 增加版本、测试样例和效果评估；
6. 将 Issue → Candidate Patch → 用户审批 → 最小复验串成可恢复 Workflow。
