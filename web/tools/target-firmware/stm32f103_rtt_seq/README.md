# STM32F103 带序号 RTT 靶子（字节级完整性测量用）

**它和 `../stm32f103_rtt_speed` 只差一行**：把死循环里的固定图案 `"hello world!\n"`
换成**等长（13 字节）带序号**的记录 `"seq %07u\r\n"`。

```c
char msg[13];
msg[0]='s'; msg[1]='e'; msg[2]='q'; msg[3]=' ';
msg[11]='\r'; msg[12]='\n';
uint32_t seq = 0;
for (;;){
  uint32_t v = seq++;
  for (uint32_t i = 0; i < 7U; i++){ msg[10U-i] = (char)('0' + (v % 10U)); v /= 10U; }
  unsigned n = SEGGER_RTT_Write(0, msg, 13U);   /* BLOCK_IF_FIFO_FULL，n 恒 13 */
  g_bytes += n; g_loops++;
}
```

## 为什么需要它

固定图案（`akaLinkPro/script_test/rtt_probe_bridge.py` 那套）只能用 `find("hello world!\n")`
做**相位**检查：一次断裂只报 1~24 字节 —— **丢 5 字节和丢 5 MB 在它眼里一模一样**，
所以那个 `lost=17` 之类的数字根本不能当字节数用。

带序号素材可以逐条对账：记录号跳了 `d` 条就是真丢了 `(d-1)×13` 字节，回退就是重复，
数字解析不出来就是数据被写坏。记录长度同为 13 B，吞吐/压力与 speed 版一致，可直接对比。

实测（`tools/selftest/probe-hid-diag.py --mode=loss --seq`，2026-10-04）：
45/60 MHz 共 8 个窗口 ~262 MB，**真丢 0 B、坏记录 0**；唯一异常是某些窗口开头
~2 KB 的记录号**回退**（=重复前缀，不是丢失）。定因与修法见
`docs/probe-rc4-and-rtt-loss.md`。

## 编译 / 烧录

```powershell
pwsh -File build.ps1 -Board ze          # 96 MHz + RTT 上行 32 KB（必须用 ze 版）
```

烧录两条路（仓库里没有 OpenOCD 的 F1 target cfg，本目录的 `flash.ps1` 只有在
`$PSScriptRoot` 的上一级能找到 `openocd_stm32f1_swd.cfg` 时才可用，即 akaLinkPro 那份）：

```powershell
# ① 仓库自带（走页面 WebUSB，和三条 full_flow 用的同一条路）
node tools/selftest/flash-elf.mjs --chip=stm32f103 --elf=/tools/target-firmware/stm32f103_rtt_seq/fw.elf

# ② 把 build-ze\fw.elf 拷进 akaLinkPro 的 script_test\stm32f103_rtt_speed\build-ze\，
#    再用那边验证过的 pwsh -File flash.ps1 -Board ze
```

## 测量

```powershell
python tools/selftest/probe-hid-diag.py --mode=loss --seq --clk=45 --iters=3 --window=15
```

跑完记得把 speed 版烧回去（akaLinkPro 的 P2 回归基线用的是固定图案那版）。
