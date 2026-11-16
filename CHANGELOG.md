# Changelog

版本号规则：MAJOR（破坏性改动或核心重写）/ MINOR（新增功能）/ PATCH（修复、依赖、文档、UI 打磨）。

## v2.9.4

### 修复

- **已存在的文件被误判为「已下载」**：判断条件写成了 `file_size or file_size == media_size`，
  只要目标路径上有非空文件就会跳过；v2.9.0 及更早版本留下的半截文件会因此被永久当成完成品。
  现在只有**字节数完全一致**才跳过
- **`file_formats` 按扩展名配置却整类文件被跳过**：之前拿 mime 子类型比较，`mp3` 实际是 `audio/mpeg`（`mpeg`）、
  `mov` 是 `video/quicktime`、`docx` 是一长串 `vnd.openxmlformats-...`。现在扩展名和 mime 子类型任一命中即放行
- **`media_types` 写成字符串时全部消息被跳过**（`media_types: video` 会被逐字符迭代）：加载和热加载时统一归一化为列表，
  未识别的类型会告警
- **配置写入不再截断文件**：`config.yaml` / `data.yaml` 改为经由 `utils.atomic_write` 原子写入；
  bind mount 场景的兜底改为「复制已写完的临时文件」，磁盘写满等失败不会再先把原配置清空
- **运行期清空 `web_login_secret` 不再立刻解除鉴权**：监听地址只在启动时收敛，
  运行期清空密码会让已暴露在 `0.0.0.0` 的 UI 变成无鉴权；现在保持旧密码并提示需要重启
- **`debug_web` 只在 `127.0.0.1` 生效**：否则会打开 Werkzeug 交互式调试器（网络可达即可执行代码）
- **`/login` 增加失败限速**：同一 IP 连续失败 5 次后锁定 60 秒，登录页会显示提示文案；
  失败记录 15 分钟后过期，公网扫描换 IP 也不会让计数器无限增长
- **仓库不再跟踪 `config.yaml` / `data.yaml`**：两者本来就在 `.gitignore` / `.dockerignore` 里，
  但历史遗留的跟踪状态会让用户把 `api_hash` 等真实密钥提交进公开仓库。仓库改为跟踪
  `config.yaml.example`，首次运行会自动从模板创建 `config.yaml`；如果 Docker 因为宿主机缺少
  对应文件而把挂载点建成目录，启动/退出时会给出明确提示而不是 `IsADirectoryError`
- **bot 配置写错文件**：`stop_download_bot()` 把配置写进了名为 `d` 的文件而不是 `bot.yaml`，
  `/add_filter` 的改动重启后丢失
- **非法 `download_filter` 不再继续建任务**：`/forward`、`/listen_forward` 里过滤表达式写错时，
  之前会照常入队并让每条消息在 `filter.exec()` 里抛异常
- **`get_chat_history_v2` 空页兜底**：把消息 ID 当「跳过条数」，会从最新消息开始跳过成千上万条并下错范围
- **Web `/retry_failed`**：非法 `message_id` 不再退化成「整频道重试」；等待超时改为返回「已提交」而不是「失败」
- **进度卡片**：上传进度为 0 字节时除零、rclone 输出 `N/A` 百分比时解析异常，都会导致进度卡片渲染失败
- **关闭流程**：取消并等待所有后台任务（含 `download_all_chat`），任一关闭步骤失败不再跳过配置落盘
- **工具**：`manage_duplicate_file` 改为分块计算 md5（GB 级文件不再整个读进内存）；
  `extract_info_from_link` 遇到非数字路径会给出可读错误
- **云盘**：aligo 上传失败时不再残留临时 zip

### 构建与发布

- **构建镜像时自动创建对应版本的 GitHub Release**：版本号统一取自 `utils/__init__.py` 的
  `__version__`，镜像标签、git tag、Release 三者不会再各说各话；Release 正文由
  `scripts/release.py` 从 `CHANGELOG.md` 抽取同名章节并附上拉取命令生成
- 镜像推送成功后才创建 Release（失败不留半成品），已存在的 Release 自动跳过，可安全重跑
- 手动推 `v*` tag 时校验 tag 与 `__version__` 一致，不一致直接失败
- 镜像注入 `org.opencontainers.image.version` / `.revision` 标签，`docker inspect` 可查版本
- README 增加 Release 徽章，仓库首页即可看到当前版本，并补充「发布新版本」流程说明

## v2.9.3

### 修复

- **`[-500 TIMEOUT] ... caused by "upload.GetFile"` 反复失败且看不出原因**
  - 根因：Pyrogram fork 的 `get_file` 把异常写进日志后**静默结束数据流**，
    `handle_download` 又把这个异常吞掉、返回 `None`，下载器只能报
    「下载中途中断，未生成文件」。外层重试 3 次、每次再续传 3 次，
    一条 210MB 的文件能卡 10 分钟以上，日志里还只有 `client.py` 的 traceback
  - `pyrogram_guard` 现在会捕获 fork 日志里的真实异常并挂到
    `DownloadIncomplete.cause`；同时给 `handle_download` 打补丁，把
    `DownloadIncomplete` 抛回下载器，不再被吞成 `None`
  - 确认是 Telegram 侧 5xx（如 `[-500 TIMEOUT]`）时不再无意义地重开同一个
    offset，直接带上原因失败；`download_media` 用指数退避重试（网络类错误 10s / 20s / 40s，其他 3s / 6s / 12s）
  - 失败原因写入失败记录：Web UI「失败」页新增**失败原因**列（窄屏显示在文件名下方），
    日志与重试提示也会带上真实原因和排查建议
  - 网络类失败的提示会指出常见原因：服务器到 Telegram 存储节点的链路问题、
    Docker/主机 MTU（可尝试 1400）、出口 IP 被限速、需要配置 `proxy`、或并发过高

## v2.9.2

### 文档

- **隐私清理**：README、`config.yaml` 模板、配置表单提示与预览页里的示例频道 ID、示例文件名
  统一替换为通用的占位数据，文档截图同步重拍，不再出现真实使用场景中的频道 / 文件信息

## v2.9.1

### 修复

- **大文件下载到一半总是失败，并被误报为「文件引用过期」**
  - 根因：Pyrogram fork 的 `Client.get_file` 会把 `upload.GetFile` 的所有异常吞掉、只写一行日志，
    于是 200MB 的文件只收到 5MB 就被当成「下载完成」返回；`handle_download` 把残缺的临时文件
    移正，随后的尺寸校验失败又被映射成 `BadRequest`，日志就变成了「文件引用过期」，
    重试也只是重新拉取消息、继续得到同样的残片
  - 新增 `module/pyrogram_guard.py`：包装 `Client.get_file`，当数据流提前结束（少于 `file_size`）时，
    按 1 MiB 块边界用 `GetFile` 的 offset **自动续传**；连续 3 次完全没有进展才抛出 `DownloadIncomplete`
  - `download_media` 新增 `DownloadIncomplete` 分支：等待后重新拉取消息（可能真是引用过期）再重试，
    日志会写明「只收到 X / 应为 Y 字节」，不再谎称文件引用过期
  - 失败原因写入 `node.last_error` 并随失败记录保存；Web UI 失败页与日志能看出是传输中断而不是引用过期
- 下载返回空路径时增加间隔重试，避免瞬时打满队列

## v2.9.0

### 新增

- **三个任务列表分页**：默认 25 条/页，可选 25 / 50 / 100 / 全部；切换标签会记住各自页码，
  每页条数存在浏览器里（localStorage），任务很多时不再一次铺满整页

### 修复

- **全局暂停后「下载中」列表没有任何暂停迹象**
  - 顶部新增暂停横幅；每行显示 `已暂停` / `单条暂停` 标签；速度和进度冻结，速度显示 `—`
  - 暂停状态也会跟随 `/get_download_status` 同步（其他客户端暂停时本页也会更新）
- **删除任务点了「确认删除」却没有删除**
  - 根因：Web UI 传的是字符串 `chat_id`，而任务字典以 int 为键；字符串键查不到任何记录，
    删除、重试、单条暂停全部静默失效（接口却返回成功）
  - 现在 API 入口统一 `normalize_chat_id()`（数字 ID 转 int，`@username` 保持字符串），
    删除下载中任务会真正设置取消标记并让任务以「跳过」结束
  - 删除按钮执行完会恢复成「删除」，不再残留红色的「确认删除」
- **失败列表点「重试」总是提示「没有匹配的失败任务」**
  - 同一个 chat_id 类型不匹配问题；现在按规范化后的 ID 匹配
  - 失败记录所属频道不在当前配置里时（例如 bot `/download` 直接下载的链接），
    会用临时任务节点重新入队，不再直接放弃
  - 重试失败时会返回具体原因（消息已不可访问 / 拉取失败 / 无匹配记录），不再是笼统提示

## v2.8.2

### 修复

- 远端目录计算改为与平台无关：先把路径分隔符统一成 `/` 再做 `dirname`/`relpath`，
  避免在 Linux（Docker）上处理 Windows 风格路径时目录部分丢失
- `remote_dir` 只有 `115:` 这类裸远端时不再产生 `115://`，重复斜杠统一折叠
- 补充 5 个远端目录映射的回归测试（结尾斜杠、反斜杠、`..`、重复斜杠）

## v2.8.1

### 修复

- **云端明明已有文件却显示「新文件上传成功」**，同时云端文件日期还是几天前
  - 根因：rclone 默认按「大小 + 修改时间」比较。刚下载的本地文件 mtime 是今天，
    远端是几天前，于是每次都被判定为不同并重新上传；而部分网盘（115/OpenList 等）
    按内容哈希去重，保留旧对象，日期就停在几天前，rclone 却仍打印 `Copied (new)`
  - 现在上传前先用 `rclone lsjson --stat` 检查目标：同名且大小一致时直接跳过，
    标记为 **云端已有**（不改动云端文件，也不再浪费带宽）
  - 上传后再检查一次远端：如果服务端去重后保留了旧对象（修改时间明显早于本次上传），
    自动改判为 **云端已有** 并打印警告
  - 大小不同时照常覆盖上传，日志会写明 `different size, replacing`
- **远端目录拼接**：改用 `os.path.relpath`（带兜底）计算相对目录，修复 `save_path`
  带结尾斜杠或反斜杠时远端路径残留本地绝对路径、导致文件被传到另一个目录的问题。
  日志现在会同时打印本地路径和完整远端路径，便于核对。
- 本地文件大小读取失败时不再让上传任务失败，而是回退给 rclone 自行判断。

### 升级后如何确认

日志会明确写出三种结果之一：

```text
remote already has 115:Telegram/频道/2026_04/xxx.mp4 (1.8 GiB), upload skipped
upload new file /app/downloads/频道/2026_04/xxx.mp4 -> 115:Telegram/频道/2026_04/xxx.mp4 success (rclone: Copied (new))
remote kept the older object at ... ; the drive deduplicated the upload, counting it as already existing
```

如果某个文件仍显示新上传、但云端日期没变，看日志里的远端路径是否和你预期的目录一致。

## v2.8.0

### 新增

- **Web UI 品牌升级为 TeleVault**，配新的圆形归档 + 下载箭头 Logo（含 favicon 与登录页），
  页面标题、顶栏、登录卡片同步更新
- **上传结果区分「新文件」和「云端已有」**：
  - rclone 以 `-v` 运行并解析每文件结果：`Copied (new)` → 新上传，`Unchanged skipping` → 云端已有
  - 「已完成」列表的标签改为 `已上传` / `云端已有` / `仅本地` 三种
  - bot 进度卡片的上传统计拆成 `新文件 N` 与 `云端已有 M`
  - 日志同样区分：`upload new file ... success` 与 `remote already has ..., upload skipped`
- 上传成功后删除本地仍然保留（云端已有也视为成功，会删除本地）

### 修复

- `aligo` 上传适配器此前会把同步调用放在事件循环里执行，并把返回值当回调传给线程池
  （必然抛 `TypeError`）；已改为 `run_in_executor` + `functools.partial`

## v2.7.4

### 修复

- 任务列表的 ID 列显示完整 chat id（此前会被截断），列宽同步调整
- 「已完成」标签补上数量徽标，并随任务完成实时更新（不再只有下载中/失败有数字）
- 表格改为顶部对齐：ID 列是两行（消息 ID + 频道 ID），此前垂直居中导致第一行比旁边的文件名高出一截
- 已完成行把「已上传 / 仅本地」状态放到文件名前面，窄列时也不会被省略号吃掉
- 已完成操作按钮「复制路径」简化为「复制」，给文件名和路径让出宽度

## v2.7.3

### 修复

- **全部下载被静默跳过（v2.7.0/2.7.1 表单保存后的配置损坏）**
  - 现象：日志里没有报错，bot 卡片只回「Direct download…」，网页列表一直为空
  - 根因一：早期表单把 `file_formats` 的列表回填成文本 `"['all']"`，保存后变成字符串。
    `_can_download` 把字符串当列表判断，导致视频/音频/文档全部判定为「跳过」
  - 根因二：空的 `chat_id` 被写成 YAML `null`，启动时 `None < int` 抛异常，
    每个频道都只打一行 `Download None error` 就结束
  - 现在：`file_formats` 在加载时统一转成列表（文本写法自动修复并告警），
    `_can_download` 对缺失/异常值一律按「允许」处理，不再静默丢任务
  - 空 `chat_id` 在加载时跳过并给出明确日志，`last_read_message_id` 为空按 0 处理，
    下载循环也会兜底跳过空频道，不再整批失败
- Web UI 表单会忽略 `null`/`None` 的频道，读取时不会显示成字符串 `None`

> 升级到 v2.7.3 后无需手动改配置：启动时会自动修复 `file_formats`；
> 频道如果之前被写成空值，日志会提示，到 Web UI「下载的频道 / 群组」重新填一次即可。

## v2.7.2

### 修复

- **配置面板的 YAML 原文显示不全**：文本框继承了旧的 `flex` 布局，放进新的面板后只有默认 20 列宽，
  右侧大片空白、长行被截断。现在铺满对话框、自动换行，只有文本框自身滚动（不再出现双层滚动条）
- `file_formats` 三个格式项兼容列表与文本两种写法：读取时 `- all` 显示为 `all`，
  保存时 `all` / `mp3, flac` / 粘贴的 `['all']` 都会写成 YAML 列表，避免把 `"['all']"` 当字符串存进去导致过滤失效
- 只有旧版 `chat_id` / `last_read_message_id` 的配置，表单会把该频道回填到「下载的频道 / 群组」里
  （不再显示空白）

## v2.7.1

### 修复

- **旧静态文件缓存导致的空白后台**：浏览器缓存了上一版 `app.js`、HTML 却是新版本时，
  旧脚本会去写已经不存在的元素（`Cannot set properties of null (setting 'textContent')`），
  导致界面一直显示「连接异常」且配置 / 退出按钮无反应
  - 静态资源 URL 加上 `?v=<版本号>`，每个版本换新地址
  - HTML 响应加 `Cache-Control: no-store`，静态资源改为长缓存 + immutable
- 所有 DOM 写入改为空值安全；初始化异常时顶部显示明确提示和「重新加载」按钮，而不是整页静默失效
- 新增页面/后端版本一致性检查，不一致时提示强制刷新
- 数据文件不再被浏览器缓存影响

### 使用提示

升级后如果仍看到旧界面，按一次 `Ctrl+F5`（Mac：`Cmd+Shift+R`）即可。

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
