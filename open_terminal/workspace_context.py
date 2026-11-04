"""可信接入端签发、终端服务验证的工作区身份上下文。"""

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

_CLOCK_SKEW = 60
PROTOCOL = "open-terminal/workspace/v1"
PROJECT_PROTOCOL = "open-terminal/workspace/v2"


@dataclass(frozen=True)
class WorkspaceIdentity:
    user_id: str
    chat_id: str
    scope_type: str = "chat"
    scope_id: str | None = None
    owner_id: str | None = None
    operation: str | None = None
    is_admin: bool = False

    @property
    def workspace_key(self) -> tuple[str, str, str]:
        return (self.owner_id or self.user_id, self.scope_type, self.scope_id or self.chat_id)

    @property
    def directory_parts(self) -> tuple[str, str]:
        owner, kind, scope_id = self.workspace_key
        owner_hash = hashlib.sha256(owner.encode("utf-8")).hexdigest()
        scope_hash = hashlib.sha256(scope_id.encode("utf-8")).hexdigest()
        return owner_hash, f"project-{scope_hash}" if kind == "project" else scope_hash


def _validate_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 1024:
        raise ValueError("用户、对话和工作区标识必须为非空字符串，且不超过 1024 字节")
    return value


def sign_workspace_context(
    api_key: str, user_id: str, chat_id: str, *, ttl: int = 300,
    scope_type: str = "chat", scope_id: str | None = None, owner_id: str | None = None,
    operation: str | None = None, is_admin: bool = False,
) -> dict[str, str]:
    """仅由完成用户认证及归属查验的服务端调用。"""
    user_id, chat_id = _validate_id(user_id), _validate_id(chat_id)
    if not api_key or not 1 <= ttl <= 300:
        raise ValueError("需要现有 API Key，签名有效期为 1 至 300 秒")
    if scope_type not in {"chat", "project"}:
        raise ValueError("不支持的工作区范围")
    payload = {"v": 1, "user": user_id, "chat": chat_id, "exp": int(time.time()) + ttl}
    protocol = PROTOCOL
    if scope_type == "project":
        if user_id != owner_id and is_admin is not True:
            raise ValueError("项目工作区必须属于当前账号或由管理员管理")
        payload.update(v=2, scope_type="project", scope_id=_validate_id(scope_id), owner=_validate_id(owner_id))
        if is_admin is True:
            payload["admin"] = True
        protocol = PROJECT_PROTOCOL
    elif (scope_id is not None and scope_id != chat_id) or (owner_id is not None and owner_id != user_id):
        raise ValueError("独立对话工作区必须属于当前用户和对话")
    if operation is not None:
        if operation != "delete" or scope_type != "project" or (user_id != owner_id and is_admin is not True):
            raise ValueError("删除签名必须属于项目所有者或管理员")
        payload["operation"] = operation
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    context = base64.urlsafe_b64encode(encoded).decode("ascii").rstrip("=")
    signature = hmac.new(api_key.encode("utf-8"), protocol.encode() + b"\0" + context.encode("ascii"), hashlib.sha256).hexdigest()
    return {"X-User-Id": user_id, "X-Session-Id": chat_id, "X-Workspace-Context": context, "X-Workspace-Signature": signature}


def verify_workspace_context(api_key: str, headers) -> WorkspaceIdentity:
    context = headers.get("x-workspace-context", "")
    signature = headers.get("x-workspace-signature", "")
    if not context or len(context) > 8192 or not isinstance(signature, str):
        raise ValueError("缺少有效的工作区签名")
    try:
        payload = json.loads(base64.b64decode(context + "=" * (-len(context) % 4), altchars=b"-_", validate=True))
        if not isinstance(payload, dict) or type(payload.get("v")) is not int or payload["v"] not in (1, 2):
            raise ValueError("不支持的工作区协议")
        protocol = PROJECT_PROTOCOL if payload["v"] == 2 else PROTOCOL
        expected = hmac.new(api_key.encode("utf-8"), protocol.encode() + b"\0" + context.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError("工作区签名无效")
        expiry = payload["exp"]
        if type(expiry) is not int or not time.time() < expiry <= time.time() + 300 + _CLOCK_SKEW:
            raise ValueError("工作区签名已过期或有效期无效")
        user_id, chat_id = _validate_id(payload["user"]), _validate_id(payload["chat"])
        is_admin = payload.get("admin", False)
        if "admin" in payload and (payload["v"] != 2 or type(is_admin) is not bool):
            raise ValueError("管理员标识必须为 v2 签名中的布尔值")
        operation = payload.get("operation")
        if operation is not None and (
            operation != "delete" or payload["v"] != 2 or payload.get("scope_type") != "project"
            or (user_id != payload.get("owner") and not is_admin)
            or chat_id != f"project:{payload.get('scope_id')}"
        ):
            raise ValueError("项目删除签名无效")
        if payload["v"] == 2:
            if payload.get("scope_type") != "project":
                raise ValueError("不支持的工作区范围")
            if user_id != payload.get("owner") and not is_admin:
                raise ValueError("项目工作区必须属于当前账号或由管理员管理")
            identity = WorkspaceIdentity(user_id, chat_id, "project", _validate_id(payload["scope_id"]), _validate_id(payload["owner"]), operation, is_admin)
        else:
            identity = WorkspaceIdentity(user_id, chat_id)
        if headers.get("x-user-id") != identity.user_id or headers.get("x-session-id") != identity.chat_id:
            raise ValueError("工作区签名与请求身份不匹配")
        return identity
    except (UnicodeError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("工作区身份上下文格式无效") from exc
