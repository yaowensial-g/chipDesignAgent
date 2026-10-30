# 上下文与记忆子系统

## 1. 为什么不能把“上下文”等同于聊天记录

一次模型调用真正需要的上下文由四类信息组成：

1. 当前输入：用户本轮消息、当前角色、项目、模块和 Run 状态；
2. 短期记忆：本会话近期原文、较早对话摘要、当前 Run 的 Observation；
3. 长期记忆：有来源的项目事实、架构决策、已确认约束和用户偏好；
4. 外部证据：FS、AS、ICD 的相关分块，以及 Workspace、日志和工具结果。

原始资料、聊天消息、工具日志和长期记忆分别存储。它们的可信度、生命周期和更新
规则不同，不能混在一个“memory”表里，也不能把 Agent 的一句推测自动提升为事实。

## 2. 四层上下文

| 层 | 典型内容 | 生命周期 | 当前存储 |
|---|---|---|---|
| L0 当前请求 | 当前消息、角色、TaskIntent、Run 状态 | 单次模型调用 | Request + Run |
| L1 短期记忆 | 近期消息、较早消息摘要、本 Run Observation | Session / Run | `conversation_messages`、`conversation_summaries`、Run result |
| L2 长期记忆 | 项目事实、正式决策、已确认假设 | Project / Module / 文档版本 | `memories`、`memory_sources` |
| L3 原始证据 | FS/AS/ICD、工具日志、Workspace 制品 | 长期保留、版本化 | `document_versions`、`document_chunks`、Artifact、Raw Logs |

短期和长期的区别不是“文本长短”，而是作用域、权威性和生命周期。一次临时讨论即使
很长仍是短期记忆；一条只有十个字但经问题单确认的接口决策属于长期记忆。

## 3. 后端会话持久化

浏览器不再是聊天历史的事实源。每次用户消息先写入 `conversation_messages`，模型响应
也在返回前写入同一会话；Run 的审批和真实架构结论同样作为消息保存。

前端启动时调用：

- `GET /api/v1/conversations` 获取会话列表；
- `GET /api/v1/conversations/{session_id}/messages` 分页恢复正文；
- `GET /api/v1/runs/{active_run_id}` 恢复当前任务状态。

消息带有会话内 `sequence`、`agent_role` 和可选 `run_id`。角色切换不会删除历史，但
ContextBuilder 只选择当前主 Agent 可见的角色私有消息。

## 4. 文档版本与混合 RAG

文档进入知识库后按以下方式处理：

```text
上传/聊天携带文档
→ 计算内容 Hash 并幂等入库
→ 保留 document + document_version
→ 按标题和段落分块
→ 提取关键词与本地语义特征
→ 用户问题触发关键词 + 语义混合检索
→ 仅把高排名分块交给 ContextBuilder
```

关键词评分适合 `ready`、寄存器名和信号名等精确匹配；语义特征处理
`backpressure/背压`、`handshake/握手` 等表达差异。当前语义特征是无需额外模型的
确定性本地实现，接口可替换为企业内部 Embedding 服务。它不声称等价于通用向量模型。

同一逻辑文档的新内容会创建新版本。旧版本和旧分块标记为 `superseded`，不会物理
删除，默认检索只使用 active 版本。

## 5. 长期记忆形成与演化

长期记忆状态：

```text
proposed ──用户/文档/问题单确认──> active
active ──来源版本被替代─────────> stale
```

- Agent 推断默认只能形成 `proposed inference`，不能自动成为 active fact；
- active 的 `fact` 和 `decision` 至少需要一个 `source_ref`；
- 来源可指向文档版本、问题单、Run 或用户确认消息；
- 文档换版时，引用旧 `version_id` 的记忆标记为 stale；
- stale 记忆保留审计，但不会默认进入模型上下文；
- `POST /memories/{id}/promote` 用于有权威证据后的显式提升。

系统对记忆执行存储、来源约束、检索、提升与文档换版失效。Reflection 合并、矛盾
聚类和遗忘属于受控治理动作，未经用户确认不得自动改变项目事实。

## 6. ContextBuilder 与 Token 预算

`ContextBuilderService` 在每次 Planner 调用前统一选择内容：

1. 近期消息优先，较早消息使用可追踪的抽取摘要；
2. 只检索当前 project/module 的 active 长期记忆；
3. 按当前问题对 active 文档分块做混合检索；
4. 分别给历史、记忆和文档证据设置 Token 预算；
5. 生成新的 `ChatRequest` 快照供 Intent、Selector、主 Agent 和 Delegate 共用。

Token 计数优先使用可选的 `tiktoken`，没有安装时使用偏保守的中英文估算。Runtime 仍有
最后一道 Token 安全阀，但不再按字符数和上传顺序裁剪整篇文档。

## 7. Context Manifest

每次 Unified Run 都保存一条 `context_manifests`：

- 使用的消息 ID；
- 使用的长期记忆 ID；
- 使用的文档 chunk ID；
- 各区段估算 Token；
- 主 Agent、总预算和创建时间。

`GET /api/v1/runs/{run_id}/contexts` 可以回答：模型当时看到过哪版规格、为什么某段没有
进入上下文、是否引用了已经失效的记忆。这是排查 RAG 和模型输出问题的审计入口。

## 8. 部署与治理边界

- 文档语义特征使用本地轻量索引，适用于单机项目知识库；企业部署可通过检索适配器连接向量数据库；
- 对话摘要使用确定性抽取，不调用模型补写事实，避免把推测写入历史；
- Run Observation 随编排结果持久化，由 Run 标识完成追踪；
- 记忆合并、反思和遗忘必须经过明确的治理策略与审计；
- 默认部署面向可信单机用户；多用户环境必须在 API 前增加身份认证与项目 ACL。
