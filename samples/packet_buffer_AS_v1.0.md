# Packet Buffer 架构方案（AS）

## 1. 文档信息

- 文档类型：Architecture Specification
- 模块名称：`packet_buffer`
- 版本：1.0
- 对应 FS：`packet_buffer_FS_v1.0.md`
- 状态：示例基线

## 2. 总体架构

模块采用单时钟同步环形缓存结构。数据载荷写入数据 RAM；每拍的 `keep/sop/eop` 信息写入伴随元数据 RAM。写指针跟踪下一可写位置，读指针跟踪下一可输出位置。

为了保证未完成报文不可见，架构使用“完整报文计数”作为读侧门控条件。只有 `packet_count > 0` 时，读控制器才能产生 `out_valid`。输入侧在接收到 `in_eop` 后增加完整报文计数；输出侧在成功发送 `out_eop` 后减少该计数。

不设置组合旁路，所有输入拍均先写入缓存。输入报文末拍成功写入后，最早在下一周期开始输出该报文。

## 3. 子模块划分

SUBMODULE: write_controller

### 3.1 `write_controller`

职责：

- 产生 `in_ready` 和 RAM 写使能；
- 维护写指针 `wr_ptr`；
- 跟踪当前是否位于一个报文内部；
- 检查 SOP/EOP 和 Keep 格式；
- 累积当前报文的错误状态；
- 在末拍完成时产生 `packet_commit` 脉冲。

关键状态：

- `IDLE`：等待带 SOP 的首拍；
- `IN_PACKET`：已经接收首拍，等待 EOP；

单拍报文在 `IDLE` 状态完成一次接收后仍返回 `IDLE`。格式错误不会丢弃数据，仅设置粘滞错误标志和报文错误元数据。

SUBMODULE: storage_array

### 3.2 `storage_array`

职责：

- 保存 `in_data`；
- 保存逐拍元数据 `in_keep/in_sop/in_eop`；
- 保存逐报文错误传播所需的信息；
- 支持同周期一次写和一次读。

建议实现：

- `data_mem[DEPTH]`，元素位宽为 `DATA_WIDTH`；
- `meta_mem[DEPTH]`，元素包含 `keep/sop/eop/error`；
- 综合目标允许时推断简单双口 RAM；
- 读延迟采用一周期同步读，输出侧增加一级保持寄存器。

报文错误信息在末拍接收时已经包含此前各拍错误的 OR 结果。为了使每个输出拍都能携带整包错误状态，设计应维护一个读侧当前报文错误寄存器；首拍装载该报文错误，直到末拍完成后清除。具体错误元数据组织由 DS 确定。

SUBMODULE: read_controller

### 3.3 `read_controller`

职责：

- 根据 `packet_available` 发起 RAM 读取；
- 维护读指针 `rd_ptr`；
- 管理同步 RAM 读延迟和一级输出保持寄存器；
- 保证下游反压时输出稳定；
- 在末拍成功发送时产生 `packet_release` 脉冲。

读控制器只有在完整报文计数非零时才能读取新报文。输出保持寄存器为空，且存在完整报文时，可以发起下一拍读取；保持寄存器被消费的同周期允许装入下一拍，实现连续每周期一拍吞吐。

SUBMODULE: occupancy_manager

### 3.4 `occupancy_manager`

职责：

- 维护已占用拍数 `used_count`；
- 维护完整报文数 `packet_count`；
- 产生 `empty/full/packet_available`；
- 处理同周期读写和报文提交/释放；
- 防止计数上溢和下溢。

`used_count` 更新规则：

| 有效写 | 有效读 | 更新 |
|---:|---:|---|
| 0 | 0 | 保持 |
| 1 | 0 | +1 |
| 0 | 1 | -1 |
| 1 | 1 | 保持 |

`packet_count` 更新规则：

| `packet_commit` | `packet_release` | 更新 |
|---:|---:|---|
| 0 | 0 | 保持 |
| 1 | 0 | +1 |
| 0 | 1 | -1 |
| 1 | 1 | 保持 |

SUBMODULE: flush_reset_control

### 3.5 `flush_reset_control`

职责：

- 统一生成内部初始化和清空控制；
- 执行复位优先级；
- 阻止 Flush 周期的输入和输出握手；
- 清除指针、计数、状态机、保持寄存器和粘滞错误。

优先级定义为：`rst_n=0` > `flush=1` > 正常读写。

## 4. 顶层接口

顶层接口信号、方向和位宽完全继承 FS 第 5～7 章，不在 AS 中重新定义不同语义。

握手事件定义：

```text
write_fire = in_valid && in_ready
read_fire  = out_valid && out_ready
packet_commit  = write_fire && in_eop
packet_release = read_fire && out_eop
```

## 5. 内部接口

| 源 | 目的 | 信号 | 说明 |
|---|---|---|---|
| write_controller | storage_array | `mem_wr_en` | 数据和元数据写使能 |
| write_controller | storage_array | `mem_wr_addr` | 当前写地址 |
| write_controller | occupancy_manager | `write_fire` | 已成功写入一拍 |
| write_controller | occupancy_manager | `packet_commit` | 完整报文提交 |
| occupancy_manager | write_controller | `full` | 无可写空间 |
| read_controller | storage_array | `mem_rd_en` | 发起同步读 |
| read_controller | storage_array | `mem_rd_addr` | 当前读地址 |
| read_controller | occupancy_manager | `read_fire` | 已成功输出一拍 |
| read_controller | occupancy_manager | `packet_release` | 完整报文释放 |
| occupancy_manager | read_controller | `packet_available` | 至少有一个完整报文 |
| flush_reset_control | all | `clear_state` | 同步清空内部状态 |

## 6. 指针和容量方案

- `wr_ptr` 和 `rd_ptr` 的位宽为 `log2(DEPTH)`；
- 指针自然回卷；
- 满空判断以 `used_count` 为准，不直接比较指针；
- 可用容量为 `DEPTH-1`，始终保留一个位置满足 FS 的预留要求；
- `full = (used_count == DEPTH-1)`；
- `empty = (used_count == 0)`；
- `packet_available = (packet_count != 0)`。

DS 需要明确参数计算的 SystemVerilog 写法，以及非法参数的 elaboration-time 检查。

## 7. 输出流水和反压

同步 RAM 读数据经过一级输出保持寄存器：

1. 当保持寄存器为空且 `packet_available=1` 时发起 RAM 读；
2. 下一周期将 RAM 返回数据装入保持寄存器并拉高 `out_valid`；
3. `out_valid && !out_ready` 时保持寄存器禁止更新；
4. `out_valid && out_ready` 时消费当前拍；
5. 如果当前拍不是 EOP 且下一拍已预取，可无气泡连续输出；
6. 如果当前拍是 EOP，只有 `packet_count` 中仍存在下一完整报文时才继续预取。

DS 需要详细解决 RAM 读请求、返回数据和保持寄存器三者之间的有效位控制。

## 8. 格式检查方案

`write_controller` 实现以下检查：

- `IDLE` 接收到没有 SOP 的有效拍；
- `IN_PACKET` 接收到新的 SOP；
- 非 EOP 拍的 Keep 不是全 1；
- EOP 拍的 Keep 为 0；
- EOP 拍的 Keep 不是低位连续 1。

任一检查失败后：

- 设置 `format_error` 粘滞状态；
- 设置当前报文错误累积位；
- 继续接收和保存数据；
- 当前报文输出期间 `out_error=1`。

## 9. Reset 和 Flush

异步复位断言时立即清除所有可见有效状态。复位释放经过两级同步器后产生内部 `reset_done`。`reset_done=0` 时输入和输出握手均被禁止。

Flush 为同步控制。检测到 Flush 的周期：

- 强制 `in_ready=0` 和 `out_valid=0`；
- 不产生 RAM 读写使能；
- 清零指针、计数、状态机、输出保持有效位和错误状态；
- 不要求擦除 RAM 内容，因为所有有效性状态已经清除。

## 10. 关键时序场景

### 10.1 单拍报文

周期 N 接收 `SOP=1,EOP=1`；周期 N+1 后 `packet_available=1`，读控制器开始取数；考虑同步 RAM 延迟后产生输出。

### 10.2 输入输出并发

当缓存既非满也存在完整报文时，输入和输出可以在同一周期握手。`used_count` 保持不变，两个指针分别前进。

### 10.3 报文提交与释放并发

一个新报文末拍写入与旧报文末拍输出可以同周期发生。`packet_count` 保持不变，不得产生瞬时的错误空状态。

### 10.4 下游反压

当 `out_valid=1,out_ready=0` 时，输出保持寄存器、`out_data/out_keep/out_sop/out_eop/out_error` 均保持不变，读指针不前进。

## 11. 设计约束

- 仅支持单时钟域；
- 不允许从 `out_ready` 到 `in_ready` 形成跨模块长组合路径；
- RAM 接口应便于综合工具推断存储宏；
- 控制状态应提供断言可观察点；
- 所有计数变化必须由明确的握手事件驱动；
- 架构 Agent 不得自行改变 FS 接口语义。

## 12. DS 应进一步展开的内容

DS 至少需要用文字详细说明：

1. 每个子模块的寄存器、组合逻辑和状态转移；
2. 同步 RAM 预取及输出保持寄存器的无气泡控制；
3. 整包错误状态如何与报文数据关联；
4. 指针位宽、计数位宽及参数合法性检查；
5. Reset 同步释放电路；
6. Flush 与并发握手的优先级实现；
7. 关键断言和验证覆盖点；
8. 需要用户自行补充的关键电路图和时序图清单。

AI 只输出关键电路的文字描述，不自动生成正式电路图。
