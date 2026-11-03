# 工作区隔离与模型图片接入

## 实现与适用范围

本改造新增可选的 `OPEN_TERMINAL_WORKSPACE_ISOLATION=true`。它使用同一镜像中的 Linux bubblewrap，为每个 `(user_id, chat_id)` 启动一个常驻 API 工作进程，不为对话创建容器或重复安装 Python、Node、FFmpeg 等依赖。

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

四种 Dockerfile 均安装 bubblewrap，并预建可写 `/workspaces`。镜像和目标平台没有经过本次构建或运行验证。容器运行策略必须允许 bubblewrap 创建 user/mount/pid/network namespace；Docker 的 seccomp、AppArmor 或 OpenShift restricted SCC 可能阻止这些操作。安装了 bubblewrap 不等于宿主允许执行。失败时启动或首个工作区请求会明确报错/503，不会退回共享目录；查看前台终端中的诊断。应按宿主策略开放必要的 namespace 能力，不要求全面开放容器特权。

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

路径以 Linux 的 `:` 分隔，必须是存在的真实绝对目录，不能包含工作区或私有状态。解释器与 Open Terminal 包必须安装在可见运行时中；内部使用 Python `-I` 启动，不依赖当前目录或用户 `PYTHONPATH`。推荐镜像中正常安装的 `/usr/local`，或安装了本项目的 `/opt` 虚拟环境。只挂载完整源码仓库可能暴露其中的私有配置，不应拿它替代干净的共享运行时。

## OpenWebUI 的身份接入

本仓库没有 OpenWebUI 数据库，不能独立核实某个 chat 的真实主人。服务端强制验证的是可信后端签发的归属声明，不接受模型工具参数自行选择工作区。

提供了可直接导入的接入函数：

```python
from open_terminal.integrations import workspace_headers

# user.id 来自已认证的 OpenWebUI 服务端会话。
# chat_id 来自当前服务端对话上下文；不要从模型生成的工具参数读取。
# owns_chat 必须实际查库并返回 bool；新对话应先持久化归属，再签发。
headers = await workspace_headers(
    terminal_api_key,
    user.id,
    chat_id,
    owns_chat=owns_chat,
)
response = await terminal_client.get(
    "/files/read",
    params={"path": "uploads/photo.png"},
    headers=headers,
)
```

`owns_chat(user_id, chat_id)` 可为同步或异步函数，必须严格返回 `True` 才签发。签名有效期最长 300 秒，覆盖协议版本、用户、对话和到期时间，使用现有 API Key 及独立域前缀做 HMAC-SHA256。每次工具调用、上传、原图下载、历史恢复都须重新在后端验证并生成这些请求头：

- `Authorization: Bearer <现有 API Key>`
- `X-User-Id`、`X-Session-Id`
- `X-Workspace-Context`、`X-Workspace-Signature`

后端必须覆盖来自客户端的同名请求头。全局 API Key、签名请求头和终端 WebSocket 首帧中的服务凭据都不能交给浏览器或模型。WebSocket 由可信后端代理，连接上附相同签名请求头，沿用 `{type: "auth", token: terminal_api_key, chat_id: ...}` 首帧；网关会替换成仅在该工作区有效的内部凭据。

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
