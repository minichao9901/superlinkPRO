# ETM 目标固件

| 路径 | 当前保留用途 |
|---|---|
| [stm32h743_cdc_trace](stm32h743_cdc_trace/README.md) | CDC 回显示例的 C 源码、CherryUSB/SDK 子集、链接脚本和构建脚本。以匹配记录的 ELF 验证；2026-11-07 主线为 CPU 60 MHz / 物理 TRACECLK 20 MHz。 |
| [stm32h7b0_etm](stm32h7b0_etm/README.md) / [stm32h7b0_etm_board_v1](stm32h7b0_etm_board_v1/README.md) | 两套 H7B0 早期低频验证工程；时钟与板级定义有区别，保留源码复现实验。 |
| `stm32h7b0_spi_trace/stable-70-35-v1/` | H7B0 SPI 业务基准的冻结 ELF；原始数据与匹配信息见 [基准说明](../baselines/spi-70-35-v1/README.md)。 |
| `stm32f103_cdc_trace/fw-ze.elf` | F103ZE ETMv3.4 兼容样本的固件；目录保留冻结 ELF。F103CB 不支持该并行 ETM 采集。 |

从仓库根目录使用 [统一 Trace CLI](../../docs/trace-cli-ai.md)。H743 四档采集条件见 [CDC 验收规范](../../docs/etm-cdc-acceptance.md)，
2026-11-07 的实录样本、匹配固件和自动解码结果见 [真机报告](../../docs/validation/h743-etm-auto-decode-20261107.md)。
记录的 ELF 应与实际镜像和 SHA-256 一致，重编译的 ELF 不覆盖冻结输入。

H743 源码固件由外部工具配置 ETM，CDC 收发辅助脚本为 `etm/firmware/tools/cdc_test.py`。烧录、复位或采集前先确认探针、目标和 Kingst 无其它会话占用。
