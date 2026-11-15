# Changelog

版本号规则：MAJOR（破坏性改动或核心重写）/ MINOR（新增功能）/ PATCH（修复、依赖、文档、UI 打磨）。

## v2.7.0

### 新增

- **配置表单**：Web UI 的配置编辑改为带中文解释的表单，已填写的项自动回填，未填写的保持空白，
  按「基础 / 并发与超时 / 文件命名与格式 / 上传到网盘 / 网络 / Web 与日志 / 广告过滤」分组
- 字段级校验：必填、数字范围、选项合法性和逐项错误提示；`media_types` 等复选框至少选一项
- 表单与 YAML 两个标签页：日常用表单，特殊配置可切到 YAML 原文直接改
- 保存时保留 `config.yaml` 里未在表单中暴露的字段（如 `chat.download_filter`、`upload_telegram_chat_id`），
  并按 `chat_id` 保留每个频道的 `last_read_message_id`
- 空值即删除对应配置项；密钥字段继续掩码，未修改不会覆盖

### 修复

- 配置表单部分提交时不再误删未渲染的配置项（`__present` 标记区分「未填写」和「未提交」）
- 任务状态持久化在 chat 列表被表单调整后不会因下标越界丢失

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
