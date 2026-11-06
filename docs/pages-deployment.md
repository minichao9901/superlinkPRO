# Web 站点发布与恢复

正式站点：[superlinkPRO Web 工作台](https://minichao9901.github.io/superlinkPRO/)。
ETM 入口：[工作台 #etm](https://minichao9901.github.io/superlinkPRO/#etm)。
下载清单：`https://minichao9901.github.io/superlinkPRO/bridge/etm/downloads/manifest.json`。

## 发布方式

仓库 Settings → Pages → Build and deployment → Source 应为 **GitHub Actions**。
发布流程在 [pages.yml](../.github/workflows/pages.yml)，上传目录为 `web/`。
工作流的源代码/生成副本检查必须通过，再部署到 `github-pages` 环境。

```powershell
node web/tools/dev/build-web.mjs --check
node web/tools/dev/sync-etm-page.mjs --check
git push origin main
```

自动发布只监听 `main` 中 `web/**` 或工作流自身的变更。
仅推送文档、或 `git push` 显示 Everything up-to-date，不会重新发布。
恢复站点配置后，在 Actions → Deploy web/ to GitHub Pages → Run workflow，选择 `main` 手动发布。

## 整站 Site not found / 404

1. 分别检查仓库地址、站点首页和下载清单的 HTTP 状态。
2. 查询 `GET https://api.github.com/repos/minichao9901/superlinkPRO` 的 `has_pages`。
   如为 false，且有权限的 `GET /repos/minichao9901/superlinkPRO/pages` 也返回 404，说明站点配置不存在。
3. 在 Settings → Pages 恢复 Actions 来源。使用 GitHub REST API 的对应恢复请求为
   `POST /repos/minichao9901/superlinkPRO/pages`，JSON 为 `{"build_type":"workflow"}`，需要现有仓库管理权限。
4. 手动运行现有 Pages 工作流，等待 build 和 deploy 两个 job 成功。
5. 核对首页、页面引用的 JS/CSS、ETM 页面、下载 manifest 均返回 200；下载版本及 SHA256 与本地清单一致。
   可以用 HEAD 检查 ZIP 可访问和 Content-Length；这不等于重新下载完整 ZIP 后的内容哈希校验。
6. 将问题、恢复动作、发布提交/run、线上验证结果同步记录在 [项目问题跟踪列表](项目问题跟踪列表.md)。

页面存在但资源 404 时，继续检查页面入口和相对路径、当前部署提交、生成资源检查及 CDN 缓存。
历史成功工作流只证明当时部署成功；GitHub 上的 Pages 设置仍需单独核对。

## 2026-11-06 事件

关联 ISSUE-035：首页与下载清单均为 GitHub Pages Site not found 404，仓库公开且正常，
`has_pages=false`、认证 Pages API 为 404，Actions 工作流仍启用，历史部署成功。
已通过站点创建 API 恢复 workflow 模式并触发当前 main 部署。
上述证据确认当时站点配置缺失，不能据此确定由谁、何时移除了配置。

本次发布范围为既有 Web 资源，Windows 正式下载包继续沿用清单中的版本。

恢复后验收通过：6 项页面/脚本/样式/清单 GET 均为 200 且内容与本地一致；完整包 HEAD 为 200，Content-Length 为 92628064。
部署 [42142498349](https://github.com/minichao9901/superlinkPRO/actions/runs/42142498349) 成功，详细结果见 [验收 JSON](validation/pages-recovery-20261106.json)。
