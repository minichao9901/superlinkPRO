# H743 CLI 候选版公开发布校验

Run-ID：20261024-0803-h743-cli-release。归档时间：2026-10-24T08:51:22.208962+08:00。
受测发布提交：000d682fde1fe966ed124f8bff18cc5f7d4f13b1。
Validation-Result：passed（公开发布与下载完整性）；Hardware-Test：not-run。

候选版已发布：https://github.com/minichao9901/superlinkPRO/releases/tag/etm-cli-v1.0.14-native.14-rc.1。
标签：etm-cli-v1.0.14-native.14-rc.1，prerelease=true，draft=false；没有将候选版设为正式最新版本。
main 已由 3875bfd 快进至 000d682，正常推送成功；没有强推或改写历史。

## 公开文件回读

正式公开 URL：https://github.com/minichao9901/superlinkPRO/releases/download/etm-cli-v1.0.14-native.14-rc.1/etm-bridge-win64.zip。
公开下载 manifest 与本地验收清单逐字段一致。从公开 URL 独立下载完整 ZIP，
共29个HTTP Range区段，检查206状态、Content-Range与每段长度，连续覆盖全部117729048字节。
完整SHA256：`7013b00bd4f37f1260c49abff9a200a6cd19af467a951cc70b59bcf7bd3fa240`，与本地已验收包完全一致。
各区段SHA、范围与耗时见 public-download-verification.json；完整回读包保存在：
C:\Users\Administrator\.codex\validation\20261024-0803-h743-cli-release\public-etm-bridge-win64.zip。

## 网页与正式版边界

Pages工作流成功：https://github.com/minichao9901/superlinkPRO/actions/runs/40044389298；head_sha匹配发布提交。
已确认Git中的正式下载ZIP/manifest与发布前native.13相同。额外尝试直接读取Pages manifest遭TLS握手失败，
因此没有把这次Pages HTTP读取写成通过；独立候选Release的manifest和完整ZIP公开回读均已通过。
初次草稿回执中的untagged下载地址已过时，最终地址以public-release-receipt.json为准；校验脚本已改用最终公开回执。

## 功能验收仍有边界

本轮源码CLI65项、冻结CLI65项和历史H743 1s完整重解对照通过，详见package-report.md。
最终版本一键运行时启动/退出、H743新硬件采集与调试/烧录回归仍未完成：
exec_command在进程创建前返回blocked by policy，无更具体原因，未绕过拒绝。
因此本次完成的是候选版公开交付，不能宣称用户要求的全部H743最终硬件验收已经完成。
F103ZE/H7B0不纳入本轮。正式下载仍保留native.13。
