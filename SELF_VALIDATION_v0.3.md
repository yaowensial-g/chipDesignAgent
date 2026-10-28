# ChipFlow v0.3 自验证报告

日期：2026-10-28

## 1. 本次范围

- Conversation 主入口与统一 Run 控制面；
- DS Task、RTL Patch、ToolRun 与 Run 的关联；
- Run 查询、事件、恢复和取消；
- 聊天编排 DS、RTL、Lint、已有用例、Sim/CDC/综合/STA；
- Skill Catalog 与架构/设计/评审 Skill；
- React Run 状态展示和 RTL Patch 审批；
- v0.3 文档同步。

## 2. 自动化验证

### Python 测试

```text
47 passed
```

覆盖原有 DS、模型配置、文档抽取、Workspace、Patch、ToolRun 和 Issue 流程，并新增：

- DS 聊天任务创建统一 Run；
- Conversation Path Session；
- RTL 聊天任务创建 Patch，批准前不写文件；
- Run 审批后应用 RTL；
- Lint Run 关联 ToolRun 并同步终态；
- 未注册综合 Adapter 时返回明确阻塞状态；
- 取消 DS Run 同步取消专业 Task；
- 项目 Skill 文件安全加载。

### Python 编译检查

```powershell
.\.venv\Scripts\python.exe -m compileall -q app tests
```

结果：通过。

### 前端生产构建

```powershell
cd frontend
npm run build
```

结果：TypeScript 构建和 Vite Production Build 通过。

## 3. 已验证的安全边界

- 普通问答不会创建 Run；
- 文档上传不会自动启动任务；
- 缺少 AS 的 DS Run 停在 `waiting_for_user`；
- 缺少已确认 DS 的 RTL Run 不生成 RTL；
- RTL Candidate 在审批前不写目标文件；
- Tool 仍执行角色权限、Workspace 和批准检查；
- Sim、CDC、综合和 STA 未注册 Adapter 时不会伪造成功；
- Skill 只提供 Prompt 规则，不拥有文件或进程权限；
- 测试强制使用 Deterministic Runtime，不消耗真实模型额度。

## 4. 已知边界

- 多目标请求尚未拆为 Run DAG；
- 浏览器刷新后不会自动恢复最近 Conversation/Run；
- 正在运行的 ToolRun 不支持真正的进程取消，取消接口会拒绝并保持状态；
- Sim、CDC、综合和 STA 仍需真实 Adapter；
- `test_execution` 当前只发现已有用例，尚未启动具体验证框架；
- Issue 到自动候选修复和最小复验仍未闭环；
- Skill 尚未包含版本元数据和独立效果评估集。
