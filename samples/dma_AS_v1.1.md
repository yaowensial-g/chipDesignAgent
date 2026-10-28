# DMA Controller 架构方案（AS）

## 1. 文档信息

- 模块：`dma_ctrl`
- 版本：1.1
- 对应 FS：`dma_FS_v1.1.md`
- 对应 ICD：`dma_ICD_v1.1.md`

## 2. 总体架构

`dma_ctrl` 由命令队列、命令调度器、传输分段器、事务执行控制器和完成上报单元组成。命令严格按接收顺序进入队列；系统只允许一条活动命令，因此完成顺序天然与接收顺序一致。

SUBMODULE: command_queue

## 3. `command_queue`

职责：

- 在 `cmd_valid && cmd_ready` 时锁存完整命令字段；
- 提供深度为 8 的同步 FIFO；
- 输出队首命令和 `queue_not_empty`；
- 在 scheduler 发出 `queue_pop` 时弹出队首命令；
- 产生 `queue_full`，顶层据此生成 `cmd_ready`。

内部接口：

| 信号 | 方向 | 说明 |
|---|---|---|
| `queue_push` | input | 等于命令通道有效握手 |
| `queue_push_data` | input | 完整 DMA 命令字段 |
| `queue_full` | output | FIFO 已满 |
| `queue_head_valid` | output | 队首命令有效 |
| `queue_head_data` | output | 队首命令字段 |
| `queue_pop` | input | scheduler 接收队首命令 |

同周期 Push 和 Pop 合法，队列占用量保持不变。队列不得仅因 `cmd_ready` 为高而写入数据。

SUBMODULE: scheduler

## 4. `scheduler`

职责：

- 在 `queue_head_valid=1` 且没有活动命令时产生 `queue_pop`；
- 锁存队首命令为活动命令；
- 检查地址对齐和长度范围；
- 合法命令交给 transfer_segmenter；
- 非法命令直接请求 completion_unit 产生 `INVALID_COMMAND`；
- 当前命令完成上报被 completion_unit 接收后释放活动命令。

调度条件明确为：

```text
schedule_fire = queue_head_valid && !active_command_valid && scheduler_ready
queue_pop     = schedule_fire
```

`scheduler_ready` 表示内部活动命令寄存器可以接收命令，与顶层 `cmd_ready` 无直接握手关系。

SUBMODULE: transfer_segmenter

## 5. `transfer_segmenter`

职责：

- 保存当前源地址、目的地址和剩余字节数；
- 生成最大 256 Byte 的事务描述；
- 每次事务成功后递增地址并递减剩余长度；
- 剩余长度归零时产生 `command_transfer_done`；
- 事务失败时停止分段并传递错误类型。

SUBMODULE: transaction_controller

## 6. `transaction_controller`

职责：

- 接收分段后的事务描述；
- 依次发起读事务和写事务；
- 在外部响应到达前保持请求字段稳定；
- 将读错误映射为 `READ_ERROR`；
- 将写错误映射为 `WRITE_ERROR`；
- 每次只允许一个未完成分段事务。

外部读写请求接口在本示例中作为抽象接口保留，DS 必须根据实际 SoC 总线协议展开信号级定义；不得自行假定 AXI、AHB 或其他协议。

SUBMODULE: completion_unit

## 7. `completion_unit`

职责：

- 接收成功、读错误、写错误或非法命令结果；
- 锁存 `completion_id/status/bytes_transferred`；
- 拉高 `completion_valid`，直到 `completion_ready=1`；
- 在反压期间保持全部完成字段稳定；
- 握手后向 scheduler 产生 `completion_accepted`；
- 当原命令 `irq_enable=1` 时，在完成握手后产生一个周期 `irq_pulse`。

完成上报内部接口：

| 信号 | 方向 | 说明 |
|---|---|---|
| `result_valid` | input | scheduler/transaction_controller 提交结果 |
| `result_id` | input | 原命令 ID |
| `result_status` | input | FS 定义的完成状态 |
| `result_bytes` | input | 实际成功搬运字节数 |
| `result_irq_enable` | input | 原命令中断使能 |
| `result_ready` | output | completion_unit 可接收结果 |
| `completion_accepted` | output | 外部完成握手成功 |

## 8. 子模块连接顺序

```text
Command Interface
  -> command_queue
  -> scheduler
  -> transfer_segmenter
  -> transaction_controller
  -> completion_unit
  -> Completion Interface
```

控制返回路径：

- `command_queue.queue_full` 控制顶层 `cmd_ready`；
- `transaction_controller` 将事务结果返回 scheduler；
- `completion_unit.completion_accepted` 允许 scheduler 释放活动命令并调度下一条。

## 9. Clock、Reset 和状态

- 所有子模块工作在 `clk` 时钟域；
- `rst_n` 异步断言、同步释放；
- Reset 清除 FIFO 指针、活动命令、分段状态、事务状态和完成有效位；
- Reset 优先级高于全部正常状态更新；
- 状态寄存器只能由相应握手事件推进。

## 10. DS 展开要求

DS 需要进一步说明：

1. command_queue 指针、计数及同时 Push/Pop 逻辑；
2. scheduler 状态机和活动命令寄存器；
3. 分段长度、地址和已搬运字节数计算；
4. transaction_controller 与实际 SoC 总线适配方案；
5. completion_unit 的保持寄存器和反压行为；
6. 各状态机的异常路径、断言和覆盖点。
