# ChipFlow Agent 技术文档

本文档集描述当前代码库的实际实现，技术基线为 **v0.4 动态 Tool Calling（2026-10-28）**，产品需求以根目录的 [PRD_v0.2.md](../PRD_v0.2.md) 为准。

## 文档导航

| 文档 | 关注内容 | 推荐读者 |
|---|---|---|
| [dynamic-orchestration-v0.4.md](dynamic-orchestration-v0.4.md) | Intent Enricher、Planner–Executor Loop、Chat Tool 和动态 Tool Calling | 所有人，建议首先阅读 |
| [architecture-overview-v0.4.md](architecture-overview-v0.4.md) | v0.4 系统上下文、动态编排、控制面、数据与安全边界 | 架构师、技术负责人 |
| [codebase-guide.md](codebase-guide.md) | 目录结构、文件职责、扩展入口 | 开发和维护人员 |
| [runtime-flow.md](runtime-flow.md) | 对话、DS、RTL、ToolRun、Issue 与恢复流程 | 开发、测试、问题定位人员 |
| [components-and-interfaces.md](components-and-interfaces.md) | 组件、角色、HTTP API、领域对象和配置 | 前后端和集成人员 |
| [orchestration-and-skills.md](orchestration-and-skills.md) | Orchestrator、Run、Workflow、Skill 与 Tool 的边界 | 架构设计、面试答辩 |
| [interview-100-questions.md](interview-100-questions.md) | 基于当前代码的 100 个面试追问与参考答案 | 求职准备、项目答辩 |
| [../SELF_VALIDATION_v0.4.md](../SELF_VALIDATION_v0.4.md) | v0.4 动态编排、Function Calling 和兼容流程验收结果 | 测试和交付人员 |

## 当前版本一句话说明

ChipFlow 是 Windows 优先的角色型芯片设计 Multi-Agent 工作台：所有用户输入进入统一 Orchestrator，模型先完善任务意图，再通过原生 Function Calling 动态选择 Tool；普通聊天也是只有 `reply_to_user` 的 Run，工程副作用仍由确定性权限、审批和 Workspace 规则约束。

## 已实现能力

- React 正式网页、普通聊天、文件上传和 DS Multi-Agent 流程；
- 统一 Run 控制面、RunEvent、查询/恢复/取消接口和前端状态展示；
- 统一 Intent Enricher 与 Planner–Executor Tool Loop；
- DeepSeek Chat Completions 和 OpenAI Responses 原生 Function Calling；
- Chat、角色咨询、DS、RTL、Lint 和用例能力统一注册为 Tool；
- DS Task、RTL Patch、ToolRun 作为专业执行记录关联到统一 Run；
- Architecture、Design、Verification、Reviewer 和 Orchestrator 角色定义；
- DeepSeek/OpenAI 真实模型及不联网的 Deterministic Runtime；
- 规格冲突讨论、真实架构结论升级、Interrupt/Resume；
- SQLite 任务快照，等待用户的任务可在服务重启后继续；
- 隔离 Workspace、Artifact 版本/Hash、路径越界保护；
- 基于 DS 的 SystemVerilog RTL 候选；
- Patch/Diff 审批，批准前不修改目标文件；
- Tool Registry、角色权限、修改等级和显式用户授权；
- 异步 ToolRun、日志落盘、标准化 Issue、指纹去重；
- 单 Active Issue 交互和四种用户决策；
- 真实 Verilator Lint 适配器、已有验证用例发现 Tool；
- SQLite、Tool Registry、模型和 Verilator 环境自检。

## 当前边界

- DS、RTL、Lint 和已有用例发现已经可以由聊天编排；工程工作台作为调试与兼容入口保留；
- 仿真、CDC、综合和 STA 能创建统一 Run，但在没有真实 Tool Adapter 时会明确停在 `tool_adapter_required`，不会伪装为已执行；
- LangGraph 内部仍使用 `MemorySaver`，SQLite 保存应用快照；重启后的等待任务由 TaskService 恢复路径继续，不是持久化 LangGraph Checkpointer；
- 当前真实 EDA 适配器只有 Verilator Lint；商业仿真、CDC、综合和 STA 尚未接入；
- 尚未实现 ToolRun 进程取消、自动候选修复 Patch、最小复验和问题单/Git 集成；
- 长期记忆、Reflection 依赖图、文档变更传播和多用户权限仍未实现；
- 模型及确定性运行时输出均为候选工程材料，不能替代真实架构批准和正式 Sign-off。

## 推荐阅读顺序

1. [v0.4 动态编排](dynamic-orchestration-v0.4.md)；
2. [总体架构](architecture-overview-v0.4.md)；
3. [运行逻辑](runtime-flow.md)；
4. [组件与接口](components-and-interfaces.md)；
5. [编排与 Skill](orchestration-and-skills.md)；
6. [代码结构](codebase-guide.md)；
7. [自验证报告](../SELF_VALIDATION_v0.4.md)。
