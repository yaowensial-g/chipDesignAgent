# DMA Controller 接口控制文档（ICD）

## 1. 文档信息

- 模块：`dma_ctrl`
- 版本：1.1
- 时钟域：`clk`
- 复位：`rst_n`，低有效异步断言、同步释放

## 2. 命令通道

| 信号 | 方向 | 位宽 | 说明 |
|---|---|---:|---|
| `cmd_valid` | input | 1 | 上游提供有效命令 |
| `cmd_ready` | output | 1 | DMA 可以接收命令 |
| `cmd_id` | input | 8 | 命令标识 |
| `cmd_src_addr` | input | 64 | 源地址 |
| `cmd_dst_addr` | input | 64 | 目的地址 |
| `cmd_length_bytes` | input | 24 | 搬运字节数 |
| `cmd_irq_enable` | input | 1 | 完成中断使能 |

握手规则：

- 仅 `cmd_valid && cmd_ready` 为 1 的上升沿接收命令；
- `cmd_ready` 单独为 1 不表示存在命令；
- `cmd_valid=1 && cmd_ready=0` 时，上游保持全部命令字段稳定；
- DMA 不会采样未握手的命令字段；
- Reset 期间 `cmd_ready=0`。

## 3. 完成通道

| 信号 | 方向 | 位宽 | 说明 |
|---|---|---:|---|
| `completion_valid` | output | 1 | 完成信息有效 |
| `completion_ready` | input | 1 | 下游可以接收完成信息 |
| `completion_id` | output | 8 | 对应原命令 `cmd_id` |
| `completion_status` | output | 2 | 0 成功、1 读错误、2 写错误、3 非法命令 |
| `completion_bytes_transferred` | output | 24 | 错误前成功搬运的字节数 |
| `irq_pulse` | output | 1 | 中断使能命令完成握手后的单周期脉冲 |

握手规则：

- 完成信息仅在 `completion_valid && completion_ready` 时被消费；
- `completion_valid=1 && completion_ready=0` 时，完成字段保持稳定；
- 每条已接收命令恰好产生一次完成握手；
- `irq_pulse` 只在完成握手且原命令 `irq_enable=1` 时产生；
- Reset 期间 `completion_valid=0`、`irq_pulse=0`。

## 4. 抽象传输事务接口

本示例只定义 DMA 控制模块与总线适配层之间的抽象事务，不指定具体 SoC 总线协议：

| 信号组 | 方向 | 说明 |
|---|---|---|
| `read_request` | output | 源地址和本次分段长度 |
| `read_response` | input | 读完成、读错误和返回数据 |
| `write_request` | output | 目的地址、分段长度和写数据 |
| `write_response` | input | 写完成或写错误 |

正式项目必须以具体总线 ICD 替换本节。DS 可以描述适配要求，但不得自行创造正式协议语义。
