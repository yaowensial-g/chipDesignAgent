# ChipFlow v0.4 自验证报告

日期：2026-10-28

## 验证范围

- 所有用户输入进入统一 Orchestrator；
- Chat 作为 `reply_to_user` Tool 执行并产生 Run；
- 用户输入被整理为可审计的 `TaskIntent`；
- 关键缺失信息触发 `ask_user`，不会补造工程事实；
- Planner 可在角色咨询后依据 Observation 重新规划；
- DeepSeek Chat Completions 与 OpenAI Responses Function Calling 结果归一化；
- Tool Catalog 暴露适用/禁用条件、JSON Schema、前置条件、副作用和审批元数据；
- DS、RTL、Lint、已有用例发现及能力缺口仍可通过统一 Run 执行；
- DS Resume、RTL Patch 审批、ToolRun 状态刷新和取消语义保持兼容；
- React 前端支持新的 `orchestrated` 路由标签。
- 总体架构 Markdown、100 道面试题和 PDF 均同步到 v0.4 设计基线。

## 自动测试结果

```text
51 passed
```

执行命令：

```powershell
.\.venv\Scripts\python.exe -m pytest -q --basetemp data\pytest-final-delivery
npm run build --prefix frontend
git diff --check
```

前端生产构建成功，Vite 完成 1746 个模块转换。`git diff --check` 通过。

文档与 PDF 检查结果：

- 面试题标题计数为 100，未丢题或重复编号；
- 架构 PDF 共 15 页，页面尺寸统一为 A4（595.3 × 841.9 pt）；
- PDF 元数据标题为 `ChipFlow Architecture Overview v0.4`；
- 文本提取未发现 Unicode 替换字符，也没有异常短页；
- 15 页全部渲染为 PNG 进行联系表检查；系统上下文已拆成入口边界、统一编排、Tool 执行边界和依赖表，控制面、信任边界与 Windows 部署图均完整可读；
- PDF 中未残留旧版 `Router` 架构表述。

测试环境强制设置 `CHIP_AGENT_MODEL_ENABLED=false`，不会读取开发者保存的 API Key 或消耗模型额度。Provider 的两种原生 Tool Calling 协议使用模拟 SDK 返回值进行单元测试。

## 已验证场景

1. 普通问答由 `reply_to_user` 完成，RunType 为 `chat`；
2. “帮我检查一下 scheduler”形成 `TaskIntent.missing_information`，调用 `ask_user` 并暂停；
3. DS 请求选择 `create_ds_candidate`，保留专业 Task 和评审结果；
4. RTL 请求优先选择 `create_rtl_candidate`，生成 Patch 并等待用户审批；
5. Lint 请求选择 `verilator_lint`，沿用 Workspace、授权、异步 ToolRun 和 Issue 处理；
6. 仿真、CDC、综合、STA 在 Adapter 缺失时选择 `report_capability_gap`；
7. `/api/v1/tools` 同时返回编排 Tool 和工程 Tool 的完整规划元数据；
8. DeepSeek 风格 `tool_calls` 与 Responses `function_call` 均被归一化；
9. 模型设置、DPAPI Key、任务快照、Patch 和工程平台回归测试通过。

## 已知边界

- 当前 Planner 一次选择一个 Tool，最多六步，尚未生成显式 Run DAG；
- 当前 Tool 数量较少，完整 Catalog 会发送给 Planner；工具增多后应加入能力检索和角色过滤；
- DS 内部仍复用 LangGraph 兼容 Handler，尚未完全拆成细粒度 Tool；
- 通用 `ask_user` 恢复后的自动重新规划尚未实现；DS/RTL/Tool 的专业恢复路径仍可用；
- 商业仿真、CDC、综合和 STA Adapter 尚未接入；
- Pytest 在当前 Windows 环境无法写默认 `.pytest_cache`，因此显示一个缓存警告；测试临时目录已显式放到项目 `data/` 下，不影响结果。
