# 上游同步记录

- 上游仓库：`https://github.com/open-webui/open-terminal.git`（仅临时拉取，不配置常驻远端）
- 个人仓库：`https://github.com/someone97421/open-terminal-ck.git`（`origin`，唯一推送目标）
- 本分支：`main`；同步流程见 [AGENTS.md](../AGENTS.md)。

## 当前状态

| 项目 | 完整哈希 | 说明 |
|---|---|---|
| 最后正式合入 | `505fa814919cf8ceecc34d3fde81096b46d39d2a` | 上游 0.17.22，真实合并已完成 |
| 最后核对目标 | `505fa814919cf8ceecc34d3fde81096b46d39d2a` | 与本轮正式合入目标一致 |
| 本轮合并前 HEAD | `a53184f5d3bf45c3d2c18b411edb0bdb89daf29a` | 干净工作区开始；上游独有 43 个提交，本地独有 15 个提交 |
| 本轮合并基点 | `32a5442cec89b20b25a72876c36daa10d0d3e09a` | 以完整目标执行 `git merge --no-ff --no-commit` |

2026-11-23 创建真实合并节点 `33d1711e7fc89f4c74f898cd5625fd119f14bae6`。已用 `git rev-list --parents -n 1` 核验双父节点为合并前 HEAD 与上游目标，并以 `git merge-base --is-ancestor 505fa814919cf8ceecc34d3fde81096b46d39d2a 33d1711e7fc89f4c74f898cd5625fd119f14bae6` 核验目标祖先关系通过。

上一轮（0.17.8）合并节点为 `26a90bffd617866847542b08e93db90422229cc7`，已核验双父节点与目标祖先关系。

## 0.17.22 吸收与适配

- 文件编辑差异：`eb0e1c664faaa7dd7fd0e1c8f93c1422e8b74042`、`0d9a77b1e494bece96ebbb724a19421a7c240a86`，`main.py`、`utils/fs.py`；`write_file` 返回 `created` 与 `patch`，`replace_file_content` 返回 `patch`，供 WebUI 0.13.4 展示增删行。沿用本地统一会话 cwd 解析。
- 多用户上传权限：同批提交，`utils/fs.py` 的 `_chown` 改为 `chmod g+rw`；本地原子上传与权限恢复逻辑不变。
- 共享文件监听：`31a36f6c0b871f5b612e651c4efd4d27761906fd` 起，`utils/events.py`；同路径客户端共用一个独立线程 watcher，不再占用文件请求的工作线程池。
- 桌面模块重构：`7705f1c5c52c086ca3bfdb65a3042d914ee74543`、`cd57047fa263081d357fffe5d0fb8411a198f2ab` 等，`desktop.py`、`desktop_native.py` 迁入 `open_terminal/desktop/`（router、native、portal、wayland、vnc、audio），`env.py` 新增 `DESKTOP_MODE`/`DESKTOP_ENVIRONMENT`，CLI 新增 `--desktop-mode`、`--desktop-environment`，`pyproject.toml` 依赖随之调整。本地 `desktop_key` 迁入 `desktop/router.py`：host 模式固定为 `("host", home)`，其余仍为 workspace_root 或用户＋home；`main.py` 的 execute 改用 `desktop_key` 与 `command_env`。隔离 worker 仍强制关闭桌面和浏览器。
- PTY 多用户环境：`utils/runner.py`，`sudo -u` 后用 `env` 显式传入调用方覆盖变量（仅 `request.env` 与桌面 `command_env`，不含完整进程环境）。
- `install-deps` 命令：`6fb0298d8cca5814fb043aa97b25ee5423afa0c6` 起，`cli.py`、`utils/packages.py`；仅手动执行，不影响服务运行。
- `open-machine` 别名包：`31e77f21bb264a856cbcd46cb14e9b2c4f4e1662`，`packages/open-machine/`；独立目录，不参与本仓库主包构建。
- 版本号：`pyproject.toml` 升至 0.17.22；CHANGELOG 仍归档于 `docs/archive/`。
- 文档：`docs/configuration.md` 引入上游 Desktop 章节，保留本地浏览器章节；README 冲突保留本地版本，补充桌面说明。

## 0.17.8 吸收与适配

- Ctrl+C 控制终端修复：`416f4e5e995e82dee5d44124173d4ab17345a96b`，`open_terminal/main.py`；保留真实 chat 归属、断线有界输出历史与可选流控、后台读取和清理，Linux PTY 在 setsid 后设置控制终端。
- Chromium 命令启动修复：`1351032bcf048a94154ead5a0ad9a9244c847444`，`open_terminal/env.py`；脱敏后的命令环境显式保留 no-sandbox 设置，沿用完整镜像原有 Chromium launcher。
- IPv6 出站规则：`f44200d581a0bf19da33a6c0c04040e7965ce3bf`，`entrypoint.sh`、`entrypoint-slim.sh`；IPv4 白名单模式下 IPv6 仅放行回环与已建立连接。
- Python 基础镜像：`33e78a8a439f480cdcd78ea56dc24555cebdc863`，四种现有 `Dockerfile*` 升至 3.12.16；完整镜像仍为默认构建结果，保留个人现有手动 CI/DockerHub 配置，删除冲突遗留的上游 `.github/workflows/docker.yml`。
- 文件监听优化 `open_terminal/utils/events.py`，仓库内递归、仓库外非递归；系统信息与前台进程 `utils/system.py`、`main.py`，增加 `psutil>=7.2.2`；Git issue、PR 搜索及计数接口 `utils/git.py`、`main.py`，完整镜像补齐 `gh`、`uv`；上传临时文件原子替换、Windows 绝对文件路径兼容、桌面帧流控及默认壁纸。
- 桌面命令环境：`b5f426a964c09753622c15ed7c309066649f8b3b`，`main.py`、`desktop.py`；复用统一桌面键，隔离场景使用固定 workspace_root（已由 owner＋project 调度），普通模式沿用用户＋home。隔离 worker 仍禁用桌面和浏览器，不新增直接 spawn 入口。
- START_DIR：`47fbd8f0f6ee632dc91500ece877a40b64014941`，`main.py`、`env.py`、`utils/setup.py`；仅非隔离模式使用 home 内已存在的默认目录，显式 cwd 和会话 cwd 优先；隔离模式持续使用 `fs.load_cwd()`，不传入新启动脚本或覆盖隔离依赖。
- Notebook extra：`2059b65ee08dd1b12435f4c8999f41c9033e1437`，`pyproject.toml`、`env.py`；普通 pip 安装默认不包含 nbclient/ipykernel，缺依赖时禁用 Notebook。四种现有 Dockerfile 显式安装 `.[notebooks]` 保留现有能力；完整镜像原先还单独安装 Jupyter，不能推断 optional extra 必然导致镜像失效。安装与变体边界已写入 README、部署和配置文档。
- 本地工作区 v1/v2、owner==user 或已验签 admin、真实 chat 会话身份、图片所有读图/最终发送校验与来源 metadata、Skill 文件包、只读依赖和项目删除标记/先停进程后清理语义均保留；相关核心模块没有回退到上游实现。
- 最终静态审查修复：原子上传临时文件按 umask 创建并在覆盖前恢复原权限、设置文件归属；失效 cwd 在非隔离模式回退 START_DIR；壁纸转换前显式创建 Xfce 背景目录。

## 暂缓 / 不采用

**本次已提交真实合并，下列暂缓代码的来源同样成为祖先。后续必须根据来源哈希与路径人工复查和适配，Git 不会自动重新引入这些被主动排除的代码。**

| 来源完整哈希 | 路径与范围 | 原因及复查条件 |
|---|---|---|
| `3d6c670ba75f365010041b79f1881215806f28ac`（agents 初始入口）、目标 `32a5442cec89b20b25a72876c36daa10d0d3e09a`（该目录最终状态） | `open_terminal/agents/` 全目录；`main.py` imports、router、maintenance、shutdown、能力声明 | 新 router/sessions 直接启动子进程，不满足 workspace_worker 调度与签名/隔离边界；不引入代码或注册入口。待所有 agent 进程、凭据、续聊、附件、取消和删除清理均纳入 owner＋project worker 后复查 |
| `7f2c306cd50f1eb3b4c3b92e9670db8c00e7338d` | `Dockerfile` agents target、`.github/workflows/docker.yml` agents 发布配置、部署文档 | 本轮不扩展镜像和 agents 运行方式；待 agents 工作区适配完成且用户授权后复查，个人当前 CI 保留 |
| `35282e54d9df1bb0d8749c859d25bae6cc97dc74` | `open_terminal/ipc.py`、`cli.py`、`config.py` 的 IPC/isolated 新选项 | 保留现有 TCP/Cloudflare CLI，不扩展传输；待明确桌面客户端需求与签名网关适配后复查 |
| `78626527331e0b9c4ad148bda361573cc78ca9f5`、`710b209fec7bf61965de9f82a935910877458f7e` | `open_terminal/browser/chrome.py`、`browser/router.py`、`env.py` personal 模式；同批浏览器捕获/断开重构 | 保留当前 managed 浏览器实现，不引入连接个人浏览器与已有登录态的新方式；待单独评估与授权后复查。同批浏览器重构暂随旧实现保留，不影响已吸收的命令启动修复 |
| `4e8eec95d5a6211312024b5d73a4d01c95b86e42` | `Dockerfile` CUDA target、`.github/workflows/docker.yml` CUDA 发布与部署说明 | 本轮不扩展运行方式和 GPU 依赖；待实际 GPU 部署需求与镜像兼容性核对后复查 |
| `47fbd8f0f6ee632dc91500ece877a40b64014941` | `cli.py` 启动 setup、`env.py` SETUP_SCRIPT、`utils/setup.py` 脚本执行/状态、`main.py` setup 状态回传 | START_DIR 已做有限适配；启动脚本会扩大原 CLI 的进程与目录写入行为，暂不接入。待明确生命周期与每个隔离 worker 的初始化语义后复查 |
| `15c1116ee65c8bd12d35a28565d673f01bddfd8b` | `utils/git.py` discard 新增 clean/rm、重置暂存区行为 | 本轮聚焦 Git issue/搜索接口，保留原 discard 仅恢复已跟踪工作树文件的语义；待单独确认未跟踪文件删除与暂存改动处理后复查 |
| `7d9ed6bae33b83c822143c0e994d7d8552580193` 等 0.17.9–0.17.21 agents 提交、目标 `505fa814919cf8ceecc34d3fde81096b46d39d2a` | `open_terminal/agents/` 全目录（新增 hermes、antigravity、copilot、goose、qwen，ACP 自定义代理，思考档位，对话分叉），README 编码代理章节 | 沿用 agents 暂缓结论：本轮删除冲突留下的 agents 文件，不注册入口；WebUI 端编码代理同样暂缓。复查条件同上 |
| `5400f71570dc0cfaa62837c84cb6ddf9e84033ed` | `open_terminal/bridge.py`、`cli.py` 的 `bridge` 命令，README IPC/SSH 说明 | 仅用于连接 `--ipc` 服务，IPC 已暂缓；随 IPC 一起复查 |

归档上游 CHANGELOG 仅作历史资料，不代表其中 agents、IPC、personal、CUDA、setup 等功能已在当前 fork 提供。README 保留个人部署入口；新增部署/配置文档已按本次实际功能适配，隧道文档使用当前源码目录的 `uv run`。

## 验证与限制

0.17.22：静态审查全部冲突与自动合并差异，重点核对工作区隔离、桌面键、会话 cwd 与 runner 环境传递；执行暂存区 `git diff --check`、Python AST 解析（3.11 语法）、冲突标记与 BOM 扫描，ruff 未定义名称/未使用导入检查通过。未执行应用代码、测试、构建、服务、依赖安装或 CI；新增桌面后端、音频、`install-deps` 与 `patch` 返回的运行时行为尚未验证。

0.17.8：仅静态审查冲突、自动合并差异及直接相关的工作区调度、身份、图片网关、Skill 和会话归属代码，执行工作树与暂存区 `git diff --check`，对本次变更的 Python 做 AST 语法解析并扫描文本编码/BOM、冲突标记。未执行应用代码、测试、构建、服务、依赖安装或 CI；运行时、依赖兼容与镜像行为尚未验证，不将静态检查描述为功能通过。

## 同步历史

| 日期 | 目标分支 | 目标完整哈希 | 合并节点 | 结果 |
|---|---|---|---|---|
| 2026-11-03 | `main` | `758db1a0f2f5967af39641d418cd76da1db399f0` | 无（基点即最新） | 初始化记录 |
| 2026-11-14 | `main` | `32a5442cec89b20b25a72876c36daa10d0d3e09a` | `26a90bffd617866847542b08e93db90422229cc7` | 非快进真实合并完成；双父节点与目标祖先关系核验通过 |
| 2026-11-23 | `main` | `505fa814919cf8ceecc34d3fde81096b46d39d2a` | `33d1711e7fc89f4c74f898cd5625fd119f14bae6` | 非快进真实合并完成；双父节点与目标祖先关系核验通过 |
