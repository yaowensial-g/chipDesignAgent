# ChipFlow v0.5 自验证报告

日期：2026-10-28

## 1. 本轮目标

本轮采用渐进式重构，不重写已经验证的 DS、RTL、Patch、ToolRun 和 Issue 流程，集中验证三项架构决策：

1. 用户工作角色决定 Design 或 Verification 主 Agent；
2. 跨专业协作使用可约束、可持久化的 Delegate；
3. Tool 先按主 Agent 权限过滤，再由轻量 LLM 选择有限候选，主 Agent 只接收候选完整 Schema。

## 2. 重构前代码分析结论

| 观察 | 风险 | v0.5 处理 |
|---|---|---|
| 顶层已有统一 Orchestrator 和 Tool Registry | 可复用，不应推翻 | 保留为确定性控制平面 |
| Design/Verification 主要通过 `consult_*` 被调用 | 真实生产角色没有主责 | 增加会话角色与 `primary_agent` |
| 角色咨询是一次 Runtime 调用 | 无任务边界、状态、超时和审计 | 增加 `DelegationTask` 与 Service |
| Planner 接收全量 Tool Schema | Tool 增长后上下文和误选风险上升 | 增加权限过滤和 Metadata-only Selector |
| `session_id` 只用于关联 Run | 无法保存用户工作角色 | 增加 `conversations` 表 |
| Run/DS/Patch/ToolRun 已形成稳定证据链 | 全量重写风险高 | 继续关联既有专业对象 |

## 3. 静态与自动化验证

### 3.1 Python 编译

命令：

```powershell
.\.venv\Scripts\python.exe -m compileall -q app tests
```

结果：通过。

### 3.2 后端测试

命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -q --tb=short --basetemp data\pytest-v05-final
```

结果：**59 passed**。

原有 51 项测试全部保留通过；新增 8 项专项测试位于 `tests/test_role_based_runtime.py`：

- 工作角色持久化并选择正确主 Agent；
- Tool Catalog 在规划前按角色过滤；
- Selector 轨迹中不存在越权 Tool，候选数量受限；
- 即使 Selector 恶意或错误返回越权名称，后端仍会丢弃；
- Verification 主 Agent 能委派 Design 专家，结果持久化并可查询；
- 同一 Session 切换工作角色后主 Agent 变化并记录审计事件。
- Session 不能跨项目复用，冲突返回明确的 HTTP 409；
- Architecture Expert 不能发起嵌套 Delegate。

测试期间只有 Windows 中文路径下 `.pytest_cache` 无法创建的 Pytest warning；测试缓存已通过 `--basetemp data/...` 隔离，该 warning 不影响测试结果和产品运行。

### 3.3 前端生产构建

命令：

```powershell
cd frontend
npm run build
```

结果：TypeScript 与 Vite 构建通过，生成 v0.5.0 生产 Bundle。

### 3.4 Git 差异检查

命令：

```powershell
git diff --check
```

结果：无空白错误。Windows 环境提示 LF 将按仓库规则转换为 CRLF，属于行尾提示，不是差异错误。

## 4. 功能验收矩阵

| 验收项 | 状态 | 证据 |
|---|---|---|
| 设计用户由 Design Agent 主导 | 通过 | Conversation/Run `primary_agent=design` |
| 验证用户由 Verification Agent 主导 | 通过 | 专项 API 测试 |
| 验证角色无法获得 DS/RTL 写入 Tool | 通过 | `/tools?role=verification` 测试 |
| Selector 只读取授权 Tool Metadata | 通过 | `ToolSelectorService` + trace |
| 主 Agent 只读取候选 Tool Schema | 通过 | Orchestrator shortlist 传参 |
| Selector 异常有安全回退 | 通过 | 本地校验、基础 Tool 回退代码路径 |
| 执行前再次检查 enabled 与角色权限 | 通过 | Orchestrator Policy 复核 |
| Delegate 有专家白名单 | 通过 | `ALLOWED_EXPERTS` |
| Delegate 有次数和超时限制 | 通过 | 每 Run 3 次、单次 90 秒 |
| Delegate 不继承完整私有历史 | 通过 | `_scoped_context(history=[])` |
| Delegate 禁止嵌套并默认只读 | 通过 | Delegation constraints |
| Delegate 结果可查询 | 通过 | `/runs/{run_id}/delegations` |
| 角色切换隔离旧角色聊天历史 | 通过 | 后端首轮清空 + 前端切换清空 |
| 原有 DS/RTL/Lint/恢复流程兼容 | 通过 | 原有 51 项测试 |

## 5. 未覆盖与诚实边界

- 没有使用真实付费模型执行测试，自动化测试使用 Deterministic Runtime；
- Provider 尚未回传 token usage，因此 Selector 观测暂不包含准确 token 与成本；
- Delegate 是同进程异步调用，还没有独立队列、Worker、重试退避或分布式 Trace；
- 当前 `user_role` 是本地工作模式，不是企业认证后的组织权限；
- Sim、CDC、综合、STA 仍未接入真实 Tool Adapter；
- 角色切换保留共享文档和持久化制品，但没有细粒度字段级上下文迁移策略；
- DS 内部继续复用 LangGraph 兼容子流程，本轮没有为了形式统一而重写稳定流程。

## 6. 结论

v0.5 已完成三项核心架构决策的可运行实现，并通过原有回归与新增专项测试。它不是一次目录层面的“换名重构”：主 Agent 身份进入 Conversation、Run、Tool 权限、Planner Prompt 和事件；Delegate 进入独立数据模型和持久化表；Tool Selector 实际改变了发送给主 Agent 的 Schema 集合。

下一阶段建议优先做真实模型对照评估与可观测性：统计 Selector 候选召回率、主 Agent Tool 选择准确率、Delegate 接受率、token/延迟/成本和权限拒绝次数，再决定是否为不同角色拆分模型或部署进程。
