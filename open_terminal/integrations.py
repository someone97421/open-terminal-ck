"""供 OpenWebUI 服务端调用的接入原语，不依赖特定 OpenWebUI 版本。"""

import asyncio
import inspect
from typing import Callable

import httpx

from open_terminal.workspace_context import sign_workspace_context
from open_terminal.utils.limits import run_io

_IMAGE_SLOTS = asyncio.Semaphore(2)


async def workspace_headers(api_key: str, authenticated_user_id: str, chat_id: str, *, owns_chat: Callable) -> dict[str, str]:
    """owns_chat 必须查询持久化归属；user_id 必须来自已认证服务端会话。"""
    allowed = owns_chat(authenticated_user_id, chat_id)
    if inspect.isawaitable(allowed):
        allowed = await allowed
    if allowed is not True:
        raise PermissionError("当前用户无权访问该对话")
    return {"Authorization": f"Bearer {api_key}", **sign_workspace_context(api_key, authenticated_user_id, chat_id)}


async def send_guarded_model_request(client: httpx.AsyncClient, url: str, payload: dict, *, gateway=None, resolver=None, headers=None, stream=False) -> httpx.Response:
    """在发送前处理完整请求副本；覆盖最终工具结果、历史记录和附件序列化结果。

    resolver 在线程中同步返回已验证归属的图片字节，不可直接下载任意模型 URL。
    stream=True 时调用方负责消费响应并 aclose；不可在此函数之后追加消息。
    """
    from open_terminal.utils.image_context import ImageContextGateway, sanitize_model_payload
    if gateway is None:
        from open_terminal.env import IMAGE_MAX_EDGE, IMAGE_MAX_BYTES, IMAGE_MAX_PIXELS
        gateway = ImageContextGateway(max_edge=IMAGE_MAX_EDGE, max_bytes=IMAGE_MAX_BYTES, max_pixels=IMAGE_MAX_PIXELS)

    async with _IMAGE_SLOTS:
        prepared = await run_io(sanitize_model_payload, payload, gateway, resolver)
    request = client.build_request("POST", url, json=prepared, headers=headers)
    response = await client.send(request, stream=stream)
    if response.is_error:
        await response.aread()
        response.raise_for_status()
    return response
