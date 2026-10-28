# ChipFlow v0.2 自验证报告

## 验证信息

| 项目 | 结果 |
|---|---|
| 验证日期 | 2026-10-27 |
| 目标环境 | Windows |
| 后端版本 | 0.2.0 |
| 前端版本 | 0.2.0 |
| 自动化测试 | 40 passed |
| Python compileall | 通过 |
| TypeScript 检查 | 通过 |
| Vite 生产构建 | 通过 |
| 页面级检查 | 通过 |
| OpenAPI Path 数 | 22 |

## 验证命令

```powershell
.\.venv\Scripts\python.exe -m pytest -q
.\.venv\Scripts\python.exe -m compileall -q app
npm --prefix frontend run build
```

测试环境通过 `tests/conftest.py` 强制关闭真实模型，不会读取用户已启用的模型状态，也不会产生 API 费用。

## 已验证功能

### v0.1 回归

- 普通聊天不需要上传文档；
- 明确 DS 请求才进入 LangGraph；
- AS 缺失校验；
- DS 生成、默认/自定义评审；
- Agent 禁用及质量警告；
- 规格冲突、等待用户、恢复和取消；
- PDF/DOCX/Markdown/TXT 抽取；
- 模型配置、DeepSeek/OpenAI Key 隔离和 DPAPI；
- 模型 JSON 提取与一次格式修复；
- 结构化错误和请求编号。

### v0.2 工程能力

- Workspace 创建和安全路径解析；
- `../` 路径逃逸被拒绝；
- Artifact 版本和内容 Hash；
- RTL 生成只创建 Candidate/Patch，不直接落盘；
- Patch 审批前目标不存在或保持原状；
- 用户批准后原子写入；
- Review 期间目标变化时拒绝覆盖；
- Tool 角色权限和显式批准；
- Verilator 缺失/参数错误形成环境 Issue；
- Tool stdout/stderr 写入 Workspace 日志；
- Verilator 日志解析和指纹去重；
- Issue 根因、影响、建议、置信度和复验范围；
- 一次一个 Active Issue；
- SQLite Task 快照查询；
- 等待用户的 Task 可由新 TaskService 实例恢复并继续。

## 页面验收

在 `http://127.0.0.1:5173` 验证：

- 顶部“工程工作台”入口可点击；
- 未完成 DS 时 RTL 按钮正确禁用并显示原因；
- 工程工作台显示 RTL 目标路径和 Lint 参数；
- Tool Registry 显示 `discover_testcases` 和 `verilator_lint`；
- 系统自检显示后端版本 0.2.0；
- SQLite 和 Tool Registry 检查通过；
- 当前机器没有 Verilator 时显示明确 warning，而不是成功状态。

## 已知未完成项

以下内容在 PRD v0.2 中定义，但当前实现尚未闭环：

1. 商业 Sim、CDC、综合和 STA Adapter；
2. ToolRun 取消及崩溃后的运行中进程恢复；
3. Agent 自动根据 Issue 生成候选修复 Patch；
4. Patch 应用后的自动最小复验；
5. `resolved/reopened` 自动状态推进；
6. 自然语言 Orchestrator 对 RTL/工具任务的统一动态 Function Call；
7. Specialist Reviewer 的自动选择和执行；
8. 问题单、Git、覆盖率数据库集成；
9. FS/AS/ICD 的受控变更和下游推论失效传播；
10. 长期记忆、Reflection、检索和遗忘；
11. 持久化 LangGraph Checkpointer；
12. 多用户登录、项目权限和企业 Secret Manager。

因此 v0.2 当前状态应描述为“第二阶段工程闭环骨架 + RTL Diff + Verilator Lint 单问题队列”，不能描述为完整支持所有 EDA 流程。
