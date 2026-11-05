"""完整 Skill 文件包；所有文件操作沿用当前已验签工作区的 UserFS。"""

import asyncio
import base64
import hashlib
import json
import os
import stat
import uuid
from contextlib import asynccontextmanager
from pathlib import PurePosixPath

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import BaseModel, Field

from open_terminal.dependencies import get_filesystem, verify_api_key
from open_terminal.utils.fs import UserFS
from open_terminal.utils.skills import parse_skill_frontmatter

ROOTS = (".agents/skills", ".cptr/skills", ".claude/skills", ".codex/skills")
POLICY_FILE = ".openwebui-skill.json"
MAX_FILE_BYTES = 10 * 1024 * 1024
MAX_PACKAGE_BYTES = 50 * 1024 * 1024
MAX_FILES = 1000
router = APIRouter(dependencies=[Depends(verify_api_key)])
_locks: dict[str, tuple[asyncio.Lock, int]] = {}


class Modes(BaseModel):
    chat: bool = True
    assistant: bool = True
    project: bool = True


class Meta(BaseModel):
    i18n: dict[str, dict[str, str]] | None = None
    tags: list[str] | None = Field(default_factory=list)
    modes: Modes = Field(default_factory=Modes)
    requires: list[str] = Field(default_factory=list)


class PackagePolicy(BaseModel):
    name: str
    description: str | None = None
    meta: Meta = Field(default_factory=Meta)
    is_active: bool = True


class PackageWrite(PackagePolicy):
    files: list[dict]
    expected_version_id: str | None = None


@asynccontextmanager
async def package_lock(path):
    lock, users = _locks.get(path, (asyncio.Lock(), 0))
    _locks[path] = (lock, users + 1)
    try:
        async with lock:
            yield
    finally:
        _, users = _locks[path]
        if users == 1:
            del _locks[path]
        else:
            _locks[path] = (lock, users - 1)


def relative_path(path):
    if not isinstance(path, str) or not path or "\\" in path or ":" in path or "\0" in path:
        raise HTTPException(400, "文件路径必须是包内相对路径")
    if path.startswith("/") or any(part in ("", ".", "..") for part in path.split("/")):
        raise HTTPException(400, "文件路径无效")
    return path


def package_path(fs, package_id):
    relative_path(package_id)
    if not any(package_id.startswith(root + "/") and "/" not in package_id[len(root) + 1:] for root in ROOTS):
        raise HTTPException(400, "Skill 包必须位于受支持的 Skill 根目录内")
    if PurePosixPath(package_id).name.startswith("."):
        raise HTTPException(400, "Skill 文件夹名称不能以点开头")
    target = fs.resolve_path(package_id)
    # 禁止链接让目录身份随外部路径变化，普通文件工具仍保留原权限。
    current = fs.home
    for part in package_id.split("/"):
        current = os.path.join(current, part)
        if os.path.islink(current):
            raise HTTPException(400, "Skill 包路径不能包含符号链接")
    return target


def scope_context(request, fs):
    if fs.workspace_root:
        scope = request.headers.get("x-skill-scope", "chat")
        scope_id = request.headers.get("x-skill-scope-id") or request.headers.get("x-session-id", "")
        if scope not in ("chat", "project") or not scope_id:
            raise HTTPException(403, "Skill 工作区身份不可用")
        return {"scope": scope, "scope_id": scope_id}
    return {"scope": "global", "scope_id": ""}


def fingerprint(files, policy):
    digest = hashlib.sha256()
    digest.update(json.dumps(policy, sort_keys=True, ensure_ascii=False, separators=(",", ":")).encode())
    for file in sorted(files, key=lambda file: file["path"]):
        digest.update(b"\0" + file["path"].encode() + b"\0")
        digest.update(hashlib.sha256(decode_file(file)).digest())
    return digest.hexdigest()


def decode_file(file):
    try:
        if file.get("encoding") not in (None, "base64"):
            raise ValueError()
        return base64.b64decode(file["content"], validate=True) if file.get("encoding") else file["content"].encode("utf-8")
    except (ValueError, TypeError, KeyError, AttributeError) as error:
        raise HTTPException(400, "Skill 文件编码无效") from error


def validate_files(files):
    if not files or len(files) > MAX_FILES:
        raise HTTPException(400, "Skill 包需要 SKILL.md，最多 1000 个文件")
    paths, total = set(), 0
    for file in files:
        path = relative_path(file.get("path"))
        if path == POLICY_FILE or path.startswith(POLICY_FILE + '/') or path in paths:
            raise HTTPException(400, "Skill 文件路径重复或使用保留的元数据文件名")
        paths.add(path)
        size = len(decode_file(file))
        total += size
        if size > MAX_FILE_BYTES or total > MAX_PACKAGE_BYTES:
            raise HTTPException(413, "Skill 包超过文件 10 MiB 或总计 50 MiB 限制")
        if path == "SKILL.md" and (file.get("encoding") or "\0" in file["content"]):
            raise HTTPException(400, "SKILL.md 必须是 UTF-8 文本")
    if "SKILL.md" not in paths:
        raise HTTPException(400, "Skill 包缺少 SKILL.md")
    for path in paths:
        if any(str(parent) in paths for parent in PurePosixPath(path).parents):
            raise HTTPException(400, "文件与文件夹路径冲突")
    return files


async def read_files(fs, directory):
    files, total = [], 0

    async def walk(path, prefix=""):
        nonlocal total
        for entry in await fs.listdir(path):
            name = entry["name"]
            relative = f"{prefix}/{name}" if prefix else name
            if relative == POLICY_FILE:
                continue
            target = os.path.join(path, name)
            info = await asyncio.to_thread(os.lstat, target)
            if stat.S_ISLNK(info.st_mode) or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode)):
                raise HTTPException(400, "Skill 包包含链接或特殊文件")
            if stat.S_ISDIR(info.st_mode):
                if len(PurePosixPath(relative).parts) > 64:
                    raise HTTPException(413, "Skill 文件夹层数过多")
                await walk(target, relative)
                continue
            total += info.st_size
            if info.st_size > MAX_FILE_BYTES or total > MAX_PACKAGE_BYTES or len(files) >= MAX_FILES:
                raise HTTPException(413, "Skill 包超过文件大小或数量限制")
            # 实际读取也设上限，避免文件在 stat 后增大。
            import aiofiles
            async with aiofiles.open(target, "rb") as source:
                data = await source.read(MAX_FILE_BYTES + 1)
            if len(data) > MAX_FILE_BYTES:
                raise HTTPException(413, "Skill 文件超过 10 MiB")
            try:
                text = data.decode("utf-8")
                if "\0" in text:
                    raise UnicodeError()
                files.append({"path": relative, "content": text})
            except UnicodeError:
                files.append({"path": relative, "content": base64.b64encode(data).decode("ascii"), "encoding": "base64"})
    await walk(directory)
    return validate_files(files)


async def load_policy(fs, directory, content):
    frontmatter, _ = parse_skill_frontmatter(content)
    policy = {}
    path = os.path.join(directory, POLICY_FILE)
    if await fs.isfile(path):
        if os.path.islink(path) or (await fs.stat(path))["size"] > 65536:
            raise HTTPException(400, "Skill 元数据文件无效")
        try:
            policy = json.loads(await fs.read_text(path))
            if not isinstance(policy, dict):
                raise ValueError()
        except (ValueError, UnicodeError) as error:
            raise HTTPException(400, "Skill 元数据文件无效") from error
    return PackagePolicy.model_validate({
        "name": policy.get("name") or frontmatter.get("name") or os.path.basename(directory),
        "description": policy.get("description", frontmatter.get("description", "")),
        "meta": policy.get("meta") or {},
        "is_active": policy.get("is_active", True),
    }).model_dump()


async def package_writable(fs, directory):
    if not await fs.is_writable(os.path.dirname(directory)):
        return False
    count = 0

    async def walk(path, depth=0):
        nonlocal count
        if depth > 64 or os.path.islink(path) or not await fs.is_writable(path):
            return False
        for entry in await fs.listdir(path):
            count += 1
            if count > MAX_FILES * 2:
                return False
            target = os.path.join(path, entry['name'])
            if os.path.islink(target) or not await fs.is_writable(target):
                return False
            if entry['type'] == 'directory' and not await walk(target, depth + 1):
                return False
        return True
    return await walk(directory)


async def detail(request, fs, package_id):
    directory = package_path(fs, package_id)
    if not await fs.isfile(os.path.join(directory, "SKILL.md")):
        raise HTTPException(404, "Skill 包不存在")
    files = await read_files(fs, directory)
    content = next(file["content"] for file in files if file["path"] == "SKILL.md")
    policy = await load_policy(fs, directory, content)
    info = await fs.stat(os.path.join(directory, "SKILL.md"))
    writable = await package_writable(fs, directory)
    return {
        "id": package_id, "package_id": package_id, "user_id": request.headers.get("x-user-id", ""),
        **policy, **scope_context(request, fs), "source": "terminal", "location": os.path.join(directory, "SKILL.md"),
        "content": content, "files": files, "data": {"files": files},
        "resources": [file["path"] for file in files if file["path"] != "SKILL.md"],
        "version_id": fingerprint(files, policy), "write_access": writable,
        "access_grants": [], "created_at": int(info["modified"]), "updated_at": int(info["modified"]),
    }


async def summaries(request, fs):
    items, statuses = [], []
    context = scope_context(request, fs)
    for root in ROOTS:
        directory = fs.resolve_path(root)
        try:
            if not await fs.isdir(directory):
                continue
            for entry in await fs.listdir(directory):
                if entry["type"] != "directory" or entry["name"].startswith("."):
                    continue
                package_id = root + "/" + entry["name"]
                try:
                    target = package_path(fs, package_id)
                    path = os.path.join(target, "SKILL.md")
                    if not await fs.isfile(path):
                        continue
                    info = await fs.stat(path)
                    if info["size"] > MAX_FILE_BYTES or os.path.islink(path):
                        raise HTTPException(413, "SKILL.md 无效或超过大小限制")
                    policy = await load_policy(fs, target, await fs.read_text(path))
                    items.append({
                        "id": package_id, "package_id": package_id, **policy, **context,
                        "source": "terminal", "location": path, "user_id": request.headers.get("x-user-id", ""),
                        "write_access": await package_writable(fs, target),
                        "version_id": None, "access_grants": [],
                        "created_at": int(info["modified"]), "updated_at": int(info["modified"]),
                    })
                except (OSError, ValueError, HTTPException) as error:
                    statuses.append({"package_id": package_id, "available": False, "detail": str(getattr(error, "detail", error))})
        except (OSError, ValueError, HTTPException) as error:
            statuses.append({"location": root, "available": False, "detail": str(getattr(error, "detail", error))})
    return {"items": items, "total": len(items), "statuses": statuses, **context}


@router.get("/skills/packages", include_in_schema=False)
async def list_packages(request: Request, fs: UserFS = Depends(get_filesystem)):
    return await summaries(request, fs)


@router.get("/skills/packages/detail", include_in_schema=False)
async def get_package(request: Request, id: str = Query(...), fs: UserFS = Depends(get_filesystem)):
    return await detail(request, fs, id)


@router.post("/skills/packages", include_in_schema=False)
@router.put("/skills/packages", include_in_schema=False)
async def save_package(request: Request, form: PackageWrite, id: str = Query(...), fs: UserFS = Depends(get_filesystem)):
    scope_context(request, fs)
    directory = package_path(fs, id)
    files = validate_files(form.files)
    policy = form.model_dump(exclude={"files", "expected_version_id"})
    policy_text = json.dumps(policy, ensure_ascii=False, sort_keys=True)
    if len(policy_text.encode()) > 65536:
        raise HTTPException(413, "Skill 元数据超过 64 KiB")
    async with package_lock(directory):
        exists = await fs.exists(directory)
        if request.method == "POST" and exists:
            raise HTTPException(409, "Skill 文件夹已存在")
        if request.method == "PUT" and not exists:
            raise HTTPException(404, "Skill 包不存在")
        if exists:
            current = await detail(request, fs, id)
            if not current["write_access"]:
                raise HTTPException(403, "Skill 包只读")
            if not form.expected_version_id:
                raise HTTPException(400, "更新 Skill 包需要 expected_version_id")
            if current["version_id"] != form.expected_version_id:
                raise HTTPException(409, "Skill 包已变化，请重新读取后保存")
        parent = os.path.dirname(directory)
        ancestor = parent
        while not await fs.exists(ancestor):
            ancestor = os.path.dirname(ancestor)
        if not await fs.is_writable(ancestor):
            raise HTTPException(403, "Skill 根目录只读")
        if not await fs.exists(parent):
            await fs.mkdir(parent)
        temporary = os.path.join(parent, ".skill-staging-" + uuid.uuid4().hex)
        backup = os.path.join(parent, ".skill-backup-" + uuid.uuid4().hex)
        try:
            await fs.mkdir(temporary)
            for file in files:
                target = os.path.join(temporary, file["path"])
                await fs.write_bytes(target, decode_file(file))
                if exists:
                    original = os.path.join(directory, file["path"])
                    if await fs.isfile(original):
                        mode = (await asyncio.to_thread(os.stat, original)).st_mode & 0o777
                        await asyncio.to_thread(os.chmod, target, mode)
            await fs.write(os.path.join(temporary, POLICY_FILE), policy_text)
            if exists:
                await fs.move(directory, backup)
            try:
                await fs.move(temporary, directory)
            except BaseException:
                if exists:
                    await fs.move(backup, directory)
                raise
            if exists:
                await fs.remove(backup)
        finally:
            if await fs.exists(temporary):
                await fs.remove(temporary)
        return await detail(request, fs, id)


@router.delete("/skills/packages", include_in_schema=False)
async def delete_package(request: Request, id: str = Query(...), expected_version_id: str = Query(...), fs: UserFS = Depends(get_filesystem)):
    directory = package_path(fs, id)
    async with package_lock(directory):
        current = await detail(request, fs, id)
        if not current["write_access"]:
            raise HTTPException(403, "Skill 包只读")
        if current["version_id"] != expected_version_id:
            raise HTTPException(409, "Skill 包已变化，请重新读取后删除")
        await fs.remove(directory)
    return True
