# 上下文构建与长期记忆实现详解

本文面向需要维护、调试或扩展 ChipFlow 上下文系统的开发者。它不只描述概念，还逐项对应当前代码中的 Service、数据表、状态转换、选择算法、失败路径和测试。

如果只想快速了解系统边界，可以先阅读[上下文与长期记忆子系统概览](context-and-memory.md)。本文以当前仓库代码为事实来源；后续修改算法时，应同步更新本文和专项测试。

## 1. 要解决的问题

一次芯片设计对话可能同时包含：当前问题、几十轮历史消息、规格文档、已确认工程约束、尚未确认的专家建议、Run 状态和 Tool 输出。如果把这些内容直接拼进 Prompt，会产生四类问题：

1. 历史持续增长，最终超过模型上下文窗口；
2. Design 与 Verification 的私有讨论可能互相泄漏；
3. 模型建议、用户确认和正式规格容易被误当成同等权威的事实；
4. 文档换版后，旧结论可能继续被检索并影响新设计。

当前实现用四层数据模型处理这些问题：

| 层级 | 内容 | 主要存储 | 是否直接视为正式事实 |
|---|---|---|---|
| L0 当前执行状态 | 当前请求、Run、Observation、审批状态 | `runs`、`run_events` | 否，属于执行现场 |
| L1 短期上下文 | 最近 5 轮完整原文、较早 30 轮独立摘要 | `conversation_messages`、`conversation_turn_summaries` | 否，摘要是派生数据 |
| L2 长期记忆 | 稳定偏好、长期约定、设计理由、经验证的经验 | `memories`、`memory_sources`、`memory_relations` | 供后续任务参考，参数与工程状态由权威工程数据提供 |
| L3 原始证据 | FS/AS/ICD、RTL、Artifact、日志 | `document_versions`、`document_chunks`、Workspace | 是证据，但仍需考虑版本和状态 |

这里最重要的约束是：**持久化不等于可信，进入 Prompt 也不等于成为正式事实。**

## 2. 代码导航

| 职责 | 代码入口 | 关键方法 |
|---|---|---|
| 请求编排 | [`orchestrator_service.py`](../app/services/orchestrator_service.py) | `OrchestratorService._handle()` |
| 上下文选择和预算 | [`context_builder_service.py`](../app/services/context_builder_service.py) | `ContextBuilderService.build()`、`save_manifest()` |
| 会话消息、轮次与角色可见性 | [`session_service.py`](../app/services/session_service.py) | `add_message()`、`completed_turns()`、`get_turn()` |
| 独立轮次摘要 | [`conversation_summary_service.py`](../app/services/conversation_summary_service.py) | `enqueue_turn()`、`summarize_turn()`、`get_recent_turn_summaries()`、`retry_failed_summaries()` |
| 项目配置 | [ConfigService](../app/services/config_service.py) | `get_config()`、`apply_updates()`、`run_config()` |
| 配置提取与完成检查 | [ConfigExtractorService](../app/services/config_extractor_service.py)、[ConfigCompletionService](../app/services/config_completion_service.py) | `prepare()`、`complete()` |
| 长期记忆治理 | [`memory_service.py`](../app/services/memory_service.py) | `save_semantic_memory()`、`retrieve()`、`create()`、`promote()`、`invalidate_sources()` |
| Run 级异步提取 | [`memory_extractor_service.py`](../app/services/memory_extractor_service.py) | `enqueue_run()`、`retry_runs()`、`run_background()` |
| Run 提取上下文 | [`memory_extraction_context.py`](../app/services/memory_extraction_context.py) | `MemoryExtractionContextBuilder.build()` |
| 文档版本与检索 | [`document_service.py`](../app/services/document_service.py) | `ingest()`、`retrieve()` |
| Delegate 上下文隔离 | [`delegation_service.py`](../app/services/delegation_service.py) | `_scoped_context()` |
| 数据模型 | [`models.py`](../app/domain/models.py) | `ConversationTurnSummaryView`、`MemoryView`、`ContextManifestView` |
| SQLite Schema 和迁移 | [`database.py`](../app/storage/database.py) | `Database._initialize()`、`transaction()` |
| HTTP 查询和治理接口 | [`routes.py`](../app/api/routes.py) | `/memories`、`/contexts`、`/runs/{id}/contexts` |
| 专项回归测试 | [`test_context_memory_v2.py`](../tests/test_context_memory_v2.py) | 长对话、角色隔离、冲突、换版、失败和迁移场景 |

## 3. 一次请求的完整数据流

```mermaid
sequenceDiagram
    participant U as User
    participant O as OrchestratorService
    participant S as SessionService
    participant E as MemoryExtractorService
    participant C as ContextBuilderService
    participant Q as ConversationSummaryService
    participant M as MemoryService
    participant D as DocumentService
    participant R as Agent Runtime
    participant DB as SQLite

    U->>O: ChatRequest
    O->>S: resolve session/role
    O->>S: add_message(current user message)
    S->>DB: 原子写消息、sequence、会话计数
    O->>DB: 校验并写入增量配置
    O->>C: build(session, role, current_message_id, project_config)
    C->>D: 入库本轮携带的文档
    C->>S: 读取完整轮次，不含当前消息
    C->>Q: 读取较早轮次 completed 摘要
    C->>M: 检索 active Memory
    C->>D: 检索 active Document Chunk
    C-->>O: effective_request + selection metadata
    O->>DB: 创建 Run 并关联 User 消息
    O->>C: save_manifest(run_id)
    O->>R: 发送实际上下文
    R-->>O: Agent/Tool 结果
    O->>S: 保存最终 Assistant 消息并绑定 turn_id
    O-->>Q: 非阻塞 enqueue_turn
    Q->>S: 后台只读取本轮 User + Assistant
    Q->>DB: 幂等保存独立摘要
    O->>DB: 按固定配置版本检查实际实现，保存最终配置快照
    O->>E: completed Run 调用 enqueue_run
    E->>DB: 幂等写入 pending 事件
    O-->>U: 返回 ChatResponse
    E->>DB: 后台认领事件、读取本 Run 上下文
    E->>M: 校验并应用 ADD 或 UPDATE
    M->>DB: 语义记忆与事件完成原子提交
```

当前用户消息先被持久化，但 `ContextBuilderService.build()` 通过 `exclude_message_id` 将它从历史中排除，因为它会继续以 `request.message` 发送给模型。这样数据库保留完整审计记录，Prompt 又不会重复出现同一句话。

## 4. 持久化模型

### 4.1 会话消息与角色摘要

`conversation_messages` 是会话正文的事实源。关键字段包括：

- `sequence`：会话内单调递增的稳定游标；
- `turn_id`：同一轮 User Request 与最终 Assistant Response 的稳定关联；
- `role`：`user`、`assistant` 或 `system`；
- `agent_role`：消息所属的 Design、Verification 等工作角色；
- `message_type`：普通聊天、Run 决策等消息分类；
- `run_id`、`delegation_task_id`：把消息关联到执行记录；
- `metadata_json`：审批动作、证据引用等附加信息。

`SessionService.add_message()` 在 `Database.transaction()` 中同时分配 sequence、插入消息并更新会话标题、预览和计数，避免出现“消息写入成功但会话计数没有更新”的半完成状态。

`conversation_turn_summaries` 每行只描述一轮完整问答，`turn_id` 唯一。主要字段为：

| 字段 | 含义 |
|---|---|
| `id` / `turn_id` | 摘要 ID 与唯一轮次键；重复执行不会新增记录 |
| `session_id` / `agent_role` / `sequence` | 会话、角色隔离和原始排序依据 |
| `user_message_id` / `assistant_message_id` | 两条真实来源消息，支持追溯 |
| `summary_json` / `token_count` | 独立结构化摘要及实际估算 Token |
| `status` | `pending`、`processing`、`completed` 或 `failed` |
| `attempts` / `last_error` | 后台重试次数与最近错误 |
| `generation_mode` | `model_structured` 或测试模式的 `deterministic_turn` |

旧的 `conversation_summaries` 和 `conversation_role_summaries` 表仍保留用于审计和兼容迁移，但新 ContextBuilder 不再读取或更新它们。

### 4.2 长期记忆

`memories` 保存记忆主体，`memory_sources` 保存证据，`memory_relations` 保存替代关系。这种拆分允许一条结论同时由多个独立来源支持。

`memories` 中对选择和治理影响最大的字段是：

| 字段 | 作用 |
|---|---|
| `project_id` / `module_name` / `scope` | 项目和模块作用域 |
| `memory_key` | 稳定语义键，例如 `uart.clock_frequency`；冲突检测依赖它 |
| `kind` | 自动提取统一使用 `semantic`。旧类型继续可读 |
| `content` | 可注入模型的记忆正文 |
| `confidence` | 1–10，参与检索排序 |
| `importance` | 1–10；高重要性的事实/决策可成为关键约束 |
| `authority` | 来源权威描述；`agent` 和 `inference` 在排序时降权 |
| `visibility` | `project_shared` 或 `role_private` |
| `owner_agent_role` | 私有记忆的所属角色 |
| `status` | 生命周期状态 |
| `review_reason` | 冲突、部分来源失效等复核原因 |
| `extraction_key` | 兼容旧候选接口的可选幂等键。新流程的键存在事件表 |

`memory_sources` 不删除历史证据，而是用 `status=active/invalidated`、`invalidated_at` 和 `invalidation_reason` 表达来源是否仍有效。

### 4.3 提取事件和 Context Manifest

`memory_extraction_events` 以 `run:<run_id>` 幂等，包含 `pending`、`processing`、`completed`、`failed`、尝试次数、记忆 ID 和错误。既有 `error` 列记录上一次失败，`updated_at` 用于判断租约及重试时间。

`context_manifests` 保存一次模型调用实际选中的证据集合，而不是仅保存配置。主要内容包括：

- 消息、原始 turn、独立摘要、Memory、Document Chunk ID；
- `raw_turn_ids`、`turn_summary_ids` 和各区实际 Token；
- 各区 Token 估算、检索分数和选择原因；
- 所有被排除候选及排除原因；
- `prompt_hash` 和 `context_version`。

## 5. 角色可见性与短期上下文

会话切换角色时，消息不会被复制或删除。隔离发生在读取路径：

```sql
agent_role IS NULL OR agent_role = :current_agent_role
```

`SessionService.completed_turns()` 使用同一规则。`agent_role IS NULL` 表示可被所有角色看到的公共消息；带角色的消息只对该角色可见。

这条规则同时约束：

- 最近 5 个完整原文轮次；
- 较早 30 个摘要或回退轮次；
- 后台摘要读取的两条来源消息。

因此，Verification 的摘要不会因为 sequence 位于 Design 消息之后就读取到 Design 私有正文。

## 6. 独立轮次摘要

### 6.1 轮次识别与窗口

`SummaryPolicy` 当前默认值为：

```python
recent_turns = 5
history_turns = 30
target_min_tokens = 150
target_max_tokens = 250
hard_max_tokens = 500
summary_budget = 6_000
max_workers = 2
max_pending_tasks = 32
```

一轮完整对话是一条 User Request 和对应的一条最终 Assistant Response。新写入消息共享 `turn_id`；兼容旧库时，`completed_turns()` 按 sequence 将一条旧 User 与其后的第一条旧 Assistant 配对。没有最终 Assistant 的当前请求、System 消息以及非消息形式的 Tool 调用不会构成完整轮次。

ContextBuilder 保留最近 5 个完整轮次原文，并从它们之前取最近 30 个完整轮次。窗口以轮次而不是消息条数滑动，因此最近原文最多是 10 条消息。

### 6.2 生成时机和有界队列

摘要不在 `ContextBuilder.build()` 中生成。普通编排和 Run Resume 都执行：

```text
保存 User Request（生成 turn_id）
→ Agent / Tool 执行
→ 保存最终 Assistant Response（复用 turn_id）
→ enqueue_turn 非阻塞投递
→ Worker 重新读取本轮两条消息
→ 生成并 Upsert 独立摘要
```

线程池最多 2 个 Worker，并用 32 个 Semaphore 槽位限制待处理任务。队列满时记录保持 pending，不无限创建线程。`turn_id` 的数据库唯一约束和进程内 scheduled 集合防止重复记录与重复排队；`retry_failed_summaries()` 可重新投递 pending/failed 任务。摘要异常不会覆盖已经持久化的 Assistant 消息，也不会让正常请求失败。

### 6.3 结构化摘要内容

模型必须返回以下字段：

```json
{
  "topic": "",
  "module": "",
  "user_intent": "",
  "key_information": [],
  "decisions": [],
  "constraints": [],
  "assumptions": [],
  "inferences": [],
  "open_questions": [],
  "keywords": []
}
```

`decisions` 和 `constraints` 的每一项包含 `content`、`source_message_ids` 和 `confirmation_status`。允许状态为 `user_confirmed`、`assistant_suggestion`、`unconfirmed` 或 `historical`。

模型输出不会被直接信任。`_validate()` 会：

1. source ID 只能是本轮 User 或 Assistant 消息；
2. 只有来源包含本轮 User 消息时才允许 `user_confirmed`；
3. 将未知确认状态降为 `unconfirmed`；
4. 不读取旧摘要，也不把 Assistant 建议自动提升为用户决策；
5. 对来源 ID 去重并保持首次出现顺序。

### 6.4 长度和失败处理

生产模式复用主模型 Provider。Prompt 要求 150–250 Token；首次结果超过 500 Token 时发起一次结构化压缩，压缩后仍超限则任务标为 failed，不保存超长 completed 摘要。测试/Mock 模式使用短小、可追溯的 `deterministic_turn`，但同样只依据本轮两条消息。

`conversation_turn_summaries.status` 的含义：

- `pending`：已登记，等待 Worker；
- `processing`：Worker 已领取；
- `completed`：结构化摘要已通过长度与来源校验；
- `failed`：模型、解析、压缩或持久化前校验失败，可重试。

### 6.5 摘要进入 Prompt 和原文回退

ContextBuilder 只注入 completed 摘要，并以原始 sequence 排序：

```text
[独立轮次摘要 turn_id=<id>; sequence=<n>; role=design]
{...结构化 JSON...}
```

若对应轮次没有 completed 摘要，则该轮 User 和 Assistant 原文一起回退。摘要与其来源原文不会同时注入。摘要、回退原文和最近原文都作为完整单元参与预算；放不下时整轮排除，并在 Manifest 的 `excluded` 中记录原因。

## 7. Run 级长期记忆提取

### 7.1 完成触发与恢复

Legacy 聊天和 Resume 在最终 Assistant 消息持久化之后投递。Legacy 异步工具的统一 Run 由 `GET /runs/{id}` 同步终态后投递。分层执行器在终态和最终回复提交后调用完成回调，覆盖初始聊天、Resume 和后台执行。文档换版继续负责 RAG 和来源失效，专家结果经由父 Run 进入提取上下文。

`enqueue_run()` 校验 Run 已完成、作用域匹配、已持久化关联回复，再写入 `pending` 事件并尝试投递。进程内用 `ThreadPoolExecutor`、`BoundedSemaphore` 和 scheduled 集合限制并发与队列。默认上限为 2 个 Worker、32 个任务槽位。队满时由应用启动后的周期扫描补投。失败事件可通过再次 enqueue 立即重试，后台扫描在 1 分钟后重试。超过 15 分钟未写回的 `processing` 事件可重新认领。

Worker 通过 SQLite 条件更新认领事件并增加 `attempts`。保存时再次验证尝试次数，过期 Worker 的结果无法覆盖新一次尝试。一次模型调用和最终提交之间若进程中断，恢复可能重新调用模型。记忆写入与事件的 `completed` 状态在同一事务提交，提交失败时整批回滚。空结果也记为 `completed`，完成的事件跳过模型调用。

### 7.2 提取上下文与输出

- 会话通过 `run_id`、会话 ID 和主角色精确关联。包含原始请求、最终回复、恢复消息和决策动作。旧 Run 的原始请求可从 `runs.user_instruction` 读取。
- 执行证据包括有引用的 Observation、真实工具状态和有限指标、独立验收结果、已验证制品的元数据和专家建议。提取上下文排除 Planner 和 Reflection 的内部推理、原始日志及原始 Tool Call。
- `ConfigService.run_config(run_id, final=True)` 返回本轮最终配置快照。Worker 优先读取完成检查后的快照，并在配置区保留 revision。完整配置仅在后端用于精确重复检查，模型最多读取 1,500 Token 的当前模块投影。
- 相关记忆按项目、模块和角色检索，默认 Top-K 为 5。内容过长的记忆不作为模型可更新的完整目标。候选记忆只携带 ID 和正文。

提取 Prompt 保留稳定偏好、长期约定、设计理由与有工具或独立验证证据的经验。项目参数与权威工程数据、临时假设、普通日志和一次性指令应当跳过。输出为 `{"memories":[...]}`，每项为 `add` 加正文，或 `update` 加本轮提供的目标 ID 与更新后完整正文。没有长期价值时数组为空。

`generate_memories(prompt, payload)` 通过现有 Provider 执行结构化提取。生产实例只使用已启用的 DeepSeek 主模型，地址必须为 `https://api.deepseek.com`。端点不可用时事件记录可重试错误。测试以本地替身覆盖输出验证及完整入库流程。

### 7.3 语义记忆写入

`MemoryService.save_semantic_memory()` 从已完成的来源 Run 校验 project、module 和角色。新记录使用 `kind=semantic`、`status=active`、`scope=module`、`visibility=role_private`，owner 为 Run 主角色。兼容列使用固定值。

ADD 在相同项目、模块、角色内比较正文的 NFKC 和空白规范化结果。完全重复的 active 记忆保留原正文和 ID，只追加来源。UPDATE 要求目标在送给模型的记忆集合内，且仍为同角色、同模块 active 语义记忆，正文与更新时间需匹配读取时的版本。正文变化后，旧引用标为 `semantic_content_updated`，历史引用可审计，同时追加当前 Run。非法或已变化的更新目标跳过。

## 8. 来源校验、激活和冲突处理

### 8.1 来源校验矩阵

`MemoryService._validate_sources()` 在创建或提升前检查真实数据库记录：

| `source_type` | 对应表 | 额外权威条件 |
|---|---|---|
| `conversation_message` | `conversation_messages` | 消息存在 |
| `user_confirmation` | `conversation_messages` | 消息存在且 `role='user'` |
| `document_version` | `document_versions` | 版本存在 |
| `document_chunk` | `document_chunks` | Chunk 存在 |
| `run` | `runs` | `status='completed'` |
| `delegation` | `delegation_tasks` | `status='completed'` |
| `patch_approval` | `patches` | `status='applied'` |
| `external` | 不查询内部表 | 为旧 API 和外部问题单保留的兼容入口 |

未知类型、空 source ID，或未达到状态条件的内部来源会被拒绝。`external` 的真实性由调用方承担，因此生产集成应尽量换成可在本地校验的正式来源类型。

### 8.2 创建和提升

公共 API 创建时只允许请求 `proposed` 或 `active`。active 的 `semantic`、`fact` 和 `decision` 至少需要一个可追溯来源。`needs_review/stale/superseded/rejected` 由生命周期规则产生，调用方不能任意指定。

显式候选的兼容路径为

```text
POST /memories → proposed/needs_review
→ 人工或正式工作流确认
→ POST /memories/{id}/promote
→ 校验新来源
→ active
```

可被 `promote()` 激活的状态是 `proposed`、`needs_review` 和 `stale`。提升、添加来源、替代冲突记忆和写关系在同一事务中完成。

### 8.3 生命周期

```mermaid
stateDiagram-v2
    [*] --> proposed: API 显式候选
    proposed --> needs_review: 同 memory_key 与 active 内容冲突
    proposed --> active: 有效来源确认并 promote
    needs_review --> active: 人工选择新结论并 promote
    active --> superseded: 同 key 新结论正式生效
    active --> stale: 所有有效来源失效
    stale --> active: 重新验证并绑定有效来源
    proposed --> rejected: 治理流程明确拒绝
```

当前代码没有对外暴露通用的 rejected 操作接口；状态图中的 rejected 是治理模型保留状态。

### 8.4 同一 memory_key 的替代

假设已有：

```text
uart.clock_frequency = UART clock 为 100 MHz (active)
```

新候选“UART clock 为 125 MHz”创建后进入 `needs_review`。当它通过有效用户确认或正式来源提升时，事务会：

1. 把新 Memory 改为 active；
2. 把旧 Memory 改为 superseded；
3. 建立 `new --supersedes--> old` 关系；
4. 默认检索只返回新 active 结论。

这不是简单按更新时间覆盖；旧正文、旧来源和关系仍可审计。

## 9. 文档换版和来源级失效

### 9.1 原子换版

文档身份由 `(project_id, module_name, kind, name)` 唯一确定。新版本入库在一个 `BEGIN IMMEDIATE` 事务中执行：

```text
查找或创建 document identity
→ 检查 version + content_hash 是否已经存在
→ 读取当前 active version/chunk ID
→ 将旧 version/chunk 标记为 superseded
→ 失效引用这些 ID 的 memory_sources
→ 根据剩余 active 来源更新 Memory 状态
→ 插入新 active version 和 chunks
→ commit
```

任一步骤抛错都会 rollback。例如来源失效失败时，不会留下“旧版本已 superseded，但新版本尚未写入”的中间状态。

相同 `version + content_hash` 再次上传会返回已有记录，不重复创建，也不会把已经 superseded 的历史版本偷偷恢复为 active。

### 9.2 来源失效不等于整条 Memory 立即失效

`MemoryService.invalidate_sources()` 逐条把被替代来源标为 invalidated，然后检查同一 Memory 是否还有其他 active 来源：

- 没有其他来源：active Memory 变为 `stale`；
- 仍有独立来源：Memory 保持 active，但写入 `review_reason=部分来源已被替代，仍有独立有效来源`。

例如“同步复位”同时由 AS v1 和已批准问题单支持。AS v2 替代 v1 后，规格来源失效，但问题单来源仍有效，因此该记忆不会被错误删除。

### 9.3 文档分块和检索

`DocumentService._chunk()` 优先按标题和段落切分，目标约 700 Token；超长段落再切片，不跨章节盲目拼接。

当前检索不依赖外部向量数据库：

1. 中英文概念别名归一化，例如 reset/复位、handshake/握手；
2. 提取英文词、数字和中文二元词；
3. 生成 192 维确定性 Hash 特征向量；
4. 同时计算带文档频率权重的关键词得分和余弦得分；
5. 最终 `score = 0.58 × keyword + 0.42 × semantic`。

默认只查询 active Chunk。ContextBuilder 最多读取前 50 个候选，再根据总预算选择。

## 10. 长期记忆检索算法

### 10.1 第一阶段：确定性过滤

`MemoryService.retrieve()` 先执行 SQL 过滤：

- `project_id` 必须匹配；
- `module_name` 可以是项目级 NULL，也可以等于当前模块；
- 只返回 `status='active'`；
- 普通主 Agent 可看 `project_shared` 和属于自己的 `role_private`；
- Delegate 使用 `shared_only=True`，只能看 `project_shared`；
- active 的 semantic、fact 和 decision 至少仍有一个 active 来源。

这一步发生在排序前，模型无法通过 Prompt 请求绕过。

### 10.2 第二阶段：相关性排序

查询和 `content + memory_key` 会被拆成英文词、数字和中文二元词。词法相关度使用集合 Jaccard：

```text
lexical = |query_terms ∩ memory_terms| / |query_terms ∪ memory_terms|
```

最终分数为：

```text
score = lexical × 0.55
      + importance/10 × 0.20
      + confidence/10 × 0.10
      + authority_score × 0.10
      + scope_score × 0.05
```

其中：

- 当前模块 Memory 的 `scope_score=1.0`，项目级 Memory 为 `0.65`；
- `authority=agent/inference` 时 `authority_score=0.35`，其他为 `1.0`；
- 普通候选要求 `lexical >= 0.04`；
- `importance >= 9` 且类型为 fact/decision 的项目约束可跳过词法阈值，标为 `critical_constraint`。

关键约束优先，其余按得分、重要性和更新时间降序。默认返回最多 5 条，显式 limit 的上限为 30。ContextBuilder 随后应用 Token 预算。

## 11. ContextBuilder 的输入和输出

### 11.1 输入

`ContextBuilderService.build()` 接收：

- `session_id`；
- 原始 `ChatRequest`；
- 当前 `agent_role`；
- 本轮已经落库的 `current_message_id`。

它返回 `ContextBuildResult`，其中：

- `effective_request`：真正交给 Runtime 的请求；
- `message_ids/memory_ids/chunk_ids`：实际选中项；
- `memory_context`：由 Orchestrator 作为 `_memory_context` 注入；
- `sections/excluded/summary`：审计元数据；
- `estimated_tokens`：预算估算；
- `prompt_hash`：选择快照标识。

### 11.2 固定预算

默认总预算为 32,000 Token。`TokenCounter` 优先使用 `tiktoken/cl100k_base`；未安装时使用偏保守的中英文估算，因此这里是模型无关预算，不是厂商账单的精确 Token 数。

默认配置如下：

| 区段 | 配置值 | 当前代码中的约束方式 |
|---|---:|---|
| System + Tool Schema | 4,000 | 固定预留 |
| 当前请求与 Run | 3,000 | 至少预留 3,000；请求更长时按请求估算增加 |
| 最近 5 轮原文 | 6,000 | User/Assistant 以完整轮次原子选择 |
| 较早 30 轮 | 6,000 | completed 摘要；未完成时使用完整原文回退 |
| 项目配置 | 2,500 | 先分配，按项选择，记录 revision 和省略数量 |
| 长期记忆 | 2,000 | 普通 Memory 上限；关键约束可借用剩余预算 |
| 文档 | 12,000 | 当前字段是规划参考值；代码实际使用最后的全部剩余预算 |
| 输出预留 | 3,000 | 至少 3,000；默认配置会参考模型 `max_output_tokens`，最高按 8,000 预留 |

`recent_messages` 和 `summary` 分别作为最近原文轮次和较早摘要/回退轮次的目标上限；所有区段仍受统一总预算约束。`documents` 最后使用剩余额度。

固定区计算为：

```text
fixed = system_and_tools
      + max(current_request_and_run, query_tokens)
      + max(output_reserve, min(model.max_output_tokens, 8000))
```

`current_request_and_run` 是预算字段的历史命名。当前 `build()` 发生在 Run 创建之前，所以代码实际计算的是当前 `request.message` 的 Token 和一段运行控制预留，并没有在这里额外注入一个已存在 Run 的完整状态对象。

使用显式 `budget_config` 的测试或定制实例不会动态调整输出预留。如果固定区已经超过总预算，Builder 立即报错。

### 11.3 选择顺序

Builder 严格按下面顺序消费预算：

1. 入库本轮随请求携带的文档；
2. 识别当前角色全部完整问答轮次，当前 User 不进入历史；
3. 划分最近 5 轮原文和更早的最近 30 轮；
4. 预留 System、当前请求和输出空间；
5. 从新到旧按整轮选择最近原文；
6. 对更早轮次选择 completed 独立摘要，缺失时选择完整原文回退；
7. 将已选单元恢复为原始 sequence 顺序；
8. 检索并选择长期记忆；
9. 检索并选择文档 Chunk；
10. 生成 `effective_request`、Manifest 数据和 hash。

这个顺序体现了当前优先级：**当前请求和输出安全 > 最近完整轮次 > 较早摘要/完整回退 > 关键 Memory > 相关文档。**

### 11.4 完整轮次选择

最近轮次和较早轮次都从最新向最旧扫描，优先保留靠近当前请求的内容，选完后再恢复时间正序。User/Assistant 的 Token 成本相加后一次判断，绝不拆分。排除记录示例：

```json
{
  "category": "raw_turn",
  "id": "<turn_id>",
  "reason": "近期完整问答区预算不足；整轮排除",
  "tokens": 123
}
```

迁移期如果后端尚无可用历史，且客户端仍携带旧 `request.history`，Builder 会最多检查最后 12 条作为一次性兼容回退。新会话稳定运行后应以后端消息为准。

### 11.5 Memory 选择

Builder 按当前角色检索最多 5 条。普通 Memory 共享 2,000 Token 区，旧版关键约束可使用总预算中的剩余额度。`_memory_context` 只包含 Memory ID、类型和正文。分数与选择原因写入 Manifest。

### 11.6 文档选择

Builder 请求最多 50 个 Chunk。零分 Chunk 默认排除，但本轮刚上传的文档即使查询词没有命中，也作为当前请求证据保留候选，避免 DS/RTL 工作流在“上传文档并立即生成”时看不到材料。

文档是最后一个大区段，可以使用之前各区没有消耗的全部剩余预算。每个选中 Chunk 被包装为：

```text
[RAG source: chunk=<id>; section=<section>; score=<score>]
<chunk content>
```

因此模型回复中可以追溯到具体 Chunk，而不只是文档名。

### 11.7 实际发送给 Runtime 的结构

`effective_request` 会替换两部分：

- `history`：较早独立摘要或完整回退轮次 + 最近 5 轮完整原文；
- `documents`：选择后的 RAG Chunk。

当前问题仍保留在 `message`。Orchestrator 另外加入两个内部字段：

```text
_memory_context
_context_manifest = {message_ids, turn_summary_ids, raw_turn_ids, memory_ids, chunk_ids}
```

这些下划线字段属于模型运行时上下文，不污染公共 `ChatRequest` API 契约。

## 12. Context Manifest 与可复现性

### 12.1 Manifest 保存时机

Builder 完成后，Orchestrator 先创建 Run，再调用 `save_manifest(session_id, run_id, role, result)`。因此即使后续 Selector、主模型或 Tool 失败，仍能查询这个 Run 当时准备了哪些证据。

### 12.2 sections 和 excluded

`sections` 记录：

- history、summary、raw_conversation、memory、documents 的条目数和 Token；
- Memory/Chunk 分数与选择原因；
- query Token；
- System、当前请求、输出的预留；
- 未使用 Token。

`excluded` 记录完整原文轮次、轮次摘要、回退轮次、Memory 或文档因预算/相关性被排除的原因。排除不是错误，但它使“模型为什么没看到某条信息”可以被定位。

### 12.3 prompt_hash 的准确含义

当前 `prompt_hash` 对以下选择元数据做 SHA-256：

- 当前 `request.message`；
- 被选中的 message ID；
- `turn_summary_ids` 与 `raw_turn_ids`；
- Memory ID；
- Chunk ID；

它是上下文选择快照标识，**不是最终序列化 Prompt 全文的哈希**。例如 System Prompt 文案变化不会必然改变该值。如果未来需要严格重放，应额外保存 Prompt 模板版本、模型配置和最终序列化正文哈希。

查询接口：

```text
GET /api/v1/runs/{run_id}/contexts
GET /api/v1/contexts/{context_id}
```

## 13. Multi-Agent 和 Delegate 的上下文边界

主 Agent 的角色历史由 `agent_role` 隔离。跨角色共享依靠正式 `project_shared` Memory 和文档，而不是复制另一角色的聊天记录。

`DelegationService._scoped_context()` 明确构造：

```json
{
  "message": "专家任务指令",
  "documents": [],
  "history": [],
  "_memory_context": ["仅相关 project_shared active Memory"]
}
```

Delegate 调用 `MemoryService.retrieve(..., shared_only=True)`，使用默认 Top-K。

- 不取得主 Agent 历史；
- 不取得任何 `role_private` Memory；
- 只能看到与 instruction 相关的 active 共享约束；
- 专家结论返回主 Agent 作为 Observation；
- 完成的专家建议由父 Run 提取器结合工程证据判断。

主 Agent 始终保留控制权，没有 Handoff。

## 14. 三个完整例子

### 14.1 保存稳定验证偏好

用户要求今后的 FIFO 设计始终评估峰值吞吐量。Run 完成后，提取器可保存该偏好，来源为当前 Run。已有同正文记忆时复用 Memory ID，将当前 Run 追加为有效来源。

### 14.2 更新设计理由

下一次 Run 的验证表明应覆盖持续背压，并说明原有吞吐量评估在该条件下的缺口。若旧记忆在本轮相关记忆中，模型可提交 UPDATE。后端检查作用域和版本，保留失效的旧引用并登记本次执行来源。FIFO 深度等参数以工程数据为准。

### 14.3 AS v1 被 AS v2 替代

1. AS v1 的 version/chunk 是 active；
2. 某 Memory 只引用 v1，另一条 Memory 同时引用 v1 和正式问题单；
3. v2 入库时 v1 来源被标为 invalidated；
4. 只依赖 v1 的 Memory 变 stale；
5. 有问题单独立支持的 Memory 保持 active 并附 review reason；
6. 新请求只检索 v2 的 active Chunk；
7. v1、旧 Chunk、来源失效时间和原因全部保留。

## 15. API 使用示例

创建候选：

```http
POST /api/v1/memories
Content-Type: application/json

{
  "project_id": "soc-a",
  "module_name": "uart",
  "memory_key": "uart.reset_policy",
  "kind": "decision",
  "content": "UART 使用同步复位",
  "confidence": 8,
  "importance": 8,
  "authority": "user_event",
  "status": "proposed",
  "source_refs": [
    {"source_type": "conversation_message", "source_id": "<message-id>"}
  ]
}
```

确认并提升：

```http
POST /api/v1/memories/<memory-id>/promote
Content-Type: application/json

{
  "authority": "user_confirmed",
  "source_refs": [
    {"source_type": "user_confirmation", "source_id": "<user-message-id>"}
  ]
}
```

查看治理状态：

```text
GET /api/v1/memories?project_id=soc-a&module_name=uart
GET /api/v1/memories?project_id=soc-a&module_name=uart&status=needs_review
```

## 16. 数据库迁移与事务约束

`Database._initialize()` 使用 `CREATE TABLE IF NOT EXISTS` 和 `PRAGMA table_info` + 条件 `ALTER TABLE ADD COLUMN` 做增量迁移。重复启动不会重复添加字段，旧数据不会被删除。

本次改造增加 `idx_conversation_run` 和 `run_design_config(run_id, input_json, final_json)`。前者用于查找 Run 对话，后者保存输入和最终配置快照。旧 Run 在该表没有快照，原表、原字段、历史记忆和工程文件继续保留。

此前的轮次摘要迁移包括

- 为 `conversation_messages` 增加可空 `turn_id`，不改写旧正文；
- 新建 `conversation_turn_summaries`，并对 `turn_id` 建唯一约束；
- 为 `context_manifests` 增加 `turn_summary_ids_json` 和 `raw_turn_ids_json`；
- 新 Manifest 写入 `context_version=3`；
- 保留 `conversation_summaries`、`conversation_role_summaries` 及其中历史数据。

`Database.transaction()` 使用进程内可重入锁和 SQLite `BEGIN IMMEDIATE`。当前需要原子性的关键操作包括：

- 写消息并更新会话索引；
- 保存 Run 语义记忆与提取事件完成；
- 创建/提升旧候选和处理冲突；
- 来源级失效；
- 文档换版、分块和来源失效。

复杂生产迁移未来应迁到 Alembic 等正式工具；当前手写迁移只适合向后兼容的新增表、字段和索引。

## 17. 回归测试

[`tests/test_context_memory_v2.py`](../tests/test_context_memory_v2.py) 使用隔离 SQLite 测试 Run 上下文、提取 Prompt、模型异常、批量回滚、幂等认领、排队恢复和来源管理，也覆盖旧记忆的显式治理、角色隔离、摘要和预算。[Legacy 路由测试](../tests/test_run_orchestration.py) 和 [Hierarchical 路由测试](../tests/test_hierarchical_run.py) 验证完成后投递及最终回复持久化。

## 18. 调试指南

### 18.1 模型没有看到某条历史消息

依次检查：

1. 消息的 `agent_role` 是否与当前角色一致；
2. User 和最终 Assistant 是否具有相同 turn_id，或能否按旧消息 sequence 配对；
3. 对应 turn summary 是否为 completed，failed 时查看 `last_error`；
4. Manifest 的 `message_ids`、`raw_turn_ids` 和 `turn_summary_ids`；
5. `excluded` 是否记录 raw_turn、raw_turn_fallback 或 turn_summary 预算不足。

### 18.2 模型没有看到某条 Memory

依次检查：

1. Memory 是否 active；
2. project/module 是否匹配；
3. visibility 和 owner role 是否允许；
4. semantic、fact 和 decision 是否至少还有一个 active 来源；
5. 普通记忆是否达到 0.04 词法阈值；
6. 是否在 ContextBuilder 的 Memory Token 区被排除；
7. Manifest 中是否有该 ID、分数或排除原因。

### 18.3 文档换版后旧结论仍出现

检查：

1. 旧 document version/chunk 是否已 superseded；
2. 相关 `memory_sources.status` 是否 invalidated；
3. Memory 是否有其他 active 来源；
4. 请求是否显式使用了 `include_superseded=true`；
5. 旧内容是否还存在于用户当前消息或近期历史，而不是来自 RAG。

### 18.4 上下文预算报错

两类报错需要区分：

- 固定区超过总预算：当前请求过大，或 System/输出预留配置不合理；
- 完整原文轮次或摘要单元超过对应预算：检查 `excluded`，恢复摘要 Provider、重试失败摘要或提高预算；系统不会拆散半轮。

## 19. 当前边界与后续演进

当前实现有意保留以下边界：

1. Memory 和文档语义检索仍是 SQLite + 确定性本地特征，不是外部向量数据库；
2. 摘要后台复用主 Provider，尚未支持独立低成本摘要模型配置或跨轮批处理；
3. `ContextBudgetConfig.summary/documents` 目前是参考额度，不是独立硬上限；
4. `prompt_hash` 不是最终 Prompt 全文哈希；
5. `external` 来源为兼容入口，无法由本地数据库验证其真实性；
6. 历史文档版本可审计，但没有独立的“恢复 superseded 版本”治理 API；
7. 单机角色隔离不是多租户安全边界，真实多用户部署仍需认证和项目 ACL；
8. Manifest 的 `observation_ids` 已预留，但当前构建阶段 Run 尚未执行，通常为空。

后续扩展时应保持三条不变量：

- 原始消息和证据不因摘要或替代而被物理删除；
- 语义记忆由后端验证来源和作用域后入库，工程数据负责当前设计状态；
- 每次进入模型的实际上下文必须可通过 Manifest 解释。

## 20. 项目配置与端到端验证

### 20.1 配置位置与读取接口

配置文件的实际位置为 `<WorkspaceView.root_path>/.chipflow/design-config.json`。默认 Workspace 位于 `data/workspaces/<项目目录>-<Workspace ID 前缀>/`。第一次成功写入使用指定的同项目 Workspace，或者项目已有的最早 Workspace。没有已登记的 Workspace 时，服务按现有规则创建。后续请求通过项目登记和文件作用域找到同一份配置。每个项目有一份权威配置，各模块与实例独立保存同名参数。

`ConfigService.get_config(project_id, module_name=None, workspace_id=None)` 读取当前配置。`get_pending_items(project_id, module_name)` 查询待实现项。`run_config(run_id, final=True)` 返回该 Run 的最终配置快照，也可通过 `final=False` 获取执行输入。示例文件如下。

```json
{
  "project_id": "chip001",
  "workspace_id": "workspace-id",
  "revision": 3,
  "modules": {
    "uart_rx": {
      "parameters": {
        "fifo_depth": {
          "config_item_id": "item-id",
          "value": 64,
          "revision": 2,
          "status": "pending",
          "source_message_id": "message-id",
          "source_evidence": "把 FIFO 深度改为64",
          "evidence_refs": [],
          "history": []
        }
      },
      "instances": {}
    }
  }
}
```

ConfigService 在 SQLite `BEGIN IMMEDIATE` 内串行应用文件增量，通过同目录临时文件原子替换。每批有效目标变更增加一次项目 revision，每个改值项增加自身 revision、保存旧版本与来源、重新进入 pending。实现状态使用同一配置项 revision。相同值跳过写入，独立配置项的并发写入可以合并，过期的同项写入被拒绝。普通 Workspace 工具写入受管目录会被拒绝。

### 20.2 Run 与状态证据

Legacy 和 Hierarchical 在创建上下文前运行相同的配置提取方法。当前请求是唯一的配置变更证据，最近 3 个完整可见轮次和已有配置用于消解指代。后端校验 Pydantic 输出、用户原文出处和模块范围。模型失败或补丁冲突时继续使用已提交配置。Resume 使用原 Run 固定的目标规格，并在完成时执行同一终态处理。

ContextBuilder 固定当前模块配置版本，先分配独立预算，并在 Manifest 的 `sections.project_config` 记录 revision、Token 和省略数量。Agent 判断实际工程状态时可以对照 RTL 和工具结果识别目标与实现的偏差。

`ConfigCompletionService` 可从本轮已应用的 Patch 或已验收的 RTL 或工程快照核对模块级整数常量声明。证据须与当前 Workspace 内容以及本轮配置项 revision 匹配。当前检查覆盖简单模块参数。实例覆盖和复杂表达式须等待可验证的展开或专项证据。

要标记 `verified`，在已批准的 EDA 清单用例上登记具体覆盖目标，并在测试台实际断言对应要求。以下片段位于 `testcases.<name>` 下。

```json
{
  "top_module": "uart_pass",
  "sources": ["dv/uart_pass.sv"],
  "depends_on": ["rtl/uart_top.sv"],
  "config_checks": [{"module": "uart_top", "parameter": "fifo_depth", "value": 64}]
}
```

只有绑定到本轮有效快照的相应用例实际通过，且匹配的模块参数已实现，才记录工具证据并推进到 verified。未覆盖该要求的仿真和 Lint 不推进 verified。其后 Memory Worker 读取该 Run 的最终快照，在保存前跳过可识别的重复配置陈述。

### 20.3 开发环境复现

在安装项目开发依赖的工作区运行以下命令。第一条执行 Legacy 和 Hierarchical 聊天、Legacy Patch Resume、分层 EDA 审批恢复、并发改值和异步记忆入库测试。第二条运行完整回归。

```powershell
python -m pytest -q tests/test_design_config.py tests/test_eda_project.py tests/test_context_memory_v2.py
python -m pytest -q
```

测试使用临时 SQLite 与 Workspace、结构化模型替身和 EDA 适配器替身。生产部署通过现有模型设置启用 DeepSeek，并填入有效 Key 和 Base URL `https://api.deepseek.com`。两条提取链路固定使用该地址，专用 HTTP Client 禁用重定向和环境代理。现有工程项目配置的读取和确定性完成检查可以独立运行。
