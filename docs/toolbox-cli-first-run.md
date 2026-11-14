# 从接线到目标烧录

从仓库运行时，双击 `dist/superlinkPRO/superlinkPRO.exe`。使用下载包时，解压完整 ZIP 后双击包内的 `superlinkPRO.exe`。在启动器打开的本机页面，从顶部进入「可执行手册」。每条命令右侧都有「执行」按钮，实际输出、退出码和日志路径显示在步骤下面。

页面按钮会使用当前工作台会话，便携版会自动调用包内 CLI。下面展示的是源码终端命令。手动使用便携版时，在 EXE 同目录打开 PowerShell，先保持工作台和浏览器标签页运行，再用 `.\workbench-cli.cmd` 替换命令开头的 `python -X utf8 toolbox.py --runtime tmp/first-run-web` 或 `python -X utf8 toolbox.py`，其余参数照填。便携版默认使用启动器配置的端口和包内 `data` 目录。

## 1. 启动并检查工作台

1. 使用源码终端时，先安装 `etm/requirements-cli.txt` 中的依赖，再从仓库根目录启动工作台。运行时目录必须尚不存在，端口须空闲。若更换运行时目录，后续源码命令使用同一路径。打开输出的 `browserUrl` 并保留标签页。在已打开的手册中点击本步，只核对并显示当前会话。便携版终端从下一步开始。

   ```powershell
   python -X utf8 toolbox.py --runtime tmp/first-run-web serve --port 8896
   ```

2. 确认 CLI 与当前浏览器执行页连通。看到 `ready: true` 后继续。

   ```powershell
   python -X utf8 toolbox.py --runtime tmp/first-run-web ping
   ```

3. 查看 CLI 支持的功能和页面。

   ```powershell
   python -X utf8 toolbox.py catalog
   ```

4. 查看工作台各页已选目标及共享配置。此步不连接设备。

   ```powershell
   python -X utf8 toolbox.py --runtime tmp/first-run-web target info
   ```

## 2. 接入探针并预览固件

用 USB 数据线连接 SuperLinkPRO。按目标板针脚图连接 SWD 或 JTAG 和共地，核对供电和 I/O 电平。确认其他工作台已释放探针。

在顶栏点「连接探针」。首次使用时按提示选择 USB；如果按钮变为「连接 HID」，再点一次，为同一只探针授权 HID。akaLinkPro 使用 JTAG 时，工作台通过 HID 配置探针引脚。已有授权时，顶栏会先枚举并复用设备。需要指定设备时，在「连接选项」分别使用「选择 USB」和「选择 HID」，核对两项属于同一只探针。

1. 查看烧录器的目标选项。选项里的 `value` 是下一步要输入的芯片 ID。

   ```powershell
   python -X utf8 toolbox.py --runtime tmp/first-run-web page flash describe
   ```

2. 按板型和实际接线输入内置芯片 ID。STM32H743 接 SWD 用 `stm32h7`，接 ARM JTAG 用 `stm32h743-jtag`。HPM6800EVK 接 RISC-V JTAG，用 `hpm6800evk`。

   ```powershell
   $Chip = Read-Host '输入目标芯片 ID'
   ```

3. 输入适配目标板的 ELF 绝对路径。路径含空格时，直接输入完整路径。

   ```powershell
   $Firmware = Read-Host '输入目标固件 ELF 的绝对路径'
   ```

4. 预览固件的目标地址、长度、时钟和哈希。返回 `prepared: true` 后核对这些数据。此步不连接目标。

   ```powershell
   python -X utf8 toolbox.py --runtime tmp/first-run-web target-flash --chip "$Chip" --file "$Firmware" --prepare-only
   ```

5. 读取目标身份。STM32H743 所在系列的 `devId` 为 `0x450`，ARM JTAG 下还会显示 TAP ID。HPM6800EVK 的 `id` 对应 JTAG TAP `0x1000563d`。CLI 的 JSON 可能用十进制显示这些值。结合板卡丝印确认型号，读不到身份时先检查芯片选项、接线和探针授权。

   ```powershell
   python -X utf8 toolbox.py --runtime tmp/first-run-web page flash invoke readIdcode
   ```

## 3. 烧录、校验和复位

确认板卡允许覆盖 Flash，需要保留原固件时先备份。烧录后的软件复位通过调试通路发送。顶栏「全局复位」走目标 NRST，需要按板卡要求接线。已验收的 HPM6800EVK 接线使用软件复位。

1. 烧录目标并读回校验，随后复位运行。点击时再次核对芯片和 ELF 路径。成功输出包含 `verified: true`、`reset: true` 和写入字节数。

   ```powershell
   python -X utf8 toolbox.py --runtime tmp/first-run-web --timeout 240 target-flash --chip "$Chip" --file "$Firmware"
   ```

2. 核对烧录器显示「读回校验通过」。

   ```powershell
   python -X utf8 toolbox.py --runtime tmp/first-run-web page flash read '#f-result'
   ```

3. 查看烧录日志。再按烧入固件的功能检查串口、LED 或其他输出。

   ```powershell
   python -X utf8 toolbox.py --runtime tmp/first-run-web page flash read '#f-log'
   ```

4. CLI 超时后，输入错误提示中的 job ID，查询原任务状态。终端操作时，用实际值替换 `JOB_ID`。

   ```powershell
   python -X utf8 toolbox.py --runtime tmp/first-run-web job JOB_ID
   ```

更多页面和 CFG 导入用法见 [CLI 总指南](toolbox-cli-ai.md)。

## 4. 烧录 SuperLinkPRO 探针固件

从 [SuperLinkPRO Releases](https://github.com/minichao9901/superlinkPRO/releases) 查阅所需版本的说明，下载与探针硬件型号匹配的打包 APP BIN。以 Release 的 SHA-256 清单为准，核对下载文件的指纹。手册按钮适用于只连接一只待升级探针的场景。

1. 输入 APP BIN 的绝对路径，检查包头、CRC 和 SHA-256。结果包含 `dryRun: true`。此步不访问探针，也不需要 dfu-util。终端使用时，将 `"$ProbeFirmware"` 换成带引号的实际路径。

   ```powershell
   python -X utf8 toolbox.py probe-flash --file "$ProbeFirmware" --dry-run
   ```

2. 停止采集，从顶栏「连接选项」断开探针，关闭该探针的 CDC 串口。准备 PATH 中的 [dfu-util](https://dfu-util.sourceforge.net/dfu-util.1.html)。在 Windows 更新 PATH 后，重新启动工作台，并重新点击上一步检查 APP BIN。此步使用上一步核对的路径。命令会通过 HID 自动切换支持的 HPM5301 探针到 DFU，也支持已进入 DFU 的探针。执行结果包含应用区备份与日志位置。写入和读回一致、同一探针重新枚举后，结果显示 `verified: true` 和 `runtimeDetected: true`。若由 HID 进入 DFU，`enteredDfuByHid` 为 `true`。

   ```powershell
   python -X utf8 toolbox.py probe-flash --file "$ProbeFirmware"
   ```

升级后从顶栏重新连接同一只探针，在「帮助」→「帮助与快捷键」中点击「读取探针固件信息」，核对固件版本和编译时间。终端使用者可用 `--serial` 指定探针序列号，用 `--dfu-util` 指定工具绝对路径。
