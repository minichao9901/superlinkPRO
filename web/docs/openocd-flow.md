# OpenOCD 执行流程详解（CMSIS-DAP → SWD → ADIv5 → Cortex-M → Flash loader）

> 目的：把 OpenOCD 的"标准答案"逐步抄下来，用于对照改造纯静态网页工具（WebUSB + CMSIS-DAP v2，不经 OpenOCD）。
> 场景：STM32F103（Cortex-M3）网页版正常；STM32H7B0（Cortex-M7）网页版一开始就 SWD FAULT；同一探针同一线 OpenOCD 两边都能烧能读。
>
> **本文所有结论均给出 `文件:行号` 出处。凡是不能从源码直接读出、或存在版本/环境不确定性的地方，一律显式标注「未确认」，不做推测。**

---

## 0. 关于版本与出处（**先读这一节，否则行号会对不上**）

### 0.1 本机实际有几个 OpenOCD

| 位置 | 版本字符串 |
|---|---|
| `E:\Share\env-windows\rtt-debugger-support-package-0.4.0\bin\openocd.exe`（PATH 上的 `openocd`） | `Open On-Chip Debugger 0.11.0+dev-02365-g488108c93 (2023-09-18-18:58)` |
| `E:\Share\env-windows\xpack-openocd-0.12.0-6\bin\openocd.exe` | `xPack Open On-Chip Debugger 0.12.0+dev-01850-geb6f2745b-dirty (2025-02-07-10:08)` |

任务书里说的 `0.12.0+dev-01850-geb6f2745b` 对应的是 **xpack 那一份**（`E:\Share\env-windows\xpack-openocd-0.12.0-6`），
它的 tcl 脚本目录是 `E:\Share\env-windows\xpack-openocd-0.12.0-6\openocd\scripts\`。

### 0.2 为什么本文的行号不采用 `v0.12.0` 标签

任务书要求"优先取 v0.12.0 标签"。但实测发现：**`v0.12.0` 标签（commit `9ea7f3d`）与本机二进制所对应的提交 `eb6f2745b` 相差 1850 个提交，关键文件差异很大**，行号完全不能互推：

| 文件 | v0.12.0 标签行数 | eb6f2745b 行数 | diff 变更行数 |
|---|---|---|---|
| `src/jtag/drivers/cmsis_dap.c` | 2152 | 2320 | 430 |
| `src/jtag/drivers/cmsis_dap_usb_bulk.c` | 483 | 678 | 257 |
| `src/target/arm_adi_v5.c` | 2865 | 2983 | 420 |
| `src/target/arm_adi_v5.h` | 759 | 812 | 95 |
| `src/target/cortex_m.c` | 2922 | 3222 | 520 |
| `src/target/adi_v5_swd.c` | 731 | 779 | 200 |
| `src/target/armv7m.c` | 1105 | 1142 | 231 |
| `src/target/arm_dap.c` | 539 | 542 | 33 |
| `src/flash/nor/stm32h7x.c` | 1213 | 1207 | 14 |
| `src/target/algorithm.c` | 41 | 41 | 0 |

而且差异不只是行号平移，**恰恰落在你最关心的"队列/重试/超时"上**：
`cmsis_dap.c` 的 `cmsis_dap_swd_queue_cmd` / `cmsis_dap_swd_run_queue` / `cmsis_dap_swd_read_process` 三处在 dev 版被重写，
dev 版新增了 `CMD_DAP_TFER_BLOCK`（`DAP_TransferBlock`）支持，而 v0.12.0 标签**根本没有块传输**（标签里只有 `CMD_DAP_TFER`）。

> **本文行的号约定**：全部以 **`eb6f2745b`（= 本机 xpack 二进制）** 为准。
> 凡与 `v0.12.0` 标签有**实质行为差异**的地方，用 `> 版本差异：` 引用块单独标注。
> 只查标签源码的人，请用本文附录 A 的"函数名 → v0.12.0 标签行号"对照表反查。

### 0.3 源码获取方式（可复现）

```powershell
# 干流 v0.12.0 标签（浅克隆）
git -c http.proxy=http://127.0.0.1:7890 -c http.version=HTTP/1.1 clone --depth 1 -b v0.12.0 `
    https://github.com/openocd-org/openocd E:\web-serial-rtt-tools\tmp\openocd-src
# 本机二进制对应提交 eb6f2745b：GitHub 不允许按 SHA fetch（实测 "couldn't find remote ref eb6f2745b"），
# 改为逐文件从 raw 拉取，重建到 tmp\ocd-dev-src\
curl.exe --proxy http://127.0.0.1:7890 -sL -o <out> `
    https://raw.githubusercontent.com/openocd-org/openocd/eb6f2745b/<相对路径>
```

本文引用的两棵源码树：

- `E:\web-serial-rtt-tools\tmp\ocd-dev-src\`  ← **主引用树（= eb6f2745b = 本机二进制）**
- `E:\web-serial-rtt-tools\tmp\openocd-src\`  ← v0.12.0 标签（仅用于版本对比）

### 0.4 文档里出现的路径写法

正文中形如 `src/jtag/drivers/cmsis_dap.c:1407` 的出处，**一律相对主引用树 `tmp\ocd-dev-src\`**。
tcl 脚本同理相对该树的 `tcl\`。若引用的是本机 xpack 实际加载的脚本副本，会写全绝对路径。

---

## 1. 传输层初始化（CMSIS-DAP）

### 1.1 命令码与常量速查（全部来自源码宏定义）

`src/jtag/drivers/cmsis_dap.c:73-79`：

```c
/* CMSIS-DAP General Commands */
#define CMD_DAP_INFO              0x00
#define CMD_DAP_LED               0x01
#define CMD_DAP_CONNECT           0x02
#define CMD_DAP_DISCONNECT        0x03
#define CMD_DAP_WRITE_ABORT       0x08
#define CMD_DAP_DELAY             0x09
#define CMD_DAP_RESET_TARGET      0x0A
```

`src/jtag/drivers/cmsis_dap.c:118-120`、`140-141`：

```c
/* CMSIS-DAP Common SWD/JTAG Commands */
#define CMD_DAP_DELAY             0x09
#define CMD_DAP_SWJ_PINS          0x10
#define CMD_DAP_SWJ_CLOCK         0x11
#define CMD_DAP_SWJ_SEQ           0x12

/* CMSIS-DAP SWD Commands */
#define CMD_DAP_SWD_CONFIGURE     0x13
#define CMD_DAP_SWD_SEQUENCE      0x1D
```

`src/jtag/drivers/cmsis_dap.c:158-170`：

```c
/* CMSIS-DAP Transfer Commands */
#define CMD_DAP_TFER_CONFIGURE    0x04
#define CMD_DAP_TFER              0x05
#define CMD_DAP_TFER_BLOCK        0x06
#define CMD_DAP_TFER_ABORT        0x07

/* DAP_TransferBlock increases the sum of command/response sizes
 * (due to 16-bit Transfer Count) if used in a small packet.
 * Prevent using it until we have at least r/w operations. */
#define CMD_DAP_TFER_BLOCK_MIN_OPS 4

/* DAP Status Code */
#define DAP_OK                    0
#define DAP_ERROR                 0xFF
```

`src/jtag/drivers/cmsis_dap.c:112-114`（连接模式）：

```c
/* CMD_CONNECT */
#define CONNECT_DEFAULT           0x00
#define CONNECT_SWD               0x01
#define CONNECT_JTAG              0x02
```

> 注意：**SWD 模式的 `DAP_Connect` 参数就是 `0x01`**，与任务书里写的一致。

`src/jtag/drivers/cmsis_dap.c:132-137`（SWJ_PINS 位定义）：

```c
#define SWJ_PIN_TCK               (1<<0)
#define SWJ_PIN_TMS               (1<<1)
#define SWJ_PIN_TDI               (1<<2)
#define SWJ_PIN_TDO               (1<<3)
#define SWJ_PIN_TRST              (1<<5)
#define SWJ_PIN_SRST              (1<<7)
```

### 1.2 `DAP_Info` 用到的项

`src/jtag/drivers/cmsis_dap.c:82-91`：

```c
/* CMD_INFO */
#define INFO_ID_VENDOR            0x01      /* string */
#define INFO_ID_PRODUCT           0x02      /* string */
#define INFO_ID_SERNUM            0x03      /* string */
#define INFO_ID_FW_VER            0x04      /* string */
#define INFO_ID_TD_VEND           0x05      /* string */
#define INFO_ID_TD_NAME           0x06      /* string */
#define INFO_ID_CAPS              0xf0      /* byte */
#define INFO_ID_PKT_CNT           0xfe      /* byte */
#define INFO_ID_PKT_SZ            0xff      /* short */
#define INFO_ID_SWO_BUF_SZ        0xfd      /* word */
```

SWD 正常初始化路径上**实际请求了 4 项**（其余 `VENDOR/PRODUCT/TD_VEND/TD_NAME/SWO_BUF_SZ` 只在别的路径用）：

| 顺序 | Info ID | 常量 | 用途 | 出处 |
|---|---|---|---|---|
| 1 | `0xF0` | `INFO_ID_CAPS` | 读能力位，确认 `INFO_CAPS_SWD`(bit0) | `cmsis_dap.c:1151`（`cmsis_dap_get_caps_info`），由 `:1312` 调用 |
| 2 | `0x04` | `INFO_ID_FW_VER` | 打印固件版本（仅日志） | `cmsis_dap.c:1136`，由 `:1316` 调用 |
| 3 | `0x03` | `INFO_ID_SERNUM` | 打印序列号（仅日志） | `cmsis_dap.c:1121`，由 `:1320` 调用 |
| 4 | `0xFF` | `INFO_ID_PKT_SZ` | 读包大小，必要时**重新分配包缓冲** | `cmsis_dap.c:1347` |
| 5 | `0xFE` | `INFO_ID_PKT_CNT` | 读可并发挂起的包数 | `cmsis_dap.c:1374` |

能力位定义（`src/jtag/drivers/cmsis_dap.c:93-102`）：

```c
#define INFO_CAPS_SWD                 BIT(0)
#define INFO_CAPS_JTAG                BIT(1)
#define INFO_CAPS_SWO_UART            BIT(2)
#define INFO_CAPS_SWO_MANCHESTER      BIT(3)
#define INFO_CAPS_ATOMIC_CMDS         BIT(4)
#define INFO_CAPS_TEST_DOMAIN_TIMER   BIT(5)
#define INFO_CAPS_SWO_STREAMING_TRACE BIT(6)
#define INFO_CAPS_UART_PORT           BIT(7)
#define INFO_CAPS_USB_COM_PORT        BIT(8)
#define INFO_CAPS__NUM_CAPS               9
```

能力解析（`src/jtag/drivers/cmsis_dap.c:1146-1169`，节选）：

```c
static int cmsis_dap_get_caps_info(void)
{
	uint8_t *data;

	/* INFO_ID_CAPS - byte */
	int retval = cmsis_dap_cmd_dap_info(INFO_ID_CAPS, &data);
	if (retval != ERROR_OK)
		return retval;

	if (data[0] == 1 || data[0] == 2) {
		uint16_t caps = data[1];
		if (data[0] == 2)
			caps |= (uint16_t)data[2] << 8;

		cmsis_dap_handle->caps = caps;

		for (unsigned int i = 0; i < INFO_CAPS__NUM_CAPS; ++i) {
			if (caps & BIT(i))
				LOG_INFO("CMSIS-DAP: %s", info_caps_str[i]);
		}
	}

	return ERROR_OK;
}
```

**注意 `DAP_Info` 的应答格式**：`response[0]` 是命令回显，`response[1]` 是**数据长度**，`response[2..]` 才是数据。
所以 `cmsis_dap_get_caps_info` 里用 `data[0]` 当长度、`data[1]` 起才是内容（`data` 指向 `response[1]`，见 `:447`）。
这一点很容易写错。

### 1.3 每条命令的确切报文字节（编码实现）

以下给出**发送缓冲区的构造代码**，这就是线上字节的来源。

**`DAP_Connect`**（`src/jtag/drivers/cmsis_dap.c:469-488`）：

```c
static int cmsis_dap_cmd_dap_connect(uint8_t mode)
{
	uint8_t *command = cmsis_dap_handle->command;

	command[0] = CMD_DAP_CONNECT;
	command[1] = mode;

	int retval = cmsis_dap_xfer(cmsis_dap_handle, 2);
	if (retval != ERROR_OK) {
		LOG_ERROR("CMSIS-DAP command CMD_CONNECT failed.");
		return ERROR_JTAG_DEVICE_ERROR;
	}

	if (cmsis_dap_handle->response[1] != mode) {
		LOG_ERROR("CMSIS-DAP failed to connect in mode (%d)", mode);
		return ERROR_JTAG_DEVICE_ERROR;
	}

	return ERROR_OK;
}
```

→ `发: 02 01`，**校验条件**：`收[1]` 必须等于 `0x01`（不是 `DAP_OK`！）。

**`SWJ_Clock`**（`src/jtag/drivers/cmsis_dap.c:391-408`）：

```c
static int cmsis_dap_cmd_dap_swj_clock(uint32_t swj_clock)
{
	uint8_t *command = cmsis_dap_handle->command;

	/* set clock in Hz */
	swj_clock *= 1000;

	command[0] = CMD_DAP_SWJ_CLOCK;
	h_u32_to_le(&command[1], swj_clock);

	int retval = cmsis_dap_xfer(cmsis_dap_handle, 5);
	if (retval != ERROR_OK || cmsis_dap_handle->response[1] != DAP_OK) {
```

→ 参数单位是 **kHz，函数内部 ×1000 转 Hz，小端 4 字节**。`发: 11 <freq_Hz_LE32>`，期望 `收: 11 00`。

**`SWJ_Sequence`**（`src/jtag/drivers/cmsis_dap.c:411-432`）：

```c
static int cmsis_dap_cmd_dap_swj_sequence(uint8_t s_len, const uint8_t *sequence)
{
	uint8_t *command = cmsis_dap_handle->command;
	...
	command[0] = CMD_DAP_SWJ_SEQ;
	command[1] = s_len;
	bit_copy(&command[2], 0, sequence, 0, s_len);

	int retval = cmsis_dap_xfer(cmsis_dap_handle, 2 + DIV_ROUND_UP(s_len, 8));
	if (retval != ERROR_OK || cmsis_dap_handle->response[1] != DAP_OK)
		return ERROR_FAIL;
```

→ `发: 12 <位长> <数据 ceil(len/8) 字节>`。`bit_copy` 决定**位序**（见 §1.4）。

**`TransferConfigure`**（`src/jtag/drivers/cmsis_dap.c:505-521`）：

```c
static int cmsis_dap_cmd_dap_tfer_configure(uint8_t idle, uint16_t retry_count, uint16_t match_retry)
{
	uint8_t *command = cmsis_dap_handle->command;

	command[0] = CMD_DAP_TFER_CONFIGURE;
	command[1] = idle;
	h_u16_to_le(&command[2], retry_count);
	h_u16_to_le(&command[4], match_retry);

	int retval = cmsis_dap_xfer(cmsis_dap_handle, 6);
	if (retval != ERROR_OK || cmsis_dap_handle->response[1] != DAP_OK) {
```

**`SWD_Configure`**（`src/jtag/drivers/cmsis_dap.c:523-537`）：

```c
static int cmsis_dap_cmd_dap_swd_configure(uint8_t cfg)
{
	uint8_t *command = cmsis_dap_handle->command;

	command[0] = CMD_DAP_SWD_CONFIGURE;
	command[1] = cfg;

	int retval = cmsis_dap_xfer(cmsis_dap_handle, 2);
```

**参数的确切取值**在 `cmsis_dap_init` 里（`src/jtag/drivers/cmsis_dap.c:1407-1420`）：

```c
	/* Ask CMSIS-DAP to automatically retry on receiving WAIT for
	 * up to 64 times. This must be changed to 0 if sticky
	 * overrun detection is enabled. */
	retval = cmsis_dap_cmd_dap_tfer_configure(0, 64, 0);
	if (retval != ERROR_OK)
		goto init_err;

	if (swd_mode) {
		/* Data Phase (bit 2) must be set to 1 if sticky overrun
		 * detection is enabled */
		retval = cmsis_dap_cmd_dap_swd_configure(0);	/* 1 TRN, no Data Phase */
		if (retval != ERROR_OK)
			goto init_err;
	}
```

即：

| 命令 | 原始字节（不含 USB 封装） | 含义 |
|---|---|---|
| `DAP_TransferConfigure` | `04 00 40 00 00 00` | idle=0 SWCLK，retry_count=64，match_retry=0 |
| `DAP_SWD_Configure` | `13 00` | cfg=0：**1 个 TRN 周期、无 Data Phase** |

> ⚠️ 这两个值**强耦合**，源码三处注释互相呼应：
> - `tfer_configure(0, 64, 0)` 让适配器自动重试 WAIT 最多 64 次；注释明说"**如果启用了 sticky overrun 检测就必须改成 0**"。
> - `swd_configure(0)` 的 cfg bit2（Data Phase）"**如果启用了 sticky overrun 检测就必须置 1**"。
> - 所以 OpenOCD 选择了"不启用 sticky overrun 检测 + 让适配器自动重试"，并在 `cmsis_dap_swd_write_from_queue` 里**主动剔除对 `CORUNDETECT` 的写入**（见 §1.6.3）。
>
> 这三者必须成组理解，单独抄一个会出事。

### 1.4 `SWJ_Sequence` 激活序列：**136 位，不是 88 位**

> ⚠️ **这是任务书里的一个事实性错误，务必更正。**
> 任务书写"88 位激活序列（确切字节 `9E E7 FF…00`）"。实测 **v0.11.0、v0.12.0 标签、以及本机 `eb6f2745b` 三个版本的 `swd_seq_jtag_to_swd` 字节完全相同，长度都是 136 位**。
> 88 位是更古老年代（IHI 0031D 时代）的写法，在当前版本的源码里**不存在**。

定义在 `src/jtag/swd.h:106-125`（主引用树）：

```c
/**
 * JTAG-to-SWD sequence.
 *
 * The JTAG-to-SWD sequence is at least 50 TCK/SWCLK cycles with TMS/SWDIO
 * high, putting either interface logic into reset state, followed by a
 * specific 16-bit sequence and finally a line reset in case the SWJ-DP was
 * already in SWD mode.
 * Bits are stored (and transmitted) LSB-first.
 */
static const uint8_t swd_seq_jtag_to_swd[] = {
	/* At least 50 TCK/SWCLK cycles with TMS/SWDIO high */
	0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
	/* Switching sequence from JTAG to SWD */
	0x9e, 0xe7,
	/* At least 50 TCK/SWCLK cycles with TMS/SWDIO high */
	0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
	/* At least 2 idle (low) cycles */
	0x00,
};
static const unsigned int swd_seq_jtag_to_swd_len = 136;
```

字节序列（17 字节 = 136 位）：

```
FF FF FF FF FF FF FF  9E E7  FF FF FF FF FF FF FF  00
└──── 7 字节 = 56 位 ────┘ └16位┘ └──── 7 字节 = 56 位 ────┘ └8位┘
         SWDIO 恒高                切换码           SWDIO 恒高        空闲低
```

**位序**：源码头注释两处明写 **"Bits are stored (and transmitted) LSB-first"**（`src/jtag/swd.h:96`、`:113`）。
即每个字节**先发 bit0**。`9E` 先发 `0 1 1 1 1 0 0 1`（LSB→MSB），`E7` 先发 `1 1 1 0 0 1 1 1`。
CMSIS-DAP 适配器收到 `DAP_SWJ_Sequence` 后按同样的 LSB-first 顺序移位输出到 SWDIO，所以**线上字节就是 `9E E7`**，不需要手工反位。

线复位序列 `src/jtag/swd.h:91-104`：

```c
/**
 * SWD Line reset.
 *
 * SWD Line reset is at least 50 SWCLK cycles with SWDIO driven high,
 * followed by at least two idle (low) cycle.
 * Bits are stored (and transmitted) LSB-first.
 */
static const uint8_t swd_seq_line_reset[] = {
	/* At least 50 SWCLK cycles with SWDIO high */
	0xff, 0xff, 0xff, 0xff, 0xff, 0xff, 0xff,
	/* At least 2 idle (low) cycles */
	0x00,
};
static const unsigned int swd_seq_line_reset_len = 64;
```

→ 线复位 = `FF×7 00`，**64 位**（注意：注释说"至少 50 + 至少 2"，但实际发 56+8=64 位）。

其余序列（现代码里有、但 STM32 单 DP 用不到，仅列出供核对）：
`swd_seq_swd_to_jtag` (`3C E7 FF`, 80 位, `swd.h:136-144`)、
`swd_seq_swd_to_dormant` (`FF×7 BC E3`, 72 位, `:153-159`)、
`swd_seq_dormant_to_swd` (224 位, `:171-190`)、
`swd_seq_jtag_to_dormant` (`FF 75 77 77 67`, 40 位, `:199-211`)、
`swd_seq_dormant_to_jtag` (160 位, `:230-244`)。

> 版本差异：`v0.12.0` 标签的 `src/jtag/swd.h:98-125` 与主引用树**字节与长度完全一致**（仅 `unsigned` vs `unsigned int` 的写法差别）。

### 1.5 线复位 / JTAG-to-SWD 的**调用顺序**与"首笔必须读 DPIDR"硬约束

这是**全文档最值得抄的一段**，因为它解释了为什么"第一条 DAP_Transfer 发错就全盘 FAULT"。

入口：`dap_init_all()` → `dap->ops->connect(dap)`。对 SWD 就是 `swd_connect`。
（`src/target/arm_dap.c:127-137`：先 `pre_connect_init`，再 `connect`）

```c
		if (pre_connect && dap->ops->pre_connect_init) {
			retval = dap->ops->pre_connect_init(dap);
			if (retval != ERROR_OK)
				return retval;

			pre_connect = false;
		}

		retval = dap->ops->connect(dap);
		if (retval != ERROR_OK)
			return retval;
```

SWD 的 `pre_connect_init` 只是把多drop状态清掉（`src/target/adi_v5_swd.c:404-409`）：

```c
static int swd_pre_connect(struct adiv5_dap *dap)
{
	swd_multidrop_in_swd_state = false;

	return ERROR_OK;
}
```

`swd_connect`（`src/target/adi_v5_swd.c:411-460`）：

```c
static int swd_connect(struct adiv5_dap *dap)
{
	int status;

	/* FIXME validate transport config ... is the
	 * configured DAP present (check IDCODE)?
	 */

	/* Check if we should reset srst already when connecting, but not if reconnecting. */
	if (!dap->do_reconnect) {
		enum reset_types jtag_reset_config = jtag_get_reset_config();

		if (jtag_reset_config & RESET_CNCT_UNDER_SRST) {
			if (jtag_reset_config & RESET_SRST_NO_GATING)
				adapter_assert_reset();
			else
				LOG_WARNING("\'srst_nogate\' reset_config option is required");
		}
	}

	if (dap_is_multidrop(dap))
		status = swd_connect_multidrop(dap);
	else
		status = swd_connect_single(dap);

	/* IHI 0031E B4.3.2:
	 * "A WAIT response must not be issued to the ...
	 * ... writes to the ABORT register"
	 * swd_clear_sticky_errors() writes to the ABORT register only.
	 *
	 * Unfortunately at least Microchip SAMD51/E53/E54 returns WAIT
	 * in a corner case. Just try if ABORT resolves the problem.
	 */
	if (status == ERROR_WAIT) {
		LOG_WARNING("Connecting DP: stalled AP operation, issuing ABORT");

		dap->do_reconnect = false;

		status = swd_queue_dp_write_inner(dap, DP_ABORT,
			DAPABORT | STKCMPCLR | STKERRCLR | WDERRCLR | ORUNERRCLR);

		if (status == ERROR_OK)
			status = swd_run_inner(dap);
	}

	if (status == ERROR_OK)
		status = dap_dp_init(dap);

	return status;
}
```

STM32 是**单 DP**，走 `swd_connect_single`（`src/target/adi_v5_swd.c:334-402`）：

```c
static int swd_connect_single(struct adiv5_dap *dap)
{
	int retval;
	uint32_t dpidr = 0xdeadbeef;
	int64_t timeout = timeval_ms() + 500;

	do {
		if (dap->switch_through_dormant) {
			swd_send_sequence(dap, JTAG_TO_DORMANT);
			swd_send_sequence(dap, DORMANT_TO_SWD);
		} else {
			swd_send_sequence(dap, JTAG_TO_SWD);
		}

		/* Clear link state, including the SELECT cache. */
		dap->do_reconnect = false;
		dap_invalidate_cache(dap);

		/* The sequences to enter in SWD (JTAG_TO_SWD and DORMANT_TO_SWD) end
		 * with a SWD line reset sequence (50 clk with SWDIO high).
		 * From ARM IHI 0031F ADIv5.2 and ARM IHI 0074C ADIv6.0,
		 * chapter B4.3.3 "Connection and line reset sequence":
		 * - DPv3 (ADIv6) only: line reset sets DP_SELECT_DPBANK to zero;
		 * - read of DP_DPIDR takes the connection out of reset;
		 * - write of DP_TARGETSEL keeps the connection in reset;
		 * - other accesses return protocol error (SWDIO not driven by target).
		 *
		 * dap_invalidate_cache() sets dap->select to zero and all validity
		 * flags to invalid. Set dap->select_dpbanksel_valid only
		 * to skip the write to DP_SELECT, avoiding the protocol error.
		 * Read DP_DPIDR to get out of reset.
		 */
		dap->select_dpbanksel_valid = true;

		retval = swd_queue_dp_read_inner(dap, DP_DPIDR, &dpidr);
		if (retval == ERROR_OK) {
			retval = swd_run_inner(dap);
			if (retval == ERROR_OK)
				break;
		}

		alive_sleep(1);

		dap->switch_through_dormant = !dap->switch_through_dormant;
	} while (timeval_ms() < timeout);

	if (retval != ERROR_OK) {
		LOG_ERROR("Error connecting DP: cannot read IDR");
		return retval;
	}

	LOG_INFO("SWD DPIDR 0x%08" PRIx32, dpidr);

	do {
		dap->do_reconnect = false;

		/* force clear all sticky faults */
		swd_clear_sticky_errors(dap);

		retval = swd_run_inner(dap);
		if (retval != ERROR_WAIT)
			break;

		alive_sleep(10);

	} while (timeval_ms() < timeout);

	return retval;
}
```

**把这段拆成硬约束清单：**

| # | 约束 | 源码依据 |
|---|---|---|
| 1 | 激活序列**必须是完整 136 位** `FF×7 9E E7 FF×7 00`，不能只发 `9E E7` | `swd.h:115-125` |
| 2 | 激活序列**本身已包含线复位**（结尾 7×FF + 00）。单 DP 时 `swd_connect_single` **不会再单独发一次 `LINE_RESET`** | `adi_v5_swd.c:345`（`switch_through_dormant` 为 false 时只发 `JTAG_TO_SWD`）；`swd.h:110-112` 注释"finally a line reset in case the SWJ-DP was already in SWD mode" |
| 3 | 激活序列之后**第一笔 DAP 事务必须是"读 DP DPIDR"**（A[3:2]=0b00、APnDP=0、RnW=1 → 请求字节 `0x02`） | `adi_v5_swd.c:368` + `:352-364` 注释（"read of DP_DPIDR takes the connection out of reset; other accesses return protocol error"） |
| 4 | 在读到 DPIDR 之前**不能写 DP_SELECT**（否则 "protocol error"）。代码用 `dap->select_dpbanksel_valid = true` 把这一步跳过 | `adi_v5_swd.c:366` 及 `:361-364` 注释 |
| 5 | 整个"发序列 + 读 DPIDR"在 **500 ms** 窗口内重试；每轮失败后 `alive_sleep(1)`，并**交替**走 `JTAG_TO_SWD` 与 `JTAG_TO_DORMANT+DORMANT_TO_SWD` 两条路径 | `adi_v5_swd.c:338`（`timeout = timeval_ms() + 500`）、`:375`、`:377`、`:378` |
| 6 | DPIDR 读成功后，**立刻清 sticky**（写 DP ABORT），失败按 `ERROR_WAIT` 重试，同样受同一个 500 ms 窗口约束，每轮 `alive_sleep(10)` | `adi_v5_swd.c:387-399` |
| 7 | 只有当 sticky 清理也返回 `ERROR_WAIT` 时，才用**带 DAPABORT 的全清**再试一次 | `adi_v5_swd.c:444-454` |
| 8 | 以上全部 OK 后才进 `dap_dp_init()` | `adi_v5_swd.c:456-457` |

> **对 H7B0 的直接提示（源码事实，非推测）**：约束 3 是"第一笔必须读 DPIDR"。
> 如果网页端在 `DAP_Connect(SWD)` 之后先写了 `DP SELECT`、或先写了 `DP CTRL/STAT`（很多教程/示例代码会这么干）、
> 或把激活序列截短成 88 位（旧写法），都会命中源码注释里写的 "other accesses return protocol error"。
> **但本文无法判断这就是你 H7B0 失败的根因**——需要你按 §6 的清单逐项对比后再定论。

### 1.6 探针侧队列 / 超时 / 重试策略

#### 1.6.1 超时常量

| 常量 | 值 | 出处 |
|---|---|---|
| `LIBUSB_TIMEOUT_MS` | `6000` | `src/jtag/drivers/libusb_helper.h:26` |
| `MAX_PENDING_REQUESTS` | `4` | `src/jtag/drivers/cmsis_dap.h:19` |
| 清管道时的短超时 | `10` ms，最多循环 64 次 | `cmsis_dap.c:311-324` |

`src/jtag/drivers/libusb_helper.h:26`：

```c
#define LIBUSB_TIMEOUT_MS	(6000)
```

`src/jtag/drivers/cmsis_dap.h:17-25`：

```c
/* Up to MIN(packet_count, MAX_PENDING_REQUESTS) requests may be issued
 * until the first response arrives */
#define MAX_PENDING_REQUESTS 4

struct pending_request_block {
	struct pending_transfer_result *transfers;
	unsigned int transfer_count;
	uint8_t command;
};
```

#### 1.6.2 开管道：先把旧包冲干净

`src/jtag/drivers/cmsis_dap.c:311-324`：

```c
static void cmsis_dap_flush_read(struct cmsis_dap *dap)
{
	unsigned int i;
	/* Some CMSIS-DAP adapters keep buffered packets over
	 * USB close/open so we need to flush up to 64 old packets
	 * to be sure all buffers are empty */
	for (i = 0; i < 64; i++) {
		int retval = dap->backend->read(dap, 10, CMSIS_DAP_BLOCKING);
		if (retval == ERROR_TIMEOUT_REACHED)
			break;
	}
	if (i)
		LOG_DEBUG("Flushed %u packets", i);
}
```

→ **OpenOCD 在打开设备后、发第一条命令前，最多用 64 次 10 ms 超时的读把残留响应清空**。
理由写在注释里：有些适配器在 USB close/open 之间会保留缓冲包。`cmsis_dap_init` 第一件事就是调它（`:1310`）。

#### 1.6.3 命令与响应的配对规则

**单命令（非 SWD 队列）**走 `cmsis_dap_xfer`（`src/jtag/drivers/cmsis_dap.c:327-368`）：

```c
/* Send a message and receive the reply */
static int cmsis_dap_xfer(struct cmsis_dap *dap, int txlen)
{
	if (dap->write_count + dap->read_count) {
		LOG_ERROR("internal: queue not empty before xfer");
	}
	if (dap->pending_fifo_block_count) {
		LOG_ERROR("pending %u blocks, flushing", dap->pending_fifo_block_count);
		while (dap->pending_fifo_block_count) {
			dap->backend->read(dap, 10, CMSIS_DAP_BLOCKING);
			dap->pending_fifo_block_count--;
		}
		dap->pending_fifo_put_idx = 0;
		dap->pending_fifo_get_idx = 0;
	}

	uint8_t current_cmd = dap->command[0];
	int retval = dap->backend->write(dap, txlen, LIBUSB_TIMEOUT_MS);
	if (retval < 0)
		return retval;

	/* get reply */
	retval = dap->backend->read(dap, LIBUSB_TIMEOUT_MS, CMSIS_DAP_BLOCKING);
	if (retval < 0)
		return retval;

	uint8_t *resp = dap->response;
	if (resp[0] == DAP_ERROR) {
		LOG_ERROR("CMSIS-DAP command 0x%" PRIx8 " not implemented", current_cmd);
		return ERROR_NOT_IMPLEMENTED;
	}

	if (resp[0] != current_cmd) {
		LOG_ERROR("CMSIS-DAP command mismatch. Sent 0x%" PRIx8
			 " received 0x%" PRIx8, current_cmd, resp[0]);

		dap->backend->cancel_all(dap);
		cmsis_dap_flush_read(dap);
		return ERROR_FAIL;
	}

	return ERROR_OK;
}
```

**配对策略**：发一条 → 收一条 → 校验 `resp[0] == 发出的命令码`；不匹配就 **cancel_all + 清管道 + 报错**（不做自动重试）。

**`DAP_Transfer` 队列**走另一条路径，支持最多 `packet_count`（≤4）个包在途。
在途数来自 `DAP_Info(0xFE)`（`cmsis_dap.c:1374-1384`）：

```c
	retval = cmsis_dap_cmd_dap_info(INFO_ID_PKT_CNT, &data);
	if (retval != ERROR_OK)
		goto init_err;

	if (data[0] == 1) { /* byte */
		unsigned int pkt_cnt = data[1];
		if (pkt_cnt > 1)
			cmsis_dap_handle->packet_count = MIN(MAX_PENDING_REQUESTS, pkt_cnt);

		LOG_DEBUG("CMSIS-DAP: Packet Count = %u", pkt_cnt);
	}
```

队列长度计算（`cmsis_dap.c:1363-1371`）：

```c
	/* Maximal number of transfers which fit to one packet:
	 * Limited by response size: 3 bytes of response header + 4 per read
	 * Plus writes to full command size: 3 bytes cmd header + 1 per read + 5 per write */
	tfer_max_command_size = cmsis_dap_handle->packet_usable_size;
	tfer_max_response_size = cmsis_dap_handle->packet_usable_size;
	unsigned int max_reads = tfer_max_response_size / 4;
	pending_queue_len = max_reads + (tfer_max_command_size - max_reads) / 5;
```

入队与"装不下就先发"（`cmsis_dap.c:1032-1103`，节选）：

```c
	unsigned int cmd_size = cmsis_dap_tfer_cmd_size(write_count, read_count,
												block_cmd);
	unsigned int resp_size = cmsis_dap_tfer_resp_size(write_count, read_count,
												block_cmd);
	unsigned int max_transfer_count = block_cmd ? 65535 : 255;

	/* Does the DAP Transfer command and also its expected response fit into one packet? */
	if (cmd_size > tfer_max_command_size
			|| resp_size > tfer_max_response_size
			|| write_count + read_count > max_transfer_count) {
		if (cmsis_dap_handle->pending_fifo_block_count)
			cmsis_dap_swd_read_process(cmsis_dap_handle, CMSIS_DAP_NON_BLOCKING);

		/* Not enough room in the queue. Run the queue. */
		cmsis_dap_swd_write_from_queue(cmsis_dap_handle);

		unsigned int packet_count = cmsis_dap_handle->quirk_mode ? 1 : cmsis_dap_handle->packet_count;
		if (cmsis_dap_handle->pending_fifo_block_count >= packet_count)
			cmsis_dap_swd_read_process(cmsis_dap_handle, CMSIS_DAP_BLOCKING);
	}
```

尺寸模型（`cmsis_dap.c:1004-1030`）：

```c
static unsigned int cmsis_dap_tfer_cmd_size(unsigned int write_count,
							unsigned int read_count, bool block_tfer)
{
	unsigned int size;
	if (block_tfer) {
		size = 5;						/* DAP_TransferBlock header */
		size += write_count * 4;		/* data */
	} else {
		size = 3;						/* DAP_Transfer header */
		size += write_count * (1 + 4);	/* DAP register + data */
		size += read_count;				/* DAP register */
	}
	return size;   /* (下一行起为 resp_size 函数) */
}
static unsigned int cmsis_dap_tfer_resp_size(unsigned int write_count,
							unsigned int read_count, bool block_tfer)
{
	unsigned int size;
	if (block_tfer)
		size = 4;						/* DAP_TransferBlock response header */
	else
		size = 3;						/* DAP_Transfer response header */

	size += read_count * 4;				/* data */
	return size;
}
```

→ 说明 OpenOCD 认为响应头是 **3 字节**（`cmd + count + 第 1 个 ACK`），这也和 §1.6.5 的解析代码一致。

#### 1.6.4 组包：什么时候用 `DAP_TransferBlock`

`src/jtag/drivers/cmsis_dap.c:798-812`：

```c
	bool block_cmd = !cmsis_dap_handle->swd_cmds_differ
					 && block->transfer_count >= CMD_DAP_TFER_BLOCK_MIN_OPS;
	block->command = block_cmd ? CMD_DAP_TFER_BLOCK : CMD_DAP_TFER;

	command[0] = block->command;
	command[1] = 0x00;	/* DAP Index */

	unsigned int idx;
	if (block_cmd) {
		h_u16_to_le(&command[2], block->transfer_count);
		idx = 4;	/* The first transfer will store the common DAP register */
	} else {
		command[2] = block->transfer_count;
		idx = 3;
	}
```

条件：**本包内所有事务的请求字节完全相同**（`!swd_cmds_differ`）**且条数 ≥ 4**（`CMD_DAP_TFER_BLOCK_MIN_OPS`）。
`swd_cmds_differ` 的维护在 `:1088-1093`：

```c
	if (block->transfer_count == 0) {
		cmsis_dap_handle->swd_cmds_differ = false;
		cmsis_dap_handle->common_swd_cmd = cmd;
	} else if (cmd != cmsis_dap_handle->common_swd_cmd) {
		cmsis_dap_handle->swd_cmds_differ = true;
	}
```

**同时**，若包内出现"对 `DP_CTRL_STAT` 写 `CORUNDETECT`"，OpenOCD 会把它摘掉（`cmsis_dap.c:824-841`）：

```c
		/* When proper WAIT handling is implemented in the
		 * common SWD framework, this kludge can be
		 * removed. However, this might lead to minor
		 * performance degradation as the adapter wouldn't be
		 * able to automatically retry anything (because ARM
		 * has forgotten to implement sticky error flags
		 * clearing). See also comments regarding
		 * cmsis_dap_cmd_dap_tfer_configure() and
		 * cmsis_dap_cmd_dap_swd_configure() in
		 * cmsis_dap_init().
		 */
		if (!(cmd & SWD_CMD_RNW) &&
		    !(cmd & SWD_CMD_APNDP) &&
		    (cmd & SWD_CMD_A32) >> 1 == DP_CTRL_STAT &&
		    (data & CORUNDETECT)) {
			LOG_DEBUG("refusing to enable sticky overrun detection");
			data &= ~CORUNDETECT;
		}
```

> ⚠️ 这条非常关键：**DP 层请求了 `CORUNDETECT`（`dap_dp_init` 会写它，见 §2.1），但 CMSIS-DAP 驱动在组包时把它清掉了。**
> 也就是说"OpenOCD 在 CMSIS-DAP 上实际并没有启用 SWD 的 sticky overrun 检测"——这是有意的，因为要换取"适配器自动重试 WAIT 64 次"的能力。
> 网页端如果照抄 `dap_dp_init` 的 `0x50000001` 却**没有**这个剔除逻辑，行为会和 OpenOCD 不同。

> 版本差异：`v0.12.0` 标签**没有 `DAP_TransferBlock`**。标签的 `cmsis_dap_swd_write_from_queue` 一律构造 `CMD_DAP_TFER`，
> 且用 `pending_queue_len` 作为唯一分界（标签 `cmsis_dap.c:937-947`），没有 `block_cmd` 概念。
> 两者对外部目标的语义基本相同，只是 dev 版包更长、吞吐更高。

#### 1.6.5 响应解析与错误映射

`src/jtag/drivers/cmsis_dap.c:910-948`：

```c
	uint8_t *resp = dap->response;
	if (resp[0] != block->command) {
		LOG_ERROR("CMSIS-DAP command mismatch. Expected 0x%x received 0x%" PRIx8,
			block->command, resp[0]);
		cmsis_dap_swd_cancel_transfers(dap);
		queued_retval = ERROR_FAIL;
		return;
	}

	unsigned int transfer_count;
	unsigned int idx;
	if (block->command == CMD_DAP_TFER_BLOCK) {
		transfer_count = le_to_h_u16(&resp[1]);
		idx = 3;
	} else {
		transfer_count = resp[1];
		idx = 2;
	}
	if (resp[idx] & 0x08) {
		LOG_DEBUG("CMSIS-DAP Protocol Error @ %d (wrong parity)", transfer_count);
		queued_retval = ERROR_FAIL;
		goto skip;
	}
	uint8_t ack = resp[idx++] & 0x07;
	if (ack != SWD_ACK_OK) {
		LOG_DEBUG("SWD ack not OK @ %d %s", transfer_count,
			  ack == SWD_ACK_WAIT ? "WAIT" : ack == SWD_ACK_FAULT ? "FAULT" : "JUNK");
		queued_retval = swd_ack_to_error_code(ack);
		/* TODO: use results of transfers completed before the error occurred? */
		goto skip;
	}

	if (block->transfer_count != transfer_count) {
		LOG_ERROR("CMSIS-DAP transfer count mismatch: expected %d, got %d",
			  block->transfer_count, transfer_count);
		cmsis_dap_swd_cancel_transfers(dap);
		queued_retval = ERROR_FAIL;
		return;
	}
```

ACK → OpenOCD 错误码（`src/jtag/swd.h:72-84`）：

```c
static inline int swd_ack_to_error_code(uint8_t ack)
{
	switch (ack) {
	case SWD_ACK_OK:
		return ERROR_OK;
	case SWD_ACK_WAIT:
		return ERROR_WAIT;
	case SWD_ACK_FAULT:
		return ERROR_SWD_FAULT;
	default:
		return ERROR_SWD_FAIL;
	}
}
```

ACK 常量（`src/target/arm_adi_v5.h:30-33`）：

```c
/* three-bit ACK values for SWD access (sent LSB first) */
#define SWD_ACK_OK    0x1
#define SWD_ACK_WAIT  0x2
#define SWD_ACK_FAULT 0x4
```

> ⚠️ **响应字节布局 — 一处必须你自己交叉验证的地方。**
> 上面的代码**只消费 1 个 ACK 字节**（`idx` 从 2 或 3 起步，读完 `resp[idx]` 后 `idx++`），
> 随后在 `:954-974` 的循环里**只对"读事务"各取 4 字节**，写事务不再消费任何字节：
>
> ```c
> 	for (unsigned int i = 0; i < transfer_count; i++) {
> 		struct pending_transfer_result *transfer = &(block->transfers[i]);
> 		if (transfer->cmd & SWD_CMD_RNW) {
> 			static uint32_t last_read;
> 			uint32_t data = le_to_h_u32(&resp[idx]);
> 			uint32_t tmp = data;
> 			idx += 4;
> 			...
> 		}
> 	}
> ```
>
> 即 OpenOCD 期望的响应布局是 `[cmd][count][ACK][读数据×N]`，而**不是**"每个事务一个 Response 字节"。
> 这一点与 CMSIS-DAP 规范文本（每个事务一个 Transfer Response 字节）的对应关系，**本文标注「未确认」**——
> 我无法只从 OpenOCD 源码判断是规范记错、还是适配器普遍只回报首个 ACK。
> **这一条请用你已验证可用的 F103 抓包来定论**，不要照抄本文。
> （`v0.12.0` 标签的解析逻辑相同但更硬编码：`transfer_count = resp[1]; ack = resp[2] & 0x07; idx = 3;`，见标签 `src/jtag/drivers/cmsis_dap.c:864-885`。）

#### 1.6.6 **posted 语义**在驱动层的体现（对读 AP 寄存器至关重要）

`src/jtag/drivers/cmsis_dap.c:954-974`：

```c
	for (unsigned int i = 0; i < transfer_count; i++) {
		struct pending_transfer_result *transfer = &(block->transfers[i]);
		if (transfer->cmd & SWD_CMD_RNW) {
			static uint32_t last_read;
			uint32_t data = le_to_h_u32(&resp[idx]);
			uint32_t tmp = data;
			idx += 4;

			LOG_DEBUG_IO("Read result: %" PRIx32, data);

			/* Imitate posted AP reads */
			if ((transfer->cmd & SWD_CMD_APNDP) ||
			    ((transfer->cmd & SWD_CMD_A32) >> 1 == DP_RDBUFF)) {
				tmp = last_read;
				last_read = data;
			}

			if (transfer->buffer)
				*(uint32_t *)(transfer->buffer) = tmp;
		}
	}
```

**代码在做什么**（纯代码事实，不做物理层推断）：
对 **AP 读**和 **DP RDBUFF 读**，写进调用者缓冲区的值是**上一次同类读**的返回值，而本次返回值被暂存到静态 `last_read` 里等下一位接收者。

与之配套的是 DP 侧的一深度流水线（`src/target/adi_v5_swd.c:570-593`）：

```c
	swd->read_reg(swd_cmd(true, true, reg), dap->last_read, ap->memaccess_tck);
	dap->last_read = data;

	return check_sync(dap);
```

即：**"上一次 AP 读的目标缓冲区"** 被注册为本次 SWD 读的落点。
收尾靠 `swd_finish_read`（`src/target/adi_v5_swd.c:66-73`）：

```c
static void swd_finish_read(struct adiv5_dap *dap)
{
	const struct swd_driver *swd = adiv5_dap_swd_driver(dap);
	if (dap->last_read) {
		swd->read_reg(swd_cmd(true, false, DP_RDBUFF), dap->last_read, 0);
		dap->last_read = NULL;
	}
}
```

`swd_finish_read` 的调用点：`swd_queue_dp_write_inner` 开头（`:144`）、`swd_queue_ap_write`（`:610`）、`swd_run`（`:628`）。

> **给你的实现的可操作结论（源码事实）**：
> 1. 一次 AP 读**不能单独存在**——后面必须跟一笔 `DP RDBUFF` 读，否则最后一个值收不回来。
> 2. `swd_run()` 里 `swd_finish_read` 在 `swd_run_inner` **之前**调用，所以 RDBUFF 读和 AP 读在**同一个 DAP_Transfer 包**里。
> 3. 任何 **DP 写**（`swd_queue_dp_write_inner` 第一行）都会先把挂起的读冲掉——这直接关系到 §2.4 的 "posted DP 写" 问题。

#### 1.6.7 失败后不自动重试，而是"标记需重连"

`src/jtag/drivers/cmsis_dap.c:983-1002`：

```c
static int cmsis_dap_swd_run_queue(void)
{
	if (cmsis_dap_handle->write_count + cmsis_dap_handle->read_count) {
		if (cmsis_dap_handle->pending_fifo_block_count)
			cmsis_dap_swd_read_process(cmsis_dap_handle, CMSIS_DAP_NON_BLOCKING);

		cmsis_dap_swd_write_from_queue(cmsis_dap_handle);
	}

	while (cmsis_dap_handle->pending_fifo_block_count)
		cmsis_dap_swd_read_process(cmsis_dap_handle, CMSIS_DAP_BLOCKING);

	cmsis_dap_handle->pending_fifo_put_idx = 0;
	cmsis_dap_handle->pending_fifo_get_idx = 0;

	int retval = queued_retval;
	queued_retval = ERROR_OK;

	return retval;
}
```

`queued_retval` 一旦置错，后续入队会被静默跳过（`cmsis_dap.c:1081-1082`）：

```c
	if (queued_retval != ERROR_OK)
		return;
```

→ 这与 §2.5 的 `swd_run` 结合，构成"**一处出错 → 整批作废 → 标记重连 → 下一条命令触发重连**"的恢复模型。

### 1.7 USB 后端（v2 bulk）的报文封装

`src/jtag/drivers/cmsis_dap_usb_bulk.c:583-597`：

```c
static int cmsis_dap_usb_alloc(struct cmsis_dap *dap, unsigned int pkt_sz)
{
	dap->packet_buffer = malloc(pkt_sz);
	if (!dap->packet_buffer) {
		LOG_ERROR("unable to allocate CMSIS-DAP packet buffer");
		return ERROR_FAIL;
	}

	dap->packet_size = pkt_sz;
	dap->packet_buffer_size = pkt_sz;
	/* Prevent sending zero size USB packets */
	dap->packet_usable_size = pkt_sz - 1;

	dap->command = dap->packet_buffer;
	dap->response = dap->packet_buffer;
```

**要点**：
- CMSIS-DAP **v2 (bulk)** 的 OUT/IN 数据**就是裸命令/裸响应，没有 Report ID 前缀**（`dap->command`/`dap->response` 直接指向 `packet_buffer`）。
  对比 HID 后端才需要 `REPORT_ID_SIZE 1`（`src/jtag/drivers/cmsis_dap.h:83`）。
- `packet_usable_size = packet_size - 1`，注释明说是"防止发送零长度 USB 包"。
- 写用 `libusb_fill_bulk_transfer(..., ep_out, buffer, txlen, ...)`（`:561-565`），读用 `ep_in` 且长度 `dap->packet_size`（`:452-456`）。
- 包大小初值来自端点的 `wMaxPacketSize`（`:332`），随后被 `DAP_Info(0xFF)` 的结果覆盖（`cmsis_dap.c:1347-1361`）。

---

## 2. DP 层（SWJ-DP / ADIv5）初始化

### 2.1 `dap_dp_init()` 逐步 —— **Q1/Q2 的答案就在这里**

`src/target/arm_adi_v5.c:779-850`（**完整函数，逐字**）：

```c
/**
 * Initialize a DAP.  This sets up the power domains, prepares the DP
 * for further use and activates overrun checking.
 *
 * @param dap The DAP being initialized.
 */
int dap_dp_init(struct adiv5_dap *dap)
{
	int retval;

	LOG_DEBUG("%s", adiv5_dap_name(dap));

	dap->do_reconnect = false;
	dap_invalidate_cache(dap);

	/*
	 * Early initialize dap->dp_ctrl_stat.
	 * In jtag mode only, if the following queue run (in dap_dp_poll_register)
	 * fails and sets the sticky error, it will trigger the clearing
	 * of the sticky. Without this initialization system and debug power
	 * would be disabled while clearing the sticky error bit.
	 */
	dap->dp_ctrl_stat = CDBGPWRUPREQ | CSYSPWRUPREQ;

	/*
	 * This write operation clears the sticky error and overrun bits in jtag
	 * mode only and is ignored in swd mode. It also powers-up system and
	 * debug domains in both jtag and swd modes, if not done before.
	 */
	retval = dap_queue_dp_write(dap, DP_CTRL_STAT,
				    dap->dp_ctrl_stat | SSTICKYERR | SSTICKYORUN);
	if (retval != ERROR_OK)
		return retval;

	retval = dap_queue_dp_read(dap, DP_CTRL_STAT, NULL);
	if (retval != ERROR_OK)
		return retval;

	retval = dap_queue_dp_write(dap, DP_CTRL_STAT, dap->dp_ctrl_stat);
	if (retval != ERROR_OK)
		return retval;

	/* Check that we have debug power domains activated */
	LOG_DEBUG("DAP: wait CDBGPWRUPACK");
	retval = dap_dp_poll_register(dap, DP_CTRL_STAT,
				      CDBGPWRUPACK, CDBGPWRUPACK,
				      DAP_POWER_DOMAIN_TIMEOUT);
	if (retval != ERROR_OK)
		return retval;

	if (!dap->ignore_syspwrupack) {
		LOG_DEBUG("DAP: wait CSYSPWRUPACK");
		retval = dap_dp_poll_register(dap, DP_CTRL_STAT,
					      CSYSPWRUPACK, CSYSPWRUPACK,
					      DAP_POWER_DOMAIN_TIMEOUT);
		if (retval != ERROR_OK)
			return retval;
	}

	retval = dap_queue_dp_read(dap, DP_CTRL_STAT, NULL);
	if (retval != ERROR_OK)
		return retval;

	/* With debug power on we can activate OVERRUN checking */
	dap->dp_ctrl_stat = CDBGPWRUPREQ | CSYSPWRUPREQ | CORUNDETECT;
	retval = dap_queue_dp_write(dap, DP_CTRL_STAT, dap->dp_ctrl_stat);
	if (retval != ERROR_OK)
		return retval;
	retval = dap_queue_dp_read(dap, DP_CTRL_STAT, NULL);
	if (retval != ERROR_OK)
		return retval;

	retval = dap_run(dap);
	if (retval != ERROR_OK)
		return retval;

	return retval;
}
```

**逐步拆解（含**读 IDCODE/DPIDR 前后各自做了什么**）：**

| # | 动作 | 说明 | 出处 |
|---|---|---|---|
| 0 | **前置**：`dap_connect` → DPIDR 读成功 → 清 sticky → 才进 `dap_dp_init` | DPIDR/IDCODE 的读取**发生在 `dap_dp_init` 之外**（`swd_connect_single`） | `adi_v5_swd.c:368`、`:387-399`、`:456-457` |
| 1 | `dap->do_reconnect = false` | 清重连标志 | `:785` |
| 2 | `dap_invalidate_cache(dap)` | 把 `select`/`select_valid`/`select1_valid`/`select_dpbanksel_valid` 全作废；**并把所有 AP 的 `tar_valid=false, csw_value=0`**（强制下次重写 CSW/TAR） | `:786` → `:756-771` |
| 3 | `dap->dp_ctrl_stat = CDBGPWRUPREQ \| CSYSPWRUPREQ` | = `0x50000000`。注释说明：**先在软件里预设**，这样万一后面 poll 失败触发 sticky 清除，也不会把电源域关掉 | `:795` |
| 4 | **写 `DP_CTRL_STAT` = `dp_ctrl_stat \| SSTICKYERR \| SSTICKYORUN`** | **★ 无条件写，不看当前状态。** 值 = `0x50000000 \| 0x20 \| 0x02` = **`0x50000022`** | `:802-803` |
| 5 | 读 `DP_CTRL_STAT`（结果丢弃 `NULL`） | 一次空读，用于把写"落实"并给后面的 poll 打底 | `:807` |
| 6 | **写 `DP_CTRL_STAT` = `dp_ctrl_stat`** | = **`0x50000000`**（CDBGPWRUPREQ + CSYSPWRUPREQ，无 sticky 位） | `:811` |
| 7 | `dap_dp_poll_register(DP_CTRL_STAT, CDBGPWRUPACK, CDBGPWRUPACK, 10)` | 轮询等 **bit29 置起**；超时返回 `ERROR_WAIT` | `:817-819` |
| 8 | `dap_dp_poll_register(..., CSYSPWRUPACK, ...)` | 轮询等 **bit31 置起**；除非用户设了 `-ignore-syspwrupack` | `:823-830` |
| 9 | 读 `DP_CTRL_STAT`（丢弃） | 又一次空读 | `:832` |
| 10 | **写 `DP_CTRL_STAT` = `dp_ctrl_stat \| CORUNDETECT`** | = **`0x50000001`**（置 bit0） | `:837-838` |
| 11 | 读 `DP_CTRL_STAT`（丢弃） | | `:841` |
| 12 | `dap_run(dap)` | **一次性把上面所有排队的 DP 事务打包发出** | `:845` |

**位定义（`src/target/arm_adi_v5.h:81-96`）：**

```c
/* Fields of the DP's CTRL/STAT register */
#define CORUNDETECT     (1UL << 0)
#define SSTICKYORUN     (1UL << 1)
/* 3:2 - transaction mode (e.g. pushed compare) */
#define SSTICKYCMP      (1UL << 4)
#define SSTICKYERR      (1UL << 5)
#define READOK          (1UL << 6) /* SWD-only */
#define WDATAERR        (1UL << 7) /* SWD-only */
/* 11:8 - mask lanes for pushed compare or verify ops */
/* 21:12 - transaction counter */
#define CDBGRSTREQ      (1UL << 26)
#define CDBGRSTACK      (1UL << 27)
#define CDBGPWRUPREQ    (1UL << 28)
#define CDBGPWRUPACK    (1UL << 29)
#define CSYSPWRUPREQ    (1UL << 30)
#define CSYSPWRUPACK    (1UL << 31)
```

**登记 `DP_CTRL_STAT` 的地址编码**（`src/target/arm_adi_v5.h:38,50`）：

```c
#define BANK_REG(bank, reg)	(((bank) << 4) | (reg))
...
#define DP_CTRL_STAT    BANK_REG(0x0, 0x4) /* DPv0+: rw */
```

→ `DP_CTRL_STAT` = `0x04`，落在 **DP bank 0**。

> ⚠️ **版本差异（会影响你逐字节对比）**
> `v0.12.0` 标签 `src/target/arm_adi_v5.c:698` 是：
> ```c
> 	retval = dap_queue_dp_write(dap, DP_CTRL_STAT, dap->dp_ctrl_stat | SSTICKYERR);
> ```
> 即**只有 `SSTICKYERR`**，值是 `0x50000020`。
> 本机 `eb6f2745b`（`arm_adi_v5.c:802-803`）多了 `| SSTICKYORUN`，值是 `0x50000022`。
> 其余步骤两个版本**逐字相同**（标签在 `:675-745`，主树在 `:779-850`）。
>
> 另外 `v0.12.0` 标签对同一段的中文注释少了 "and overrun bits" 字样（标签 `:693-697`）。

### 2.2 `dap_dp_poll_register()` 的超时语义

`src/target/arm_adi_v5.h:674-701`：

```c
static inline int dap_dp_poll_register(struct adiv5_dap *dap, unsigned int reg,
				       uint32_t mask, uint32_t value, int timeout)
{
	assert(timeout > 0);
	assert((value & mask) == value);

	int ret;
	uint32_t regval;
	LOG_DEBUG("DAP: poll %x, mask 0x%08" PRIx32 ", value 0x%08" PRIx32,
		  reg, mask, value);
	do {
		ret = dap_dp_read_atomic(dap, reg, &regval);
		if (ret != ERROR_OK)
			return ret;

		if ((regval & mask) == value)
			break;

		alive_sleep(10);
	} while (--timeout);

	if (!timeout) {
		LOG_DEBUG("DAP: poll %x timeout", reg);
		return ERROR_WAIT;
	} else {
		return ERROR_OK;
	}
}
```

超时常量（`src/target/arm_adi_v5.c:749`）：

```c
#define DAP_POWER_DOMAIN_TIMEOUT (10)
```

→ **最多 10 轮，每轮之间 `alive_sleep(10)` ms，即约 100 ms 上限**；每轮都是"读—判断—睡"。
→ 超时**返回 `ERROR_WAIT`**（不是 `ERROR_TIMEOUT_REACHED`），这一点在写自己的实现时容易搞错。
→ `dap_dp_read_atomic` = 入队一次 DP 读 + 立刻 `dap_run`（`arm_adi_v5.h:662-672`），所以**每次轮询都是一个独立的 USB 往返**。
→ 它**只用于上电握手**（`dap_dp_init`），**不用于内存访问重试**。

### 2.3 `DP SELECT` 什么时候写 —— **Q4 的答案**

`DP SELECT` 的地址（`src/target/arm_adi_v5.h:57`）：

```c
#define DP_SELECT       BANK_REG(0x0, 0x8) /* DPv0+: JTAG: rw; SWD: wo */
```

字段定义（`src/target/arm_adi_v5.h:100-107`）：

```c
#define ADIV5_DP_SELECT_APSEL	0xFF000000
#define ADIV5_DP_SELECT_APBANK	0x000000F0
#define DP_SELECT_DPBANK		0x0000000F
/*
 * Mask of AP ADDR in select cache, concatenating DP SELECT and DP_SELECT1.
 * In case of ADIv5, the mask contains both APSEL and APBANKSEL fields.
 */
#define SELECT_AP_MASK			(~(uint64_t)DP_SELECT_DPBANK)
```

**注意：本版本没有 `dap_ap_select()` 函数**（两棵树都没有；子代理穷尽 grep 确认）。
APSEL/APBANKSEL 的写入真身是 `swd_queue_ap_bankselect()`（`src/target/adi_v5_swd.c:519-568`，见 §3.2）。

**DP 寄存器 bank 的选择**在 `swd_queue_dp_bankselect()`（`src/target/adi_v5_swd.c:96-120`）：

```c
/** Select the DP register bank */
static int swd_queue_dp_bankselect(struct adiv5_dap *dap, unsigned int reg)
{
	/* Only register address 0 (ADIv6 only) and 4 are banked. */
	if (is_adiv6(dap) ? (reg & 0xf) > 4 : (reg & 0xf) != 4)
		return ERROR_OK;

	uint32_t sel = (reg >> 4) & DP_SELECT_DPBANK;

	/* ADIv6 ensures DPBANKSEL = 0 after line reset */
	if ((dap->select_valid || (is_adiv6(dap) && dap->select_dpbanksel_valid))
			&& (sel == (dap->select & DP_SELECT_DPBANK)))
		return ERROR_OK;

	/* Use the AP part of dap->select regardless of dap->select_valid:
	 * if !dap->select_valid
	 * dap->select contains a speculative value likely going to be used
	 * in the following swd_queue_ap_bankselect() */
	sel |= (uint32_t)(dap->select & SELECT_AP_MASK);

	LOG_DEBUG_IO("DP BANK SELECT: %" PRIx32, sel);

	/* dap->select cache gets updated in the following call */
	return swd_queue_dp_write_inner(dap, DP_SELECT, sel);
}
```

**DP 写路径**（`src/target/adi_v5_swd.c:137-176`）：

```c
static int swd_queue_dp_write_inner(struct adiv5_dap *dap, unsigned int reg,
		uint32_t data)
{
	int retval = ERROR_OK;
	const struct swd_driver *swd = adiv5_dap_swd_driver(dap);
	assert(swd);

	swd_finish_read(dap);

	if (reg == DP_SELECT) {
		dap->select = data | (dap->select & (0xffffffffull << 32));

		swd->write_reg(swd_cmd(false, false, reg), data, 0);

		retval = check_sync(dap);
		dap->select_valid = (retval == ERROR_OK);
		dap->select_dpbanksel_valid = dap->select_valid;

		return retval;
	}

	if (reg == DP_SELECT1)
		dap->select = ((uint64_t)data << 32) | (dap->select & 0xffffffffull);

	/* DP_ABORT write is not banked.
	 * Prevent writing DP_SELECT before as it would fail on locked up DP */
	if (reg != DP_ABORT)
		retval = swd_queue_dp_bankselect(dap, reg);

	if (retval == ERROR_OK) {
		swd->write_reg(swd_cmd(false, false, reg), data, 0);

		retval = check_sync(dap);
	}

	if (reg == DP_SELECT1)
		dap->select1_valid = (retval == ERROR_OK);

	return retval;
}
```

**结论（源码事实）：**

1. **什么时候写**：满足以下任一条件时写 `DP SELECT`
   - 缓存无效（`dap->select_valid == false`）——例如 `dap_invalidate_cache()` 之后、连接/重连之后；
   - 目标 bank 与缓存不符（`sel != (dap->select & 0xF)`，只对 `reg & 0xf == 4` 的 DP 寄存器）；
   - AP 访问时 AP 部分不同（`swd_queue_ap_bankselect` 里的 `sel_diff` 判断，见 §3.2）。
2. **写什么**：DP 场景是 `sel = DPBANKSEL`（低 4 位）`| (dap->select & ~0xF)`；AP 场景是 `(ap_num << 24) | (reg & 0xF0) | DPBANKSEL`。
3. **「上一笔 DP 写还是 posted 状态时会不会写 SELECT」**——
   **会，而且 OpenOCD 不为此做任何额外等待。**
   - 每笔 DP 写的**第一件事**是 `swd_finish_read(dap)`（`:144`），它只处理**挂起的 AP 读**（补一笔 `DP_RDBUFF` 读），**不等待上一笔 DP 写完成**；
   - `DP SELECT` 的写是作为**普通 DP 写**入队的（`swd->write_reg(...)`），与前后事务**同序**排在同一个 `DAP_Transfer` 包里，由 SWD 协议本身保证先后；
   - 唯一的例外是 `DP_ABORT`：**它前面绝不写 SELECT**（`:161-164` 注释："Prevent writing DP_SELECT before as it would fail on locked up DP"）。
4. **`dap_dp_init` 的第一笔 DP 访问真的会先写一次 SELECT**：
   `dap_invalidate_cache()` 把 `select_valid` 置 false → 第一个 `dap_queue_dp_write(DP_CTRL_STAT, ...)` 走 `swd_queue_dp_bankselect(DP_CTRL_STAT)`，
   `DP_CTRL_STAT` 的 `reg & 0xf == 4` 不被早退，且 `select_valid == false`，于是 **先入队一笔 `DP SELECT = 0x00000000`**。
   之后的同类访问因为 `select_valid` 已为 true 且 `sel` 相同，**不再重写 SELECT**。

### 2.4 DP 写是 posted 的 —— 代码里怎么体现

**SWD 层的 posting 只体现在 AP 读上**（DP 读不需要冲刷），机制见 §1.6.6：

- `swd_finish_read()`（`src/target/adi_v5_swd.c:66-73`）补发一笔 `DP_RDBUFF` 读，把最后一笔 AP 读的值收回来。
- **三个调用点**：
  - `swd_queue_dp_write_inner` 开头（`adi_v5_swd.c:144`）—— **任何 DP 写之前**
  - `swd_queue_ap_write`（`adi_v5_swd.c:610`）—— **任何 AP 写之前**
  - `swd_run`（`adi_v5_swd.c:628`）—— 每次 `dap_run` 收尾
- `DP_RDBUFF` 地址（`src/target/arm_adi_v5.h:58`）：`#define DP_RDBUFF BANK_REG(0x0, 0xC) /* DPv0+: ro */`

`src/target/adi_v5_swd.c:621-637`：

```c
/** Executes all queued DAP operations. */
static int swd_run(struct adiv5_dap *dap)
{
	int retval = swd_multidrop_select(dap);
	if (retval != ERROR_OK)
		return retval;

	swd_finish_read(dap);

	retval = swd_run_inner(dap);
	if (retval != ERROR_OK) {
		/* fault response */
		dap->do_reconnect = true;
	}

	return retval;
}
```

**给你的实现的可照抄结论**：
- 结构体里需要一个"挂起读目标指针"字段（OpenOCD 叫 `dap->last_read`）。
- 顺序固定为 `[AP 读][AP 读]...[RDBUFF 读]`，且 RDBUFF 读**与最后一笔 AP 读同包**。
- 读 AP 寄存器和读内存**都必须走这个机制**，否则拿到的是上一笔的值。

### 2.5 ABORT 与 sticky 错误 —— **Q3 / Q5 的答案**

**（a）sticky 清除走的是 DP 的 ABORT 寄存器**

`src/target/adi_v5_swd.c:75-82`：

```c
static void swd_clear_sticky_errors(struct adiv5_dap *dap)
{
	const struct swd_driver *swd = adiv5_dap_swd_driver(dap);
	assert(swd);

	swd->write_reg(swd_cmd(false, false, DP_ABORT),
		STKCMPCLR | STKERRCLR | WDERRCLR | ORUNERRCLR, 0);
}
```

`DP_ABORT` 与位定义（`src/target/arm_adi_v5.h:46,67-72`）：

```c
#define DP_ABORT        BANK_REG(0x0, 0x0) /* DPv1+: SWD: wo */
...
/* Fields of the DP's AP ABORT register */
#define DAPABORT        (1UL << 0)
#define STKCMPCLR       (1UL << 1) /* SWD-only */
#define STKERRCLR       (1UL << 2) /* SWD-only */
#define WDERRCLR        (1UL << 3) /* SWD-only */
#define ORUNERRCLR      (1UL << 4) /* SWD-only */
```

→ 清 sticky 的**确切值** = `(1<<1)|(1<<2)|(1<<3)|(1<<4)` = **`0x1E`**，写到 **DP ABORT（DP bank0 地址 0x00）**。

**（b）`DAPABORT`（真正的 abort 请求）只在连接阶段收到 WAIT 时发一次**

`src/target/adi_v5_swd.c:444-454`（在 `swd_connect` 里）：

```c
	if (status == ERROR_WAIT) {
		LOG_WARNING("Connecting DP: stalled AP operation, issuing ABORT");

		dap->do_reconnect = false;

		status = swd_queue_dp_write_inner(dap, DP_ABORT,
			DAPABORT | STKCMPCLR | STKERRCLR | WDERRCLR | ORUNERRCLR);

		if (status == ERROR_OK)
			status = swd_run_inner(dap);
	}
```

→ 值 = **`0x1F`**。

另有一处多drop路径写 `DP_ABORT = ORUNERRCLR`（`0x10`）（`src/target/adi_v5_swd.c:216-224`），STM32 单 DP **不走这条**。

**（c）AP 侧的 ABORT 通道存在但从未被调用**

`src/target/arm_adi_v5.h:632-636`：

```c
static inline int dap_queue_ap_abort(struct adiv5_dap *dap, uint8_t *ack)
{
	assert(dap->ops);
	return dap->ops->queue_ap_abort(dap, ack);
}
```

其实现 `swd_queue_ap_abort()`（`src/target/adi_v5_swd.c:470-486`）**自己带 TODO 注释**：

```c
	/* TODO: Send DAPABORT in swd_multidrop_select_inner()
	 * in the case the multidrop dap is not selected?
	 * swd_queue_ap_abort() is not currently used anyway...
	 */
```

全树 grep 确认：`dap_queue_ap_abort` / `swd_queue_ap_abort` **没有任何调用者**；
并且源码里**不存在 AP ABORT 寄存器**（没有 `AP_REG_ABORT` 之类的定义）。
**AP 侧的 abort 也是写同一个 DP ABORT 寄存器**（这是个常见误解）。

**（d）失败时会不会判死？（Q3）**

**不会立刻判死，但也绝不自动重试目标侧错误。** 分层看：

| 层 | 遇到 WAIT / FAULT 的处理 | 出处 |
|---|---|---|
| CMSIS-DAP 适配器 | **WAIT 由适配器重试 64 次**（`tfer_configure(0, 64, 0)`） | `cmsis_dap.c:1407-1410` |
| 驱动响应解析 | ACK≠OK → `swd_ack_to_error_code()` → WAIT=`ERROR_WAIT`、FAULT=`ERROR_SWD_FAULT`；`goto skip`，**本包剩余事务全部作废** | `cmsis_dap.c:933-940` |
| SWD 层 | `swd_run()` 只要 `swd_run_inner` 非 OK，就 `dap->do_reconnect = true` | `adi_v5_swd.c:630-634` |
| 恢复 | 下一次 `swd_queue_dp_read/write`、`swd_queue_ap_read/write` 会先调 `swd_check_reconnect()`；若 `do_reconnect` 则执行**完整 `swd_connect()`**（重发 136 位激活序列 + 读 DPIDR + 清 sticky + `dap_dp_init`） | `adi_v5_swd.c:462-468`、`:491`、`:508`、`:577`、`:602` |
| 内存访问 | **不重试、不发 ABORT**，错误直接冒泡到调用者 | `arm_adi_v5.c:577-586`（只打印 TAR 便于定位） |

`src/target/adi_v5_swd.c:462-468`：

```c
static int swd_check_reconnect(struct adiv5_dap *dap)
{
	if (dap->do_reconnect)
		return swd_connect(dap);

	return ERROR_OK;
}
```

**另一条独立的恢复入口** `dap_dp_init_or_reconnect()`（`src/target/arm_adi_v5.c:852-879`）：

```c
/**
 * Initialize a DAP or do reconnect if DAP is not accessible.
 *
 * @param dap The DAP being initialized.
 */
int dap_dp_init_or_reconnect(struct adiv5_dap *dap)
{
	LOG_DEBUG("%s", adiv5_dap_name(dap));

	/*
	 * Early initialize dap->dp_ctrl_stat.
	 * In jtag mode only, if the following atomic reads fail and set the
	 * sticky error, it will trigger the clearing of the sticky. Without this
	 * initialization system and debug power would be disabled while
	 * clearing the sticky error bit.
	 */
	dap->dp_ctrl_stat = CDBGPWRUPREQ | CSYSPWRUPREQ;

	dap->do_reconnect = false;

	dap_dp_read_atomic(dap, DP_CTRL_STAT, NULL);
	if (dap->do_reconnect) {
		/* dap connect calls dap_dp_init() after transport dependent initialization */
		return dap->ops->connect(dap);
	} else {
		return dap_dp_init(dap);
	}
}
```

**调用点只有 2 处**，都在 Cortex-M 的 reset 路径（`src/target/cortex_m.c:1820`、`:1861`）。

> **Q3 小结（源码事实）**：写失败（FAULT）**不会判死**，OpenOCD 也不会当场发 ABORT 去救；
> 它做的是**标记 `do_reconnect`，等下一次访问时整套重连**（重发激活序列 + 重新 `dap_dp_init`）。
> 如果重连也失败，`swd_connect_single` 会在 **500 ms** 窗口耗尽后返回错误，上层报
> `Error connecting DP: cannot read IDR`（`adi_v5_swd.c:380-383`）。

### 2.6 结构体字段（写自己的实现时的对照表）

`struct adiv5_dap` 相关字段（`src/target/arm_adi_v5.h:348-437`，节选并注明行号）：

| 字段 | 行号 | 用途 |
|---|---|---|
| `bool do_reconnect` | `:414` | SWD 层错误后置位，下次访问触发完整重连 |
| `uint64_t apsel` | `:367` | `dap apsel` 命令选中值（**不影响内存访问路径**） |
| `select` / `select_valid` / `select1_valid` / `select_dpbanksel_valid` | `:372` 起 | SELECT 缓存与三个有效性标志 |
| `last_read` | 见 `:406` 附近注释 | 挂起读的目标缓冲区指针 |
| `dp_ctrl_stat` | — | 上一次写进 `DP CTRL/STAT` 的值 |

> 注意：**`dap->sticky_err` 这个字段在本版本不存在**（子代理穷尽 grep 确认）。sticky 状态只存在于目标侧的 `DP CTRL/STAT`（`SSTICKYERR` 等，`arm_adi_v5.h:83-86`）。

---

## 3. AP 层与内存访问

### 3.1 进入 AP 之前的顺序（`cortex_m_examine`）

`src/target/cortex_m.c:2583-2624`（节选）：

```c
int cortex_m_examine(struct target *target)
{
	...
	/* hla_target shares the examine handler but does not support
	 * all its calls */
	if (!armv7m->is_hla_target) {
		if (!armv7m->debug_ap) {
			if (cortex_m->apsel == DP_APSEL_INVALID) {
				/* Search for the MEM-AP */
				retval = cortex_m_find_mem_ap(swjdp, &armv7m->debug_ap);
				if (retval != ERROR_OK) {
					LOG_TARGET_ERROR(target, "Could not find MEM-AP to control the core");
					return retval;
				}
			} else {
				armv7m->debug_ap = dap_get_ap(swjdp, cortex_m->apsel);
				if (!armv7m->debug_ap) {
					LOG_TARGET_ERROR(target, "Cannot get AP");
					return ERROR_FAIL;
				}
			}
		}

		armv7m->debug_ap->memaccess_tck = 8;

		retval = mem_ap_init(armv7m->debug_ap);
		if (retval != ERROR_OK)
			return retval;
	}

	if (!target_was_examined(target)) {
		target_set_examined(target);

		/* Read from Device Identification Registers */
		retval = target_read_u32(target, CPUID, &cpuid);
```

`CPUID` 地址（`src/target/cortex_m.h:32`）：

```c
#define CPUID		0xE000ED00
```

**顺序要点**：
1. 因为 `stm32h7x.cfg:86` 写了 `-ap-num 0`，`cortex_m->apsel = 0`（`cortex_m.c:2931`：`cortex_m->apsel = pc->ap_num;`），
   所以走 `dap_get_ap(swjdp, 0)` 分支，**不做 AP 扫描**（不读 IDR）。
   如果是 `dap_find_get_ap`，它会遍历 AP 并读 IDR（`arm_adi_v5.c:1107-1155`）——H7 场景用不到。
2. `memaccess_tck = 8`（`cortex_m.c:2611`）—— AP 访问后插入的 idle 周期数提示。
3. `mem_ap_init()` 会**读一次 AP 的 CFG 寄存器（0xF4）**，见下。
4. 然后才读 `CPUID`（`0xE000ED00`）—— 这是**第一次真正的目标内存读**。

`mem_ap_init()`（`src/target/arm_adi_v5.c:888-938`，节选）：

```c
int mem_ap_init(struct adiv5_ap *ap)
{
	/* check that we support packed transfers */
	uint32_t cfg;
	int retval;
	struct adiv5_dap *dap = ap->dap;

	/* Set ap->cfg_reg before calling mem_ap_setup_transfer(). */
	/* mem_ap_setup_transfer() needs to know if the MEM_AP supports LPAE. */
	retval = dap_queue_ap_read(ap, MEM_AP_REG_CFG(dap), &cfg);
	if (retval != ERROR_OK)
		return retval;

	retval = dap_run(dap);
	if (retval != ERROR_OK)
		return retval;

	ap->cfg_reg = cfg;
	ap->tar_valid = false;
	ap->csw_value = 0;      /* force csw and tar write */

	/* CSW 32-bit size must be supported (IHI 0031F and 0074D). */
	ap->csw_size_supported_mask = BIT(CSW_32BIT);
	ap->csw_size_probed_mask = BIT(CSW_32BIT);
	...
	ap->packed_transfers_supported = false;
	ap->packed_transfers_probed = dap->ti_be_32_quirks ? true : false;
	...
	ap->unaligned_access_bad = dap->ti_be_32_quirks;
```

注意 `MEM_AP_REG_CFG` = `0xF4`（`arm_adi_v5.h:129`）。
**这一步是 AP 读，所以同样受 §2.4 的 posted 机制约束，后面必须跟一笔 `DP RDBUFF` 读。**

### 3.2 CSW：值、缓存策略、什么时候重写

**CSW 地址**（`src/target/arm_adi_v5.h:119,122`）：

```c
#define ADIV5_MEM_AP_REG_CSW    (0x00)
#define ADIV5_MEM_AP_REG_TAR    (0x04)
#define ADIV5_MEM_AP_REG_DRW    (0x0C)		/* RW: Data Read/Write register */
#define ADIV5_MEM_AP_REG_BD0    (0x10)		/* RW: Banked Data register 0-3 */
```

**AP bank 选择（= APSEL/APBANKSEL 的真正写入点）** `src/target/adi_v5_swd.c:519-568`：

```c
/** Select the AP register bank */
static int swd_queue_ap_bankselect(struct adiv5_ap *ap, unsigned int reg)
{
	int retval;
	struct adiv5_dap *dap = ap->dap;
	uint64_t sel;

	if (is_adiv6(dap))
		sel = ap->ap_num | (reg & 0x00000FF0);
	else
		sel = (ap->ap_num << 24) | (reg & ADIV5_DP_SELECT_APBANK);

	uint64_t sel_diff = (sel ^ dap->select) & SELECT_AP_MASK;

	bool set_select = !dap->select_valid || (sel_diff & 0xffffffffull);
	bool set_select1 = is_adiv6(dap) && dap->asize > 32
						&& (!dap->select1_valid
							|| sel_diff & (0xffffffffull << 32));

	if (set_select && set_select1) {
		/* Prepare DP bank for DP_SELECT1 now to save one write */
		sel |= (DP_SELECT1 & 0x000000f0) >> 4;
	} else {
		/* ... */
		sel |= dap->select & DP_SELECT_DPBANK;
	}

	if (set_select) {
		LOG_DEBUG_IO("AP BANK SELECT: %" PRIx32, (uint32_t)sel);

		retval = swd_queue_dp_write(dap, DP_SELECT, (uint32_t)sel);
		if (retval != ERROR_OK)
			return retval;
	}
	...
}
```

→ **ADIv5 的 SELECT 值 = `(ap_num << 24) | (reg & 0xF0) | DPBANKSEL`**。
对 AP0 / CSW：`sel = 0x00000000`；对 AP0 / DRW（0x0C，APBANKSEL=0）：`0x00`；
对 AP0 / BD0（0x10，APBANKSEL=1）：`0x10`。
→ **是缓存的**：只有 `sel_diff` 非零或 `select_valid==false` 才写。

**CSW 组装与缓存**（`src/target/arm_adi_v5.c:94-108`）：

```c
static int mem_ap_setup_csw(struct adiv5_ap *ap, uint32_t csw)
{
	csw |= ap->csw_default;

	if (csw != ap->csw_value) {
		/* LOG_DEBUG("DAP: Set CSW %x",csw); */
		int retval = dap_queue_ap_write(ap, MEM_AP_REG_CSW(ap->dap), csw);
		if (retval != ERROR_OK) {
			ap->csw_value = 0;
			return retval;
		}
		ap->csw_value = csw;
	}
	return ERROR_OK;
}
```

> ★ **所以 MEM-AP 的 CSW 不需要每次重写**——OpenOCD 用 `ap->csw_value` 缓存，值不变就完全不发。
> 强制重写的三个地方：`dap_invalidate_cache()`（`arm_adi_v5.c:769`，置 `csw_value = 0`）、
> `mem_ap_init()`（`:907`）、写失败时（`:102`）。

**CSW 位定义**（`src/target/arm_adi_v5.h:164-176`）：

```c
/* Fields of the MEM-AP's CSW register */
#define CSW_SIZE_MASK		7
#define CSW_8BIT		0
#define CSW_16BIT		1
#define CSW_32BIT		2
#define CSW_64BIT		3
#define CSW_128BIT		4
#define CSW_256BIT		5
#define CSW_ADDRINC_MASK    (3UL << 4)
#define CSW_ADDRINC_OFF     0UL
#define CSW_ADDRINC_SINGLE  (1UL << 4)
#define CSW_ADDRINC_PACKED  (2UL << 4)
#define CSW_DEVICE_EN       (1UL << 6)
#define CSW_TRIN_PROG       (1UL << 7)
```

`src/target/arm_adi_v5.h:182-203`：

```c
#define CSW_SPIDEN          (1UL << 23)
#define CSW_DBGSWENABLE     (1UL << 31)

/* AHB: Privileged */
#define CSW_AHB_HPROT1          (1UL << 25)
/* AHB: set HMASTER signals to AHB-AP ID */
#define CSW_AHB_MASTER_DEBUG    (1UL << 29)
/* AHB5: non-secure access via HNONSEC
 * AHB3: SBO, UNPREDICTABLE if zero */
#define CSW_AHB_SPROT           (1UL << 30)
/* AHB: initial value of csw_default */
#define CSW_AHB_DEFAULT         (CSW_AHB_HPROT1 | CSW_AHB_MASTER_DEBUG | CSW_DBGSWENABLE)
```

**`csw_default` 的初值**（`src/target/arm_dap.c:33-52`）：

```c
static void dap_instance_init(struct adiv5_dap *dap)
{
	int i;
	/* Set up with safe defaults */
	for (i = 0; i <= DP_APSEL_MAX; i++) {
		dap->ap[i].dap = dap;
		dap->ap[i].ap_num = DP_APSEL_INVALID;
		/* memaccess_tck max is 255 */
		dap->ap[i].memaccess_tck = 255;
		/* Number of bits for tar autoincrement, impl. dep. at least 10 */
		dap->ap[i].tar_autoincr_block = (1<<10);
		/* default CSW value */
		dap->ap[i].csw_default = CSW_AHB_DEFAULT;
		dap->ap[i].cfg_reg = MEM_AP_REG_CFG_INVALID; /* mem_ap configuration reg (large physical addr, etc.) */
		dap->ap[i].refcount = 0;
		dap->ap[i].config_ap_never_release = false;
	}
	...
}
```

→ `CSW_AHB_DEFAULT = 0x02000000 | 0x20000000 | 0x80000000` = **`0xA2000000`**。
（注意：`mem_ap.c` **不是** MEM-AP 逻辑所在文件，本版本的 `mem_ap.c` 是"无核 target 驱动"。）

**32 位字访问的最终 CSW 值推导：**

| 场景 | 计算 | 结果 |
|---|---|---|
| 单字读/写（走 `BD0`，见 §3.3） | `csw_default 0xA2000000 \| CSW_32BIT(0x2)`，AddrInc 继承 `ap->csw_value` 的旧值（初值 0） | **`0xA2000002`** |
| 块读/写（32 位、自增） | `csw_default 0xA2000000 \| CSW_32BIT(0x2) \| CSW_ADDRINC_SINGLE(0x10)` | **`0xA2000012`** |
| packed 8/16 位 | `0xA2000000 \| size \| CSW_ADDRINC_PACKED(0x20)` | `0xA2000022` 等 |
| **STM32H7 上的 32 位块写**（见下） | `csw_default` 变成 `0xAA000000`，再 `\| 0x12` | **`0xAA000012`** |

> ⚠️ **任务书里出现的 `0x23000052` 不是本源码能产生的值。**
> `0x23000052` 含 bit24（`0x01000000`）和 bit6（`0x40` = `CSW_DEVICE_EN`）。
> 本文两棵树中**没有任何 define 会置 bit24**；而 `CSW_DEVICE_EN`（bit6）虽然定义在
> `arm_adi_v5.h:175`，但**全代码零引用**（子代理穷尽 grep `src/**/*.c` 确认）。
> `0x23000052` 的真实出处**未确认**（很可能是 pyOCD 或你自己代码的写法），本文不做推测。

> 🔴 **STM32H7 专属：`stm32h7x.cfg` 会额外改 `csw_default`！**
> `tcl/target/stm32h7x.cfg:158-164`：
> ```tcl
>    # Set CSW[27], which according to ARM ADI v5 appendix E1.4 maps to AHB signal
>    # HPROT[3], which according to AMBA AHB/ASB/APB specification chapter 3.7.3
>    # makes the data access cacheable. This allows reading and writing data in the
>    # CPU cache from the debugger, which is far more useful than going straight to
>    # RAM when operating on typical variables, and is generally no worse when
>    # operating on special memory locations.
>    $_CHIPNAME.dap apcsw 0x08000000 0x08000000
> ```
> `apcsw` 的处理（`src/target/arm_adi_v5.c:2708-2721`）：
> ```c
> 	case 2:
> 		COMMAND_PARSE_NUMBER(u32, CMD_ARGV[0], csw_val);
> 		COMMAND_PARSE_NUMBER(u32, CMD_ARGV[1], csw_mask);
> 		if (csw_mask & (CSW_SIZE_MASK | CSW_ADDRINC_MASK)) {
> 			LOG_ERROR("CSW mask cannot include 'Size' and 'AddrInc' bit-fields");
> 			return ERROR_COMMAND_ARGUMENT_INVALID;
> 		}
> 		ap = dap_get_config_ap(dap, dap->apsel);
> 		if (!ap) {
> 			command_print(CMD, "Cannot get AP");
> 			return ERROR_FAIL;
> 		}
> 		ap->csw_default = (ap->csw_default & ~csw_mask) | (csw_val & csw_mask);
> ```
> → `csw_default = (0xA2000000 & ~0x08000000) | 0x08000000` = **`0xAA000000`**。
> **即：STM32H7 上 OpenOCD 实际下发的 CSW 是 `0xAA0000xx`（bit27 置 1），而不是默认的 `0xA20000xx`。**
> 如果网页端固定用某个 CSW 常量，这一条值得逐位对比。

### 3.3 TAR 与自增回绕边界 —— **1KB 还是 4KB？答案是"取决于内核"**

**回绕边界字段**（`src/target/arm_adi_v5.h:309`）：`uint32_t tar_autoincr_block;`

**默认值 1KB**（`src/target/arm_dap.c:42-43`）：

```c
		/* Number of bits for tar autoincrement, impl. dep. at least 10 */
		dap->ap[i].tar_autoincr_block = (1<<10);
```

→ `1<<10` = **1024 = 0x400（1KB）**。同样的默认值还出现在 `src/target/arm_adi_v5.c:1223`。

**★ 但 Cortex-M3/M4 会被改成 4KB**（`src/target/cortex_m.c:2719-2724`）：

```c
		if (!armv7m->is_hla_target) {
			if (cortex_m->core_info->flags & CORTEX_M_F_TAR_AUTOINCR_BLOCK_4K)
				/* Cortex-M3/M4 have 4096 bytes autoincrement range,
				 * s. ARM IHI 0031C: MEM-AP 7.2.2 */
				armv7m->debug_ap->tar_autoincr_block = (1 << 12);
		}
```

**哪些核带这个标志**（`src/target/cortex_m.c:54-140`，抓关键行）：

```
 54: 		.name = "Cortex-M0",
 59: 		.name = "Cortex-M0+",
 64: 		.name = "Cortex-M1",
 69: 		.name = "Cortex-M3",
 71: 		.flags = CORTEX_M_F_TAR_AUTOINCR_BLOCK_4K,
 75: 		.name = "Cortex-M4",
 77: 		.flags = CORTEX_M_F_HAS_FPV4 | CORTEX_M_F_TAR_AUTOINCR_BLOCK_4K,
 81: 		.name = "Cortex-M7",
 83: 		.flags = CORTEX_M_F_HAS_FPV5,
 87: 		.name = "Cortex-M23",
 92: 		.name = "Cortex-M33",
 94: 		.flags = CORTEX_M_F_HAS_FPV5,
```

> 🔴🔴 **这正好把 STM32F103 与 STM32H7B0 分到了两边：**
>
> | 芯片 | 内核 | `CORTEX_M_F_TAR_AUTOINCR_BLOCK_4K` | `tar_autoincr_block` |
> |---|---|---|---|
> | STM32F103 | Cortex-M3 | **有**（`cortex_m.c:69-71`） | **4096 字节（4KB）** |
> | STM32H7B0 | Cortex-M7 | **无**（`cortex_m.c:81-83` 只有 `CORTEX_M_F_HAS_FPV5`） | **1024 字节（1KB）** |
>
> 也就是说：**在 M3 上按 4KB 边界做长块传输是安全的，搬到 M7 上就会在 1KB 处提前回绕。**
> 这是本文发现的两颗芯片在"内存访问"层面最直接、最可复现的源码级差异。
> （**注意：本文只陈述源码事实，不宣称这就是你 H7B0 失败的根因**——请按 §6 自行核对。）

**回绕判断与处理**（`src/target/arm_adi_v5.c:83-86` 与 `:185-195`）：

```c
static uint32_t max_tar_block_size(uint32_t tar_autoincr_block, target_addr_t address)
{
	return tar_autoincr_block - ((tar_autoincr_block - 1) & address);
}
```

```c
/* mem_ap_update_tar_cache is called after an access to MEM_AP_REG_DRW
 */
static void mem_ap_update_tar_cache(struct adiv5_ap *ap)
{
	if (!ap->tar_valid)
		return;

	uint32_t inc = mem_ap_get_tar_increment(ap);
	if (inc >= max_tar_block_size(ap->tar_autoincr_block, ap->tar_value))
		ap->tar_valid = false;
	else
		ap->tar_value += inc;
}
```

**机制**：OpenOCD **不硬编码任何 0x400/0x1000 常量**（子代理确认全树无 `TAR_AUTOINCREMENT_MASK` 之类）。
它维护 `ap->tar_value` + `ap->tar_valid` 的影子副本；每次 DRW 访问后算一次增量，
一旦"下一次增量会越过块边界"就把 `tar_valid=false`，于是**下一次传输会显式重写 TAR**。
增量由 CSW 决定（`src/target/arm_adi_v5.c:157-181`）：

```c
static uint32_t mem_ap_get_tar_increment(struct adiv5_ap *ap)
{
	switch (ap->csw_value & CSW_ADDRINC_MASK) {
	case CSW_ADDRINC_SINGLE:
		switch (ap->csw_value & CSW_SIZE_MASK) {
		case CSW_8BIT:
			return 1;
		case CSW_16BIT:
			return 2;
		case CSW_32BIT:
			return 4;
		...
		case CSW_ADDRINC_PACKED:
			return 4;
	}
	return 0;
}
```

**TAR 写入函数**（`src/target/arm_adi_v5.c:110-128`）：

```c
static int mem_ap_setup_tar(struct adiv5_ap *ap, target_addr_t tar)
{
	if (!ap->tar_valid || tar != ap->tar_value) {
		/* LOG_DEBUG("DAP: Set TAR %x",tar); */
		int retval = dap_queue_ap_write(ap, MEM_AP_REG_TAR(ap->dap), (uint32_t)(tar & 0xffffffffUL));
		if (retval == ERROR_OK && is_64bit_ap(ap)) {
			/* See if bits 63:32 of tar is different from last setting */
			if (!ap->tar_valid || (ap->tar_value >> 32) != (tar >> 32))
				retval = dap_queue_ap_write(ap, MEM_AP_REG_TAR64(ap->dap), (uint32_t)(tar >> 32));
		}
		if (retval != ERROR_OK) {
			ap->tar_valid = false;
			return retval;
		}
		ap->tar_value = tar;
		ap->tar_valid = true;
	}
	return ERROR_OK;
}
```

→ **TAR 也是"变了才写"**。
→ `mem_ap_setup_transfer()` 的顺序是 **先 CSW 再 TAR**（`src/target/arm_adi_v5.c:214-220`）。

**单个字读写走的是 banked 寄存器 BD0，不是 DRW**（`src/target/arm_adi_v5.c:237-252`、`:289-305`）：

```c
int mem_ap_read_u32(struct adiv5_ap *ap, target_addr_t address,
		uint32_t *value)
{
	int retval;

	/* Use banked addressing (REG_BDx) to avoid some link traffic
	 * (updating TAR) when reading several consecutive addresses.
	 */
	retval = mem_ap_setup_transfer(ap,
			CSW_32BIT | (ap->csw_value & CSW_ADDRINC_MASK),
			address & 0xFFFFFFFFFFFFFFF0ull);
	if (retval != ERROR_OK)
		return retval;

	return dap_queue_ap_read(ap, MEM_AP_REG_BD0(ap->dap) | (address & 0xC), value);
}
```

→ **TAR 被对齐到 16 字节**（`address & ~0xF`），地址低 4 位编码进 BD0~BD3 的寄存器号（`| (address & 0xC)`）。
这就是为什么读一个字不需要动 TAR：**只要地址落在同一个 16 字节行内，TAR 都不用重写**。

**块传输的最大字数**：
- 逻辑层：`mem_ap_read/write` 内部按 `max_tar_block_size()` 自动切段，**没有显式上限**（每段长度由 CSW/边界决定）。
- 驱动层单包上限（`src/jtag/drivers/cmsis_dap.c:1062`）：`unsigned int max_transfer_count = block_cmd ? 65535 : 255;`
- 包大小上限：`tfer_max_command_size = tfer_max_response_size = packet_usable_size`（`cmsis_dap.c:1366-1369`），
  USB bulk 下 `packet_usable_size = packet_size - 1`（`cmsis_dap_usb_bulk.c:594`）。
- **实际每包字数 ≈ `min(65535, (packet_usable_size - 4) / 4)`**（块读响应头 4 字节，每字 4 字节；见 `cmsis_dap.c:1019-1030`）。

> 版本差异：`v0.12.0` 标签**完全不使用 `DAP_TransferBlock`**（宏有定义但无使用），
> 所以"单包 65535 笔"只对 `eb6f2745b`（= 本机 xpack）成立。标签下单包最多 255 笔且没有块命令。

### 3.4 DRW 的读写语义与块传输

**块写的实现骨架**（`src/target/arm_adi_v5.c:518-589`，节选）：

```c
	while (nbytes > 0) {
		unsigned int this_size;
		retval = mem_ap_setup_transfer_verify_size_packing_fallback(ap,
					size, address ^ ti_be_addr_xor,
					addrinc, pack && nbytes >= 4, &this_size);
		if (retval != ERROR_OK)
			return retval;

		/* How many source bytes each transfer will consume, and their location in the DRW,
		 * depends on the type of transfer and alignment. See ARM document IHI0031C. */
		uint32_t drw_byte_idx = address;
		unsigned int drw_ops = DIV_ROUND_UP(this_size, 4);

		while (drw_ops--) {
			uint32_t outvalue = 0;
			...
			unsigned int drw_bytes = MIN(this_size, 4);
			while (drw_bytes--)
				outvalue |= (uint32_t)*buffer++ <<
							8 * ((drw_byte_idx++ & 3) ^ ti_be_lane_xor);

			retval = dap_queue_ap_write(ap, MEM_AP_REG_DRW(dap), outvalue);
			if (retval != ERROR_OK)
				break;
		}
		if (retval != ERROR_OK)
			break;

		mem_ap_update_tar_cache(ap);
		nbytes -= this_size;
		if (addrinc)
			address += this_size;
	}

	/* REVISIT: Might want to have a queued version of this function that does not run. */
	if (retval == ERROR_OK)
		retval = dap_run(dap);

	if (retval != ERROR_OK) {
		target_addr_t tar;
		if (mem_ap_read_tar(ap, &tar) == ERROR_OK)
			LOG_ERROR("Failed to write memory at " TARGET_ADDR_FMT, tar);
		else
			LOG_ERROR("Failed to write memory and, additionally, failed to find out where");
	}
```

**要点**：
- 块传输时用 **`DRW`（0x0C）** 而不是 BD0；写入的是 `MEM_AP_REG_DRW`。
- 大量事务**先在主机侧排队**，最后 `dap_run` 一次发出去（`:578`）。
- **失败时额外做一次"读回 TAR"** 来告诉用户写到哪失败了（`:580-586`）→ 这会再产生一次 AP 读 + RDBUFF。
- 对齐：`drw_byte_idx = address` 用于把数据摆到正确的字节道（`8 * (idx & 3)`）。

**块读**（`src/target/arm_adi_v5.c:604-720`）结构类似，但读数据要按 posted 规则处理（§2.4）。
块读的包装函数（`src/target/arm_adi_v5.c:722-743`）：

```c
int mem_ap_read_buf(struct adiv5_ap *ap,
	uint8_t *buffer, uint32_t size, uint32_t count, target_addr_t address);
int mem_ap_write_buf(struct adiv5_ap *ap,
	const uint8_t *buffer, uint32_t size, uint32_t count, target_addr_t address);
int mem_ap_read_buf_noincr(struct adiv5_ap *ap,
	uint8_t *buffer, uint32_t size, uint32_t count, target_addr_t address);
int mem_ap_write_buf_noincr(struct adiv5_ap *ap,
	const uint8_t *buffer, uint32_t size, uint32_t count, target_addr_t address);
```

**`DAP_TransferBlock` 的构造**（`src/jtag/drivers/cmsis_dap.c:798-850`，节选）：

```c
	bool block_cmd = !cmsis_dap_handle->swd_cmds_differ
					 && block->transfer_count >= CMD_DAP_TFER_BLOCK_MIN_OPS;
	block->command = block_cmd ? CMD_DAP_TFER_BLOCK : CMD_DAP_TFER;

	command[0] = block->command;
	command[1] = 0x00;	/* DAP Index */

	unsigned int idx;
	if (block_cmd) {
		h_u16_to_le(&command[2], block->transfer_count);
		idx = 4;	/* The first transfer will store the common DAP register */
	} else {
		command[2] = block->transfer_count;
		idx = 3;
	}

	for (unsigned int i = 0; i < block->transfer_count; i++) {
		...
		if (!block_cmd || i == 0)
			command[idx++] = (cmd >> 1) & 0x0f;

		if (!(cmd & SWD_CMD_RNW)) {
			h_u32_to_le(&command[idx], data);
			idx += 4;
		}
	}
```

→ **块命令只有 1 个请求字节**（`i == 0` 时写），因为它要求包内所有事务的请求字节相同。
→ 块命令的 count 是**小端 16 位**（`h_u16_to_le`），非块命令是 **1 字节**。

**分块策略（三层）**：
1. **硬件边界层**：`max_tar_block_size()` 在 TAR 回绕边界处切断（M7 = 1KB，M3/M4 = 4KB）。
2. **命令层**：`mem_ap_write/read` 的 while 循环按 `this_size` 推进。
3. **USB 包层**：`cmsis_dap_swd_queue_cmd` 在"命令或响应装不下一个包"时切包（`cmsis_dap.c:1064-1077`）。

### 3.5 读写失败（WAIT/FAULT）时的重试与 ABORT 策略

**答案：内存访问失败时 OpenOCD 既不重试也不发 ABORT。** 分层证据：

| 层 | 行为 | 出处 |
|---|---|---|
| 探针 | WAIT 自动重试最多 64 次（`tfer_configure(0,64,0)`） | `cmsis_dap.c:1407-1410` |
| CMSIS-DAP 驱动 | ACK≠OK → 置 `queued_retval`，`goto skip`，**整包剩余事务作废**；不重试 | `cmsis_dap.c:933-940` |
| SWD 层 | `swd_run()` 出错 → `dap->do_reconnect = true` | `adi_v5_swd.c:630-634` |
| MEM-AP 层 | `mem_ap_write/read` 失败 → 读回 TAR 打日志 → 返回错误；**无重试、无 ABORT** | `arm_adi_v5.c:577-586` |

**唯一"重试"性质的循环是上电握手** `dap_dp_poll_register`（10 × 10 ms，`arm_adi_v5.h:674-701`），
它**不用于内存访问**。

**ABORT 的使用时机汇总（全树穷尽）：**

| 位置 | 写的寄存器 | 值 | 触发条件 |
|---|---|---|---|
| `adi_v5_swd.c:80-81` | `DP_ABORT` | `0x1E`（STK/STKERR/WDERR/ORUNERR 四个 CLR） | DPIDR 读成功之后**无条件清一次**；多drop选DP时也调 |
| `adi_v5_swd.c:449-450` | `DP_ABORT` | `0x1F`（再加 `DAPABORT`） | `swd_connect` 收到 `ERROR_WAIT` |
| `adi_v5_swd.c:221` | `DP_ABORT` | `0x10`（仅 `ORUNERRCLR`） | 多drop 选 DP 且不要求清全部 sticky（**STM32 不走**） |

> **Q5 明确答案**：**用的是 DP 的 ABORT 寄存器**（SWD 上的 DP bank0 地址 `0x00`，写-only）。
> 命令就是普通的 `DAP_Transfer` 写事务，请求字节 `0x00`，4 字节数据。
> **不存在"AP ABORT 寄存器"**；`dap_queue_ap_abort()` 这条 API 在本版本**没有任何调用者**。

---

## 4. 跑 flash 算法（"loader"）的完整顺序

### 4.0 先纠正几个任务描述里的函数名（源码里不存在这些名字）

| 任务书里的名字 | 源码里的真名 | 出处 |
|---|---|---|
| `cortex_m_run_algorithm` | **`armv7m_run_algorithm`**（在 `armv7m.c`，注册到 `cortex_m.c`） | `src/target/armv7m.c:511`；注册见 `src/target/cortex_m.c:3204` |
| `struct algorithm` / `target_algorithm` / `algo->stack_*` | **不存在**；`algorithm.h` 全文只有 `mem_param` / `reg_param` | `src/target/algorithm.h`（42 行全文） |
| `stm32h7x_write` / `_stm32h7x_...` / `stm32h7x_flash_write_code` | 全是 **`stm32x_`** 前缀：`stm32x_write` / `stm32x_write_block` / `stm32x_erase` / `stm32x_probe` / `stm32x_flash_write_code` | `src/flash/nor/stm32h7x.c:661` / `:554` / `:463` / `:749` / `:572` |
| `stm32h7x_wait_flash_op_status` | **`stm32x_wait_flash_op_queue`** | `src/flash/nor/stm32h7x.c:263` |
| `cortex_m_reset()` | **不存在**；只有 `cortex_m_assert_reset` / `cortex_m_deassert_reset` / `cortex_m_soft_reset_halt` | `cortex_m.c:1684` / `:1844` / `:1237` |
| `SWJ_Clock`（tcl 命令） | **不存在**；tcl 侧是 `adapter speed`，探针命令是 `CMD_DAP_SWJ_CLOCK`(0x11) | tcl: `stm32h7x.cfg:129`；命令码 `src/jtag/drivers/cmsis_dap.c:119` |
| `dap_ap_select()` | **不存在**；真身 `swd_queue_ap_bankselect()` | `src/target/adi_v5_swd.c:519` |

### 4.1 两条算法执行路径（选错会找不到代码）

```
flash_write()                                   src/flash/nor/core.c:996
 └─ flash_write_unlock_verify()                 src/flash/nor/core.c:730
     ├─ flash_erase_address_range() → flash_driver_erase()   core.c:29
     └─ flash_driver_write()  ← 一次调用写整段  core.c:85
         └─ bank->driver->write = stm32x_write   stm32h7x.c:661
             └─ stm32x_write_block()             stm32h7x.c:554
                 ├─ 同步路径: target_run_algorithm()        target.c:773
                 └─ 异步路径: target_run_flash_async_algorithm()  target.c:930
```

> ⚠️ **通用 flash 核心并不做"固定块大小的分块循环"。**
> `flash_write_unlock_verify()` 把同一 bank 内连续的段合并成一次 `malloc(run_size)`，
> 然后**一次**调用 `flash_driver_write(c, buffer, run_address - c->base, run_size)`
> （`src/flash/nor/core.c:967`）。
> 唯一那个 `1024` 常量属于**空片检查**（`core.c:336` `const int buffer_size = 1024;`），不在写路径上。
> **真正的分块常量 `512 * block_size` 在驱动里**（见 §4.3）。

> ⚠️ **另外，ARMv7-M 路径里没有"用 LR 当断点返回地址"的技巧。**
> 那个技巧确实存在于 OpenOCD 里，但在**别的驱动**（`lpc2000.c` 的 ROM IAP 调用，把 `lr` 指向工作区里预置的 `BKPT`）。
> ARMv7-M 的算法框架要求 **blob 自己以 `BKPT` 结尾**（见 §4.4 与 `src/target/armv7m.c:547-548`）。
> 也**不存在** `0xfffffffe` 之类的 PC 魔数（`cortex_m_store_core_reg_u32` 对 PC 无任何特判）。

### 4.2 前置条件：目标必须已经 HALTED（由驱动自己检查）

三个驱动入口都在最前面检查，**通用 flash 核心不会替你 halt**：

| 检查点 | 出处 | 代码 |
|---|---|---|
| `stm32x_write_block` | `stm32h7x.c:526-528` | `if (target->state != TARGET_HALTED) { LOG_ERROR("Target not halted"); return ERROR_TARGET_NOT_HALTED; }` |
| `stm32x_write` | `stm32h7x.c:669-670` | 同上 |
| `stm32x_erase` | `stm32h7x.c:965-966` | 同上 |

### 4.3 RAM 里算法代码 / 缓冲区的地址怎么定

工作区由 target 层统一管理（`target_alloc_working_area`），地址来自用户 tcl：

- `tcl/target/stm32h7x.cfg:88`：
  ```tcl
  $_CHIPNAME.cpu0 configure -work-area-phys 0x20000000 -work-area-size $_WORKAREASIZE -work-area-backup 0
  ```
  其中 `_WORKAREASIZE` 默认 `0x10000`（64 KB，`stm32h7x.cfg:52-58`）。
- 于是 **H7B0 的工作区 = `0x20000000` 起 64 KB**（DTCM/AXI SRAM 起始），且 `-work-area-backup 0`（**不备份原内容**，加快速度）。

**算法代码块与数据缓冲区的分配**（`src/flash/nor/stm32h7x.c:554-604`，节选）：

```c
	uint32_t data_size = 512 * stm32x_info->part_info->block_size;
	uint32_t buffer_size = 8 + data_size;
	struct working_area *write_algorithm;
	struct working_area *source;
	uint32_t address = bank->base + offset;
	struct reg_param reg_params[6];
	struct armv7m_algorithm armv7m_info;
	int retval = ERROR_OK;

	static const uint8_t stm32x_flash_write_code[] = {
#include "../../../contrib/loaders/flash/stm32/stm32h7x.inc"
	};

	if (target_alloc_working_area(target, sizeof(stm32x_flash_write_code),
			&write_algorithm) != ERROR_OK) {
		LOG_WARNING("no working area available, can't do block memory writes");
		return ERROR_TARGET_RESOURCE_NOT_AVAILABLE;
	}

	retval = target_write_buffer(target, write_algorithm->address,
			sizeof(stm32x_flash_write_code),
			stm32x_flash_write_code);
	if (retval != ERROR_OK) {
		target_free_working_area(target, write_algorithm);
		return retval;
	}

	/* memory buffer */
	while (target_alloc_working_area_try(target, buffer_size, &source) != ERROR_OK) {
		data_size /= 2;
		buffer_size = 8 + data_size;
		if (data_size <= 256) {
			/* we already allocated the writing code, but failed to get a
			 * buffer, free the algorithm */
			target_free_working_area(target, write_algorithm);

			LOG_WARNING("no large enough working area available, can't do block memory writes");
			return ERROR_TARGET_RESOURCE_NOT_AVAILABLE;
		}
	}
```

**关键数字（STM32H7B0）**：
- `block_size = 16`（`stm32h7x.c:186`，H7A/H7B 行）
- `data_size = 512 × 16 = 8192` 字节，`buffer_size = 8 + 8192 = 8200` 字节
- 分配失败时**逐次减半**：8200 → 4104 → 2056 → 1032 → 520，`data_size <= 256` 才放弃
- FIFO 结构：`[0..3]=写指针 wp，[4..7]=读指针 rp，[8..buffer_size-1]=数据区`

> **算法栈怎么办？**
> 源码事实：**ARMv7-M 的算法框架不分配、也不设置算法栈指针。**
> `armv7m_start_algorithm()` 全文（`armv7m.c:536-648`）只做：保存现场 → 写 mem_params → 设 reg_params → 设 xPSR → 可选设 CONTROL → `target_resume`。
> **没有任何 SP/MSP 赋值。**
> 个别架构驱动确实会把栈当普通寄存器参数传进去（例如 v0.12.0 树的 `stm32l4x.c` 传 `"sp"`），
> 但 **STM32H7 的 `stm32x_write_block` 传的 6 个参数是 r0–r5，没有 sp**（`stm32h7x.c:609-621`）。
> 因此结论是：**H7 的算法 blob 不使用栈**（自包含、只用寄存器 + FIFO）。
> 这一点也可以从 blob 的规模（108 字节）和它只做"读 wp/rp、搬运、写 FLASH 寄存器"推断，
> 但**"blob 内部是否用到栈"本文未逐条反汇编验证 → 未确认**。

### 4.4 把 blob 写进 RAM

用 **`target_write_buffer()`**（不是 `target_write_memory`），发生在**任何寄存器写进硬件之前**
（`stm32h7x.c:582-584`；同步路径的例子见 `armv7m.c:931-934`）。

**算法 blob 的确切内容**（`contrib/loaders/flash/stm32/stm32h7x.inc`，主树已含此文件，**共 108 字节**，7 行）：

```
0x46,0x68,0x07,0x68,0x6f,0xb3,0xbf,0x1b,0x42,0xbf,0x7f,0x18,0x3f,0x1a,0x08,0x3f,
0xa7,0x42,0xf6,0xd3,0x4f,0xf0,0x02,0x07,0xef,0x60,0x4f,0xf0,0x04,0x08,0xb4,0xfb,
0xf8,0xf8,0xbf,0xf3,0x4f,0x8f,0x56,0xf8,0x04,0x7b,0x42,0xf8,0x04,0x7b,0xbf,0xf3,
0x4f,0x8f,0x8e,0x42,0x28,0xbf,0x00,0xf1,0x08,0x06,0xb8,0xf1,0x01,0x08,0xf0,0xd1,
0x2f,0x69,0x17,0xf0,0x04,0x0f,0xfb,0xd1,0xdf,0xf8,0x1c,0x80,0x17,0xea,0x08,0x0f,
0x03,0xd1,0x46,0x60,0x01,0x3b,0xd4,0xd1,0x03,0xe0,0x5f,0xf0,0x00,0x08,0xc0,0xf8,
0x04,0x80,0x38,0x46,0x00,0xbe,0x00,0x00,0x00,0x00,0xee,0x07,
```

**尾部关键事实**：偏移 100–101 处是 `0x00, 0xbe` → 小端半字 `0xBE00` = **`BKPT #0`**（Thumb 断点指令）。
这就是算法"跑完自己停下来"的机制，与 `armv7m.c:547-548` 的注释一致：

```c
	/* NOTE: armv7m_run_algorithm requires that each algorithm uses a software breakpoint
	 * at the exit point */
```

### 4.5 寄存器装配与**写入顺序**（PC 不是最后写的！）

**参数定义**（`src/flash/nor/stm32h7x.c:606-621`）：

```c
	armv7m_info.common_magic = ARMV7M_COMMON_MAGIC;
	armv7m_info.core_mode = ARM_MODE_THREAD;

	init_reg_param(&reg_params[0], "r0", 32, PARAM_IN_OUT);		/* buffer start, status (out) */
	init_reg_param(&reg_params[1], "r1", 32, PARAM_OUT);		/* buffer end */
	init_reg_param(&reg_params[2], "r2", 32, PARAM_OUT);		/* target address */
	init_reg_param(&reg_params[3], "r3", 32, PARAM_OUT);		/* count of words (word size = .block_size (bytes) */
	init_reg_param(&reg_params[4], "r4", 32, PARAM_OUT);		/* word size in bytes */
	init_reg_param(&reg_params[5], "r5", 32, PARAM_OUT);		/* flash reg base */

	buf_set_u32(reg_params[0].value, 0, 32, source->address);
	buf_set_u32(reg_params[1].value, 0, 32, source->address + source->size);
	buf_set_u32(reg_params[2].value, 0, 32, address);
	buf_set_u32(reg_params[3].value, 0, 32, count);
	buf_set_u32(reg_params[4].value, 0, 32, stm32x_info->part_info->block_size);
	buf_set_u32(reg_params[5].value, 0, 32, stm32x_info->flash_regs_base);
```

**`armv7m_start_algorithm()` 的顺序**（`src/target/armv7m.c:536-648`，节选）：

```c
	if (target->state != TARGET_HALTED) {
		LOG_TARGET_ERROR(target, "not halted (start target algo)");
		return ERROR_TARGET_NOT_HALTED;
	}

	/* Store all non-debug execution registers to armv7m_algorithm_info context */
	for (unsigned int i = 0; i < armv7m->arm.core_cache->num_regs; i++) {
		struct reg *reg = &armv7m->arm.core_cache->reg_list[i];
		if (!reg->exist)
			continue;

		if (!reg->valid)
			armv7m_get_core_reg(reg);

		if (!reg->valid)
			LOG_TARGET_WARNING(target, "Storing invalid register %s", reg->name);

		armv7m_algorithm_info->context[i] = buf_get_u32(reg->value, 0, 32);
	}

	for (int i = 0; i < num_mem_params; i++) {
		if (mem_params[i].direction == PARAM_IN)
			continue;
		retval = target_write_buffer(target, mem_params[i].address,
				mem_params[i].size,
				mem_params[i].value);
		if (retval != ERROR_OK)
			return retval;
	}

	for (int i = 0; i < num_reg_params; i++) {
		if (reg_params[i].direction == PARAM_IN)
			continue;
		...
		armv7m_set_core_reg(reg, reg_params[i].value);
	}

	{
		/*
		 * Ensure xPSR.T is set to avoid trying to run things in arm
		 * (non-thumb) mode, which armv7m does not support.
		 *
		 * We do this by setting the entirety of xPSR, which should
		 * remove all the unknowns about xPSR state.
		 *
		 * Because xPSR.T is populated on reset from the vector table,
		 * it might be 0 if the vector table has "bad" data in it.
		 */
		struct reg *reg = &armv7m->arm.core_cache->reg_list[ARMV7M_XPSR];
		buf_set_u32(reg->value, 0, 32, 0x01000000);
		reg->valid = true;
		reg->dirty = true;
	}
	...
	/* save previous core mode */
	armv7m_algorithm_info->core_mode = core_mode;

	retval = target_resume(target, 0, entry_point, 1, 1);
```

**顺序总结**：
1. 校验 `ARMV7M_COMMON_MAGIC`
2. 校验目标 HALTED
3. **保存全部寄存器现场**到 `armv7m_algorithm_info->context[]`
4. 写 `mem_params`（本例无）
5. 把 `reg_params` 写进**寄存器缓存**并标 dirty（`armv7m_set_core_reg`）——**此时还没进硬件**
6. **xPSR 整体强制写 `0x01000000`**（bit24 = T 位）——注释解释了为什么要整体写
7. 可选设 `CONTROL`（core_mode）
8. `target_resume(target, 0, entry_point, 1, 1)`

**`cortex_m_resume` → `cortex_m_restore_one`**（`src/target/cortex_m.c:1314-1405`）里：
- `debug_execution = 1` ⇒ 保留工作区不覆盖、**PRIMASK = 1**、**xPSR.T = 1**、PC = entry_point、跳过 bkpt-skip
- 然后 `armv7m_restore_context(target)`（`:1382`）
- 再 `cortex_m_restart_one`（`:1408-1425`）清 `C_HALT`，状态置 **`TARGET_DEBUG_RUNNING`**（`:1424`，注意不是 `TARGET_RUNNING`）

**★ 真正的写入顺序：寄存器缓存索引「降序」**（`src/target/armv7m.c:193-218`）：

```c
int armv7m_restore_context(struct target *target)
{
	int i;
	struct armv7m_common *armv7m = target_to_armv7m(target);
	struct reg_cache *cache = armv7m->arm.core_cache;

	LOG_TARGET_DEBUG(target, " ");

	if (armv7m->pre_restore_context)
		armv7m->pre_restore_context(target);

	/* The descending order of register writes is crucial for correct
	 * packing of ARMV7M_PMSK_BPRI_FLTMSK_CTRL!
	 * See also comments in the register table above */
	for (i = cache->num_regs - 1; i >= 0; i--) {
		struct reg *r = &cache->reg_list[i];

		if (r->exist && r->dirty) {
			int retval = armv7m->arm.write_core_reg(target, r, i, ARM_MODE_ANY, r->value);
			if (retval != ERROR_OK)
				return retval;
		}
	}

	return ERROR_OK;
}
```

（以上为 `src/target/armv7m.c:193-218` 的逐字内容，已按主树核对。
关键是 `for (i = cache->num_regs - 1; i >= 0; i--)` 这一行及其上方注释。）

Cortex-M 的索引表（`src/target/armv7m.h:36-66`、`src/target/armv7m.c:107-140`）：
`R0..R12 = 0..12`，`R13(sp) = 13`，`R14(lr) = 14`，`PC = 15`，`XPSR = 16`，`MSP = 17`，`PSP = 18`，
打包容器 `PMSK_BPRI_FLTMSK_CTRL = 0x14`，`CONTROL`，然后 FPU `D0..D15`，`FPSCR`。

**➡ 于是实际写入顺序是（只写 dirty 的）：**

```
… FPSCR → D15 … D0 → CONTROL → FAULTMASK → BASEPRI → PRIMASK → PMSK容器
  → PSP(18) → MSP(17) → xPSR(16) → PC(15) → LR(14) → SP(13) → R12 … R0
```

> 🔴 **回答任务书里的"PC 最后写这类细节"：源码里 PC 既不是第一个也不是最后一个。**
> 由于是**索引降序**，PC(15) 排在 `xPSR(16)/MSP(17)/PSP(18)` **之后**，但排在 `LR(14)/SP(13)/R12..R0` **之前**。
> 降序的**真实原因**写在注释里：让**打包寄存器（PRIMASK/BASEPRI/FAULTMASK/CONTROL 共用一个容器寄存器）先写进容器，再统一刷进硬件**。
> 如果你自己的实现是"PC 最后写"，那是**另一种**做法，OpenOCD 并不是这么做的——这一点请以源码为准。

**寄存器到底怎么写进硬件**（`src/target/cortex_m.c:398-446`）：

```c
static int cortex_m_store_core_reg_u32(struct target *target,
		uint32_t regsel, uint32_t value)
{
	struct cortex_m_common *cortex_m = target_to_cm(target);
	struct armv7m_common *armv7m = target_to_armv7m(target);
	int retval;
	uint32_t dcrdr;
	int64_t then;

	/* because the DCB_DCRDR is used for the emulated dcc channel
	 * we have to save/restore the DCB_DCRDR when used */
	if (target->dbg_msg_enabled) {
		retval = mem_ap_read_u32(armv7m->debug_ap, DCB_DCRDR, &dcrdr);
		if (retval != ERROR_OK)
			return retval;
	}

	retval = mem_ap_write_u32(armv7m->debug_ap, DCB_DCRDR, value);
	if (retval != ERROR_OK)
		return retval;

	retval = mem_ap_write_u32(armv7m->debug_ap, DCB_DCRSR, regsel | DCRSR_WNR);
	if (retval != ERROR_OK)
		return retval;

	/* check if value is written into register */
	then = timeval_ms();
	while (1) {
		retval = cortex_m_read_dhcsr_atomic_sticky(target);
		if (retval != ERROR_OK)
			return retval;
		if (cortex_m->dcb_dhcsr & S_REGRDY)
			break;
		if (timeval_ms() > then + DHCSR_S_REGRDY_TIMEOUT) {
			LOG_TARGET_ERROR(target, "Timeout waiting for DCRDR transfer ready");
			return ERROR_TIMEOUT_REACHED;
		}
		keep_alive();
	}
	...
```

**顺序**：先写 `DCRDR`（值），**再**写 `DCRSR = REGSEL | DCRSR_WNR`（触发），**然后轮询 DHCSR 的 `S_REGRDY`**。

相关常量：
- `src/target/cortex_m.h:79-82`：`DCB_DHCSR 0xE000EDF0`、`DCB_DCRSR 0xE000EDF4`、`DCB_DCRDR 0xE000EDF8`、`DCB_DEMCR 0xE000EDFC`
- `src/target/cortex_m.h:88`：`#define DCRSR_WNR BIT(16)`
- `src/target/cortex_m.h:129-135`：`DBGKEY (0xA05Ful << 16)`、`C_DEBUGEN BIT(0)`、`C_HALT BIT(1)`、`C_STEP BIT(2)`、`C_MASKINTS BIT(3)`、`S_REGRDY BIT(16)`、`S_HALT BIT(17)`
- `src/target/cortex_m.c:48`：`#define DHCSR_S_REGRDY_TIMEOUT (500)` —— **每次寄存器读写的等待上限 500 ms**

### 4.6 等它停下来：DHCSR / S_HALT / 断点

**等待逻辑**（`src/target/armv7m.c:651-689`）：

```c
	retval = target_wait_state(target, TARGET_HALTED, timeout_ms);
	/* If the target fails to halt due to the breakpoint, force a halt */
	if (retval != ERROR_OK || target->state != TARGET_HALTED) {
		retval = target_halt(target);
		if (retval != ERROR_OK)
			return retval;
		retval = target_wait_state(target, TARGET_HALTED, 500);
		if (retval != ERROR_OK)
			return retval;
		return ERROR_TARGET_TIMEOUT;
	}

	if (exit_point) {
		/* PC value has been cached in cortex_m_debug_entry() */
		uint32_t pc = buf_get_u32(armv7m->arm.pc->value, 0, 32);
		if (pc != exit_point) {
			LOG_TARGET_DEBUG(target, "failed algorithm halted at 0x%" PRIx32 ", expected 0x%" TARGET_PRIxADDR,
					  pc, exit_point);
			return ERROR_TARGET_ALGO_EXIT;
		}
	}
```

**要点**：
- 主等待用调用者给的 `timeout_ms`；**失败后强制 `target_halt()` 再等 500 ms**，仍失败返回 `ERROR_TARGET_TIMEOUT`。
- 停机判定靠 `cortex_m_poll_one` 读 **`DHCSR.S_HALT`**（bit17），进而走 `cortex_m_debug_entry()` 缓存 PC 等寄存器。
- `exit_point != 0` 时才比较 PC，不等则 `ERROR_TARGET_ALGO_EXIT`。
- **H7 的异步 flash 算法传的 `exit_point = 0`**（`stm32h7x.c:630` 的倒数第二个实参），所以 **H7 不检查 PC**，只靠"停了 + FIFO 协议"判断成败。

**停机后恢复现场**（`src/target/armv7m.c:726-752`）：把与保存的 `context[]` 不同的寄存器标 dirty
（**注意：只标 dirty，写回硬件发生在下一次 resume/restore**），再恢复 `core_mode`。

### 4.7 异步 FIFO 算法（ST 系列实际走的就是这条）

`src/target/target.c:930-1079`（节选，主树行号）：

```c
int target_run_flash_async_algorithm(struct target *target,
		const uint8_t *buffer, uint32_t count, int block_size,
		int num_mem_params, struct mem_param *mem_params,
		int num_reg_params, struct reg_param *reg_params,
		uint32_t buffer_start, uint32_t buffer_size,
		uint32_t entry_point, uint32_t exit_point, void *arch_info)
{
	int retval;
	int timeout = 0;

	const uint8_t *buffer_orig = buffer;

	/* Set up working area. First word is write pointer, second word is read pointer,
	 * rest is fifo data area. */
	uint32_t wp_addr = buffer_start;
	uint32_t rp_addr = buffer_start + 4;
	uint32_t fifo_start_addr = buffer_start + 8;
	uint32_t fifo_end_addr = buffer_start + buffer_size;

	uint32_t wp = fifo_start_addr;
	uint32_t rp = fifo_start_addr;

	/* validate block_size is 2^n */
	assert(IS_PWR_OF_2(block_size));

	retval = target_write_u32(target, wp_addr, wp);
	if (retval != ERROR_OK)
		return retval;
	retval = target_write_u32(target, rp_addr, rp);
	if (retval != ERROR_OK)
		return retval;

	/* Start up algorithm on target and let it idle while writing the first chunk */
	retval = target_start_algorithm(target, num_mem_params, mem_params,
			num_reg_params, reg_params,
			entry_point,
			exit_point,
			arch_info);

	if (retval != ERROR_OK) {
		LOG_ERROR("error starting target flash write algorithm");
		return retval;
	}

	while (count > 0) {

		retval = target_read_u32(target, rp_addr, &rp);
		...
		if (rp == 0) {
			LOG_ERROR("flash write algorithm aborted by target");
			retval = ERROR_FLASH_OPERATION_FAILED;
			break;
		}

		if (!IS_ALIGNED(rp - fifo_start_addr, block_size) || rp < fifo_start_addr || rp >= fifo_end_addr) {
			LOG_ERROR("corrupted fifo read pointer 0x%" PRIx32, rp);
			break;
		}
		...
		if (thisrun_bytes == 0) {
			/* Throttle polling a bit if transfer is (much) faster than flash
			 * programming. ... */
			alive_sleep(2);

			/* to stop an infinite loop on some targets check and increment a timeout
			 * this issue was observed on a stellaris using the new ICDI interface */
			if (timeout++ >= 2500) {
				LOG_ERROR("timeout waiting for algorithm, a target reset is recommended");
				return ERROR_FLASH_OPERATION_FAILED;
			}
			continue;
		}
		...
		/* Write data to fifo */
		retval = target_write_buffer(target, wp, thisrun_bytes, buffer);
		...
		/* Store updated write pointer to target */
		retval = target_write_u32(target, wp_addr, wp);
		...
	}

	if (retval != ERROR_OK) {
		/* abort flash write algorithm on target */
		target_write_u32(target, wp_addr, 0);
	}

	int retval2 = target_wait_algorithm(target, num_mem_params, mem_params,
			num_reg_params, reg_params,
			exit_point,
			10000,
			arch_info);
	...
```

**协议与常量**：
- FIFO 布局：`+0` 写指针 `wp`，`+4` 读指针 `rp`，`+8..` 数据区（`target.c:942-947`）
- 初始 `wp = rp = fifo_start_addr`（**空**的判据是 `wp == rp`，所以**永不填满**）
- **目标端放弃的标志是 `rp == 0`**（`target.c:985-989`，收尾时再查一次 `:1071-1078`）
- `rp` 不对齐/越界 → `corrupted fifo read pointer`（`:991-994`）
- FIFO 空转看门狗：**2500 次 × `alive_sleep(2)`**（`:1016-1019`）
- 结束等待：**`target_wait_algorithm(..., 10000, ...)` = 10 秒**（`:1060-1064`）
- 出错时主动写 `wp = 0` 通知目标端中止（`:1055-1058`）

### 4.8 事件驱动层 / 同步算法的超时常量

| 用途 | 值 | 出处 |
|---|---|---|
| `cortex_m_crc` 算法 | `20000 * (1 + count/1MB)` ms | `armv7m.c:945` |
| 擦除检查算法 | `(timed_out ? 30000 : 2000) + total_size * 3/1000` ms | `armv7m.c:1052` |
| 异步 flash 结束等待 | `10000` ms | `target.c:1063` |
| FIFO 空转看门狗 | `2500 × alive_sleep(2)` | `target.c:1012-1019` |
| 算法超时后强制 halt 的再等 | `500` ms | `armv7m.c:675` |
| `S_REGRDY` 轮询上限 | `500` ms | `cortex_m.c:48` |
| DP 上电域轮询 | `10 × alive_sleep(10)` | `arm_adi_v5.c:749` |
| `soft_reset_halt` 等待 | `100 × 1ms` | `cortex_m.c:1275` |
| `assert_reset` 收尾等待 | `jtag_sleep(50000)` = 50 ms | `cortex_m.c:1837` |

**DEMCR / 中断屏蔽**：
- `src/target/cortex_m.h:141-150`：`TRCENA BIT(24)`、`VC_BUSERR BIT(8)`、`VC_HARDERR BIT(10)`、`VC_CORERESET BIT(0)`
- `src/target/cortex_m.c:647`：examine 时写 `TRCENA | armv7m->demcr`
- `src/target/cortex_m.c:1259-1261`：`soft_reset_halt` 写 `DCB_DEMCR = TRCENA | VC_HARDERR | VC_BUSERR | VC_CORERESET`
- `src/target/cortex_m.c:1779-1780`：`reset halt` 时写同一个值（= `0x01000501`）
- ⚠️ **`MON_EN` / `MON_PEND` / `MON_STEP` / `MON_REQ` 在 v0.12.0 全树 0 命中**；
  `cortex_m.c:615` 源码原话是 **"The four debug monitor bits are currently ignored"**。
  **不要**照抄某些教程里的 "DEMCR 要设 MON_EN"。

### 4.9 reset / halt 在烧录前后的处理（H7B0 具体路径）

1. **`program` 命令走 `reset init`**（不是 `reset halt`）。tcl 事件 `reset-init` **只在 `init` 模式触发**，
   所以 `stm32h7x.cfg:201-204` 里的 `adapter speed 4000` **只有 `reset init` 才生效**；
   `reset halt` 会停在 **1800 kHz**。
2. **`reset_config srst_nogate`（`stm32h7x.cfg:147`）只置 `RESET_SRST_NO_GATING`，不置 `RESET_HAS_SRST`**，
   因此 **H7B0 路径实际上不拉 nSRST**，走软件复位（`AIRCR = AIRCR_VECTKEY | AIRCR_SYSRESETREQ = 0x05FA0004`）。
   （常量出处：`cortex_m.h:160,174-177`；写点 `cortex_m.c:1814-1816`。
   `reset_config` 的位语义来自 **v0.12.0 树** `src/jtag/adapter.c:483-497`，**主树未下载该文件 → 该位的解读标注为「未确认（引自 v0.12.0 树）」**。）
3. `cortex_m reset_config sysresetreq`（`stm32h7x.cfg:152`，默认是 VECTRESET，脚本改成软复位）。
4. `assert_reset` 收尾会调 `dap_dp_init_or_reconnect()`（`cortex_m.c:1861`）—— 这就是 §2.5 里那条恢复路径。
5. `stm32h7x.cfg:167-199` 的 `examine-end` 会做 8 次 DBGMCU 读-改-写（非 hla 时经 **AP2**，基址 `0xE00E1000`），
   用来开 DBG 时钟、低功耗下保持调试、停看门狗。**这是 H7 特有的、F103 完全没有的一步。**

### 4.10 STM32H7B0 专属常量（供你核对网页实现）

> ⚠️ **任务书里"H7B0 有 128KB 扇区"是错的。**
> `stm32h7x.c:180-194`（H7A/H7B 行）：
> ```c
> 	{
> 	.id					= DEVID_STM32H7A_H7BXX,
> 	...
> 	.device_str			= "STM32H7Ax/7Bx",
> 	.page_size_kb		= 8,
> 	.block_size			= 16,
> 	.max_flash_size_kb	= 2048,
> 	.max_bank_size_kb	= 1024,
> 	.has_dual_bank		= true,
> 	.fsize_addr			= 0x08FFF80C,
> 	```
> **H7B0 的擦除扇区是 8 KB，写粒度 `block_size = 16` 字节。**
> 128 KB 是 **H74/H75（`:170`）和 H72/H73（`:200`）** 的扇区大小。
> H7B0 的"128 KB"是**整片容量**（FSIZE = 128），不是扇区。

| 项 | 值 | 出处 |
|---|---|---|
| DEVID（H7A/H7B） | `0x480` | `stm32h7x.c:93-95`（`DEVID_STM32H7A_H7BXX`） |
| DEVICE ID 寄存器 | `0x5C001000`（`DBGMCU_IDCODE_REGISTER`） | `stm32h7x.c:86` |
| FLASH 寄存器基址 | `0x52002000`（bank1）；bank2 `0x52002100` | `stm32h7x.c:89-90` |
| FLASH bank 基址 | `0x08000000` | `stm32h7x.c:87` |
| FSIZE 地址 | `0x08FFF80C`（u16，单位 KB） | `stm32h7x.c:190` |
| 扇区大小 | **8 KB** | `stm32h7x.c:185` |
| 写粒度 `block_size` | **16 字节** | `stm32h7x.c:186` |
| 工作区 | `0x20000000` + 64 KB，不备份 | `stm32h7x.cfg:88`、`:57` |
| 写缓冲 | `512 × 16 = 8192`（+8 头）= 8200 字节 | `stm32h7x.c:563-564` |
| 算法入口（entry） | `write_algorithm->address`（工作区内分配） | `stm32h7x.c:630` |
| 算法出口（exit） | **`0`（不检查 PC）** | `stm32h7x.c:630` |
| `r4` 传参 | `block_size = 16` | `stm32h7x.c:620` |
| `r5` 传参 | `flash_regs_base = 0x52002000` | `stm32h7x.c:621` |

**H7A/H7B 与 H74/H75 的 FLASH_CR 拼装不同**（`src/flash/nor/stm32h7x.c:148-162`）：

```c
static uint32_t stm32h74_h75xx_compute_flash_cr(uint32_t cmd, int snb)
{
	return cmd | (snb << 8);
}

static uint32_t stm32h7a_h7bxx_compute_flash_cr(uint32_t cmd, int snb)
{
	/* save FW and START bits, to be right shifted by 2 bits later */
	const uint32_t tmp = cmd & (FLASH_FW | FLASH_START);

	/* mask parallelism (ignored), FW and START bits */
	cmd &= ~(FLASH_PSIZE_64 | FLASH_FW | FLASH_START);

	return cmd | (tmp >> 2) | (snb << 6);
}
```

→ **H7B0 的扇区号是 `snb << 6`（不是 `<< 8`），且 FW/START 位要右移 2 位。**
如果你的网页 flash 算法是照抄 H743 的寄存器布局，**在 H7B0 上会写错寄存器**。

---

## 5. 最小可跑通的**字节级报文序列**

### 5.1 前提与约定

| 项 | 取值 | 来源 |
|---|---|---|
| 探针 | CMSIS-DAP **v2（USB bulk）** | 你描述的场景 |
| `DAP index` | `0x00`（单 DP） | `cmsis_dap.c:803` |
| adapter speed | **1800 kHz** | `stm32h7x.cfg:129` |
| DPIDR（H7，SWD） | `0x6BA02477`（tcl 预期值） | `stm32h7x.cfg:67` |
| AP | **AP0**（`-ap-num 0`） | `stm32h7x.cfg:86` |
| 包大小 | 假设 ≥ 64（示例响应按 64 估） | — |

**`DAP_Transfer`(0x05) 请求字节的推导公式**（`src/jtag/drivers/cmsis_dap.c:844`：`command[idx++] = (cmd >> 1) & 0x0f;`）：

```
请求字节 = APnDP(bit0) | RnW(bit1) | A[2](bit2) | A[3](bit3)
```

**对照表（本文全部报文都用这张表）：**

| 访问 | A[3:2] | APnDP | RnW | 请求字节 | 出处（swd_cmd 输入） |
|---|---|---|---|---|---|
| DP 写 ABORT (0x00) | 00 | 0 | 0 | **0x00** | `arm_adi_v5.h:46` |
| DP 读 DPIDR (0x00) | 00 | 0 | 1 | **0x02** | `arm_adi_v5.h:45` |
| DP 写 CTRL/STAT (0x04) | 01 | 0 | 0 | **0x04** | `arm_adi_v5.h:50` |
| DP 读 CTRL/STAT (0x04) | 01 | 0 | 1 | **0x06** | 同上 |
| DP 写 SELECT (0x08) | 10 | 0 | 0 | **0x08** | `arm_adi_v5.h:57` |
| DP 读 RDBUFF (0x0C) | 11 | 0 | 1 | **0x0E** | `arm_adi_v5.h:58` |
| AP 写 CSW (0x00) | 00 | 1 | 0 | **0x01** | `arm_adi_v5.h:119` |
| AP 读 CSW (0x00) | 00 | 1 | 1 | **0x03** | 同上 |
| AP 写 TAR (0x04) | 01 | 1 | 0 | **0x05** | `arm_adi_v5.h:120` |
| AP 读 TAR (0x04) | 01 | 1 | 1 | **0x07** | 同上 |
| AP 写 DRW (0x0C) | 11 | 1 | 0 | **0x0D** | `arm_adi_v5.h:122` |
| AP 读 DRW (0x0C) | 11 | 1 | 1 | **0x0F** | 同上 |
| AP 写 BD0 (bank1/reg0) | 00 | 1 | 0 | **0x01** | `arm_adi_v5.h:123`（bank 由 SELECT 决定） |
| AP 读 BD0 (bank1/reg0) | 00 | 1 | 1 | **0x03** | 同上 |
| AP 读 CFG (0xF4) | 01 | 1 | 1 | **0x07** | `arm_adi_v5.h:129` |

> **SWD 的奇偶位（cmd bit5）由探针自己算**，`DAP_Transfer` 的请求字节里**不含**奇偶。

**响应布局**：本文按 **OpenOCD 解析器实际消费的布局**书写
（`[cmd][count][ACK][读数据×N]`，见 §1.6.5）。
⚠️ 这与 CMSIS-DAP 规范文本的对应关系**本文标注为「未确认」**，请用你已验证的 F103 抓包核对。

### 5.2 阶段 A —— 传输层初始化（`cmsis_dap_init`）

| # | 发 | 收 | 说明 | 出处 |
|---|---|---|---|---|
| A1 | `00 F0` | `00 01 03` | `DAP_Info` CAPS：len=1，caps=0x03（SWD+JTAG） | `cmsis_dap.c:1151` / `:434-450` |
| A2 | `00 04` | `00 0A …` | `DAP_Info` FW_VER（len=10 + 字符串） | `cmsis_dap.c:1136` |
| A3 | `00 03` | `00 0C …` | `DAP_Info` SERNUM | `cmsis_dap.c:1121` |
| A4 | `02 01` | `02 01` | **`DAP_Connect`(SWD=1)**；校验 `收[1]==0x01` | `cmsis_dap.c:1292` / `:469-488` |
| A5 | `00 FF` | `00 02 40 00` | `DAP_Info` PKT_SZ → 0x0040 = 64 | `cmsis_dap.c:1347` |
| A6 | `00 FE` | `00 01 01` | `DAP_Info` PKT_CNT = 1 | `cmsis_dap.c:1374` |
| A7 | `10 00 00 00 00 00 00` | `10 xx` | `DAP_SWJ_Pins`(pins=0, mask=0, delay=0) 仅读状态 | `cmsis_dap.c:1399` / `:370-389` |
| A8 | `11 40 77 1B 00` | `11 00` | **`DAP_SWJ_Clock` = 1 800 000 Hz**（1800×1000=0x1B7740） | `cmsis_dap.c:1403` / `:391-408` |
| A9 | `04 00 40 00 00 00` | `04 00` | **`DAP_TransferConfigure`(idle=0, retry=64, match_retry=0)** | `cmsis_dap.c:1410` / `:505-521` |
| A10 | `13 00` | `13 00` | **`DAP_SWD_Configure`(cfg=0)** | `cmsis_dap.c:1417` / `:523-537` |
| A11 | `01 00 01` | `01 00` | `DAP_LED`(CONNECT, ON) | `cmsis_dap.c:1424` |
| A12 | `01 01 01` | `01 00` | `DAP_LED`(RUN, ON) | `cmsis_dap.c:1425` |

### 5.3 阶段 B —— SWD 链路激活（`swd_connect_single`）

| # | 发 | 收 | 说明 | 出处 |
|---|---|---|---|---|
| B1 | `12 88 FF FF FF FF FF FF FF 9E E7 FF FF FF FF FF FF FF 00` | `12 00` | **`DAP_SWJ_Sequence`，136 位（0x88）**。第 2 字节是**位长**，其后 17 字节是序列 | `adi_v5_swd.c:345` → `cmsis_dap.c:1276` / `:411-432`；序列定义 `swd.h:115-125` |
| B2 | `05 00 01 02` | `05 01 01 77 24 A0 6B` | **`DAP_Transfer`：第 1 个事务必须是"读 DP DPIDR"（请求 0x02）**。响应数据 = DPIDR `0x6BA02477`（小端） | `adi_v5_swd.c:368` / `:352-366` |
| B3 | `05 00 01 00 1E 00 00 00` | `05 01 01` | **写 DP ABORT = `0x0000001E`**（STKCMPCLR\|STKERRCLR\|WDERRCLR\|ORUNERRCLR） | `adi_v5_swd.c:391` → `:75-82`；位定义 `arm_adi_v5.h:69-72` |

> B2 的响应示例 `05 01 01 77 24 A0 6B` 与任务书里给的例子完全一致 —— 说明 DPIDR 这一段你那边已经对了。
> **如果 B1 被截短成 88 位、或 B2 不是 DPIDR 读，就会命中 `adi_v5_swd.c:359` 注释里的 "other accesses return protocol error"。**

### 5.4 阶段 C —— `dap_dp_init()`（**全部排队后一次性发出**）

`dap_dp_init` 把下面这些**排进队列**，直到第一次 `dap_dp_poll_register` 里的 `dap_run` 才真正发包。

**队列内容（按入队顺序）**：

| 序 | 事务 | 请求字节 | 数据（小端） | 来源 |
|---|---|---|---|---|
| t1 | **DP 写 SELECT** | `08` | `00 00 00 00` | `swd_queue_dp_bankselect`，因 `select_valid==false`（`adi_v5_swd.c:106-119`） |
| t2 | DP 写 CTRL/STAT | `04` | `22 00 00 50`（=`0x50000022`） | `arm_adi_v5.c:802-803` |
| t3 | DP 读 CTRL/STAT（丢弃） | `06` | — | `arm_adi_v5.c:807` |
| t4 | DP 写 CTRL/STAT | `04` | `00 00 00 50`（=`0x50000000`） | `arm_adi_v5.c:811` |
| t5 | DP 读 CTRL/STAT（进 regval） | `06` | — | `dap_dp_poll_register`，`arm_adi_v5.h:685` |

**第 1 个 USB 往返**（`CMD_DAP_TFER`，count=5，因为包内请求字节有 3 种 → `swd_cmds_differ` → 不用块命令，`cmsis_dap.c:798-812`）：

```
发: 05 00 05 08 00 00 00 00  04 22 00 00 50  06  04 00 00 00 50  06
收: 05 05 01  <t3 数据 4B>  <t5 数据 4B>
```

**后续（`CDBGPWRUPACK` 命中后）**：

| # | 发 | 收 | 说明 | 出处 |
|---|---|---|---|---|
| C2 | `05 00 01 06` | `05 01 01 <4B>` | 等 `CSYSPWRUPACK`（bit31）的独立轮询读 | `arm_adi_v5.c:825-827` |
| C3 | `05 00 03 06 04 00 00 00 50 06` | `05 03 01 <4B> <4B>` | 收尾三笔：读 CTRL/STAT、写 CTRL/STAT=`0x50000001`、读 CTRL/STAT | `arm_adi_v5.c:832-845` |

> 🔴 **注意 C3 里那笔"写 CTRL/STAT"的线上数据是 `00 00 00 50`（`0x50000000`），不是 `0x50000001`！**
> 因为 `dap_dp_init` 请求的 `CORUNDETECT`（bit0）在组包时被
> `cmsis_dap_swd_write_from_queue` **主动剔除**了（`cmsis_dap.c:835-841`），
> 目的是保留"探针自动重试 WAIT 64 次"的能力（见 §1.3 与 §1.6.4）。
> **这是 CMSIS-DAP 后端特有的行为**；JTAG 后端不会剔除。

### 5.5 阶段 D —— 读 `0xE000ED00` 的 CPUID

`cortex_m_examine` 先 `mem_ap_init()` 读 AP CFG，再读 CPUID。

**D1：`mem_ap_init()` 读 AP CFG（0xF4）** —— `arm_adi_v5.c:897-903`

```
发: 05 00 03 08 F0 00 00 00  07  0E
收: 05 03 01  <CFG 数据 4B>  <RDBUFF 数据 4B>
```

| 事务 | 请求 | 来源 |
|---|---|---|
| DP 写 SELECT = `0x000000F0` | `08 F0 00 00 00` | `swd_queue_ap_bankselect`：`sel = (0<<24) \| (0xF4 & 0xF0)`（`adi_v5_swd.c:529`） |
| AP 读 CFG（0xF4） | `07` | `swd_queue_ap_read`（`adi_v5_swd.c:589`） |
| DP 读 RDBUFF（冲刷挂起读） | `0E` | `swd_finish_read`（`adi_v5_swd.c:70`） |

> **`0E` 这笔是 OpenOCD 特有的"posted 读冲刷"**，见 §2.4。少发这一笔就会拿到上一笔的值。

**D2：读 CPUID `0xE000ED00`** —— `cortex_m.c:2622`

```
发: 05 00 06  08 00 00 00 00   01 02 00 00 AA   05 00 ED 00 E0   08 10 00 00 00   03   0E
收: 05 06 01   <BD0 数据 4B>    <RDBUFF 数据 4B>
```

| 序 | 事务 | 字节 | 为什么 | 出处 |
|---|---|---|---|---|
| 1 | DP 写 SELECT = `0x00` | `08 00 00 00 00` | 切回 AP bank0（CSW 在 0x00） | `adi_v5_swd.c:529,551-557` |
| 2 | **AP 写 CSW = `0xAA000002`** | `01 02 00 00 AA` | `csw_default(0xAA000000) \| CSW_32BIT(0x2)`；**H7 因为 `apcsw` 所以是 `0xAA…` 而不是 `0xA2…`** | `arm_adi_v5.c:94-108,246`；`stm32h7x.cfg:164` |
| 3 | AP 写 TAR = `0xE000ED00` | `05 00 ED 00 E0` | TAR 变了才写 | `arm_adi_v5.c:110-128` |
| 4 | DP 写 SELECT = `0x10` | `08 10 00 00 00` | 切到 AP bank1（BD0 在 0x10） | `adi_v5_swd.c:529` |
| 5 | AP 读 BD0 | `03` | `MEM_AP_REG_BD0 \| (addr & 0xC)`，这里低 4 位为 0 | `arm_adi_v5.c:251` |
| 6 | DP 读 RDBUFF | `0E` | 冲刷挂起读，**CPUID 的值在这里落到 `&cpuid`** | `adi_v5_swd.c:70` |

**CPUID 期望值**：Cortex-M7 的 partno 字面值是 `0xC27`（`src/target/cortex_m.h:53`：
`CORTEX_M7_PARTNO = ARM_MAKE_CPUID(ARM_IMPLEMENTER_ARM, 0xC27)`）。
**实读典型值 `0x411FC271`** → 小端字节 `71 C2 1F 41`。
⚠️ 三点声明，避免过度断言：
① 示例里的高字节 `0x41` 对应宏 `ARM_IMPLEMENTER_ARM`，但**该宏的数值定义在本文未下载的头文件里
（`cortex_m.h` 只引用、未定义）→ 「未确认」**，本文**不宣称** `0x41` 来自源码；
② 高/低修订位（示例里的 `1`/`1`）随芯片批次不同，**也不是源码常量**；
③ 本文只保证 **partno 掩码字面值 `0xC27` 来自源码**，完整 32 位 CPUID 请以实读为准。

### 5.6 阶段 E —— 写一个 4 字节内存（`0x12345678` → `0x20000000`）

基于 `mem_ap_write_u32()` + `dap_run()` 原语（`src/target/arm_adi_v5.c:289-305` 与 `:318-327`）。

```
发: 05 00 02  05 00 00 00 20   01 78 56 34 12
收: 05 02 01
```

| 序 | 事务 | 字节 | 为什么 | 出处 |
|---|---|---|---|---|
| 1 | AP 写 TAR = `0x20000000` | `05 00 00 00 20` | TAR 与上次不同 → 写 | `arm_adi_v5.c:110-128` |
| 2 | AP 写 BD0 = `0x12345678` | `01 78 56 34 12` | `BD0 \| (addr & 0xC)`，低 4 位为 0 | `arm_adi_v5.c:303-304` |

**这里特别注意两笔"没发"的东西**（这是 OpenOCD 与"每次都重配"的实现最大的区别）：

- **没有重发 CSW**：`ap->csw_value` 已是 `0xAA000002`，`mem_ap_setup_csw` 判断相等直接跳过
  （`arm_adi_v5.c:98-106`）。
- **没有重发 DP SELECT**：`sel_diff = (0x10 ^ 0x10) & ~0xF = 0`，`swd_queue_ap_bankselect` 直接返回
  （`adi_v5_swd.c:531-533,551`）。
- **没有 DRW 也没有 RDBUFF**：写不需要冲刷挂起读（`dap->last_read` 为 NULL，`swd_finish_read` 什么都不做）。

> 高一层（`target_write_u32` / `target_write_memory`）可能还有 target 层的额外步骤（如缓存处理、
> 快速路径选择）。**本文只保证 `mem_ap_write_u32` + `dap_run` 这一层的报文**，
> 更上层的具体路径**未逐行核对 → 未确认**。

### 5.7 端到端汇总（把 A–E 串成一条线，**一个 USB 往返一行**）

> 这是"从 `DAP_Connect` 到读到 CPUID、再到写一个 4 字节内存"的**完整连续序列**，
> 每一行 = 一次 bulk OUT + 一次 bulk IN。
> 多事务包（`count > 1`）的响应按 OpenOCD 解析器的布局书写（见 §1.6.5 的**未确认**提示）。
> 数值前提：CMSIS-DAP v2、SWD、AP0、`adapter speed 1800`、`DPIDR = 0x6BA02477`、H7B0（`apcsw` 已生效）。

```text
# ---------- 阶段 A：传输层初始化（cmsis_dap_init） ----------
发: 00 f0                                      # DAP_Info(CAPS)
收: 00 01 03
发: 00 04                                      # DAP_Info(FW_VER)
收: 00 0a xx xx xx xx xx xx xx xx xx xx
发: 00 03                                      # DAP_Info(SERNUM)
收: 00 0c xx xx xx xx xx xx xx xx xx xx xx xx
发: 02 01                                      # DAP_Connect(SWD=1)   <-- DAP_Connect
收: 02 01
发: 00 ff                                      # DAP_Info(PKT_SZ)
收: 00 02 40 00
发: 00 fe                                      # DAP_Info(PKT_CNT)
收: 00 01 01
发: 10 00 00 00 00 00 00                        # DAP_SWJ_Pins 仅读状态
收: 10 xx
发: 11 40 77 1b 00                             # DAP_SWJ_Clock = 1 800 000 Hz
收: 11 00
发: 04 00 40 00 00 00                          # DAP_TransferConfigure(0, 64, 0)
收: 04 00
发: 13 00                                      # DAP_SWD_Configure(0)
收: 13 00
发: 01 00 01                                      # DAP_LED(CONNECT, ON)
收: 01 00
发: 01 01 01                                      # DAP_LED(RUN, ON)
收: 01 00

# ---------- 阶段 B：SWD 链路激活（swd_connect_single） ----------
发: 12 88 ff ff ff ff ff ff ff 9e e7 ff ff ff ff ff ff ff 00   # SWJ_Sequence 136 位
收: 12 00
发: 05 00 01 02                                # 读 DP DPIDR（激活后第一笔，必须）
收: 05 01 01 77 24 a0 6b                       #   -> 0x6BA02477
发: 05 00 01 00 1e 00 00 00                     # 写 DP ABORT = 0x1E 清 sticky
收: 05 01 01

# ---------- 阶段 C：dap_dp_init（排队后一次性发出） ----------
发: 05 00 05 08 00 00 00 00  04 22 00 00 50  06  04 00 00 00 50  06
收: 05 05 01 xx xx xx xx  xx xx xx xx
发: 05 00 01 06                                # 等 CSYSPWRUPACK 的读
收: 05 01 01 xx xx xx xx
发: 05 00 03 06  04 00 00 00 50  06             # 读 / 写 CTRL/STAT / 读
收: 05 03 01 xx xx xx xx  xx xx xx xx

# ---------- 阶段 D：mem_ap_init 读 AP CFG ----------
发: 05 00 03 08 f0 00 00 00  07  0e
收: 05 03 01 xx xx xx xx  xx xx xx xx

# ---------- 阶段 D：读 CPUID @ 0xE000ED00 ----------
发: 05 00 06 08 00 00 00 00  01 02 00 00 aa  05 00 ed 00 e0  08 10 00 00 00  03  0e
收: 05 06 01 71 c2 1f 41  xx xx xx xx            # CPUID = 0x411FC271（典型值，见 §5.5）
                                                #   CSW=0xAA000002  TAR=0xE000ED00  SELECT=0x10

# ---------- 阶段 E：写 4 字节 0x12345678 -> 0x20000000 ----------
发: 05 00 02 05 00 00 00 20  01 78 56 34 12     # TAR=0x20000000  BD0=0x12345678
收: 05 02 01
```

**这条序列里"发"的字节全部可从源码逐字节复现**（对照表见 §5 各阶段表格与 §8 ④ 的来源表）。
**"收"里的 `xx` 是硬件相关结果**（固件版本串、CFG、CTRL/STAT 实际值），本文不臆造。

**三个最值得你盯住的"少了就不对"的点**（都在上表里）：

1. **B 阶段第 1 行必须是完整 136 位激活序列**（`12 88 …`），且**紧接着第 2 行必须是 `05 00 01 02`（读 DPIDR）**
   —— 中间不能插任何其它 DAP 事务。
2. **D 阶段读 CPUID 的那一包里，最后必须是 `03 0e`（AP 读 BD0 + DP 读 RDBUFF）**
   —— 少了 `0e` 这笔，`cpuid` 拿到的就不是本次的值。
3. **E 阶段只有 2 笔**（TAR + BD0），**没有 CSW、没有 SELECT、没有 RDBUFF**
   —— 这是缓存生效的正常表现。如果你的实现每次都重发 CSW/SELECT，功能上通常也对，
   但一旦某处缓存逻辑写错（比如把 `tar_valid` 处理错），就会在 **M7 的 1KB 边界**上翻车（见 §3.3）。

---

## 6. 与网页实现的差异核对表（**右列留空，由你填写**）

> 用法：逐行读左列（= OpenOCD 源码事实，含出处），在右列写下你网页版的实际行为。
> 不一致的行就是嫌疑点。**§6.9 的三行是本文认为最值得先查的。**

### 6.1 传输层

| # | OpenOCD 的做法（源码事实） | 出处 | 你的网页实现 |
|---|---|---|---|
| 1 | 打开设备后先最多 64 次 × 10 ms 读，把残留响应冲干净，再发第一条命令 | `cmsis_dap.c:311-324` | |
| 2 | `DAP_Connect` 参数 = `0x01`(SWD)，校验 `resp[1] == 0x01`（**不是** `DAP_OK`） | `cmsis_dap.c:1292,482-485` | |
| 3 | `DAP_SWJ_Clock` 参数 = 频率 **Hz**（tcl 的 kHz × 1000），小端 4 字节 | `cmsis_dap.c:396-399` | |
| 4 | `DAP_TransferConfigure(idle=0, retry=64, match_retry=0)` | `cmsis_dap.c:1410` | |
| 5 | `DAP_SWD_Configure(cfg=0)`（无 Data Phase、1 TRN） | `cmsis_dap.c:1417` | |
| 6 | 单命令收发严格配对：发完立刻收，校验 `resp[0]==cmd`，不匹配则 cancel_all + 清管道 | `cmsis_dap.c:342-365` | |
| 7 | USB 超时 `6000 ms`；清管道用 `10 ms` | `cmsis_dap.c:343,348`；`libusb_helper.h:26` | |
| 8 | 用 `DAP_Info(0xFF)` 读到的包大小重建缓冲区，`usable = size - 1` | `cmsis_dap.c:1347-1361`；`cmsis_dap_usb_bulk.c:594` | |
| 9 | v2 bulk 收发**不带 Report ID 前缀** | `cmsis_dap_usb_bulk.c:596-597` | |
| 10 | 探针可挂起包数 = `min(DAP_Info(0xFE), 4)`；为 1 时退化为严格同步 | `cmsis_dap.c:1374-1384`；`cmsis_dap.h:19` | |
| 11 | 一包内请求字节全同且 ≥4 笔时才用 `DAP_TransferBlock`(0x06) | `cmsis_dap.c:166,798-800` | |
| 12 | 块命令 count 是**小端 16 位**；非块命令 count 是**1 字节** | `cmsis_dap.c:806-812` | |
| 13 | 对 `DP CTRL/STAT` 的 `CORUNDETECT` 写入会被**主动剔除** | `cmsis_dap.c:835-841` | |
| 14 | ACK: `0x1`=OK、`0x2`=WAIT、`0x4`=FAULT；响应 bit3 = 协议/奇偶错 | `arm_adi_v5.h:31-33`；`cmsis_dap.c:928-937` | |
| 15 | 适配器自动重试 WAIT 64 次，**代价是不启用 sticky overrun 检测** | `cmsis_dap.c:1407-1420` | |

### 6.2 SWD 链路激活

| # | OpenOCD 的做法（源码事实） | 出处 | 你的网页实现 |
|---|---|---|---|
| 16 | `DAP_Connect(SWD)` **之前**只发 `DAP_Info`/`DAP_SWJ_Pins`/`DAP_SWJ_Clock`/`DAP_TransferConfigure`/`DAP_SWD_Configure` | `cmsis_dap.c:1302-1420` | |
| 17 | 激活序列 **136 位**：`FF×7 9E E7 FF×7 00`（第 2 字节 `0x88`） | `swd.h:115-125` | |
| 18 | 位序 **LSB-first**（每字节先发 bit0） | `swd.h:96,113` | |
| 19 | 激活序列**只在 `swd_init` 之后、第一次 connect 时发一次**，之后不重复 | `adi_v5_swd.c:334-346` | |
| 20 | **激活后第一笔 DAP 事务必须是"读 DP DPIDR"（请求 0x02）** | `adi_v5_swd.c:352-368` | |
| 21 | 读 DPIDR 之前**不写 DP SELECT** | `adi_v5_swd.c:361-366` | |
| 22 | 整段（发序列+读 DPIDR）在 **500 ms** 内重试，每轮 `alive_sleep(1)`，并交替走两套切换序列 | `adi_v5_swd.c:338,375-378` | |
| 23 | DPIDR 成功后**立刻写 DP ABORT = `0x1E`** 清 sticky；WAIT 就 `alive_sleep(10)` 重试，同受 500 ms 约束 | `adi_v5_swd.c:387-399` | |
| 24 | 只有 sticky 清理也 WAIT 时，才写 DP ABORT = `0x1F`（带 DAPABORT） | `adi_v5_swd.c:444-454` | |
| 25 | `reset_config` 含 `connect_assert_srst` 时才在 connect 前拉 SRST | `adi_v5_swd.c:419-429` | |

### 6.3 DP 层

| # | OpenOCD 的做法（源码事实） | 出处 | 你的网页实现 |
|---|---|---|---|
| 26 | **无条件**写 `DP CTRL/STAT`（不看当前值） | `arm_adi_v5.c:802-803` | |
| 27 | 第 1 次写 = `0x50000022`（**dev**）或 `0x50000020`（**v0.12.0 标签**） | `arm_adi_v5.c:802`；标签 `:698` | |
| 28 | 中间插一次"读 CTRL/STAT 丢弃" | `arm_adi_v5.c:807` | |
| 29 | 第 2 次写 = `0x50000000`（CDBGPWRUPREQ\|CSYSPWRUPREQ） | `arm_adi_v5.c:811` | |
| 30 | 轮询 `CDBGPWRUPACK`(bit29)：**10 轮 × 10 ms**，超时返回 `ERROR_WAIT` | `arm_adi_v5.c:817-819,749`；`arm_adi_v5.h:674-701` | |
| 31 | 轮询 `CSYSPWRUPACK`(bit31)，同样 10×10 ms（除非 `-ignore-syspwrupack`） | `arm_adi_v5.c:823-830` | |
| 32 | 第 3 次写 = `0x50000001`（加 CORUNDETECT）；**CMSIS-DAP 后端会把它剔成 `0x50000000`** | `arm_adi_v5.c:837-838`；`cmsis_dap.c:835-841` | |
| 33 | 结尾再读两次 CTRL/STAT | `arm_adi_v5.c:841-843` | |
| 34 | 首次 DP 访问会**先写一次 `DP SELECT = 0`**（因为 `select_valid == false`） | `arm_adi_v5.c:786`；`adi_v5_swd.c:106-119` | |
| 35 | 之后 `DP SELECT` 只在"缓存无效 / bank 变了 / AP 部分变了"时写 | `adi_v5_swd.c:531-557` | |
| 36 | 任何 DP 写之前先冲刷挂起的 AP 读（补一笔 `DP_RDBUFF`），但**不等上一笔 DP 写完成** | `adi_v5_swd.c:144` | |
| 37 | `DP_ABORT` 写之前**绝不写 SELECT** | `adi_v5_swd.c:161-164` | |
| 38 | sticky 清除 = 写 **DP ABORT**（DP bank0 地址 0x00）= `0x1E` | `adi_v5_swd.c:80-81` | |
| 39 | 出错 → `do_reconnect = true` → 下一次访问**整套重连**（重发激活序列 + 重读 DPIDR + 重跑 `dap_dp_init`） | `adi_v5_swd.c:630-634,462-468` | |

### 6.4 AP 层与内存访问

| # | OpenOCD 的做法（源码事实） | 出处 | 你的网页实现 |
|---|---|---|---|
| 40 | `SELECT` 编码（ADIv5）= `(ap_num << 24) \| (reg & 0xF0) \| DPBANKSEL` | `adi_v5_swd.c:529` | |
| 41 | `CSW` 组装 = `size \| addrinc \| csw_default`，**只在变化时才写** | `arm_adi_v5.c:94-108` | |
| 42 | `csw_default` 初值 = `CSW_AHB_DEFAULT` = `0xA2000000` | `arm_dap.c:45`；`arm_adi_v5.h:193` | |
| 43 | **H7 因 `apcsw 0x08000000 0x08000000`，`csw_default` 变成 `0xAA000000`** | `stm32h7x.cfg:164`；`arm_adi_v5.c:2720` | |
| 44 | `CSW_DEVICE_EN`(bit6) **定义了但全代码零引用**（OpenOCD 不置 bit6） | `arm_adi_v5.h:175`（0 引用） | |
| 45 | 32 位单字访问 CSW = `0xA2000002`（H7：`0xAA000002`） | `arm_adi_v5.c:246` | |
| 46 | 32 位块访问 CSW = `0xA2000012`（H7：`0xAA000012`） | `arm_adi_v5.c:381-383` | |
| 47 | **单字读写走 banked 寄存器 `BD0`，TAR 对齐到 16 字节** | `arm_adi_v5.c:245-251,297-303` | |
| 48 | `TAR` 也是"变了才写" | `arm_adi_v5.c:110-128` | |
| 49 | **TAR 自增回绕：默认 1 KB；Cortex-M3/M4 = 4 KB；Cortex-M7 = 1 KB** | `arm_dap.c:43`；`cortex_m.c:2719-2724` | |
| 50 | 回绕靠 `max_tar_block_size()` + `tar_valid` 影子副本，**没有硬编码常量** | `arm_adi_v5.c:83-86,185-195` | |
| 51 | `mem_ap_setup_transfer` 顺序：**先 CSW 再 TAR** | `arm_adi_v5.c:214-220` | |
| 52 | 块写：循环把数据塞进 `DRW`(0x0C)，**全部排队后一次 `dap_run`** | `arm_adi_v5.c:531-566,578` | |
| 53 | 块读：一次 `calloc` 全排队后一次 `dap_run` | `arm_adi_v5.c:630,666` | |
| 54 | 每包上限：块 65535 笔 / 非块 255 笔；再受包大小约束 | `cmsis_dap.c:1062,1366-1369` | |
| 55 | **内存访问失败：不重试、不发 ABORT**，只报错 + 读回 TAR 便于定位 | `arm_adi_v5.c:577-586` | |
| 56 | **AP 读必须跟一笔 `DP RDBUFF` 读**才算完成 | `adi_v5_swd.c:66-73` | |
| 57 | `mem_ap_init()` 会先读一次 AP CFG (0xF4) | `arm_adi_v5.c:897-903` | |

### 6.5 Flash loader

| # | OpenOCD 的做法（源码事实） | 出处 | 你的网页实现 |
|---|---|---|---|
| 58 | 目标必须 HALTED，由 flash 驱动自己检查（核心不替你 halt） | `stm32h7x.c:526-528` 等 | |
| 59 | 工作区来自 tcl：`0x20000000` + 64 KB，`-work-area-backup 0` | `stm32h7x.cfg:88,57` | |
| 60 | 先分配算法代码工作区，再用 `target_write_buffer` 写入 blob | `stm32h7x.c:576-588` | |
| 61 | 再分配 FIFO 缓冲 `8 + 512*block_size`，失败逐次减半 | `stm32h7x.c:563-602` | |
| 62 | FIFO 布局：`+0` wp、`+4` rp、`+8..` 数据；初始两者相等 | `target.c:942-947` | |
| 63 | 参数经 `r0..r5` 传（**没有 sp**，框架不设算法栈） | `stm32h7x.c:609-621`；`armv7m.c:536-648` | |
| 64 | 先保存全部寄存器现场，再设参数，**再整体写 xPSR = `0x01000000`** | `armv7m.c:560-623` | |
| 65 | **寄存器写入顺序 = 索引降序**（xPSR → PC → LR → SP → R12…R0） | `armv7m.c:207-215` | |
| 66 | 写寄存器的硬件顺序：先 `DCRDR`，再 `DCRSR = REGSEL\|WNR`，再轮询 `S_REGRDY` | `cortex_m.c:415-436` | |
| 67 | `S_REGRDY` 等待上限 **500 ms** | `cortex_m.c:48,431-434` | |
| 68 | 算法 blob 以 **`BKPT`（`00 BE`）** 结尾自停 | `stm32h7x.inc` 偏移 100 | |
| 69 | H7 异步算法 **exit_point = 0 → 不检查 PC** | `stm32h7x.c:630` | |
| 70 | 异步结束等待 **10000 ms**；FIFO 空转看门狗 **2500 × 2 ms** | `target.c:1063,1012-1019` | |
| 71 | 目标端放弃的标志是 **`rp == 0`** | `target.c:985-989,1071-1078` | |
| 72 | 出错时主机主动写 `wp = 0` 通知目标端中止 | `target.c:1055-1058` | |
| 73 | `assert_reset` 用 `AIRCR = 0x05FA0004`（软复位） | `cortex_m.c:1814-1816` | |
| 74 | `reset halt` 时写 `DEMCR = 0x01000501`（TRCENA\|VC_HARDERR\|VC_BUSERR\|VC_CORERESET） | `cortex_m.c:1779-1780` | |
| 75 | **`MON_EN` 等 4 个 monitor 位在 0.12 里被忽略**（源码原话） | `cortex_m.c:615` | |
| 76 | `reset-init` 事件**只在 `reset init` 触发**，`reset halt` 不会提速到 4000 kHz | v0.12.0 树 `startup.tcl`（**未在主树核对 → 未确认**） | |
| 77 | H7 的 `examine-end` 会经 AP2 做 8 次 DBGMCU 读-改-写 | `stm32h7x.cfg:167-199` | |

### 6.6 H7B0 专属

| # | OpenOCD 的做法（源码事实） | 出处 | 你的网页实现 |
|---|---|---|---|
| 78 | H7B0 = DEVID `0x480`，FLASH 寄存器基址 `0x52002000` | `stm32h7x.c:93-95,89` | |
| 79 | **擦除扇区 8 KB**（不是 128 KB） | `stm32h7x.c:185` | |
| 80 | 写粒度 `block_size = 16` 字节 | `stm32h7x.c:186` | |
| 81 | FSIZE 在 `0x08FFF80C`（u16，KB） | `stm32h7x.c:190` | |
| 82 | **扇区号 `snb << 6`**（H74/H75 是 `<< 8`）；FW/START 位右移 2 位 | `stm32h7x.c:153-162` | |
| 83 | `r5` 传 `0x52002000`；`r4` 传 16 | `stm32h7x.c:620-621` | |
| 84 | 写缓冲 8200 字节 | `stm32h7x.c:563-564` | |

### 6.7 最容易出问题的三行（建议优先核对）

| # | 要点 | 为什么值得先查 |
|---|---|---|
| **★49** | **TAR 自增回绕边界：F103(M3) = 4 KB，H7B0(M7) = 1 KB** | **这是两颗芯片在内存访问层面最直接、最可复现的源码级差异。** 若网页版按 4 KB 边界做长块传输，在 F103 上正常、在 H7 上会提前回绕写错地址 |
| **★20/21** | **激活后第一笔必须是"读 DPIDR"，且之前不能写 DP SELECT** | 源码注释明写 "other accesses return protocol error"。SWD 表现为立即失败/FAULT，与"从一开始就失败"的症状吻合 |
| **★17** | **激活序列是 136 位，不是 88 位** | 若网页版按 88 位发（旧写法），SWJ-DP 可能进不了 SWD 状态 |

> 再次强调：以上只是**源码事实对照**，本文**不宣称**其中任何一条就是你 H7B0 失败的根因。

---

## 7. 用源码原文回答 Q1–Q5

### Q1：OpenOCD 会不会**无条件**写 `DP CTRL/STAT`？如果目标已经上电，它写不写？

**答：会，无条件写；目标已上电也照样写。**

`src/target/arm_adi_v5.c:795-813`：

```c
	dap->dp_ctrl_stat = CDBGPWRUPREQ | CSYSPWRUPREQ;

	/*
	 * This write operation clears the sticky error and overrun bits in jtag
	 * mode only and is ignored in swd mode. It also powers-up system and
	 * debug domains in both jtag and swd modes, if not done before.
	 */
	retval = dap_queue_dp_write(dap, DP_CTRL_STAT,
				    dap->dp_ctrl_stat | SSTICKYERR | SSTICKYORUN);
	if (retval != ERROR_OK)
		return retval;

	retval = dap_queue_dp_read(dap, DP_CTRL_STAT, NULL);
	if (retval != ERROR_OK)
		return retval;

	retval = dap_queue_dp_write(dap, DP_CTRL_STAT, dap->dp_ctrl_stat);
	if (retval != ERROR_OK)
		return retval;
```

**依据**：
- 代码里**没有任何"先读判断"的分支**——`dap->dp_ctrl_stat` 是在主机侧直接赋值的常量表达式
  （`:795` 赋 `CDBGPWRUPREQ | CSYSPWRUPREQ`），紧接着就 `dap_queue_dp_write`。
- 它**甚至不读回 CTRL/STAT 来判断 CDBGPWRUPACK**；判断放在后面的 `dap_dp_poll_register`（`:817`、`:825`），
  而那是在**写完两次之后**。
- 注释 "powers-up system and debug domains in both jtag and swd modes, **if not done before**"
  是描述硬件语义（再写一次请求位是幂等的），**不是**软件层面的条件分支。

**整段 `dap_dp_init` 里一共有 3 次 `DP CTRL/STAT` 写，全部是无条件的**：

| 次 | 值（dev `eb6f2745b`） | 值（v0.12.0 标签） | 出处 |
|---|---|---|---|
| 1 | `0x50000022` | `0x50000020` | `:802-803` / 标签 `:698` |
| 2 | `0x50000000` | `0x50000000` | `:811` / 标签 `:706` |
| 3 | `0x50000001`（CMSIS-DAP 后端会剔成 `0x50000000`） | 同 | `:837-838` / 标签 `:732-733` |

### Q2：`DP CTRL/STAT` 上电请求用的是哪几个位、确切值是多少？

**答：`CDBGPWRUPREQ` = bit28，`CSYSPWRUPREQ` = bit30；确切值 `0x50000000`。**

`src/target/arm_adi_v5.h:93-96`：

```c
#define CDBGPWRUPREQ    (1UL << 28)
#define CDBGPWRUPACK    (1UL << 29)
#define CSYSPWRUPREQ    (1UL << 30)
#define CSYSPWRUPACK    (1UL << 31)
```

**逐值展开：**

| 写次 | 表达式 | 位 | 值 |
|---|---|---|---|
| 1 | `CDBGPWRUPREQ \| CSYSPWRUPREQ \| SSTICKYERR \| SSTICKYORUN`（dev） | 28,30,5,1 | **`0x50000022`** |
| 1 | `CDBGPWRUPREQ \| CSYSPWRUPREQ \| SSTICKYERR`（v0.12.0 标签） | 28,30,5 | **`0x50000020`** |
| 2 | `CDBGPWRUPREQ \| CSYSPWRUPREQ` | 28,30 | **`0x50000000`** |
| 3 | `CDBGPWRUPREQ \| CSYSPWRUPREQ \| CORUNDETECT` | 28,30,0 | **`0x50000001`** |

`SSTICKYERR` = bit5（`arm_adi_v5.h:86`），`SSTICKYORUN` = bit1（`:83`），`CORUNDETECT` = bit0（`:82`）。

**等待的确认位**（`arm_adi_v5.c:817-830`）：

```c
	retval = dap_dp_poll_register(dap, DP_CTRL_STAT,
				      CDBGPWRUPACK, CDBGPWRUPACK,
				      DAP_POWER_DOMAIN_TIMEOUT);
	...
		retval = dap_dp_poll_register(dap, DP_CTRL_STAT,
					      CSYSPWRUPACK, CSYSPWRUPACK,
					      DAP_POWER_DOMAIN_TIMEOUT);
```

→ 等 **bit29**（`0x20000000`），再等 **bit31**（`0x80000000`），各 10 轮 × 10 ms。

> ⚠️ **注意 `STKCMPCLR/STKERRCLR/WDERRCLR/ORUNERRCLR`（bit1–bit4）不属于 `DP CTRL/STAT`**，
> 它们属于 **`DP ABORT`** 寄存器（`arm_adi_v5.h:67-72`，注释标题就是 "Fields of the DP's **AP ABORT** register"）。
> 常见错误是把它们写进 `CTRL/STAT`。`CTRL/STAT` 里 bit1 是 `SSTICKYORUN`、bit5 是 `SSTICKYERR`，是**只读的 sticky 状态**。

### Q3：写失败（FAULT）时它怎么处理？会判死吗？

**答：不会判死。分三层处理：**

**（1）适配器层：WAIT 由探针自动重试 64 次；FAULT 直接上抛**

`src/jtag/drivers/cmsis_dap.c:1407-1410`：

```c
	/* Ask CMSIS-DAP to automatically retry on receiving WAIT for
	 * up to 64 times. This must be changed to 0 if sticky
	 * overrun detection is enabled. */
	retval = cmsis_dap_cmd_dap_tfer_configure(0, 64, 0);
```

**（2）驱动层：ACK≠OK → 映射成错误码，整包作废，不重试**

`src/jtag/drivers/cmsis_dap.c:933-940`：

```c
	uint8_t ack = resp[idx++] & 0x07;
	if (ack != SWD_ACK_OK) {
		LOG_DEBUG("SWD ack not OK @ %d %s", transfer_count,
			  ack == SWD_ACK_WAIT ? "WAIT" : ack == SWD_ACK_FAULT ? "FAULT" : "JUNK");
		queued_retval = swd_ack_to_error_code(ack);
		/* TODO: use results of transfers completed before the error occurred? */
		goto skip;
	}
```

`src/jtag/swd.h:72-84`：

```c
static inline int swd_ack_to_error_code(uint8_t ack)
{
	switch (ack) {
	case SWD_ACK_OK:
		return ERROR_OK;
	case SWD_ACK_WAIT:
		return ERROR_WAIT;
	case SWD_ACK_FAULT:
		return ERROR_SWD_FAULT;
	default:
		return ERROR_SWD_FAIL;
	}
}
```

**（3）SWD 层：置 `do_reconnect`，下一次访问整套重连（不是判死）**

`src/target/adi_v5_swd.c:621-637`：

```c
/** Executes all queued DAP operations. */
static int swd_run(struct adiv5_dap *dap)
{
	int retval = swd_multidrop_select(dap);
	if (retval != ERROR_OK)
		return retval;

	swd_finish_read(dap);

	retval = swd_run_inner(dap);
	if (retval != ERROR_OK) {
		/* fault response */
		dap->do_reconnect = true;
	}

	return retval;
}
```

`src/target/adi_v5_swd.c:462-468`：

```c
static int swd_check_reconnect(struct adiv5_dap *dap)
{
	if (dap->do_reconnect)
		return swd_connect(dap);

	return ERROR_OK;
}
```

**（4）内存访问层：不重试、不发 ABORT，只记日志**

`src/target/arm_adi_v5.c:577-586`：

```c
	/* REVISIT: Might want to have a queued version of this function that does not run. */
	if (retval == ERROR_OK)
		retval = dap_run(dap);

	if (retval != ERROR_OK) {
		target_addr_t tar;
		if (mem_ap_read_tar(ap, &tar) == ERROR_OK)
			LOG_ERROR("Failed to write memory at " TARGET_ADDR_FMT, tar);
		else
			LOG_ERROR("Failed to write memory and, additionally, failed to find out where");
	}
```

**结论**：
- **不会判死**；错误会一路冒泡到调用者。
- **唯一的"重试"是探针对 WAIT 的 64 次自动重试**（目标侧 FAULT 不重试）。
- **唯一的"自愈"是 `do_reconnect` 触发的整套重连**（重发 136 位激活序列 + 重读 DPIDR + 重跑 `dap_dp_init`）。
- 如果重连也失败，最终停在 `Error connecting DP: cannot read IDR`（`adi_v5_swd.c:381`）。

### Q4：它什么时候写 `DP SELECT`，会不会在"上一个 DP 写还是 posted 状态"时写？

**答：三个条件之一满足就写；会，在 posted 状态下照写，不等。**

**写入条件**（`src/target/adi_v5_swd.c:531-557`，AP 路径）：

```c
	uint64_t sel_diff = (sel ^ dap->select) & SELECT_AP_MASK;

	bool set_select = !dap->select_valid || (sel_diff & 0xffffffffull);
	...
	if (set_select) {
		LOG_DEBUG_IO("AP BANK SELECT: %" PRIx32, (uint32_t)sel);

		retval = swd_queue_dp_write(dap, DP_SELECT, (uint32_t)sel);
		if (retval != ERROR_OK)
			return retval;
	}
```

DP 路径同理（`src/target/adi_v5_swd.c:106-119`）：

```c
	if ((dap->select_valid || (is_adiv6(dap) && dap->select_dpbanksel_valid))
			&& (sel == (dap->select & DP_SELECT_DPBANK)))
		return ERROR_OK;
	...
	return swd_queue_dp_write_inner(dap, DP_SELECT, sel);
```

→ **三种情况写**：
1. `dap->select_valid == false`（刚 `dap_invalidate_cache`：连接、重连、`dap_dp_init` 开头）；
2. DP 寄存器 bank 变了（`sel != dap->select & 0xF`）；
3. AP 路径里 `(sel ^ dap->select) & ~0xF` 非零（AP 号或 APBANKSEL 变了）。

**"posted 状态"问题**（`src/target/adi_v5_swd.c:137-156`）：

```c
static int swd_queue_dp_write_inner(struct adiv5_dap *dap, unsigned int reg,
		uint32_t data)
{
	int retval = ERROR_OK;
	const struct swd_driver *swd = adiv5_dap_swd_driver(dap);
	assert(swd);

	swd_finish_read(dap);

	if (reg == DP_SELECT) {
		dap->select = data | (dap->select & (0xffffffffull << 32));

		swd->write_reg(swd_cmd(false, false, reg), data, 0);

		retval = check_sync(dap);
		dap->select_valid = (retval == ERROR_OK);
		dap->select_dpbanksel_valid = dap->select_valid;

		return retval;
	}
```

→ **会写，且不加任何等待**：
- DP 写的**第一件事**只有 `swd_finish_read(dap)`（`:144`），它只处理**挂起的 AP 读**（补一笔 `DP_RDBUFF`），
  **不检查上一笔 DP 写是否完成**；
- `DP_SELECT` 的写入是作为**普通 DP 写事务**入队（`swd->write_reg(...)`），
  与前后事务**同序**排在同一个 `DAP_Transfer` 包里，靠 SWD 协议本身的顺序保证；
- **唯一的例外是 `DP_ABORT`**（`src/target/adi_v5_swd.c:161-164`）：

```c
	/* DP_ABORT write is not banked.
	 * Prevent writing DP_SELECT before as it would fail on locked up DP */
	if (reg != DP_ABORT)
		retval = swd_queue_dp_bankselect(dap, reg);
```

→ 源码注释说得很直白：`DP_ABORT` 之前写 SELECT **会在 DP 卡死时失败**，所以跳过。

**补充事实**：`dap_dp_init` 的**第一笔** DP 访问确实会先写一次 `DP SELECT = 0`
（因为 `dap_invalidate_cache()` 把 `select_valid` 置 false，而 `DP_CTRL_STAT` 的 `reg & 0xf == 4` 不会被早退）。

### Q5：清 sticky 用的是哪个寄存器、哪个命令（DP ABORT 还是 AP ABORT）？

**答：用 DP 的 ABORT 寄存器（DP bank0，地址 0x00，SWD 写-only）。命令就是普通的 `DAP_Transfer` 写事务。
`dap_queue_ap_abort()` 这条 API 存在但全代码无调用者；源码里没有"AP ABORT 寄存器"这个实体。**

**清 sticky 的确切代码**（`src/target/adi_v5_swd.c:75-82`）：

```c
static void swd_clear_sticky_errors(struct adiv5_dap *dap)
{
	const struct swd_driver *swd = adiv5_dap_swd_driver(dap);
	assert(swd);

	swd->write_reg(swd_cmd(false, false, DP_ABORT),
		STKCMPCLR | STKERRCLR | WDERRCLR | ORUNERRCLR, 0);
}
```

**寄存器地址与位定义**（`src/target/arm_adi_v5.h:46-47,67-72`）：

```c
#define DP_ABORT        BANK_REG(0x0, 0x0) /* DPv1+: SWD: wo */
#define DP_DPIDR        BANK_REG(0x0, 0x0) /* DPv1+: ro */
...
/* Fields of the DP's AP ABORT register */
#define DAPABORT        (1UL << 0)
#define STKCMPCLR       (1UL << 1) /* SWD-only */
#define STKERRCLR       (1UL << 2) /* SWD-only */
#define WDERRCLR        (1UL << 3) /* SWD-only */
#define ORUNERRCLR      (1UL << 4) /* SWD-only */
```

**确切值**：`STKCMPCLR | STKERRCLR | WDERRCLR | ORUNERRCLR` = `0x02|0x04|0x08|0x10` = **`0x1E`**。

**报文**（`DAP_Transfer`，单事务写）：

```
发: 05 00 01 00 1E 00 00 00
收: 05 01 01
```

- `05` = `CMD_DAP_TFER`（`cmsis_dap.c:159`）
- `00` = DAP Index（`cmsis_dap.c:803`）
- `01` = 事务数（`cmsis_dap.c:810`）
- `00` = 请求字节（DP 写 ABORT，A[3:2]=00、APnDP=0、RnW=0）
- `1E 00 00 00` = 数据小端

**"AP ABORT" 的真相**：
- 源码里**没有** `AP_REG_ABORT` / `dap_ap_abort` / `dap_dp_abort` 之类的定义（两棵树穷尽 grep）。
- 唯一名字像的 `dap_queue_ap_abort()`（`src/target/arm_adi_v5.h:632-636`）与其实现
  `swd_queue_ap_abort()`（`src/target/adi_v5_swd.c:470-486`）**全代码无调用者**，
  而且**它的实现也是写同一个 `DP_ABORT` 寄存器**：

```c
static int swd_queue_ap_abort(struct adiv5_dap *dap, uint8_t *ack)
{
	const struct swd_driver *swd = adiv5_dap_swd_driver(dap);
	assert(swd);

	/* TODO: Send DAPABORT in swd_multidrop_select_inner()
	 * in the case the multidrop dap is not selected?
	 * swd_queue_ap_abort() is not currently used anyway...
	 */
	int retval = swd_multidrop_select(dap);
	if (retval != ERROR_OK)
		return retval;

	swd->write_reg(swd_cmd(false, false, DP_ABORT),
		DAPABORT | STKCMPCLR | STKERRCLR | WDERRCLR | ORUNERRCLR, 0);
	return check_sync(dap);
}
```

**`DP_ABORT` 的三个写入点全表**：

| 位置 | 值 | 条件 |
|---|---|---|
| `adi_v5_swd.c:80-81` | `0x1E` | 每次 connect 成功后无条件清一次 |
| `adi_v5_swd.c:449-450` | `0x1F` | connect 阶段收到 `ERROR_WAIT` |
| `adi_v5_swd.c:221` | `0x10` | 多drop 选 DP（STM32 不走） |

---

## 8. 自检

### ① 每个流程步骤都有 `文件:行号` 出处吗？

**是。** 本文全部流程步骤都带出处。分布如下：

| 章节 | 出处数量（约） | 说明 |
|---|---|---|
| §1 传输层 | 60+ | 每个命令的编码、每个参数值、每处超时都有行号 |
| §2 DP 层 | 35+ | `dap_dp_init` 逐步表 12 行全部带行号 |
| §3 AP 层 | 30+ | CSW/TAR/DRW/块传输/ABORT |
| §4 flash loader | 45+ | 含 108 字节 blob 的确切内容 |
| §5 字节序列 | 每笔事务一行出处 | 见下表 |
| §7 Q1–Q5 | 全部 | 每问都贴了源码原文 |

**例外（已显式标注，未冒充）**：
- `reset_config` 位语义引自 **v0.12.0 树** `src/jtag/adapter.c:483-497`（**主树未下载该文件**），已在 §4.9 标注。
- `reset-init` 事件触发时机引自 **v0.12.0 树** `src/target/startup.tcl`，已在 §6.5 第 76 行标注 **未确认**。
- STM32H7B0 的 CPUID 高/低修订位**不是源码常量**，已在 §5.5 标注。

### ② Q1–Q5 是不是都用源码原文回答的（贴关键代码片段）？

**是，五问全部贴了源码原文，且每段都给了 `文件:行号`。**

| 问 | 结论 | 关键原文出处 | 是否贴原文 |
|---|---|---|---|
| Q1 | 无条件写；已上电也写 | `arm_adi_v5.c:795-813`（含 `dap_queue_dp_write` 无分支） | ✅ 贴了 20 行 |
| Q2 | bit28 + bit30 = `0x50000000`；三次写的确切值 | `arm_adi_v5.h:93-96`；`arm_adi_v5.c:802,811,837` | ✅ 贴了位定义 + 逐步表 |
| Q3 | 不判死；探针重试 64 次 + `do_reconnect` 整套重连；内存层不重试不发 ABORT | `cmsis_dap.c:1407-1410`、`:933-940`；`swd.h:72-84`；`adi_v5_swd.c:621-637`、`:462-468`；`arm_adi_v5.c:577-586` | ✅ 贴了 5 段 |
| Q4 | 三条件之一即写；posted 状态下照写不等；唯 `DP_ABORT` 例外 | `adi_v5_swd.c:531-557`、`:106-119`、`:137-156`、`:161-164` | ✅ 贴了 4 段 |
| Q5 | **DP ABORT**（bank0/0x00），值 `0x1E`；AP ABORT 不存在且那条 API 无调用者 | `adi_v5_swd.c:75-82`、`:470-486`；`arm_adi_v5.h:46,67-72,632-636` | ✅ 贴了 3 段 |

### ③ 有没有任何"我猜的"内容未标注？

**逐项排查，结论：没有未标注的猜测。以下 9 处全部显式标了「未确认」或「版本差异」：**

| # | 位置 | 内容 | 标注方式 |
|---|---|---|---|
| 1 | §1.6.5 | `DAP_Transfer` 响应是"1 个 ACK + 读数据"还是"每事务 1 个 ACK" —— 与 CMSIS-DAP 规范文本的对应关系 | **「未确认」**，并给出源码原文让读者自行判断 |
| 2 | §2.1 / §1.6.4 | dev 版第 1 次 `CTRL/STAT` 写多一个 `SSTICKYORUN`；dev 版新增 `DAP_TransferBlock` | **`> 版本差异：`** |
| 3 | §3.2 | `0x23000052` 的出处 | **「未确认」**，明确说明本源码推不出该值（bit24 无 define、bit6 零引用） |
| 4 | §4.3 | H7 算法 blob 内部是否使用栈 | **「未确认」**（未逐条反汇编） |
| 5 | §4.9 / §6.5 | `reset_config srst_nogate` 的位语义、`reset-init` 触发时机 | **「未确认（引自 v0.12.0 树）」**；主树无 `src/jtag/adapter.c`、无 `startup.tcl` |
| 6 | §5.5 | Cortex-M7 CPUID 的完整 32 位值 | 只保证 **partno 字面值 `0xC27` 来自源码**；**implementer 宏的数值与修订位均标「未确认」** |
| 7 | §5.6 | 更上层（`target_write_u32`）的具体报文路径 | **「未确认」**，只保证 `mem_ap_write_u32` + `dap_run` 这一层 |
| 8 | §0.2 | 本机二进制与源码的逐行一致性 | 只能确认版本串一致；**行号以 `eb6f2745b` 源码为准**，已在 §0.2 明确 |
| 9 | §5.1 | 响应字节布局 | 与 #1 同一处，**「未确认」**，提示以 F103 抓包核对 |
| 10 | §5.5 | 宏 `ARM_IMPLEMENTER_ARM` 的数值（`0x41` 这个说法） | **「未确认」**：`cortex_m.h` 只引用该宏、未定义，定义所在头文件本文未下载 → 不宣称其数值来自源码 |

**另外主动纠正了任务书中的 4 处事实性错误（均给出源码依据，不是猜测）：**

| 任务书原文 | 纠正 | 依据 |
|---|---|---|
| "88 位激活序列（`9E E7 FF…00`）" | **136 位**（`FF×7 9E E7 FF×7 00`） | `swd.h:115-125`；v0.11.0 / v0.12.0 / dev 三版一致 |
| "STM32H7B0（… 128KB flash sectors）" | **扇区 8 KB**；128 KB 是整片容量 | `stm32h7x.c:185` |
| "`SWJ_Clock`" | 源码函数是 `cmsis_dap_cmd_dap_swj_clock`，命令码 `CMD_DAP_SWJ_CLOCK = 0x11` | `cmsis_dap.c:119,391` |
| "`cortex_m_run_algorithm`" / "`algo->stack_*`" / "PC 最后写" | 真名 `armv7m_run_algorithm`；无 `algo->stack_*`；**PC 不是最后写的**（索引降序） | `armv7m.c:511,207-215`；`algorithm.h` 全文 |

### ④ "字节级报文序列"里每一笔的来源是哪段代码？

**逐笔对照表（§5 的每一条都在此可溯源）：**

| 报文 | 字节 | 来源代码 | 出处 |
|---|---|---|---|
| A1 | `00 F0` | `cmsis_dap_cmd_dap_info(INFO_ID_CAPS=0xF0, …)` | `cmsis_dap.c:1151` → `:434-450` |
| A2 | `00 04` | `cmsis_dap_get_version_info()` → `INFO_ID_FW_VER` | `cmsis_dap.c:1136` |
| A3 | `00 03` | `cmsis_dap_get_serial_info()` → `INFO_ID_SERNUM` | `cmsis_dap.c:1121` |
| A4 | `02 01` | `cmsis_dap_swd_open()` → `cmsis_dap_cmd_dap_connect(CONNECT_SWD=1)` | `cmsis_dap.c:1292` → `:469-488` |
| A5 | `00 FF` | `cmsis_dap_cmd_dap_info(INFO_ID_PKT_SZ, …)` | `cmsis_dap.c:1347` |
| A6 | `00 FE` | `cmsis_dap_cmd_dap_info(INFO_ID_PKT_CNT, …)` | `cmsis_dap.c:1374` |
| A7 | `10 …` | `cmsis_dap_get_status()` → `cmsis_dap_cmd_dap_swj_pins(0,0,0,&d)` | `cmsis_dap.c:1399` → `:370-389` |
| A8 | `11 40 77 1B 00` | `cmsis_dap_cmd_dap_swj_clock(1800)`，内部 `×1000` → 1 800 000 = `0x001B7740` | `cmsis_dap.c:1403` → `:391-408` |
| A9 | `04 00 40 00 00 00` | `cmsis_dap_cmd_dap_tfer_configure(0, 64, 0)` | `cmsis_dap.c:1410` → `:505-521` |
| A10 | `13 00` | `cmsis_dap_cmd_dap_swd_configure(0)` | `cmsis_dap.c:1417` → `:523-537` |
| A11/A12 | `01 xx 01` | `cmsis_dap_cmd_dap_led(...)` | `cmsis_dap.c:1424-1425` → `:452-467` |
| B1 | `12 88 FF…00` | `cmsis_dap_swd_switch_seq(JTAG_TO_SWD)` → `cmsis_dap_cmd_dap_swj_sequence(136, swd_seq_jtag_to_swd)` | `adi_v5_swd.c:345` → `cmsis_dap.c:1276` → `:411-432`；数据 `swd.h:115-125` |
| B2 | `05 00 01 02` | `swd_queue_dp_read_inner(dap, DP_DPIDR, &dpidr)`；请求字节 `(swd_cmd(1,0,0x00)>>1)&0xF = 0x02` | `adi_v5_swd.c:368` → `:122-135`；编码 `cmsis_dap.c:844` |
| B3 | `05 00 01 00 1E …` | `swd_clear_sticky_errors()` 写 `DP_ABORT = 0x1E` | `adi_v5_swd.c:391` → `:75-82` |
| C1-t1 | `08 00 00 00 00` | `swd_queue_dp_bankselect(DP_CTRL_STAT)` → `swd_queue_dp_write_inner(DP_SELECT, 0)` | `adi_v5_swd.c:97-120` → `:137-156` |
| C1-t2 | `04 22 00 00 50` | `dap_queue_dp_write(DP_CTRL_STAT, dp_ctrl_stat\|SSTICKYERR\|SSTICKYORUN)` = `0x50000022` | `arm_adi_v5.c:802-803` |
| C1-t3 | `06` | `dap_queue_dp_read(DP_CTRL_STAT, NULL)` | `arm_adi_v5.c:807` |
| C1-t4 | `04 00 00 00 50` | `dap_queue_dp_write(DP_CTRL_STAT, 0x50000000)` | `arm_adi_v5.c:811` |
| C1-t5 | `06` | `dap_dp_poll_register` → `dap_dp_read_atomic` | `arm_adi_v5.c:817-819` → `arm_adi_v5.h:685` |
| C2 | `05 00 01 06` | 第二次 `dap_dp_poll_register`（CSYSPWRUPACK） | `arm_adi_v5.c:825-827` |
| C3 | `05 00 03 06 04 00 00 00 50 06` | 读 → 写 `0x50000001`（被剔成 `0x50000000`）→ 读，最后一次 `dap_run` | `arm_adi_v5.c:832-845`；剔除 `cmsis_dap.c:835-841` |
| D1 | `05 00 03 08 F0 … 07 0E` | `mem_ap_init` 读 AP CFG（`MEM_AP_REG_CFG`=0xF4）；`08`=SELECT、`07`=AP 读 0xF4、`0E`=RDBUFF 冲刷 | `arm_adi_v5.c:897-903`；`adi_v5_swd.c:529,589,70` |
| D2-t1 | `08 00 00 00 00` | `swd_queue_ap_bankselect(ap, 0x00)` | `adi_v5_swd.c:529,554` |
| D2-t2 | `01 02 00 00 AA` | `mem_ap_setup_csw(ap, CSW_32BIT)` → `0xAA000002` | `arm_adi_v5.c:246,100`；`stm32h7x.cfg:164` |
| D2-t3 | `05 00 ED 00 E0` | `mem_ap_setup_tar(ap, 0xE000ED00)` | `arm_adi_v5.c:114` |
| D2-t4 | `08 10 00 00 00` | `swd_queue_ap_bankselect(ap, 0x10)`（BD0 在 bank1） | `adi_v5_swd.c:529` |
| D2-t5 | `03` | `dap_queue_ap_read(ap, MEM_AP_REG_BD0, &cpuid)` | `arm_adi_v5.c:251` |
| D2-t6 | `0E` | `swd_finish_read()` 补 `DP_RDBUFF` 读 | `adi_v5_swd.c:70` |
| E-t1 | `05 00 00 00 20` | `mem_ap_setup_tar(ap, 0x20000000)` | `arm_adi_v5.c:114` |
| E-t2 | `01 78 56 34 12` | `dap_queue_ap_write(ap, MEM_AP_REG_BD0\|(addr&0xC), value)` | `arm_adi_v5.c:303-304` |

**④ 结论**：每一笔都能追到具体函数行号。**唯一非"逐字推导"的部分是各响应中的具体数据值**
（DPIDR、CFG、CPUID、FLASH 状态），那些取决于真实硬件；文中已标明哪些来自 tcl 预期值、哪些需要实读。

---

## 附录 A：函数名 → 行号对照（两棵树）

调试时按函数名反查行号用。**主树 = `tmp\ocd-dev-src`（`eb6f2745b`，= 本机二进制）；次树 = `tmp\openocd-src`（`v0.12.0` 标签）。**

| 函数 | 主树行号 | v0.12.0 标签行号 |
|---|---|---|
| `cmsis_dap_cmd_dap_connect` | `cmsis_dap.c:469` | `cmsis_dap.c:464` 附近 |
| `cmsis_dap_cmd_dap_swj_clock` | `cmsis_dap.c:391` | 同名函数存在 |
| `cmsis_dap_cmd_dap_swj_sequence` | `cmsis_dap.c:411` | — |
| `cmsis_dap_cmd_dap_tfer_configure` | `cmsis_dap.c:505` | — |
| `cmsis_dap_cmd_dap_swd_configure` | `cmsis_dap.c:523` | — |
| `cmsis_dap_swd_write_from_queue` | `cmsis_dap.c:773` | `cmsis_dap.c:762` 附近（无 block_cmd） |
| `cmsis_dap_swd_read_process` | `cmsis_dap.c:870` | `cmsis_dap.c:842` 附近 |
| `cmsis_dap_swd_run_queue` | `cmsis_dap.c:983` | `cmsis_dap.c:914` |
| `cmsis_dap_swd_queue_cmd` | `cmsis_dap.c:1032` | `cmsis_dap.c:933` |
| `cmsis_dap_swd_switch_seq` | `cmsis_dap.c:1209` | — |
| `cmsis_dap_init` | `cmsis_dap.c:1302` | — |
| `swd_seq_line_reset` / `_len` | `swd.h:98-104` | `swd.h:98-104`（**逐字相同**） |
| `swd_seq_jtag_to_swd` / `_len` | `swd.h:115-125` | `swd.h:115-125`（**逐字相同**） |
| `swd_clear_sticky_errors` | `adi_v5_swd.c:75` | 同名 |
| `swd_queue_dp_bankselect` | `adi_v5_swd.c:97` | 同名 |
| `swd_queue_dp_read_inner` | `adi_v5_swd.c:122` | 同名 |
| `swd_queue_dp_write_inner` | `adi_v5_swd.c:137` | 同名 |
| `swd_connect_single` | `adi_v5_swd.c:334` | 同名 |
| `swd_connect` | `adi_v5_swd.c:411` | 同名 |
| `swd_check_reconnect` | `adi_v5_swd.c:462` | 同名 |
| `swd_queue_ap_abort` | `adi_v5_swd.c:470` | 同名（同样无调用者） |
| `swd_queue_ap_bankselect` | `adi_v5_swd.c:519` | 同名 |
| `swd_queue_ap_read` / `_write` | `adi_v5_swd.c:570` / `:595` | 同名 |
| `swd_run` | `adi_v5_swd.c:621` | 同名 |
| `swd_dap_ops` | `adi_v5_swd.c:677` | 同名 |
| `mem_ap_setup_csw` | `arm_adi_v5.c:94` | `arm_adi_v5.c:95` 附近（**逐字相同，行号偏移 1**） |
| `mem_ap_setup_tar` | `arm_adi_v5.c:110` | 同上偏移 |
| `mem_ap_update_tar_cache` | `arm_adi_v5.c:185` | 同名 |
| `mem_ap_read_u32` / `write_u32` | `arm_adi_v5.c:237` / `:289` | 同名（逐字相同） |
| `mem_ap_setup_transfer_verify_size_packing` | `arm_adi_v5.c:345` | **不存在**（v0.12.0 只支持 size∈{1,2,4}） |
| `mem_ap_write` / `mem_ap_read` | `arm_adi_v5.c:474` / `:604` | 同名 |
| `dap_invalidate_cache` | `arm_adi_v5.c:756` | 同名 |
| `dap_dp_init` | `arm_adi_v5.c:779` | `arm_adi_v5.c:675` |
| `dap_dp_init_or_reconnect` | `arm_adi_v5.c:857` | `arm_adi_v5.c:752` |
| `mem_ap_init` | `arm_adi_v5.c:888` | 同名 |
| `DAP_POWER_DOMAIN_TIMEOUT` | `arm_adi_v5.c:749` | `arm_adi_v5.c:649` |
| `dap_instance_init` | `arm_dap.c:33` | 同名 |
| `dap_init_all` | `arm_dap.c:90` | 同名 |
| `armv7m_restore_context` | `armv7m.c:193` | `armv7m.c:168` 附近 |
| `armv7m_run_algorithm` | `armv7m.c:511` | 同名 |
| `armv7m_start_algorithm` | `armv7m.c:536` | 同名 |
| `armv7m_wait_algorithm` | `armv7m.c:651` | 同名 |
| `cortex_m_store_core_reg_u32` | `cortex_m.c:398` | **逐字相同**（diff 无 hunk） |
| `cortex_m_write_debug_halt_mask` | `cortex_m.c:448` | 同名 |
| `cortex_m_halt_one` | `cortex_m.c:1199` | 主树才拆分（标签是 `cortex_m_halt` 内含） |
| `cortex_m_restore_one` | `cortex_m.c:1314` | 同名 |
| `cortex_m_resume` | `cortex_m.c:1460` | 同名 |
| `cortex_m_examine` | `cortex_m.c:2583` | 同名 |
| `cortex_m_parts`（含 4K 标志） | `cortex_m.c:54-140` | 同名 |
| `target_run_algorithm` | `target.c:773` | `target.c:846` 附近 |
| `target_run_flash_async_algorithm` | `target.c:930` | `target.c:1003` 附近 |
| `stm32x_wait_flash_op_queue` | `stm32h7x.c:263` | 同名 |
| `stm32x_write_block` | `stm32h7x.c:554` | 同名 |
| `stm32x_write` / `erase` / `probe` | `stm32h7x.c:661` / `:463` / `:749` | 同名 |
| `stm32h7a_h7bxx_compute_flash_cr` | `stm32h7x.c:153` | 同名 |
| `stm32h7x_parts[]` | `stm32h7x.c:164` | 同名 |

> ⚠️ 表中标"同名"的条目表示"该函数在两棵树里都存在且行为一致（或差异已在正文标注）"，
> **不代表行号相同**——除标注"逐字相同"者外，请以主树行号为准。
> 标 "—" 的表示我未在次树中定位该函数（**未确认**）。

## 附录 B：本文未下载/不存在的源码文件（避免你按名字找空）

以下路径**在本文的两棵树里不存在**，正文中也没有引用它们的行号（引用时已另行标注）：

- `src/target/arm_swd.c` —— **不存在**；SWD 传输层在 `src/target/adi_v5_swd.c`
- `src/target/arm_dap.h` —— **不存在**（上游无此文件；DAP 声明在 `arm_adi_v5.h` / `arm_dap.c`）
- `src/jtag/swd.c` —— **不存在**（只有 `src/jtag/swd.h`）
- `src/target/mem_ap.c` —— 存在但**与 MEM-AP 寄存器逻辑无关**（它是"无核 target 驱动"）；CSW/TAR 逻辑全在 `arm_adi_v5.c`
- `src/target/arm_swd.c`、`src/target/algorithm.c`（存在但只有 42 行的 `mem_param`/`reg_param` 定义，**不含** `target_run_flash_async_algorithm`，后者在 `src/target/target.c:930`）
- 主树未下载（如需引用请自行拉取并核对行号）：
  `src/jtag/adapter.c`、`src/jtag/core.c`、`src/jtag/tcl.c`、`src/helper/*`、
  `src/target/startup.tcl`、`src/flash/startup.tcl`、`src/flash/nor/tcl.c`
  —— §4.9 / §6.5 中涉及它们的结论已全部标注 **「未确认（引自 v0.12.0 树）」**。

---

*文档结束。全部结论基于 `eb6f2745b`（= 本机 `xpack-openocd-0.12.0-6` 二进制）源码；*
*凡与 `v0.12.0` 标签有实质差异处已用 `> 版本差异：` 标注；凡不能从源码直接读出者已标「未确认」。*


