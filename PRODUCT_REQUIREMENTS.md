# ChipFlow Agent 产品需求文档

## 1. 文档定位

| 项目 | 内容 |
|---|---|
| 产品名称 | ChipFlow Agent |
| 文档性质 | 产品需求与验收基线 |
| 产品范围 | DS、RTL 与 EDA 工具闭环 |
| 主要使用者 | 芯片设计工程师、验证工程师、架构师、评审人员 |
| 运行环境 | Windows 优先，工具执行接口保留 Linux/远程扩展能力 |

## 2. 产品能力总览

ChipFlow 将规格理解、设计协作、代码候选、工程工具调用和问题闭环组织为统一的芯片工程协作平台。核心能力包括：

1. 产品能力从 DS 扩展到 RTL 候选生成与修改；
2. 接入 Sim、Lint、CDC、综合和 STA 工具执行框架；
3. 支持发现并执行已有验证用例；
4. 建立“日志解析—单问题分析—用户决策—修改—复验”闭环；
5. Agent 不再按照每个任务拆分，而是对应真实生产角色；
6. 设计和验证中的原子任务统一封装为可 Function Call 的 Tool；
7. Tool 负责确定性执行，Agent 负责工程判断、规划和解释；
8. 设计人员默认不生成验证计划或验证用例，只发现和执行验证人员已经维护的用例；
9. 所有文件修改继续采用候选 Patch、Diff Review 和用户批准机制；
10. 工作流状态、工具运行、问题队列和用户决策需要持久化。

## 3. 产品定位

ChipFlow Agent 是一套面向数字芯片设计与验证流程的 Human-in-the-loop 工程 Agent 平台。

系统以 FS、AS、ICD、DS 和 RTL 为设计上下文，由编排 Agent 组织架构、设计、验证及评审角色，通过 Function Call 使用文档、代码、验证和 EDA 工具，辅助完成：

- DS 设计与评审；
- RTL 编写、修改和 Review；
- 已有验证用例发现与执行；
- Sim、Lint、CDC 问题定位；
- 综合与时序分析；
- 候选修复、用户审批和复验。

系统不替代真实架构师、设计负责人或验证负责人，不自动推进芯片项目阶段，不擅自修改正式设计资产，也不将 Agent 共识当作正式工程结论。

## 4. 产品目标

### 4.1 核心目标

1. 辅助工程师完成符合架构基线的 DS；
2. 根据已确认 DS 生成或修改 RTL 候选；
3. 通过统一 Tool 接口执行 Sim、Lint、CDC、综合和 STA；
4. 将不同工具输出标准化为可追踪的问题；
5. 一次向用户反馈一个问题，避免一次展示大量级联错误；
6. 为当前问题提供证据、根因判断、影响和候选修改；
7. 允许用户选择按照建议修改、暂不修改或自定义修改方式；
8. 修改后自动执行最小范围复验；
9. 允许设计人员发现并执行验证人员已经维护的用例；
10. 保存文档、代码、工具结果、问题、修改和用户决策的全链路关系。

### 4.2 产品约束

- 不自动修改或发布 FS、AS、ICD；
- 不由系统决定何时进入 RTL、验证、综合或 Sign-off；
- 不允许模型直接执行任意 Shell 命令；
- 不允许未经批准直接修改正式 RTL 或 Testbench；
- 不以 LLM 替代确定性日志解析器；
- 不保证一次生成即可得到生产可用 RTL；
- 不自动放宽时序、CDC 或功能约束以消除问题；
- 不默认帮助设计人员制定验证计划；
- 商业 EDA 工具按统一 Adapter 契约由部署环境接入；
- 不将工具 Warning 自动视为必须修改的问题。

## 5. 核心产品原则

### 5.1 用户决定任务和阶段

用户通过自然语言明确当前任务，例如：

- “帮我完成 dma_ctrl 的 DS”；
- “根据当前 DS 生成 RTL 候选”；
- “执行 dma_ctrl 的 basic 用例”；
- “运行 Lint，并逐个帮我处理 Error”；
- “执行综合并分析面积变化”；
- “分析当前最差 Setup 路径”。

系统只组织完成当前任务所需的角色和 Tool，不自行宣布项目进入下一研发阶段。

### 5.2 Agent 对应真实生产角色

Agent 以架构、设计、验证和评审等真实职责划分，不按照 `DS Agent`、`Lint Agent`、`Log Agent` 等任务名称无限扩张。

### 5.3 单项工程动作封装为 Tool

文件读取、代码搜索、用例发现、工具执行、日志解析、问题队列、Patch 保存和复验等操作均封装为 Tool，由有权限的 Agent 通过 Function Call 调用。

### 5.4 思考与执行分离

- Agent 负责理解、判断、规划、权衡和解释；
- Tool 负责输入输出明确、可重复、可审计的工程动作；
- 工具原始结果不能被 Agent 静默修改；
- Agent 对工具结果的解释必须与原始证据同时保存。

### 5.5 正式资产默认不可直接修改

DS、RTL、Testbench、Assertion 和约束文件的修改流程为：

```text
生成候选修改
→ 校验修改范围
→ 展示 Before/After Diff
→ 用户 Review
→ 用户批准
→ 应用修改
→ 保存修改记录
→ 执行最小复验
```

### 5.6 一次处理一个问题

工具运行产生多个错误时，系统先去重、聚类并排序，然后一次只激活一个问题。当前问题完成用户决策和必要复验后，才能展示下一个问题。

### 5.7 证据优先

正式文档、经批准的问题单、用户确认决策和客观工具结果优先于 Agent 推断。工具结果与规格冲突时创建问题，不自动覆盖规格。

## 6. 目标用户

### 6.1 设计工程师

主要需求：

- 理解 FS、AS、ICD；
- 生成或修改 DS；
- 生成、补全和修改 RTL；
- 执行已有用例；
- 运行 Sim、Lint、CDC、综合和 STA；
- 分析设计侧错误；
- Review 候选 Diff；
- 追踪修改与规格、工具问题的关系。

设计人员默认不需要系统制定验证计划、Testpoint、Testcase 或 Coverage Plan。只有用户明确切换到验证任务并授权时，系统才调用验证角色执行这些工作。

### 6.2 验证工程师

主要需求：

- 管理和修改验证计划及用例；
- 维护 Testbench、Assertion 和 Coverage；
- 执行单用例和回归；
- 判断失败来自 RTL、验证环境、激励还是预期结果；
- 向设计人员提供复现条件和证据；
- 对修复结果进行复验。

### 6.3 架构师

主要需求：

- Review 规格理解和设计方案；
- 回答被升级的架构问题；
- 提供问题单或正式结论；
- 检查 DS、RTL 是否偏离架构基线；
- 查看规格变更影响范围。

### 6.4 评审人员

主要需求：

- 从独立视角读取冻结版本；
- 查看来源、Diff 和工具结果；
- 创建和复核 Finding；
- 保留异议；
- 不直接修改被评审资产。

## 7. Agent 体系

### 7.1 系统级 Agent

#### Conversation Router

- 区分普通聊天和工程任务；
- 识别 DS、RTL、工具执行、问题处理和验证任务；
- 上传文档或代码本身不自动触发工作流；
- 路由结果和原因可见。

#### Orchestrator

- 根据用户明确目标选择生产角色、评审角色和 Tool；
- 生成可执行计划；
- 管理顺序、并行、暂停和恢复；
- 管理用户审批点；
- 不作技术裁决；
- 不直接执行 EDA 命令。

### 7.2 生产角色 Agent

#### Architecture Agent

- 解释和质疑 FS、AS、ICD；
- 回答设计和验证角色问题；
- 判断问题是否需要真实架构师；
- 检查 DS 和 RTL 是否偏离架构意图；
- 对正式规格只读；
- 不能代表真实架构师批准修改。

#### Design Agent

- 生成和修改 DS 候选；
- 生成和修改 RTL 候选；
- 调用设计侧代码、Sim、Lint、CDC、综合和 STA Tool；
- 分析 RTL 根因；
- 生成候选 Patch；
- 根据用户自定义意见重新生成 Patch；
- 修改后调用最小复验 Tool。

#### Verification Agent

- 理解现有验证环境和用例意图；
- 为工具失败提供验证侧上下文；
- 判断失败是否来自 RTL、Testbench、激励或期望值；
- 在验证人员明确请求时创建或修改用例、Assertion 和 Coverage；
- 设计人员执行已有用例时，不主动生成验证计划。

### 7.3 评审角色 Agent

默认包含：

1. Architecture Reviewer；
2. Design Reviewer；
3. Verification Reviewer；
4. Integration Reviewer；
5. Specialist Reviewer。

Specialist Reviewer 通过配置按需加载 CDC、低功耗、安全、DFT、PPA 或时序检查清单，不为每个专项创建常驻 Agent。

评审角色只能读取冻结对象并创建 Finding，不得应用修改。

## 8. Tool 架构

### 8.1 Tool 定义原则

每个 Tool 必须满足：

- 单一职责；
- 明确输入和结构化输出；
- 明确允许调用的 Agent；
- 明确读写等级；
- 明确是否需要用户批准；
- 限定 Workspace 范围；
- 可记录、可重试、可取消；
- 原始输出可审计；
- 不在 Tool 内隐藏架构或设计决策。

### 8.2 Tool 元数据

```yaml
name: run_testcase
description: 执行一个已经注册的验证用例
allowed_roles:
  - design
  - verification
mutability: execution
requires_user_approval: true
idempotent: false
workspace_scope: project
timeout_seconds: 1800
input_schema: {}
output_schema: {}
audit_policy:
  save_command: true
  save_stdout: true
  save_stderr: true
  save_exit_code: true
```

### 8.3 修改等级

| 等级 | 含义 | 用户确认 |
|---|---|---|
| `read` | 读取文档、代码和结果 | 通常不需要 |
| `analysis` | 解析日志、计算 Diff、影响分析 | 通常不需要 |
| `candidate_write` | 写候选文件或 Patch | 可按项目策略配置 |
| `execution` | 执行编译、仿真和 EDA 工具 | 首次或高成本任务需要 |
| `formal_write` | 修改正式工作文件 | 必须确认 |
| `external_write` | 写 Git、问题单或远程系统 | 必须确认 |

### 8.4 Tool 权限矩阵

| Tool 类型 | Architecture | Design | Verification | Reviewer |
|---|---:|---:|---:|---:|
| 读取规格 | 是 | 是 | 是 | 是 |
| 读取 RTL | 是 | 是 | 是 | 是 |
| 创建 DS 候选 | 建议 | 是 | 否 | 否 |
| 创建 RTL Patch | 否 | 是 | 否 | 否 |
| 发现已有用例 | 否 | 是 | 是 | 只读查看 |
| 执行已有用例 | 否 | 是 | 是 | 否 |
| 创建用例候选 | 否 | 否 | 是 | 否 |
| 执行 Sim/Lint | 只读结果 | 是 | 是 | 只读结果 |
| 执行 CDC/综合/STA | 只读结果 | 是 | 按需 | 只读结果 |
| 应用正式 Patch | 否 | 批准后 | 批准后 | 否 |
| 修改 FS/AS/ICD | 否 | 否 | 否 | 否 |

## 9. Tool Catalog

### 9.1 文档与代码

```text
read_document
search_document
read_source_file
search_source_symbol
find_module_definition
find_signal_references
get_module_hierarchy
get_source_diff
```

### 9.2 DS 与 RTL

```text
create_ds_candidate
save_ds_candidate
compare_ds_versions
create_ds_patch
create_rtl_candidate
create_rtl_patch
validate_patch_scope
preview_source_diff
apply_approved_patch
rollback_patch
format_systemverilog
```

### 9.3 验证用例

```text
discover_testcases
list_test_suites
get_testcase_metadata
find_tests_for_module
find_tests_for_requirement
run_testcase
run_test_suite
run_regression
rerun_failed_test
run_impacted_tests
get_test_result
get_failed_testcases
get_waveform_artifact
get_coverage_summary
```

以下 Tool 默认仅对 Verification Agent 开放：

```text
create_testcase_candidate
create_testbench_patch
create_assertion_patch
create_coverage_patch
```

### 9.4 EDA 工具执行

```text
validate_tool_environment
run_compile
run_simulation
run_lint
run_cdc
run_synthesis
run_sta
cancel_tool_run
get_tool_run_status
get_tool_run_artifacts
```

### 9.5 日志与报告解析

```text
parse_compile_log
parse_sim_log
parse_lint_log
parse_cdc_log
parse_synthesis_log
parse_sta_report
normalize_tool_issues
deduplicate_issues
```

### 9.6 问题和 Patch

```text
create_issue_queue
get_next_issue
get_issue_details
record_user_decision
mark_issue_skipped
mark_issue_resolved
reopen_issue
list_remaining_issues
save_candidate_patch
apply_approved_patch
```

## 10. 核心能力需求

### 10.1 普通聊天

- 用户无需上传 FS、AS 即可聊天；
- Chat Agent 获得最近对话、项目、模块和可选文档上下文；
- 普通解释和讨论不启动工程工作流；
- 用户明确要求生成、修改、执行或评审时才进入对应任务。

### 10.2 DS 辅助设计

DS 能力包括：

- DS 持久化版本；
- DS 候选 Diff；
- Finding 逐条处理；
- DS 与 RTL 对应关系；
- 用户批准后的状态记录；
- 规格变化后的影响标记。

### 10.3 RTL 辅助实现

输入：

- 当前有效 FS、AS、ICD；
- 已确认 DS；
- 编码规范；
- 已有 RTL、Package 和公共模块；
- 用户指定范围。

输出：

- SystemVerilog/Verilog 候选；
- 端口、参数、寄存器、组合逻辑和时序逻辑；
- 必要 Assertion 建议；
- RTL 与 DS 条目映射；
- 未实现内容、假设和风险；
- 修改前后 Diff。

默认范围：第二阶段优先支持 SystemVerilog。是否支持 Verilog-2001 作为兼容输入，在技术设计阶段确定。

### 10.4 Sim、Lint、CDC

系统应能够：

1. 校验工具环境；
2. 生成受控命令；
3. 在指定 Workspace 中执行；
4. 保存命令、环境摘要、退出码、标准输出和错误输出；
5. 收集日志、波形和报告；
6. 将日志解析为统一 Issue；
7. 对 Issue 去重并识别级联错误；
8. 一次激活一个问题；
9. 由 Design Agent 结合代码和 DS 分析；
10. 涉及用例语义时邀请 Verification Agent；
11. 涉及规格语义时邀请 Architecture Agent；
12. 展示候选修改并等待用户决策；
13. 修改后执行最小复验。

### 10.5 综合与时序分析

综合应支持：

- 编译和 Elaborate 错误；
- 不可综合语法；
- 锁存器、多驱动、未连接信号；
- 面积、寄存器、存储器及主要资源摘要；
- 同一设计不同版本 QoR 比较。

STA 应支持：

- Setup、Hold、Recovery 和 Removal 违例；
- 起点、终点、时钟、路径类型、Slack；
- 最差路径排序；
- RTL 结构问题、约束问题和实现问题分类；
- 流水化、逻辑重构和扇出优化建议。

Agent 不得通过自动添加 False Path、Multicycle Path 或放宽时钟周期来关闭问题。此类约束修改必须单独说明依据并获得用户确认。

### 10.6 已有用例自动执行

设计人员场景：

```text
用户指定模块、用例或目录
→ discover_testcases
→ 展示发现的用例和执行范围
→ 用户确认或项目策略自动放行
→ run_testcase/run_test_suite
→ 收集结果
→ 失败进入单问题处理
```

系统不得因为设计人员执行已有用例而自动生成验证计划、新 Testcase、Assertion 或 Coverage。

验证人员明确请求时，可以由 Verification Agent 调用创建类 Tool 生成验证资产候选。

## 11. 长任务执行模型

EDA 工具调用采用异步 Run 模型。启动 Tool 后立即返回：

```json
{
  "run_id": "RUN-20261027-001",
  "status": "queued",
  "tool": "simulation",
  "target": "dma_basic_001"
}
```

运行状态：

```text
queued
→ preparing
→ running
→ collecting
→ parsing
→ completed / failed / cancelled / timed_out
```

系统通过事件流或轮询展示进度。用户可以取消尚未结束的 Run。Run 超时不能自动重试高成本任务，必须依据 Tool 策略处理。

## 12. 单问题处理闭环

### 12.1 Issue 生成

工具原始结果统一标准化为：

```yaml
issue_id: SIM-ERROR-003
run_id: RUN-20261027-001
stage: simulation
tool: verilator
severity: error
code: MULTIDRIVEN
message: Signal issue_valid has multiple drivers
file: rtl/dma_scheduler.sv
line: 128
raw_log_ref: artifacts/RUN-20261027-001/stderr.log
fingerprint: ...
```

### 12.2 去重和排序

排序优先考虑：

1. 工具无法继续执行的首个 Error；
2. 编译和 Elaborate 根因；
3. 可能导致大量级联错误的问题；
4. 严重 CDC、综合或时序问题；
5. 其他 Error；
6. Warning 和建议。

系统不能仅根据日志出现顺序假设根因，排序依据必须可解释。

### 12.3 用户展示内容

每次只展示一个 Active Issue，并包含：

- Issue ID；
- 阶段、工具和严重程度；
- 文件、行号和原始工具信息；
- Agent 根因判断；
- 相关 RTL、DS 和规格证据；
- 影响范围；
- 修改建议；
- 候选 Diff；
- 置信度；
- 修改后的建议复验范围。

### 12.4 用户决策

#### 按建议修改

- 记录批准；
- 应用批准的 Patch；
- 执行最小复验；
- 通过则标记 Resolved；
- 失败则 Reopened，并保存新的工具证据。

#### 暂不修改

- 不应用 Patch；
- 状态设为 Skipped，不得标记 Resolved；
- 保存用户原因；
- 最终报告持续展示风险；
- 进入下一个问题。

#### 自定义修改方式

- 保存用户修改原则；
- Design 或 Verification Agent 重新生成候选 Patch；
- 再次展示 Diff；
- 用户再次批准后才能应用。

#### 仅解释

- Agent 提供进一步解释；
- 不改变问题和文件状态。

### 12.5 Issue 状态

```text
detected
→ analyzing
→ awaiting_user
→ approved / skipped / custom_requested
→ patch_ready
→ applied
→ verifying
→ resolved / reopened
```

同一时间，一个用户任务默认只有一个 `awaiting_user` 的工具 Issue。

## 13. Workspace 与资产管理

### 13.1 Workspace Manager

负责：

- 为项目和任务建立隔离工作区；
- 限制 Tool 可访问目录；
- 区分正式文件、候选文件和临时生成物；
- 记录文件 Hash 和版本；
- 防止路径穿越和范围外修改；
- 保存工具执行前后的工作区状态。

### 13.2 Artifact Registry

管理：

- FS、AS、ICD、DS；
- RTL、Package、Testbench、Assertion；
- 约束和工具配置；
- 原始日志和规范化问题；
- 波形、覆盖率、综合和时序报告；
- Patch 和 Diff；
- 用户决策及复验结果。

每个 Artifact 至少记录：

```yaml
artifact_id: ART-001
type: rtl
project_id: dma-project
path: rtl/dma_scheduler.sv
version: 3
content_hash: ...
created_by: design_agent
derived_from:
  - DS-DMA-1.2
status: candidate
```

## 14. 数据对象

Workspace 与资产管理包括：

- `Workspace`；
- `Artifact`；
- `ArtifactVersion`；
- `ToolDefinition`；
- `ToolRun`；
- `ToolArtifact`；
- `NormalizedIssue`；
- `IssueQueue`；
- `UserDecision`；
- `Patch`；
- `PatchReview`；
- `Testcase`；
- `TestSuite`；
- `TestRun`；
- `RegressionRun`；
- `TraceLink`；
- `ApprovalToken`。

关键关系：

```text
Requirement
→ refined_by → DS Item
→ implemented_by → RTL Object
→ exercised_by → Testcase
→ checked_by → ToolRun
→ failed_by → NormalizedIssue
→ changed_by → Patch
→ verified_by → TestRun/ToolRun
```

## 15. 记忆与追踪

继续采用权威层、活跃知识层和审计归档层，并增加：

- 工具原始结果属于不可改写证据；
- Agent 根因判断属于带来源的推断；
- 用户批准的 Patch 属于项目决策；
- Skipped Issue 保留在活跃风险中；
- 修改 RTL 后，关联 DS、用例和历史工具结果进入影响分析；
- 复验成功不能删除旧失败记录，只能新增解决证据；
- 通用工具知识和项目事实使用不同命名空间。

## 16. 前端交互要求

### 16.1 工作台区域

建议包含：

1. 项目和 Workspace；
2. 文档与代码资产；
3. 对话和任务输入；
4. 当前执行计划；
5. Agent 和 Tool 活动；
6. 工具 Run 进度；
7. 单问题处理卡片；
8. Diff Review；
9. DS、RTL、日志和报告查看；
10. 历史任务和审计记录。

### 16.2 单问题卡片

必须提供：

- 查看原始日志；
- 跳转文件和行号；
- 查看相关 DS/规格；
- 查看根因解释；
- 查看候选 Diff；
- “按照建议修改”；
- “暂不修改”；
- “自定义修改方式”；
- “仅解释”；
- 查看剩余问题数量，但默认不展开全部错误。

### 16.3 Tool 执行确认

执行前展示：

- Tool；
- 命令摘要；
- 工作目录；
- 目标模块或用例；
- 预计影响；
- 超时；
- 是否使用商业许可证；
- 是否允许取消。

低风险且项目已授权的重复执行可以免除逐次确认，但正式写入和外部写入始终需要确认。

## 17. 功能需求

### FR-201 对话路由

系统应区分普通聊天、DS、RTL、验证用例执行和 EDA 工具任务。

### FR-202 角色型 Agent

系统应以 Architecture、Design、Verification 和 Reviewer 为主要专业角色，不为每个单项任务创建常驻 Agent。

### FR-203 Tool Registry

系统应注册 Tool 的 Schema、权限、修改等级、超时和审计策略。

### FR-204 Function Calling

Agent 应通过结构化 Function Call 调用 Tool，参数必须通过 Schema 校验。

### FR-205 Workspace 隔离

所有文件和执行 Tool 必须限制在当前项目 Workspace 中。

### FR-206 RTL 候选生成

Design Agent 应能基于已确认 DS 生成 RTL 候选及来源说明。

### FR-207 RTL Diff Review

RTL 修改必须先生成 Patch 和 Diff，用户批准后才能应用。

### FR-208 用例发现

系统应能从指定目录或注册表发现已有用例，并返回结构化元数据。

### FR-209 用例执行

设计和验证人员应能执行已有单用例、Suite 和回归。

### FR-210 角色化验证行为

设计人员执行用例时，系统不得默认生成验证计划或新用例；创建验证资产必须由 Verification Agent 在明确任务下完成。

### FR-211 EDA 环境检查

Tool 执行前应检查可执行文件、版本、配置、许可证和必要输入。

### FR-212 异步 ToolRun

长时间 Tool 应返回 Run ID，并支持进度、取消、超时和结果收集。

### FR-213 日志标准化

系统应保留原始日志，并将已支持工具的消息解析为统一 Issue。

### FR-214 问题去重

系统应根据工具、错误码、位置和消息指纹去重，并识别可能的级联问题。

### FR-215 单问题交互

系统应一次只向用户请求处理一个 Active Issue。

### FR-216 修改建议

Agent 应结合工具证据、代码、DS 和规格给出根因、影响和候选修改。

### FR-217 用户决策

用户应能选择按照建议修改、暂不修改、自定义修改方式或仅解释。

### FR-218 最小复验

修改应用后，系统应根据影响关系选择最小 Tool 或用例集合复验。

### FR-219 综合分析

系统应解析综合错误、资源摘要和版本间 QoR 变化。

### FR-220 时序分析

系统应解析关键路径和违例，并区分 RTL、约束及实现问题。

### FR-221 约束修改保护

False Path、Multicycle Path、时钟周期等约束修改必须单独获得用户批准。

### FR-222 持久化恢复

任务、Checkpoint、ToolRun、IssueQueue 和用户决策必须支持服务重启后恢复。

### FR-223 审计

系统应记录 Agent 推理摘要、Function Call、命令、结果、用户批准和文件变化。

### FR-224 失败隔离

单个 Agent 或 Tool 失败不得丢失已完成状态；用户可以从失败节点重试。

## 18. 非功能需求

### 18.1 安全

- 禁止模型生成任意命令后直接执行；
- Tool 使用命令模板和参数白名单；
- 路径必须解析并验证在 Workspace 内；
- API Key 和工具凭据不得进入模型上下文或日志；
- Tool 进程使用最小权限；
- 文件写入采用候选区和原子替换；
- 外部写入必须审批。

### 18.2 可恢复性

- 使用持久化任务数据库；
- LangGraph 使用持久化 Checkpointer；
- ToolRun 状态可在重启后重新查询；
- 已应用 Patch 和用户决策不可因重启丢失。

### 18.3 可观测性

- 每个请求、任务、Agent 调用、Function Call 和 ToolRun 具有唯一 ID；
- 记录耗时、Token、执行资源、退出码和失败分类；
- 前端可运行无费用系统自检；
- 用户可使用请求编号定位后端日志。

### 18.4 性能与成本

- 普通聊天不启动完整 Multi-Agent 流程；
- 日志先由确定性 Parser 过滤，再传递必要上下文给模型；
- 大日志按 Issue 和相关片段检索，不能整体送入模型；
- Tool 和模型调用需要预算及并发限制；
- 失败重试次数必须有上限。

### 18.5 可扩展性

- 不同 EDA 工具通过 Adapter 接入；
- 上层 Agent 不依赖具体日志格式；
- Tool Schema 版本化；
- 可扩展本机、WSL、Linux 和远程 Runner。

## 19. 技术与组件建议

### 19.1 核心组件

```text
Conversation Router
Orchestrator
Role-based Agents
Tool Registry
Workspace Manager
Artifact Registry
Execution Runner
EDA Adapters
Log Parsers
Issue Queue
Patch Service
Testcase Registry
Persistent Checkpointer
Memory and Traceability Service
```

### 19.2 持久化

第二阶段早期可使用 SQLite 完成单机持久化，数据模型稳定后迁移 PostgreSQL。文件和大体积工具产物保存在 Workspace/Artifact Storage，数据库保存索引、状态和关系。

### 19.3 首批工具建议

第一批只选择一套可在开发环境稳定运行的工具链：

- HDL：SystemVerilog；
- 编译/仿真/Lint：优先选择一个现有可用工具；
- 综合：在 Tool 框架稳定后接入；
- CDC 和 STA：在日志规范化、问题队列和审批闭环稳定后接入。

具体工具由项目实际环境决定，PRD 不绑定商业厂商。

## 20. 能力分区

### Phase 2A：持久化、Workspace 与 RTL

- Workspace Manager；
- Artifact Registry；
- 持久化 Task 和 Checkpoint；
- Tool Registry；
- 代码读取与搜索 Tool；
- RTL Agent 能力；
- RTL 候选和 Diff Review；
- 用户批准和安全应用 Patch。

验收结果：能够根据已确认 DS 生成 RTL 候选，并经过 Diff Review 应用到候选工作区。

### Phase 2B：Sim、Lint 与单问题闭环

- Execution Runner；
- 一个 Sim Adapter；
- 一个 Lint Adapter；
- 原始日志保存；
- Log Parser 和统一 Issue；
- Issue Queue；
- 单问题用户交互；
- 修改后最小复验。

验收结果：能够运行工具、发现错误、一次反馈一个问题，并完成用户控制的修改和复验。

### Phase 2C：已有用例执行

- Testcase Registry；
- 目录扫描和用例发现；
- 单用例、Suite 和 Regression Run；
- 失败用例与 Tool Issue 关联；
- 修改后的受影响用例选择。

验收结果：设计人员可以在指定目录发现并执行验证人员已有用例，不自动生成验证计划。

### Phase 2D：CDC、综合和 STA

- CDC Adapter 和专项 Review；
- 综合 Adapter 和 QoR 比较；
- STA Adapter 和关键路径分析；
- 约束修改审批；
- 跨阶段追踪报告。

## 21. 验收标准

### 21.1 Phase 2A 验收

1. 服务重启后任务和审批状态不丢失；
2. Tool 只能访问当前 Workspace；
3. Design Agent 能读取 DS 和相关 RTL；
4. 能生成至少一个 SystemVerilog 模块候选；
5. 能展示文件级和行级 Diff；
6. 未经批准不能修改工作文件；
7. 批准后能安全应用 Patch；
8. 修改记录能追溯到 DS 和用户任务。

### 21.2 Phase 2B 验收

1. 能检查并调用选定 Sim 和 Lint 工具；
2. 能保存命令、退出码和完整日志；
3. 能将预置错误解析为结构化 Issue；
4. 能对重复和级联错误去重；
5. 页面一次只展示一个待决策问题；
6. 能展示文件位置、原始日志、根因和候选 Diff；
7. 用户可选择修改、跳过或自定义修改；
8. Skipped 问题不会被标记为 Resolved；
9. 修改后能自动执行最小复验；
10. 复验失败能重新打开问题。

### 21.3 Phase 2C 验收

1. 能从约定目录发现已有用例；
2. 能按模块、标签和 Suite 筛选；
3. 设计人员可执行已有用例；
4. 执行结果包含 Pass、Fail、Blocked；
5. 失败用例进入相同单问题闭环；
6. 设计人员场景不会自动创建验证计划或用例；
7. Verification Agent 只有在明确验证任务时调用创建类 Tool。

### 21.4 Phase 2D 验收

1. 能解析至少一种 CDC 报告；
2. 能解析综合错误和资源摘要；
3. 能比较两个版本的 QoR；
4. 能解析 Setup/Hold 关键路径；
5. 时序建议能追溯到路径和 RTL；
6. 约束修改必须经过单独审批。

## 22. 产品评价指标

- RTL 候选编译通过率；
- 工具日志解析准确率；
- Error 去重率和根因排序准确率；
- Agent 建议被用户接受的比例；
- 修改后首次复验通过率；
- Issue 误关闭率；
- Skipped 风险保留率；
- 规格—DS—RTL—用例追踪覆盖率；
- 用户定位单个问题所需时间变化；
- 单个问题平均交互轮次；
- 工具执行成功率和超时率；
- 模型调用 Token 和成本；
- 服务重启后的任务恢复成功率。

## 23. 主要风险

| 风险 | 应对方式 |
|---|---|
| Agent 执行任意命令 | Tool 白名单、参数 Schema、受控 Runner |
| 修改范围超出目标模块 | Workspace Scope、Patch Scope 校验 |
| 日志过大导致模型成本和误判 | 确定性 Parser、按 Issue 检索相关片段 |
| 多个错误是同一根因的级联 | 指纹去重、根因排序、修改后重新解析 |
| Agent 建议修复了症状但破坏功能 | 规格追踪、独立 Review、最小复验和回归 |
| 用户跳过严重问题 | 保持 Skipped 状态并在最终报告展示风险 |
| 自动修改约束掩盖真实问题 | 约束修改单独审批 |
| 商业工具许可证不可用 | 环境预检、明确分类、支持远程 Runner |
| Tool 输出格式随版本变化 | Adapter/Parser 版本化和样例回归测试 |
| 长任务因服务重启丢失 | 持久化 ToolRun 和 Checkpoint |
| 设计人员误触发验证资产生成 | 角色权限和显式任务类型限制 |
| Agent 数量持续膨胀 | 稳定角色型 Agent + 可扩展 Tool/Skill |

## 24. 待确认事项

进入 Phase 2A 技术设计前需要确定：

1. 第一版是否只支持 SystemVerilog；
2. RTL 项目的标准目录结构；
3. DS 与 RTL 的追踪粒度；
4. 首个 Sim 和 Lint 工具；
5. 工具运行在 Windows、本机 WSL、Linux 服务器还是远程平台；
6. 现有验证用例的发现方式：目录规则、Manifest 或回归系统；
7. 用户批准后修改候选工作区还是当前工作区；
8. 是否要求 Git 仓库作为正式修改前提；
9. Tool 执行命令的审批粒度；
10. 商业工具许可证和环境变量管理方式；
11. 单个 ToolRun 的时间、资源和日志大小上限；
12. SQLite 到 PostgreSQL 的迁移时点；
13. 首批用于验收的 RTL、用例和预置错误集；
14. CDC、综合和 STA 报告的首批支持格式。

## 25. 产品结论

ChipFlow Agent 采用“角色型 Agent + Function Tool”的核心架构：

- Agent 对应真实的架构、设计、验证和评审职责；
- Tool 对应可执行、可复用、可审计的单项工程动作；
- Orchestrator 根据用户任务选择参与角色和工具；
- 工具结果先结构化，再由 Agent 进行工程解释；
- 所有正式修改由用户通过 Diff Review 控制；
- EDA 错误按照单问题队列逐个分析、决策和复验；
- 设计人员默认执行已有验证用例，不替代验证人员制定验证计划。

第二阶段优先完成持久化、Workspace、RTL 候选与 Sim/Lint 单问题闭环，再扩展已有用例自动执行、CDC、综合和 STA。
