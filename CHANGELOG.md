# Changelog

版本号规则：MAJOR（破坏性改动或核心重写）/ MINOR（新增功能）/ PATCH（修复、依赖、文档、UI 打磨）。

## v2.6.6

### 新增

- Web UI 重新设计：更清晰的层级、明暗主题、移动端适配、键盘可操作与可见焦点环
- 单任务控制：下载中任务可「暂停 / 继续 / 删除」，暂停只影响当前消息
- 配置在线编辑：齿轮按钮打开 `config.yaml` 编辑器，保存时校验 YAML 并原子写入；
  `api_hash` / `bot_token` / `web_login_secret` / `proxy.password` 以掩码显示，未修改不会覆盖
- 退出登录按钮与 `/logout` 接口
- 已完成页显示完成时间、「已上传 / 仅本地」状态，可复制路径或删除记录
- 新增接口：`/pause_task`、`/resume_task`、`/delete_task`、`/get_config`、`/set_config`、`/logout`
- `CHANGELOG.md` 与语义化版本号

### 修复

- 任务完成后仍停留在「下载中」列表（bot 已成功、网页一直 100%）——下载完成会移入「已完成」，
  列表上限 500 条，避免进程长期运行后内存增长
- `/get_download_list` 返回真实 JSON；此前返回 `text/html`，前端解析失败导致列表恒为空
- 已完成任务查询、`total_size == 0` 时的除零 500
- 删除失败记录时同步清理 `ids_to_retry`，重启后不会重新排队
- 重试接口等待真实结果，未匹配到任务时返回失败而不是恒定成功
- 全局暂停按钮的返回值改为真实下一状态，避免按钮标签与实际状态相反
- 任务结束、失败、取消都在 `finally` 中记账，异常不会留下永久「下载中」状态

### 工程

- 测试 job 由 `continue-on-error` 改为必过（`Tests`），修复上游遗留的测试腐化
- 新增 Web 接口、rclone 参数、上传失败删除、任务终态、进度状态等回归测试（82 项通过）
- 明确 `prefers-reduced-motion`、forced-colors、320px 窄屏与 3:1 边界对比度处理

## v2.2.6-fork.1（历史）

- rclone 上传改用 `copyto` + 退出码判定，上传成功才删除本地
- bot 进度卡片限制长度、编辑成功后才写缓存
- 失败任务独立登记，Web UI 可重试 / 删除
- Docker `STOPSIGNAL SIGINT`，停止时正常写回 `data.yaml`
