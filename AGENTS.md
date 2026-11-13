# 智能体规则

## 1. 仓库定位

- 本仓库是 `open-webui/open-terminal` 的个人自用魔改版，仅供自用与深度定制。
- 不向上游做任何贡献，不受上游社区规范、协作流程与发布政策的约束。
- 上游的历史文档（安全策略、变更日志等）已归档至 `docs/archive/`，仅作查阅用，无需遵守。
- 上游同步状态记录在 `docs/UPSTREAM_SYNC.md`。

## 2. 核心底线

- **隔离上游**：不向上游发起 Issue、PR、评论或推送；同步只通过 URL 临时拉取，不保留常驻上游远端，只推送到个人 origin。
- **安全保密**：密钥、令牌、凭据及私人数据严禁入库，提交前必须审查代码差异。
- **数据安全**：修改持久化结构或数据格式时，必须向下兼容或给出明确的迁移方案。
- **自用优先**：以个人使用便利为最高优先级，不引入繁琐的工程限制。
- **规范提交**：提交信息统一使用中文，采用清晰的语义化格式，拒绝笼统描述。
- **诚实汇报**：未执行的测试或构建直接说明未跑，严禁伪造验证结果。
- **破坏性改动保护**：删除现有可用功能、修改公共接口或变更底层数据格式时，必须在报告中重点高亮提示。

## 3. 开发自由度

- 免除设计提案、架构记录与变更清单流程。
- 无需新建分支或工作树，直接在主分支开发。
- 默认仅静态审查与 `git diff --check`；测试、构建、安装及服务按用户明确授权执行。
- 未经明确要求，不得触发 CI 工作流（含手动 dispatch 与 re-run），也不得执行 Docker 镜像构建。

## 4. 规则维护原则

更新本文件时，优先通过删减、合并与提炼通用原则来表达意图，避免堆砌碎片化的场景禁令。

## 5. 本地定制保留项

后续魔改在此登记，同步上游时逐项对照保护。

| 定制项 | 位置 | 保留原因 | 同步上游时的防冲突要点 |
|---|---|---|---|
| 智能体规则 | `AGENTS.md` | 个人规则唯一入口 | 上游若自带同类规则文件，只提炼有价值内容并入本文件，然后删除上游文件 |
| 历史文档归档 | `docs/archive/` | 保留社区文档的查阅价值 | 上游更新 `SECURITY.md` / `CHANGELOG.md` 时不要挪回根目录 |
| 同步追踪记录 | `docs/UPSTREAM_SYNC.md` | 增量同步与暂缓项依据 | 与上游同名文件冲突时以本地记录为准 |
| 个人流水线与分发 | `.github/workflows/ci.yml`、`README.md`、`pyproject.toml` | 保留个人现有手动检查、DockerHub 发布和本地构建 | 不引入已删除的上游发布工作流，不向上游写入；未经授权不触发任何 CI |
| 对话与项目工作区隔离 | `open_terminal/workspaces.py`、`workspace_worker.py`、`workspace_context.py`、`dependencies.py`、`utils/fs.py`、`main.py` | 普通账号隔离、同一账号项目多聊天共享文件、管理员跨账号管理、只读依赖共用 | 保留 v1 目录兼容、v2 owner＋project 调度；普通用户要求 user==owner，管理员凭已验签的 admin:true 保留跨账号管理权，目录仍按实际 owner 选择；保留真实 chat 会话身份及 WebUI 按次签名；项目删除需所属账号或管理员的 delete 操作签名，先停进程再清理目录，并保留删除标记阻止旧请求重建；保留整工作进程沙箱与文件接口统一会话 cwd，不退回仅 cwd 隔离 |
| 模型图片网关 | `open_terminal/utils/image_context.py`、`integrations.py`、`main.py`、`utils/notebooks.py`、`docs/WORKSPACE_IMAGE_CONTEXT.md` | 大图受限预览、原图保留与 OpenWebUI 接入 | 保留所有读图及最终发送校验、图片来源元数据和分类错误，不恢复模型直接读取原图的路径 |
| 运行入口与可选依赖 | `Dockerfile*`、`pyproject.toml`、`env.py`、`main.py` | 保留 managed 浏览器、原有 CLI、完整默认镜像和 Notebook 能力 | 四种现有镜像显式安装 notebooks extra；START_DIR 不覆盖隔离 cwd，桌面不绕过工作区网关；agents、IPC、personal Chrome、CUDA 暂缓项按同步记录人工复查 |

## 6. 上游同步固定流程

收到“同步上游并保留本地定制”指令时，按以下顺序执行：

1. 查阅本文件与 `docs/UPSTREAM_SYNC.md`，确认工作区干净，不覆盖未提交的本地修改。
2. 直接通过上游地址 `https://github.com/open-webui/open-terminal.git` 临时拉取目标分支最新代码，记录目标提交哈希，不添加常驻远端。
3. 检查合并基点。未合并时采用保留双父节点的非快进合并（`--no-ff`），零散修复可用拣选（`cherry-pick`）。严禁压缩合并或强行覆盖伪造合并历史。
4. 逐项审查冲突与自动合并结果，无条件保护本地定制项。对上游新增功能先评估完整度与兼容性：半成品功能暂缓引入并记录原因；成熟且优于本地同类实现的功能，采用上游方案并做本地化适配，同时清理本地冗余代码。
5. 提交前完整审查代码差异，默认跳过测试与构建。
6. 更新 `docs/UPSTREAM_SYNC.md`，记录完整目标、基点、吸收项和暂缓项来源哈希及路径；提交前不得虚填合并节点，提交后补录并核验双父节点及目标祖先关系。
7. 下次按最后正式合入节点核对增量；已真实合并但暂缓的代码也在祖先中，必须按来源人工复查，不能依赖 Git 自动重引入。

## 7. 环境速查

- 安装：`uv sync`（Notebook 使用 `--extra notebooks`，MCP 使用 `--extra mcp`，可组合）
- 开发：`uv run uvicorn open_terminal.main:app --reload`（等价 `dev.sh`）
- 运行：`uv run open-terminal run --host 0.0.0.0 --port 8000 --api-key <key>`
- 构建：`uv build`；容器镜像 `docker build -t open-terminal-ck .`
