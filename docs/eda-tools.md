# 多文件 RTL 工程工具

| 类别 | 仓库盘点与本轮改造 |
| --- | --- |
| 直接复用 | Global Planner 契约校验、Task Executor、Local ReAct、角色隔离、Tool Registry、用例发现、DS/RTL 候选、Patch/Diff 人工审批、ToolRun 和 EngineeringIssue。 |
| 扩展 | Workspace 的 `read_rtl_source` 支持受控的工程文件预算。原单文件 `verilator_lint` 继续可用，多文件 `run_lint` 复用 Verilator 命令选项、工具探测、审批和 ToolRun 恢复。Issue 解析同时处理编译诊断和仿真 `%Fatal`。 |
| 新增 | 冻结工程配置、模块/信号索引、真实编译和仿真、测试套件、日志与波形、工程报告。加入基于失败证据的候选 Patch，以及绑定已应用 Patch 的重测、完整回归、受影响用例和真实覆盖率。 |
| 独立 Mock | 旧聊天入口的 `run_mock_eda` 保持原语义。工程入口的验收使用真实 ToolRun 和产物。 |
| 后续 | Verible、Yosys/OpenSTA、Formal、CDC/RDC、OpenROAD 和 PPA 适配器待接入。公共结果和执行接口可供这些工具复用。 |

实施顺序是先冻结清单和实际编译/Lint，再接入仿真、日志、波形，最后用已有 Patch 审批连接修复重测。工程入口根据用户列出的操作生成 DAG，由现有 Global Planner 校验。每个节点的工具选择、Reflection、提交和验收仍由原编排链路完成。

## 工程清单

将 `chipdesign.json` 放在同项目的 Workspace。路径以 Workspace 根为基准。Filelist 支持 HDL 路径、`+incdir+` 和 `+define+`。列在前面的 Package 会先传给 Verilator。Include 必须是已冻结的字面路径。

```json
{
  "top_module": "uart_top",
  "filelists": ["rtl.f"],
  "include_dirs": ["include"],
  "defines": ["UART_RESET=1"],
  "testcases": {
    "uart_reset": {
      "top_module": "uart_reset_tb",
      "sources": ["dv/uart_reset_tb.sv"],
      "depends_on": ["rtl/uart_tx.sv", "rtl/uart_top.sv", "include/uart_width.svh"]
    }
  }
}
```

可以直接声明 `sources`。工具限制文件数、单文件和总输入字节数，拒绝任意 Filelist 参数、路径穿越和符号链接。模块层级与信号引用是词法索引，引用包含文件、行、列和类别。条件宏、generate 展开和精确类型绑定仍需 elaboration 支持。

## 启用和调用

启动时设置 `CHIP_AGENT_HIERARCHICAL_ENABLED=1` 和 `CHIP_AGENT_VERILATOR_ENABLED=1`。可通过 `CHIP_AGENT_VERILATOR_PATH` 指定可执行文件。仿真还需要支持 C++20 的 `g++` 和 Verilator runtime headers。便携部署可设置 `VERILATOR_ROOT`。也可以在 WSL2/Linux 中启动后端，使用该环境的工具链。工具不可用时，规划返回能力缺口。

向 `POST /api/v1/chat` 发送：

```json
{
  "project_id": "demo",
  "module_name": "uart_top",
  "message": "编译、检查并验证 UART 复位行为",
  "orchestration_mode": "eda_project",
  "workspace_id": "<workspace-id>",
  "eda_operations": ["run_compile", "run_lint", "run_testcase", "parse_eda_log", "get_waveform_artifact", "analyze_signal_trace"],
  "eda_testcases": ["uart_reset"],
  "eda_seed": 42,
  "eda_signal": "uart_reset_tb.tx",
  "eda_clock": "uart_reset_tb.clk"
}
```

工程清单和报告节点自动加入。默认最多选六项操作。可以用 `GET /api/v1/tools?role=design&task=true` 查询当前可用能力。可以单独请求 `get_module_hierarchy` 和 `find_signal_references`。波形操作绑定同一个 Run 中的仿真产物，`eda_wave_testcase` 指定要读取的已选用例。`eda_ready` 可用于在时钟上升沿统计握手，时间单位为 VCD tick。

每项外部执行都有独立审批。使用原有 Run resume 接口提交 `action: "approve"`、`wait_id` 和稳定的 `client_request_id`。审批关联冻结工程 Hash、Verilator 标识、顶层、用例和 Seed。执行使用固定参数列表、独立工作目录、日志与临时输出预算和超时。恢复时复用已登记的 ToolRun。

`run_compile` 使用 Verilator 解析、展开并生成 RTL 编译模型。`run_lint` 执行静态检查。`run_simulation`、`run_testcase`、`run_test_suite` 构建并运行已在清单登记的 SystemVerilog Testbench。用例通过独立一行 `CHIPFLOW_TEST_PASS` 表明断言已完成，并用 `$dumpfile("wave.vcd")` 与 `$dumpvars` 输出波形。退出码、断言和测试标记共同判定 PASS、FAIL、ERROR 或 TIMEOUT。Cocotb/Python 用例和 FST 尚未接入。

最终 Run 的 `result.eda_tool_runs` 给出各次真实操作状态。`GET /api/v1/tool-runs/{id}/eda-result` 返回版本、输入 Hash、命令、日志路径、用例、Seed、Issue 和波形引用。VCD 可从 `GET /api/v1/tool-runs/{id}/artifacts/{name}` 获取。原始日志保存在 Workspace 的 `logs/`。实际失败可作为诊断证据进入报告，Run 保留失败状态。

加入 `get_coverage_summary` 时，仿真会实际启用 Verilator 覆盖率插桩并保存覆盖率文件。汇总按用例给出覆盖点总数、命中数和百分比。无可用覆盖率的用例单独列出。

## 候选 Patch 与重测

由 Design 角色指定 `propose_eda_patch` 和 `eda_repair_path`，候选任务绑定真实 ToolRun 失败和带行号的 Issue，最多尝试三次动作。Patch 保持 `patch_ready`，通过已有 Patch/Diff 接口审核和应用。

批准并应用后，发起新的工程 Run，传入 `eda_applied_patch_id`。系统核对冻结文件是否包含已经应用的补丁。选择 `run_testcase` 重测单个用例，或用 `run_regression` 执行显式选择的完整清单套件。`run_impacted_tests` 要求选择集合与清单依赖计算一致，缺少依赖声明的用例保守纳入。报告引用 Patch ID 和新 Hash。失败后的自动多轮重规划尚未接入。

## 集成测试

测试工程在 `tests/fixtures/uart_project`。设置 `CHIP_AGENT_TEST_REAL_VERILATOR` 和相应的 `VERILATOR_ROOT`，运行 `python -m pytest tests/test_eda_project.py`。测试覆盖实际多文件编译/Lint、成功和失败用例、失败波形、候选 Patch 审批和重新执行。编排使用确定性模型响应，工程结果来自实际子进程。
