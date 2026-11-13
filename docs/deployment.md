# 部署

[返回 README](../README.md)

本 fork 保留完整、slim、alpine 和 openshift 四种镜像。默认 `docker build .` 仍构建完整环境；本轮不提供 agents 或 CUDA target。个人现有手动 CI 与 DockerHub 发布工作流保持不变，不引入上游发布工作流。

```bash
docker build -t open-terminal-ck .
docker build -f Dockerfile.slim -t open-terminal-ck:slim .
docker build -f Dockerfile.alpine -t open-terminal-ck:alpine .
docker build -f Dockerfile.openshift -t open-terminal-ck:openshift .
```

完整镜像包含 Python、Node.js、构建工具、浏览器、桌面、文档与数据处理工具，支持现有运行时安装变量；其余变体继续保持既有非 root 和精简部署限制，额外系统工具需在构建时安装。安装变量、Docker socket 和网络限制见 [README](../README.md)。

Notebook 是 Python 包的可选 `notebooks` extra。普通 `pip install .` 不安装它；需要 Notebook 时安装 `pip install '.[notebooks]'`。四种 Dockerfile 均显式安装 extra 保留原有能力，完整镜像还单独安装 Jupyter。未安装 Notebook 依赖时服务仍能启动，配置接口将其标为不可用。

完整镜像的普通单用户数据目录为 `/home/user`，传统多用户为 `/home`。启用工作区隔离时，持久化 `/workspaces`，保留 owner＋project 目录、v1 聊天兼容及已签名的真实 chat 会话身份；运行依赖继续只读挂载。项目删除保留删除标记，先停进程后清理文件。部署细节及 WebUI 接入见[工作区文档](WORKSPACE_IMAGE_CONTEXT.md)。

非隔离模式支持现有 managed 浏览器、桌面和 TCP/Cloudflare CLI。隔离工作区继续禁用浏览器与桌面；没有新增 Coding agents、IPC 或 personal 浏览器运行入口。`START_DIR` 只作为非隔离新会话默认值，不覆盖隔离工作区的持久化 cwd；不自动执行启动脚本。
