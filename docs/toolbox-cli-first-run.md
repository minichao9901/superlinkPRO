# 从接线到目标烧录

本机双击 `dist/superlinkPRO/superlinkPRO.exe`，在顶部打开「可执行手册」。每条命令右侧可点击执行，输出、退出码和日志显示在该步骤下面。便携版使用包内 CLI。使用源码时，从仓库根目录启动服务并安装 `etm/requirements-cli.txt` 中的依赖。

## 1. 启动并检查工作台

1. 从源码启动工作台。打开命令输出的 `browserUrl` 并保留这个标签页。已经打开可执行手册时，此步核对并复用当前会话。

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

用 USB 数据线连接 SuperLinkPRO。按目标板针脚图连接 SWD 或 JTAG 和共地，核对供电和 I/O 电平。确认其他工作台已释放探针。首次连接，在可执行手册点击「授权 CMSIS-DAP」，在浏览器设备窗口选中你的探针。

1. 查看烧录器的目标选项。选项里的 `value` 是下一步要输入的芯片 ID。

   ```powershell
   python -X utf8 toolbox.py --runtime tmp/first-run-web page flash describe
   ```

2. 输入板卡对应的内置芯片 ID，例如 STM32H743 用 `stm32h7`，HPM6800EVK 用 `hpm6800evk`。

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

5. 读取目标身份。STM32H743 所在系列的 `devId` 为 `0x450`，HPM6800EVK 的 JTAG TAP ID 为 `0x1000563d`。结合板卡丝印确认型号，读不到身份时先检查选项和接线。

   ```powershell
   python -X utf8 toolbox.py --runtime tmp/first-run-web page flash invoke readIdcode
   ```

## 3. 烧录、校验和复位

确认板卡允许覆盖 Flash，需要保留原固件时先备份。软件复位通过调试通路发送，硬复位脚接线按板卡要求处理。

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

断开工作台里的探针连接和采集。让要更新的 HPM5301 探针进入 DFU 模式（`0D28:0207`）。支持 USER 键的板型可长按 USER 键进入。准备打包后的探针 APP BIN 和 PATH 中的 [dfu-util](https://dfu-util.sourceforge.net/dfu-util.1.html)。在 Windows 上把工具目录加入 PATH 后，重新启动工作台。

1. 备份探针应用区，写入 APP，逐字节读回校验并确认同一探针重新枚举。点击前在本步填写 APP BIN 的绝对路径。终端使用时，将 `$ProbeFirmware` 换成带引号的实际文件路径。成功结果包含 `verified: true` 和 `runtimeDetected: true`。

   ```powershell
   python -X utf8 toolbox.py probe-flash --file "$ProbeFirmware"
   ```

用 `--dry-run` 可只检查 APP 包头、CRC 和 SHA-256。命令支持 `--serial` 指定多只 DFU 设备中的一只，支持 `--dfu-util` 指定工具绝对路径。备份和各阶段日志的位置见命令输出。
