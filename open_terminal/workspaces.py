"""将完整终端 API 放入单工作区的 Linux 文件系统视图。"""

import asyncio
import contextlib
import logging
import math
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
    WORKSPACE_NETWORK, WORKSPACE_STARTUP_TIMEOUT,
)
from open_terminal.dependencies import matches_api_key
from open_terminal.workspace_context import verify_workspace_context

log = logging.getLogger(__name__)

_PUBLIC = {"/health", "/api/config", "/openapi.json", "/docs", "/docs/oauth2-redirect", "/redoc"}
_HOP = {b"host", b"connection", b"keep-alive", b"transfer-encoding", b"upgrade", b"authorization", b"proxy-authorization", b"x-workspace-context", b"x-workspace-signature", b"x-user-id", b"x-session-id", b"x-skill-scope", b"x-skill-scope-id"}
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


class WorkspaceStartupError(RuntimeError):
    def __init__(self, message, *, retryable=False, exit_code=None):
        super().__init__(message)
        self.retryable = retryable
        self.exit_code = exit_code


class WorkspaceDeletedError(RuntimeError):
    pass


def _remove_workspace(path):
    root = os.path.realpath(path)

    def onerror(function, failed_path, exc_info):
        error = exc_info[1]
        if isinstance(error, FileNotFoundError):
            return
        if not isinstance(error, PermissionError):
            raise error
        for target in (os.path.dirname(failed_path), failed_path):
            if target == os.path.dirname(root) or os.path.islink(target):
                continue
            if os.path.commonpath((root, os.path.realpath(target))) != root:
                raise error
            os.chmod(target, 0o700)
        if function in (os.open, os.scandir, os.listdir):
            shutil.rmtree(failed_path, onerror=onerror)
        else:
            function(failed_path)

    if os.path.lexists(path):
        if os.path.islink(path) or os.path.realpath(path) != path or os.path.ismount(path):
            raise RuntimeError("项目工作区路径无效，未删除文件")
        shutil.rmtree(path, onerror=onerror)


async def _stop_process(process):
    if process.returncode is None:
        with contextlib.suppress(ProcessLookupError):
            os.killpg(process.pid, signal.SIGTERM)
        try:
            await asyncio.wait_for(process.wait(), 5)
        except TimeoutError:
            with contextlib.suppress(ProcessLookupError):
                os.killpg(process.pid, signal.SIGKILL)
            await process.wait()


@dataclass
class WorkspaceWorker:
    process: asyncio.subprocess.Process
    runtime: str
    key: str
    client: httpx.AsyncClient
    active: int = 0
    last_used: float = 0
    draining: bool = False
    stderr_task: asyncio.Task | None = None
    stderr_tail: bytes = b""

    async def relay_stderr(self):
        while chunk := await self.process.stderr.read(4096):
            self.stderr_tail = (self.stderr_tail + chunk)[-8192:]
            with contextlib.suppress(OSError, ValueError):
                sys.stderr.write(chunk.decode("utf-8", errors="replace"))
                sys.stderr.flush()

    def startup_error(self, error):
        output = self.stderr_tail.decode("utf-8", errors="replace").lower()
        if "bwrap:" in output and "resource temporarily unavailable" in output and isinstance(error, WorkspaceStartupError):
            return error
        if "bwrap:" in output:
            return WorkspaceStartupError(
                "工作区沙箱初始化失败，请检查 namespace、seccomp/AppArmor、系统路径挂载及运行依赖；详见 Terminal 日志",
                exit_code=getattr(error, "exit_code", None),
            )
        if any(message in output for message in (
            "modulenotfounderror:", "importerror:", "permissionerror:", "filenotfounderror:",
        )):
            return WorkspaceStartupError(
                "工作区 Python 依赖或文件访问初始化失败，请查看 Terminal 日志",
                exit_code=getattr(error, "exit_code", None),
            )
        return error if isinstance(error, WorkspaceStartupError) else WorkspaceStartupError(
            str(error) or "工作区启动失败，请查看 Terminal 日志"
        )

    def startup_diagnostic(self):
        text = self.stderr_tail.decode("utf-8", errors="replace")
        for secret in (API_KEY, self.key):
            if secret:
                text = text.replace(secret, "[已隐藏]")
        return text[-2048:]

    @property
    def socket(self):
        return os.path.join(self.runtime, "worker.sock")

    async def close(self):
        await self.client.aclose()
        await _stop_process(self.process)
        if self.stderr_task is not None:
            await asyncio.wait({self.stderr_task}, timeout=0.5)
            if not self.stderr_task.done():
                self.stderr_task.cancel()
            await asyncio.gather(self.stderr_task, return_exceptions=True)
        shutil.rmtree(self.runtime, ignore_errors=True)


class WorkspaceManager:
    def __init__(self):
        if not API_KEY:
            raise RuntimeError("工作区隔离需要已配置的 API Key")
        if platform.system() != "Linux" or not shutil.which("bwrap"):
            raise RuntimeError("工作区隔离需要 Linux 和 bubblewrap；不会降级为共享目录")
        if WORKSPACE_NETWORK not in ("isolated", "shared") or WORKSPACE_MAX_WORKERS < 1:
            raise RuntimeError("工作区网络必须为 isolated/shared，进程上限必须大于零")
        if not math.isfinite(WORKSPACE_STARTUP_TIMEOUT) or WORKSPACE_STARTUP_TIMEOUT <= 0:
            raise RuntimeError("工作区启动等待时间必须是大于零的有限秒数")
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
        self.deleting = {}
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

    def _workspace(self, identity, *, allow_deleted=False):
        if not allow_deleted:
            self._check_deleted(identity)
        # 主进程只建立根下的哈希目录，不打开工作区内由命令控制的文件。
        user_hash, scope_hash = identity.directory_parts
        user_dir = self.root / user_hash
        user_dir.mkdir(mode=0o700, exist_ok=True)
        if user_dir.is_symlink() or user_dir.resolve() != user_dir:
            raise RuntimeError("用户工作目录无效")
        workspace = user_dir / scope_hash
        workspace.mkdir(mode=0o700, exist_ok=True)
        if workspace.is_symlink() or workspace.resolve() != workspace:
            raise RuntimeError("工作区目录无效")
        return str(workspace)

    def _deletion_marker(self, identity):
        user_hash, scope_hash = identity.directory_parts
        return self.root / user_hash / f".{scope_hash}.deleted"

    def _check_deleted(self, identity):
        if identity.scope_type == "project" and (
            identity.workspace_key in self.deleting or self._deletion_marker(identity).exists()
        ):
            raise WorkspaceDeletedError("项目工作区已删除或正在删除")

    async def delete_project(self, identity):
        if identity.scope_type != "project" or (identity.user_id != identity.owner_id and not identity.is_admin):
            raise ValueError("只能由项目所属账号或管理员删除项目工作区")
        workspace_key = identity.workspace_key
        async with self.lock:
            task = self.deleting.get(workspace_key)
            if task is None:
                workspace = self._workspace(identity, allow_deleted=True)
                marker = self._deletion_marker(identity)
                try:
                    marker.touch(mode=0o600, exist_ok=False)
                except FileExistsError:
                    if marker.is_symlink() or not marker.is_file():
                        raise RuntimeError("项目删除标记无效")
                starting = self.starting.get(workspace_key)
                if starting and not starting.done():
                    starting.cancel()
                worker = self.workers.get(workspace_key)
                if worker:
                    worker.draining = True
                task = asyncio.create_task(self._delete_workspace(workspace_key, workspace, starting, worker))
                self.deleting[workspace_key] = task
                task.add_done_callback(lambda done: self._finished_delete(workspace_key, done))
        await asyncio.shield(task)

    def _finished_delete(self, workspace_key, task):
        if self.deleting.get(workspace_key) is task:
            del self.deleting[workspace_key]
        if not task.cancelled():
            task.exception()

    async def _delete_workspace(self, workspace_key, workspace, starting, worker):
        if starting:
            results = await asyncio.gather(starting, return_exceptions=True)
            for result in results:
                if isinstance(result, BaseException) and not isinstance(result, (asyncio.CancelledError, WorkspaceStartupError, WorkspaceDeletedError)):
                    raise RuntimeError("项目启动进程清理失败，请重试删除") from result
        if worker:
            await worker.close()
            if self.workers.get(workspace_key) is worker:
                del self.workers[workspace_key]
        await asyncio.to_thread(_remove_workspace, workspace)

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
            if worker.active or worker.draining or identity in self.starting or identity in self.deleting:
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
        workspace_key = identity.workspace_key
        self._check_deleted(identity)
        worker = self.workers.get(workspace_key)
        if worker and not worker.draining and worker.process.returncode is None:
            worker.active += 1
            return worker
        async with self.lock:
            self._check_deleted(identity)
            worker = self.workers.get(workspace_key)
            if worker and not worker.draining and worker.process.returncode is None:
                worker.active += 1
                return worker
            for key, expired in list(self.workers.items()):
                if expired.process.returncode is not None and key not in self.deleting:
                    await expired.close()
                    del self.workers[key]
            task = self.starting.get(workspace_key)
            if task is None:
                if len(set(self.workers) | set(self.starting)) >= WORKSPACE_MAX_WORKERS and not await self._evict_idle():
                    raise RuntimeError("所有工作区均在使用，暂时无法分配新工作区；文件会保留")
                task = asyncio.create_task(self._start_with_retry(identity))
                self.starting[workspace_key] = task
                task.add_done_callback(lambda done: self._finished_start(workspace_key, done))
            self.waiters[workspace_key] = self.waiters.get(workspace_key, 0) + 1
        try:
            try:
                worker = await asyncio.shield(task)
            except asyncio.CancelledError:
                self._check_deleted(identity)
                raise
            self._check_deleted(identity)
            if worker.draining:
                raise WorkspaceDeletedError("项目工作区正在删除")
            worker.active += 1
            return worker
        finally:
            self.waiters[workspace_key] -= 1
            if not self.waiters[workspace_key]:
                del self.waiters[workspace_key]
                if task.done() and self.starting.get(workspace_key) is task:
                    del self.starting[workspace_key]

    async def _start_with_retry(self, identity):
        deadline = time.monotonic() + WORKSPACE_STARTUP_TIMEOUT
        first_error = None
        for attempt in range(2):
            try:
                return await self._start(identity, deadline)
            except WorkspaceStartupError as exc:
                if attempt:
                    raise WorkspaceStartupError(
                        f"工作区重试启动失败：{exc}；首次失败：{first_error}",
                        exit_code=exc.exit_code,
                    ) from exc
                if not exc.retryable or time.monotonic() + 0.5 >= deadline:
                    raise
                first_error = exc
                log.warning("工作区就绪前退出，完成清理后重试一次；exit_code=%s", exc.exit_code)
                await asyncio.sleep(0.5)

    async def _start(self, identity, deadline=None):
        deadline = deadline if deadline is not None else time.monotonic() + WORKSPACE_STARTUP_TIMEOUT
        process = client = worker = runtime = None
        try:
            workspace = self._workspace(identity)
            runtime = tempfile.mkdtemp(prefix="open-terminal-worker-")
            key = secrets.token_urlsafe(32)
            command = self._command(workspace, runtime)
            executable = sys.executable
            if os.path.basename(executable) == "python3-ot":
                executable = os.path.join(os.path.dirname(executable), "python3")
            command += [executable, "-I", "-m", "open_terminal.workspace_worker"]
            process = await asyncio.create_subprocess_exec(*command, stdin=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE, env={}, start_new_session=True)
            client = httpx.AsyncClient(transport=httpx.AsyncHTTPTransport(uds=os.path.join(runtime, "worker.sock")), base_url="http://workspace", timeout=None, trust_env=False)
            worker = WorkspaceWorker(process, runtime, key, client, last_used=time.monotonic())
            worker.stderr_task = asyncio.create_task(worker.relay_stderr())
            import json
            try:
                process.stdin.write(json.dumps(self._environment(workspace, key)).encode() + b"\n")
                await asyncio.wait_for(process.stdin.drain(), max(0.001, deadline - time.monotonic()))
            except (BrokenPipeError, ConnectionResetError):
                pass
            except TimeoutError as exc:
                raise WorkspaceStartupError("工作区初始化超时，请查看 Terminal 日志") from exc
            finally:
                process.stdin.close()
            while time.monotonic() < deadline:
                if process.returncode is not None:
                    raise WorkspaceStartupError(
                        f"工作区进程在就绪前退出（退出码 {process.returncode}），请查看 Terminal 日志",
                        retryable=process.returncode not in (-signal.SIGKILL, 128 + signal.SIGKILL),
                        exit_code=process.returncode,
                    )
                try:
                    response = await client.get("/health", timeout=min(0.25, max(0.001, deadline - time.monotonic())))
                    if response.status_code == 200:
                        async with self.lock:
                            self._check_deleted(identity)
                            self.workers[identity.workspace_key] = worker
                        return worker
                except httpx.TransportError:
                    pass
                await asyncio.sleep(min(0.1, max(0, deadline - time.monotonic())))
            raise WorkspaceStartupError("工作区启动超时，请检查启动日志或调整 OPEN_TERMINAL_WORKSPACE_STARTUP_TIMEOUT")
        except BaseException as exc:
            if worker:
                await worker.close()
            else:
                try:
                    if client:
                        await client.aclose()
                finally:
                    try:
                        if process:
                            await _stop_process(process)
                    finally:
                        if runtime:
                            shutil.rmtree(runtime, ignore_errors=True)
            if isinstance(exc, WorkspaceDeletedError):
                raise
            if worker and isinstance(exc, (OSError, RuntimeError)):
                error = worker.startup_error(exc)
                log.warning("工作区启动失败；exit_code=%s；error=%s；stderr=%s", error.exit_code, error, worker.startup_diagnostic())
                if error is exc:
                    raise
                raise error from exc
            if worker is None and isinstance(exc, OSError):
                log.warning("工作区进程创建失败；error=%s", exc)
                raise WorkspaceStartupError("工作区进程创建失败，请检查磁盘、运行依赖和 Terminal 日志") from exc
            raise

    async def close(self):
        tasks = list(self.starting.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        await asyncio.gather(*list(self.deleting.values()), return_exceptions=True)
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
        deletion_request = kind == "http" and scope["method"] == "DELETE" and scope["path"] == "/api/workspace"
        if identity.operation is not None or deletion_request:
            if not deletion_request or identity.operation != "delete":
                if kind == "websocket":
                    await send({"type": "websocket.close", "code": 1008, "reason": "工作区操作签名不匹配"})
                else:
                    await JSONResponse({"detail": "工作区操作签名不匹配"}, status_code=403)(scope, receive, send)
                return
            try:
                await self.manager.delete_project(identity)
            except (OSError, RuntimeError, ValueError):
                log.exception("项目工作区删除未完成")
                await JSONResponse({"detail": "工作区文件清理未完成，请检查 Terminal 日志后重试删除"}, status_code=503)(scope, receive, send)
                return
            await JSONResponse({"deleted": True, "project_id": identity.scope_id})(scope, receive, send)
            return
        try:
            worker = await self.manager.get(identity)
        except WorkspaceDeletedError as exc:
            if kind == "websocket":
                await send({"type": "websocket.close", "code": 1008, "reason": str(exc)})
            else:
                await JSONResponse({"detail": str(exc)}, status_code=410)(scope, receive, send)
            return
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
        forwarded += [(b"x-skill-scope", identity.scope_type.encode("ascii")), (b"x-skill-scope-id", (identity.scope_id or identity.chat_id).encode("latin-1"))]
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
