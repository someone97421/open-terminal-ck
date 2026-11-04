# 工作区隔离与模型图片接入

## 实现与适用范围

本改造新增可选的 `OPEN_TERMINAL_WORKSPACE_ISOLATION=true`。它使用同一镜像中的 Linux bubblewrap，为每个独立聊天 `(user_id, chat_id)` 或项目 `(owner_id, project_id)` 启动一个常驻 API 工作进程，不为对话创建容器或重复安装 Python、Node、FFmpeg 等依赖。

主网关验证现有 Bearer API Key 和工作区签名，然后将 HTTP、上传下载、流式输出、终端 WebSocket、notebook 与端口代理转发给对应工作进程。文件处理与任意命令都处于同一个受限文件系统视图，其他工作区没有挂载进去；服务端直接文件读写也在这个视图内，避免只检查路径后仍从宿主打开文件的竞态。

工作区路径为：

```text
/workspaces/<SHA256(user_id)>/<SHA256(chat_id)>/
  uploads/
  generated/
  tmp/
  cache/
    cwd.json
    image-context/
```

原始标识不作为目录名，使用完整哈希。已有上传、写入和执行接口仍可以选择工作区内部的任意子目录；这些子目录是默认整理结构，不强制重命名用户文件。`/files/cwd` 设置的目录持久化到各自的 `cache/cwd.json`，无效或已删除时退回工作区根。普通 shell 的 `cd` 仍只影响该 shell；跨工具修改默认目录使用现有的 cwd 接口。

项目使用 `/workspaces/<SHA256(owner_id)>/project-<SHA256(project_id)>/`，共享该项目的聊天和成员复用同一工作进程。共享的 `AGENTS.md` 位于项目根目录；`X-Session-Id` 仍为真实聊天 ID，shell/notebook 会话仍按用户与聊天区分。项目归档只限制 WebUI 的新请求，不删除目录；服务端不自行清理项目文件。v1 聊天目录完全兼容，不自动迁移旧数据。

`/usr`、`/bin`、`/sbin`、`/lib`、`/lib64` 只读共享，`/etc` 仅挂载证书、动态链接、字体、账号名称和名称解析等必要项；不挂载宿主 `/home`、`/root`、Docker socket、sudoers 或主服务状态目录。每个工作区使用独立的 PID、IPC、挂载及默认网络命名空间。`TMPDIR` 指向工作区的 `tmp/`，缓存及日志也保存在自己的工作区；硬编码 `/tmp` 的程序使用独立的临时视图，该部分不跨进程重启保留。工作进程通信 socket 位于临时运行目录。

**边界：这是共享 Linux 内核的文件系统/进程隔离，不是独立虚拟机。未增加 CPU、内存或磁盘配额。同一工作区中的代码可以修改自己的文件、观察或终止自己的 API 工作进程；不会因此获得主网关的全局密钥。不要在共享运行时目录放私有文件。**

## 部署配置

现有 API Key、Token 和认证配置继续使用，不需要更换。示例只展示新增环境变量：

```dotenv
OPEN_TERMINAL_WORKSPACE_ISOLATION=true
OPEN_TERMINAL_WORKSPACE_ROOT=/workspaces
OPEN_TERMINAL_WORKSPACE_MAX_WORKERS=32
OPEN_TERMINAL_WORKSPACE_NETWORK=isolated
OPEN_TERMINAL_IMAGE_MAX_EDGE=2048
OPEN_TERMINAL_IMAGE_MAX_BYTES=2097152
OPEN_TERMINAL_IMAGE_MAX_PIXELS=40000000
```

四种 Dockerfile 均安装 bubblewrap，并预建可写 `/workspaces`。容器运行策略必须允许 bubblewrap 创建 user/mount/pid/network namespace 及挂载工作区内的 `/proc`；Docker 的 seccomp、AppArmor、系统路径遮罩或 OpenShift restricted SCC 可能阻止这些操作。安装了 bubblewrap 不等于宿主允许执行。失败时启动或首个工作区请求会明确报错/503，不会退回共享目录；查看容器日志中的诊断。

若日志出现 `bwrap: No permissions to create a new namespace`，但 `/api/config` 正常、文件与执行接口返回 503，请先核对宿主的 `user.max_user_namespaces`、存在时的 `kernel.unprivileged_userns_clone` 及内核 `CONFIG_USER_NS`。缺少某一 sysctl 项本身不代表不支持 user namespace。

在 NAS 的 Linux 6.12.30+、Docker 26.1.0、Compose v2.26.1 上，独立探测已确认 seccomp 和 AppArmor 均参与阻断；仅解除这两项仍会因 `/proc` 挂载被拒而失败。以下是该环境中已验证可启动真实工作进程的策略组合：

```yaml
security_opt:
  - seccomp=unconfined
  - apparmor=unconfined
  - systempaths=unconfined
```

这会取消外层容器的 seccomp/AppArmor 策略及默认系统路径遮罩、只读限制，明显放宽外层保护；不修改 Key、工作区目录或应用内的 bubblewrap 隔离，也未使用 `privileged` 或额外 `SYS_ADMIN`。这里的“已验证”限定为探测过的策略组合，不代表不存在更精细的规则。

`systempaths=unconfined` 可能不出现在 `docker inspect` 的 `SecurityOpt` 数组中，应检查实际的 `MaskedPaths=[]` 和 `ReadonlyPaths=[]`。该 NAS 的 Compose 已验证能正确应用；其他平台需自行确认，不能仅凭参数被接受就认定生效。若内核缺少 user namespace 能力，这些选项仍无法补足。

实测通过工作进程健康检查、cwd、临时文件写入、列目录与 Python 命令执行；旧镜像的文本读取在图片识别阶段暴露 `KeyError: 'MPO'`，源码已将打开插件列表与可接受格式分开，修复仍待新镜像复测。生产项目签名、网关转发及 WebSocket 尚未验证。

工作进程启动失败会持续输出原始 stderr 到容器日志，并保留最多 8 KiB 尾部用于错误分类；命名空间权限失败通过 HTTP 503 返回可读诊断。WebSocket 在 accept 前遇到同类失败会表现为握手 403，不能单凭该状态认定 Key 错误。

在已有本地镜像上使用持久卷的前台运行示例：

```bash
docker run --rm --name open-terminal \
  -p 8000:8000 \
  -v open-terminal-workspaces:/workspaces \
  --env OPEN_TERMINAL_API_KEY \
  -e OPEN_TERMINAL_WORKSPACE_ISOLATION=true \
  -e OPEN_TERMINAL_WORKSPACE_NETWORK=isolated \
  open-terminal-ck
```

API Key 通过当前环境传入，也可继续用原有 secret 挂载方式。bind mount 的目录需预先允许服务 UID 写入；服务不会递归 chown 旧数据。主网关应只运行一个 Uvicorn worker，不使用 reload。默认最多 32 个工作进程，达到上限时按最近使用顺序回收空闲进程，保留文件和 cwd；有 HTTP/WS 请求、后台命令、子进程、终端或 notebook 会话的工作区不回收，全部忙碌时才返回 503。不同工作区可并发冷启动。服务停止时回收子进程及 socket 目录，重启后按相同身份恢复原文件和 cwd；执行中的 shell、notebook 内存与后台进程不会跨服务重启恢复。

默认 `isolated` 不提供外网访问；notebook 与终端启动的本地服务可由同工作区内的 `/proxy` 访问。若设置 `OPEN_TERMINAL_WORKSPACE_NETWORK=shared`，可联网安装当前工作区的依赖、访问外部站点，同时也能连接宿主及其他工作区的网络监听服务，**不再具有网络互访隔离**。不要将其他工作区的文件通过未认证网络服务暴露出去。

全局依赖由管理员在镜像构建、入口安装阶段或受信任的共享环境中统一安装。工作区不能修改只读共享依赖。需要额外虚拟环境时，可使用：

```dotenv
OPEN_TERMINAL_WORKSPACE_RUNTIME_PATHS=/opt/open-terminal-venv:/opt/shared-tools
```

路径以 Linux 的 `:` 分隔，必须是存在的真实绝对目录，不能包含工作区或私有状态。解释器与 Open Terminal 包必须安装在可见运行时中；网关启动时检查当前解释器（及其符号链接目标）位于 `/usr` 等系统目录或这些路径内，否则直接报错，不会等到首个请求才 503。内部使用 Python `-I` 启动，不依赖当前目录或用户 `PYTHONPATH`。各路径下存在的 `bin/` 按配置顺序置于工作区 `PATH` 前部，终端和命令中的 `python`、`pip` 等优先使用共享虚拟环境；其仍为只读，需要额外包时在工作区内另建虚拟环境（可用 `--system-site-packages` 复用共享包）。推荐镜像中正常安装的 `/usr/local`，或安装了本项目的 `/opt` 虚拟环境。只挂载完整源码仓库可能暴露其中的私有配置，不应拿它替代干净的共享运行时。

## OpenWebUI 的身份接入（双仓库链路）

隔离由两个仓库共同完成：`open-webui-CK` 服务端负责认证用户、持久化对话及其归属并签名；本仓库只验证签名并路由到对应工作进程。本仓库没有 OpenWebUI 数据库，不能独立核实 chat 的真实主人，也不接受模型工具参数或浏览器自行选择工作区。

链路：

1. 浏览器 → OpenWebUI：普通登录会话，不接触终端 API Key 与签名。
2. OpenWebUI 服务端：普通聊天在首次上传或访问终端前通过 `/api/v1/chats/new` 持久化，首次发送沿用同一 ID；临时聊天通过 `/api/v1/terminals/chats/prepare` 验证登录用户和实时连接后登记归属，不写入普通聊天库。后续请求重新查验相应的会话归属，再签名。
3. OpenWebUI → Open Terminal：每次工具调用、上传、原图下载、历史恢复及 WebSocket 建连都重新签名（HMAC 有效期至多 300 秒），附带下列请求头。
4. Open Terminal 网关：校验 Bearer Key 与签名，v1 按 `SHA256(user_id)/SHA256(chat_id)`、v2 按 `SHA256(owner_id)/project-SHA256(project_id)` 启动或复用工作进程，删除客户端传入的 `X-User-Id`、`X-Session-Id` 后改写为签名内的值再转发。WebUI 的新普通 Chat 使用独立 Pyodide，项目 Chat 使用此 v2 Terminal 链路。

**自动识别：** 未认证可读的 `GET /api/config` 中 `features.workspace_isolation` 表示是否开启；开启时另含：

```json
{"features":{"workspace_isolation":true,"project_workspaces":true},"workspace_context":{"protocol":"open-terminal/workspace/v1","protocols":["open-terminal/workspace/v1","open-terminal/workspace/v2"],"scopes":["chat","project"],"version":1,"algorithm":"HMAC-SHA256","max_ttl":300}}
```

WebUI 的原生 Bearer 终端连接在携带有效聊天身份时统一签名，兼容未开启隔离的终端；项目要求隔离已开启且声明 v2/project 能力。隔离网关接受 v1 与 v2 的有效签名，不会退回共享空间。

**签名格式**（与 `open_terminal/workspace_context.py` 一致）：

- `payload = {"v":1,"user":<user_id>,"chat":<chat_id>,"exp":<unix 秒>}`，JSON 紧凑序列化（`separators=(",",":")`，`ensure_ascii=False`），UTF-8 编码。
- `X-Workspace-Context = base64url(payload)` 去掉末尾 `=`。
- `X-Workspace-Signature = hex(HMAC-SHA256(key=现有 API Key, msg="open-terminal/workspace/v1" + "\0" + X-Workspace-Context))`。
- 项目 v2 的 payload 为 `{"v":2,"user":<当前用户>,"chat":<真实聊天>,"scope_type":"project","scope_id":<项目ID>,"owner":<项目所有者>,"exp":<unix秒>}`；签名前缀改为 `open-terminal/workspace/v2`，编码及请求头校验相同。可信接入端必须查验项目和聊天真实归属，不能接受客户端指定 owner/scope。`sign_workspace_context` 支持相应关键字参数；原有 `integrations.workspace_headers` 仍用于 v1 聊天。
- 项目设置使用可信 WebUI 内部的 `project:<id>` session 读写根目录 `AGENTS.md`；此上下文不由浏览器直接签发，不占用真实聊天的实时会话。
- `exp` 必须晚于当前时间且不超过当前时间 + 300 秒（另容忍 60 秒时钟偏差）；用户与对话标识为非空字符串、各不超过 1024 字节，且须能作为 latin-1 HTTP 头值发送（OpenWebUI 的 UUID 满足）。

请求头：

- `Authorization: Bearer <现有 API Key>`
- `X-User-Id`、`X-Session-Id`：必须与签名内的 user/chat 完全相同
- `X-Workspace-Context`、`X-Workspace-Signature`

Python 服务端可直接使用本仓库的同格式实现：

```python
from open_terminal.integrations import workspace_headers

# owns_chat(user_id, chat_id) 可同步或异步，必须实际核验数据库或临时会话登记并严格返回 True。
headers = await workspace_headers(terminal_api_key, user.id, chat_id, owns_chat=owns_chat)
response = await terminal_client.get("/files/read", params={"path": "uploads/photo.png"}, headers=headers)
```

后端必须覆盖来自客户端的同名请求头。全局 API Key、签名请求头和 WebSocket 首帧中的服务凭据都不能交给浏览器或模型。WebSocket 由可信后端代理，握手请求附相同签名请求头，首帧仍为 `{type: "auth", token: terminal_api_key}`；网关会把 `token` 换成仅在该工作区有效的内部凭据，并用签名身份覆盖首帧的 `user_id`、`chat_id`，客户端填写的值不生效。长连接只在建连时验签，签名过期不影响已建立连接；后续新连接须重新签名。

**真实部署：** 两个服务共用同一个 API Key（OpenWebUI 中配置的终端连接 Key 即 `OPEN_TERMINAL_API_KEY`），Open Terminal 按上文“部署配置”开启隔离并挂载 `/workspaces` 持久卷，OpenWebUI 的终端连接地址指向该服务。Open Terminal 端口只应对 OpenWebUI 服务端可达，不直接暴露给浏览器；时钟需同步（NTP）。默认安全策略不变：未开启隔离时行为与原来一致，开启后缺少或无效签名一律 401。

**工作区接口的兼容性变化仅在隔离模式生效：** 文件及执行 URL/工具名保留；缺少签名的旧直连方式返回 401，无法退回共享空间。现有 FastMCP 适配器只注入固定 API Key，不会自动获得当前对话身份，需在下游增加每次调用的身份转发，否则调用会被拒绝。`browser`、`desktop` 在隔离模式关闭，避免现有共享会话与额外截图入口绕过边界；其他模式不受此开关影响。`/health`、`/api/config` 和 OpenAPI/文档仍可全局读取，不包含工作区文件。

不能只信任普通的 `X-User-Id`，也不能只靠模型系统提示词约束。原有 `OPEN_TERMINAL_MULTI_USER` 模式保留，隔离模式优先使用新的工作区进程，不继续给每个对话创建 Linux 账号。

## 图片预览与完整原图

`GET /files/read` 对图片先检查实际内容，避免把改为 `.txt` 的 PNG 当文本返回；声明为图片但无法解析的文件也拒绝。图片统一 EXIF 转正、动画取首帧、透明区域铺白，编码为 JPEG。最长边默认不超过 2048，编码文件默认不超过 2 MiB；先降质量，仍过大就继续降采样。base64 和 JSON 封装另有约三分之一的编码开销。原始图片默认至多 32 MiB、四千万像素，超限或未知格式拒绝并返回 415。
图片预览规则在所有模式生效：`read_file` 的图片内容类型改为 JPEG 预览，notebook 图片返回值也经过网关；原始文件与用户下载接口保持原样。


支持 JPEG（含 MPO 首帧）、PNG、WebP、GIF、BMP、TIFF；SVG 等无法安全栅格解码的输入不进入模型。原图、元数据和原始动画文件保留，预览不覆盖源文件。下载和 `/files/view`、`/files/serve` 仍返回原始字节，`display_file` 仍仅返回原文件位置和存在状态。

局部细节使用重复的 `crop` 查询参数，坐标基于 EXIF 转正后的原图：

```text
/files/read?path=uploads/photo.png&crop=100&crop=200&crop=1100&crop=900
```

等价工具参数为 `crop: [100, 200, 1100, 900]`，右边和下边不包含在内。裁剪结果遵守相同预览限制，越界或非图片裁剪返回 415。响应通过 `X-Image-Width`、`X-Image-Height` 提供实际预览尺寸。

预览持久缓存位于工作区 `cache/image-context/`，旧模式使用用户主目录的 `.cache/open-terminal/image-context/`。键包含原图内容哈希、裁剪、限制及编码版本；缓存命中仍检查真实编码、尺寸和字节数。POSIX 下用 no-follow 目录句柄和原子替换保护缓存写入；不能安全使用缓存的平台会即时生成，不覆盖原图。缓存可重新生成，不自动清理其他对话或历史文件。notebook 正常写盘时保留完整原始输出，返回副本的图片经过同一网关；单项无法处理时返回拒绝说明，其他输出继续返回。

## OpenWebUI 的最终 LLM 出口

**Open Terminal 本身不发送 LLM 请求。修改 `/files/read` 无法控制 OpenWebUI 直接上传的附件、历史消息、远程 URL、第三方工具结果或 OpenWebUI 自行拼装的 data URI。以下接入必须在 OpenWebUI/供应商适配层完成，才形成完整保证。**

提供实际发送函数 `send_guarded_model_request`，在构建请求前规范化完整请求副本：

```python
from open_terminal.integrations import send_guarded_model_request

response = await send_guarded_model_request(
    provider_client,
    provider_url,
    final_payload,
    headers=existing_provider_headers,
    resolver=resolve_owned_image,
    stream=True,
)
# 按原有逻辑转发流，并在 finally 中 await response.aclose()。
```

`final_payload` 必须是最终 HTTP 请求体：所有附件、工具返回、历史消息和供应商格式转换都完成后调用，之后不可再追加消息。覆盖每一次工具循环、重试以及 Chat Completions、Responses 等发送路径；不要仅挂一个早期 inlet filter。helper 不修改调用者保存的原始历史消息。

网关支持 Chat `image_url`、Responses `input_image`、Anthropic `image/source`、MCP `image/data` 及 notebook MIME 数据包。嵌套 JSON 工具文本也递归检查；工具 Schema 中名为 `image` 的普通参数不被当成图片。结构化图片始终重新编码。纯文本嵌入的图片 data URI、未知图片类型及过长的疑似 base64 文本会被拒绝。

`resolve_owned_image(reference)` 是同步可信解析器，在线程中返回已验证图片原始字节；图片处理并发限制为每进程两项，避免阻塞异步请求循环。建议异步预取并验证归属后，用只读请求内映射实现同步 resolver，同时限制源文件字节数。OpenWebUI 文件标识须查验附件归属；远程 URL 只能通过显式受信任来源解析，不可直接 GET 模型提供的任意地址。默认没有 resolver 时，所有 URL、文件标识和未知来源拒绝，不联网、不静默放行。供应商已有图片 file_id 也必须解析成受限预览，不能直接透传原 file_id。

这层保障针对可识别的结构化图片和图片 data URI，不承诺从任意 shell 输出、加密文本、拆分字符串或供应商未知私有格式中识别所有伪装图片。对不支持的图片协议应增加适配或拒绝；不能直接绕过发送函数。旧模式的 browser/desktop 截图入口保留原协议，若下游把截图放入 LLM，仍必须经过最终发送函数。截图缩放后，坐标工具须按原尺寸比例换算，不能直接套用预览像素坐标。

## 旧数据迁移

关闭隔离开关时，旧单用户和用户级文件布局继续可用。不会自动复制、删除或重命名旧 `/home/user`、`/home/<os-user>` 数据。

迁移时先备份旧卷，按 OpenWebUI 的真实用户/对话记录确认归属。让已签名的目标对话首次请求以建立哈希工作区，再停止服务，由管理员将该对话的文件复制到目标目录，保留原目录作为备份，并保证服务 UID 可读写。一个旧用户目录无法推断哪些文件属于哪次对话，必须人工分配；不能将包含多用户私有文件的整个旧卷挂成共享运行时。验证迁移完成前不要移除旧卷。

## 本次验证与后续验收

本次仅进行相关源码静态审查、Python AST 语法解析、差异空白与 UTF-8 检查；未运行测试、服务、镜像构建或真实 OpenWebUI 调用。

部署环境仍需验收：两用户两对话互读失败、同用户切换与重启后原文件恢复、符号链接与并发替换无法看到其他工作区、上传下载哈希一致、超大图片返回受限预览且原图不变、坏图/未知 URL 被拒、notebook 返回受限预览、终端 WS 与流式输出正常，以及最终供应商请求中不存在超限结构化图片。这些是后续运行验收项，不代表本次已经执行。

## 修改文件清单

- 工作区网关、进程与身份：`open_terminal/workspaces.py`、`open_terminal/workspace_worker.py`、`open_terminal/workspace_context.py`。
- 文件及执行接入：`open_terminal/main.py`、`open_terminal/dependencies.py`、`open_terminal/utils/fs.py`、`open_terminal/utils/notebooks.py`。
- 图片与下游发送：`open_terminal/utils/image_context.py`、`open_terminal/integrations.py`。
- 配置：`open_terminal/env.py`、`Dockerfile`、`Dockerfile.slim`、`Dockerfile.alpine`、`Dockerfile.openshift`。
- 文档与定制登记：`README.md`、`AGENTS.md`、`docs/WORKSPACE_IMAGE_CONTEXT.md`。
