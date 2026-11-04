"""可信接入端签发、终端服务验证的工作区身份上下文。"""

import base64
import hashlib
import hmac
import json
import time
from dataclasses import dataclass

# 容忍签发端与终端服务之间的时钟偏差。
_CLOCK_SKEW = 60
PROTOCOL = "open-terminal/workspace/v1"


@dataclass(frozen=True)
class WorkspaceIdentity:
    user_id: str
    chat_id: str

    @property
    def directory_parts(self) -> tuple[str, str]:
        return tuple(hashlib.sha256(value.encode("utf-8")).hexdigest() for value in (self.user_id, self.chat_id))


def _validate_id(value: object) -> str:
    if not isinstance(value, str) or not value.strip() or len(value.encode("utf-8")) > 1024:
        raise ValueError("用户和对话标识必须为非空字符串，且不超过 1024 字节")
    return value


def sign_workspace_context(api_key: str, user_id: str, chat_id: str, *, ttl: int = 300) -> dict[str, str]:
    """仅由完成用户认证及对话归属查验的服务端调用；不可作为模型工具暴露。"""
    user_id, chat_id = _validate_id(user_id), _validate_id(chat_id)
    if not api_key or not 1 <= ttl <= 300:
        raise ValueError("需要现有 API Key，签名有效期为 1 至 300 秒")
    payload = json.dumps({"v": 1, "user": user_id, "chat": chat_id, "exp": int(time.time()) + ttl}, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    context = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    signature = hmac.new(api_key.encode("utf-8"), PROTOCOL.encode() + b"\0" + context.encode("ascii"), hashlib.sha256).hexdigest()
    return {"X-User-Id": user_id, "X-Session-Id": chat_id, "X-Workspace-Context": context, "X-Workspace-Signature": signature}


def verify_workspace_context(api_key: str, headers) -> WorkspaceIdentity:
    context = headers.get("x-workspace-context", "")
    signature = headers.get("x-workspace-signature", "")
    if not context or len(context) > 8192 or not isinstance(signature, str):
        raise ValueError("缺少有效的工作区签名")
    try:
        expected = hmac.new(api_key.encode("utf-8"), PROTOCOL.encode() + b"\0" + context.encode("ascii"), hashlib.sha256).hexdigest()
        if not hmac.compare_digest(expected, signature):
            raise ValueError("工作区签名无效")
        payload = json.loads(base64.b64decode(context + "=" * (-len(context) % 4), altchars=b"-_", validate=True))
        expiry = payload["exp"]
        if payload.get("v") != 1 or type(expiry) is not int or not time.time() < expiry <= time.time() + 300 + _CLOCK_SKEW:
            raise ValueError("工作区签名已过期或有效期无效")
        identity = WorkspaceIdentity(_validate_id(payload["user"]), _validate_id(payload["chat"]))
        if headers.get("x-user-id") != identity.user_id or headers.get("x-session-id") != identity.chat_id:
            raise ValueError("工作区签名与请求身份不匹配")
        return identity
    except (UnicodeError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ValueError("工作区身份上下文格式无效") from exc
