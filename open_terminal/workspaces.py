"""将完整终端 API 放入单工作区的 Linux 文件系统视图。"""

import asyncio
import contextlib
import os
from pathlib import Path
import platform
import secrets
import shutil
import signal
import sys
import tempfile
import time
from dataclasses import dataclass

import httpx
from starlette.datastructures import Headers
from starlette.responses import JSONResponse

from open_terminal.env import (
    API_KEY, WORKSPACE_ROOT, WORKSPACE_RUNTIME_PATHS, WORKSPACE_MAX_WORKERS,
    WORKSPACE_NETWORK,
)
from open_terminal.dependencies import matches_api_key
from open_terminal.workspace_context import verify_workspace_context

_PUBLIC = {"/health", "/api/config", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
_HOP = {b"host", b"connection", b"keep-alive", b"transfer-encoding", b"upgrade", b"authorization", b"proxy-authorization", b"x-workspace-context", b"x-workspace-signature", b"x-user-id", b"x-session-id"}
_RUNTIME = ("/usr", "/bin", "/sbin", "/lib", "/lib64")
_ETC = ("alternatives", "ssl/certs", "ssl/openssl.cnf", "ca-certificates", "fonts", "libreoffice", "ImageMagick-6", "ImageMagick-7", "texmf", "ld.so.cache", "ld.so.conf", "ld.so.conf.d", "nsswitch.conf", "passwd", "group", "hosts", "resolv.conf", "localtime", "timezone")
_SETTINGS = {
    "BINARY_MIME_PREFIXES": "BINARY_FILE_MIME_PREFIXES", "ENABLE_TERMINAL": "ENABLE_TERMINAL",
    "ENABLE_NOTEBOOKS": "ENABLE_NOTEBOOKS", "ENABLE_SYSTEM_PROMPT": "ENABLE_SYSTEM_PROMPT",
    "SYSTEM_PROMPT": "SYSTEM_PROMPT", "EXECUTE_DESCRIPTION": "EXECUTE_DESCRIPTION",
    "EXECUTE_TIMEOUT": "EXECUTE_TIMEOUT", "MAX_SESSIONS": "MAX_TERMINAL_SESSIONS", "TERM": "TERMINAL_TERM",
    "MAX_LOG_SIZE": "MAX_PROCESS_LOG_SIZE", "LOG_RETENTION": "PROCESS_LOG_RETENTION",
    "LOG_FLUSH_INTERVAL": "LOG_FLUSH_INTERVAL", "LOG_FLUSH_BUFFER": "LOG_FLUSH_BUFFER",
    "SESSION_CWD_TTL": "SESSION_CWD_TTL", "IMAGE_MAX_EDGE": "IMAGE_MAX_EDGE",
    "IMAGE_MAX_BYTES": "IMAGE_MAX_BYTES", "IMAGE_MAX_PIXELS": "IMAGE_MAX_PIXELS",
    "INFO": "OPEN_TERMINAL_INFO", "CORS_ALLOWED_ORIGINS": "CORS_ALLOWED_ORIGINS",
}


@dataclass
class WorkspaceWorker:
    process: asyncio.subprocess.Process
    runtime: str
    key: str
    client: httpx.AsyncClient
    active: int = 0
    last_used: float = 0
    draining: bool = False

    @property
    def socket(self):
        return os.path.join(self.runtime, "worker.sock")

    async def close(self):
        await self.client.aclose()
        if self.process.returncode is None:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(self.process.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(self.process.wait(), 5)
            except TimeoutError:
                with contextlib.suppress(ProcessLookupError):
                    os.killpg(self.process.pid, signal.SIGKILL)
                await self.process.wait()
        shutil.rmtree(self.runtime, ignore_errors=True)


class WorkspaceManager:
    def __init__(self):
        if not API_KEY:
            raise RuntimeError("工作区隔离需要已配置的 API Key")
        if platform.system() != "Linux" or not shutil.which("bwrap"):
            raise RuntimeError("工作区隔离需要 Linux 和 bubblewrap；不会降级为共享目录")
        if WORKSPACE_NETWORK not in ("isolated", "shared") or WORKSPACE_MAX_WORKERS < 1:
            raise RuntimeError("工作区网络必须为 isolated/shared，进程上限必须大于零")
        self.root = Path(WORKSPACE_ROOT)
        if not self.root.is_absolute() or self.root == Path("/") or any(self.root == Path(path) or Path(path) in self.root.parents for path in (*_RUNTIME, "/etc", "/run", "/proc", "/dev")):
            raise RuntimeError("工作区根目录必须是独立的绝对路径")
        self.root.mkdir(mode=0o700, parents=True, exist_ok=True)
        if self.root.is_symlink() or self.root.resolve() != self.root:
            raise RuntimeError("工作区根目录不可经过符号链接")
        self.workers = {}
        self.lock = asyncio.Lock()
        self.starting = {}
        self.waiters = {}
        self.runtime_paths = []
        for value in WORKSPACE_RUNTIME_PATHS:
            path = Path(value)
            if not path.is_absolute() or not path.is_dir() or path.resolve() != path:
                raise RuntimeError("共享依赖路径必须为存在且不经过符号链接的绝对目录")
            if path == Path("/") or path in self.root.parents or self.root == path or self.root in path.parents or path.parts[1] in ("home", "root", "tmp", "run", "proc", "dev", "etc", "var"):
                raise RuntimeError("共享依赖路径不可包含私有目录、工作区或服务状态")
            self.runtime_paths.append(str(path))
        visible = (*_RUNTIME, *self.runtime_paths)
        executable = os.path.join(os.path.dirname(sys.executable), "python3") if os.path.basename(sys.executable) == "python3-ot" else sys.executable
        for path in {executable, os.path.realpath(executable)}:
            if not any(path.startswith(root.rstrip("/") + "/") for root in visible):
                raise RuntimeError(f"工作区内不可见 Python 解释器 {path}；请将其所在虚拟环境或前缀加入 OPEN_TERMINAL_WORKSPACE_RUNTIME_PATHS")

    def _workspace(self, identity):
        # 主进程只建立根下的哈希目录，不打开工作区内由命令控制的文件。
        user_hash, chat_hash = identity.directory_parts
        user_dir = self.root / user_hash
        user_dir.mkdir(mode=0o700, exist_ok=True)
        if user_dir.is_symlink() or user_dir.resolve() != user_dir:
            raise RuntimeError("用户工作目录无效")
        workspace = user_dir / chat_hash
        workspace.mkdir(mode=0o700, exist_ok=True)
        if workspace.is_symlink() or workspace.resolve() != workspace:
            raise RuntimeError("对话工作目录无效")
        return str(workspace)

    def _command(self, workspace, runtime):
        command = [shutil.which("bwrap"), "--die-with-parent", "--new-session", "--unshare-all", "--cap-drop", "ALL", "--clearenv"]
        if WORKSPACE_NETWORK == "shared":
            command.append("--share-net")
        for path in (*_RUNTIME, *self.runtime_paths):
            if os.path.exists(path):
                command += ["--ro-bind", path, path]
        command += ["--dir", "/etc"]
        for name in _ETC:
            path = f"/etc/{name}"
            if os.path.exists(path):
                command += ["--ro-bind", path, path]
        command += ["--proc", "/proc", "--dev", "/dev", "--dir", "/dev/shm", "--tmpfs", "/tmp", "--tmpfs", "/run", "--bind", runtime, "/run/open-terminal", "--bind", workspace, workspace, "--chdir", workspace]
        return command

    def _environment(self, workspace, key):
        # 不继承主服务的认证、代理凭据、Python 注入或其他私有环境变量。
        path = ":".join([*(p + "/bin" for p in self.runtime_paths if os.path.isdir(p + "/bin")), "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"])
        values = {"PATH": path, "LANG": "C.UTF-8", "HOME": workspace, "SHELL": "/bin/bash", "TMPDIR": workspace + "/tmp", "XDG_CACHE_HOME": workspace + "/cache", "OPEN_TERMINAL_API_KEY": key, "OPEN_TERMINAL_WORKSPACE_ISOLATION": "false", "OPEN_TERMINAL_WORKSPACE_DIR": workspace, "OPEN_TERMINAL_MULTI_USER": "false", "OPEN_TERMINAL_ENABLE_BROWSER": "false", "OPEN_TERMINAL_ENABLE_DESKTOP": "false", "OPEN_TERMINAL_LOG_DIR": workspace + "/cache/logs", "OPEN_TERMINAL_FILE_BROWSER_ROOT": "home"}
        from open_terminal import env as settings
        for setting, attribute in _SETTINGS.items():
            value = getattr(settings, attribute)
            if value is not None:
                values["OPEN_TERMINAL_" + setting] = ",".join(value) if isinstance(value, list) else str(value)
        return values

    def release(self, worker):
        worker.active -= 1
        worker.last_used = time.monotonic()

    def _finished_start(self, identity, task):
        if not self.waiters.get(identity) and self.starting.get(identity) is task:
            self.starting.pop(identity, None)
        if not task.cancelled():
            task.exception()

    async def _evict_idle(self):
        candidates = sorted(self.workers.items(), key=lambda item: item[1].last_used)
        for identity, worker in candidates:
            if worker.active or identity in self.starting:
                continue
            try:
                response = await worker.client.get("/api/workspace/idle", headers={"Authorization": f"Bearer {worker.key}"}, timeout=1)
                state = response.json() if response.status_code == 200 else None
                idle = isinstance(state, dict) and state.get("idle") is True
            except (httpx.TransportError, ValueError):
                idle = False
            if idle and not worker.active:
                worker.draining = True
                await worker.close()
                del self.workers[identity]
                return True
        return False

    async def get(self, identity):
        worker = self.workers.get(identity)
        if worker and not worker.draining and worker.process.returncode is None:
            worker.active += 1
            return worker
        async with self.lock:
            worker = self.workers.get(identity)
            if worker and not worker.draining and worker.process.returncode is None:
                worker.active += 1
                return worker
            for key, expired in list(self.workers.items()):
                if expired.process.returncode is not None:
                    await expired.close()
                    del self.workers[key]
            task = self.starting.get(identity)
            if task is None:
                if len(set(self.workers) | set(self.starting)) >= WORKSPACE_MAX_WORKERS and not await self._evict_idle():
                    raise RuntimeError("所有工作区均在使用，暂时无法分配新工作区；文件会保留")
                task = asyncio.create_task(self._start(identity))
                self.starting[identity] = task
                task.add_done_callback(lambda done: self._finished_start(identity, done))
            self.waiters[identity] = self.waiters.get(identity, 0) + 1
        try:
            worker = await asyncio.shield(task)
            worker.active += 1
            return worker
        finally:
            self.waiters[identity] -= 1
            if not self.waiters[identity]:
                del self.waiters[identity]
                if task.done() and self.starting.get(identity) is task:
                    del self.starting[identity]

    async def _start(self, identity):
        workspace = self._workspace(identity)
        runtime = tempfile.mkdtemp(prefix="open-terminal-worker-")
        key = secrets.token_urlsafe(32)
        command = self._command(workspace, runtime)
        executable = sys.executable
        if os.path.basename(executable) == "python3-ot":
            executable = os.path.join(os.path.dirname(executable), "python3")
        command += [executable, "-I", "-m", "open_terminal.workspace_worker"]
        worker = None
        try:
            process = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE, env={}, start_new_session=True)
            client = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=os.path.join(runtime, "worker.sock")), base_url="http://workspace", timeout=None, trust_env=False)
            worker = WorkspaceWorker(process, runtime, key, client, last_used=time.monotonic())
            import json
            process.stdin.write(json.dumps(self._environment(workspace, key)).encode() + b"\n")
            await process.stdin.drain()
            process.stdin.close()
            for _ in range(100):
                if process.returncode is not None:
                    raise RuntimeError("工作区进程退出，请检查 namespace 权限及共享 Python 依赖路径")
                try:
                    response = await client.get("/health", timeout=0.25)
                    if response.status_code == 200:
                        self.workers[identity] = worker
                        return worker
                except httpx.TransportError:
                    pass
                await asyncio.sleep(0.1)
            raise RuntimeError("工作区启动超时，请检查 bubblewrap 和运行依赖")
        except BaseException:
            if worker:
                await worker.close()
            else:
                shutil.rmtree(runtime, ignore_errors=True)
            raise

    async def close(self):
        tasks = list(self.starting.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*(worker.close() for worker in self.workers.values()))
        self.workers.clear()


class WorkspaceGateway:
    """主服务仅转发；文件内容和原始路径均留在工作区进程内处理。"""

    def __init__(self, app):
        self.app = app
        self.manager = WorkspaceManager()

    async def __call__(self, scope, receive, send):
        kind = scope["type"]
        if kind == "lifespan":
            try:
                await self.app(scope, receive, send)
            finally:
                await self.manager.close()
            return
        if kind not in ("http", "websocket") or (kind == "http" and scope["path"] in _PUBLIC):
            await self.app(scope, receive, send)
            return
        headers = Headers(scope=scope)
        authorization = headers.get("authorization", "")
        try:
            scheme, _, token = authorization.partition(" ")
            if scheme.lower() != "bearer" or not matches_api_key(token):
                raise ValueError("Invalid API key")
            identity = verify_workspace_context(API_KEY, headers)
        except ValueError as exc:
            if kind == "websocket":
                await send({"type": "websocket.close", "code": 1008, "reason": "需要可信后端的工作区身份"})
            else:
                await JSONResponse({"detail": str(exc)}, status_code=401)(scope, receive, send)
            return
        try:
            worker = await self.manager.get(identity)
        except (OSError, RuntimeError) as exc:
            if kind == "websocket":
                await send({"type": "websocket.close", "code": 1013, "reason": "工作区不可用"})
            else:
                await JSONResponse({"detail": str(exc)}, status_code=503)(scope, receive, send)
            return
        try:
            await self._forward(worker, identity, scope, receive, send)
        finally:
            self.manager.release(worker)

    async def _forward(self, worker, identity, scope, receive, send):
        kind = scope["type"]
        forwarded = [(k, v) for k, v in scope["headers"] if k.lower() not in _HOP]
        forwarded += [(b"authorization", f"Bearer {worker.key}".encode()), (b"x-user-id", identity.user_id.encode("latin-1")), (b"x-session-id", identity.chat_id.encode("latin-1"))]
        raw_path = scope.get("raw_path", scope["path"].encode())
        target = raw_path.decode("ascii") + ("?" + scope["query_string"].decode("ascii") if scope["query_string"] else "")
        if kind == "websocket":
            await self._websocket(worker, identity, target, forwarded, scope, receive, send)
            return

        async def body():
            while True:
                message = await receive()
                if message["type"] == "http.disconnect":
                    raise ClientDisconnected
                if message.get("body"):
                    yield message["body"]
                if not message.get("more_body", False):
                    return

        response = None
        started = False
        try:
            request = worker.client.build_request(scope["method"], target, headers=forwarded, content=body())
            response = await worker.client.send(request, stream=True)
            await send({"type": "http.response.start", "status": response.status_code, "headers": [(k, v) for k, v in response.headers.raw if k.lower() not in {b"transfer-encoding", b"connection"}]})
            started = True
            async def stream_body():
                async for chunk in response.aiter_raw():
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
                await send({"type": "http.response.body", "body": b""})

            async def disconnected():
                while (await receive())["type"] != "http.disconnect":
                    pass

            tasks = [asyncio.create_task(stream_body()), asyncio.create_task(disconnected())]
            try:
                done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                for task in done:
                    task.result()
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)
        except ClientDisconnected:
            return
        except httpx.TransportError:
            if started:
                raise
            await JSONResponse({"detail": "工作区连接中断"}, status_code=502)(scope, receive, send)
        finally:
            if response:
                await response.aclose()

    async def _websocket(self, worker, identity, target, headers, scope, receive, send):
        import json
        from websockets.asyncio.client import unix_connect
        from websockets.exceptions import ConnectionClosed

        # 浏览器须经可信后端转发；禁止把全局 API Key/签名发给浏览器。
        headers = [(k.decode("latin-1"), v.decode("latin-1")) for k, v in headers if not k.lower().startswith(b"sec-websocket-")]
        try:
            async with unix_connect(worker.socket, uri="ws://workspace" + target, additional_headers=headers, max_size=None) as remote:
                message = await receive()
                if message["type"] != "websocket.connect":
                    return
                await send({"type": "websocket.accept"})

                async def upstream():
                    first = True
                    while True:
                        message = await receive()
                        if message["type"] == "websocket.disconnect":
                            await remote.close()
                            return
                        if first:
                            first = False
                            try:
                                payload = json.loads(message.get("text") or "")
                            except (ValueError, TypeError):
                                payload = None
                            if not isinstance(payload, dict) or payload.get("type") != "auth" or not matches_api_key(payload.get("token")):
                                await remote.close(code=4001)
                                return
                            payload.update(token=worker.key, user_id=identity.user_id, chat_id=identity.chat_id)
                            await remote.send(json.dumps(payload))
                        else:
                            value = message.get("text") if message.get("text") is not None else message.get("bytes", b"")
                            if isinstance(value, str):
                                try:
                                    payload = json.loads(value)
                                except (ValueError, TypeError):
                                    payload = None
                                if isinstance(payload, dict) and payload.get("type") == "chat":
                                    payload["chat_id"] = identity.chat_id
                                    value = json.dumps(payload)
                            await remote.send(value)

                async def downstream():
                    try:
                        async for value in remote:
                            await send({"type": "websocket.send", **({"text": value} if isinstance(value, str) else {"bytes": value})})
                    except ConnectionClosed:
                        return

                tasks = [asyncio.create_task(upstream()), asyncio.create_task(downstream())]
                try:
                    done, _ = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
                    for task in done:
                        task.result()
                finally:
                    for task in tasks:
                        task.cancel()
                    await asyncio.gather(*tasks, return_exceptions=True)
                    with contextlib.suppress(OSError, RuntimeError):
                        code = remote.close_code or 1000
                        await send({"type": "websocket.close", "code": 1011 if code in (1005, 1006, 1015) else code, "reason": remote.close_reason or ""})
        except (OSError, ConnectionClosed, ValueError):
            with contextlib.suppress(OSError, RuntimeError):
                await send({"type": "websocket.close", "code": 1011})


class ClientDisconnected(Exception):
    pass
