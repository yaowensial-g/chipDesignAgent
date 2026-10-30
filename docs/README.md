# ChipFlow 技术文档

这套文档描述 ChipFlow 的产品边界、总体架构、运行逻辑、代码组织和验证证据。阅读时不需要了解项目的开发历史；每份文档都以代码库中生效的设计为准。

## 推荐阅读顺序

| 顺序 | 文档 | 重点 |
|---:|---|---|
| 1 | [产品需求](../PRODUCT_REQUIREMENTS.md) | 产品目标、角色、能力、约束和验收条件 |
| 2 | [总体架构](architecture-overview.md) | 系统上下文、控制面、执行面、数据流和信任边界 |
| 3 | [角色驱动 Multi-Agent](multi-agent-architecture.md) | 主 Agent、Delegate、Reviewer 与权限模型 |
| 4 | [动态编排](dynamic-orchestration.md) | TaskIntent、Tool Selector、Function Calling 和 Planner-Executor Loop |
| 5 | [上下文与记忆](context-and-memory.md) | 后端会话、四层上下文、混合检索、长期记忆和失效传播 |
| 6 | [运行逻辑](runtime-flow.md) | 从 HTTP 请求到 Run、Tool、Issue、Patch 和响应的完整时序 |
| 7 | [组件与接口](components-and-interfaces.md) | 服务职责、HTTP API、领域对象和 SQLite Schema |
| 8 | [代码结构](codebase-guide.md) | 目录、关键文件、扩展点和测试组织 |
| 9 | [编排与 Skills](orchestration-and-skills.md) | Skill、Tool、Policy 和 Workflow 的边界 |
| 10 | [系统验证报告](../VALIDATION_REPORT.md) | 自动化验证范围、结果与部署约束 |
| 11 | [面试 100 问](interview-100-questions.md) | 项目讲解、技术取舍和追问回答 |

## 系统能力地图

ChipFlow 以 Web 对话为统一入口。用户选择设计或验证工作角色后，角色主 Agent 根据请求和上下文决定下一步动作；普通问答、规格检索、DS 候选、RTL 候选、专家咨询和工程阶段调用均通过 Tool 进入同一条可观测运行链。

系统包含以下核心能力：

- 后端持久化会话和历史消息，前端按会话恢复；
- FS、AS、ICD 的版本化保存、分块、关键词与语义混合检索；
- 带来源的长期事实与决策，以及文档换版后的失效标记；
- Design、Verification 主 Agent 与 Architecture、Reviewer 专家 Delegate；
- 先权限过滤、再轻量筛选、最后原生 Function Calling 的两阶段 Tool 选择；
- DS 和 RTL 候选生成，正式文件修改采用 Patch、Diff 与用户审批；
- 统一 Run、事件、错误码、请求编号和 Context Manifest；
- 编译、仿真、Lint、CDC、综合和 STA 的环境无关执行接口。

## 部署配置

默认配置面向 Windows 单机可信环境。模型可使用 OpenAI 兼容接口，DeepSeek 通过配置 Base URL、模型名和 API Key 接入；密钥由 Windows DPAPI 绑定本机用户保存。

EDA 执行提供两类配置：

- 本地仿真配置：`run_mock_eda` 返回结构化执行结果，不启动外部工具，也不产生 Sign-off 结论，用于验证编排、状态和交互闭环；
- 工程适配配置：按照 Tool Registry 和 Adapter 契约接入项目实际使用的 Sim、Lint、CDC、综合和 STA 工具，并由部署方配置命令、许可证、Workspace 与审批策略。

两类配置使用相同的 Run 和权限协议，因此切换执行后端不会改变上层 Agent 的角色边界和用户审批方式。

## 事实口径

文档中的“支持”表示代码路径、数据契约和测试证据同时存在；“接口”表示系统已经定义稳定的接入契约，但具体厂商工具由部署环境提供。FS、AS、ICD、问题单和工具原始日志属于证据，Agent 结论属于推断；推断不能覆盖受控证据。
