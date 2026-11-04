"""为模型生成有大小上限的图片预览，并规范化结构化图片消息。

原图只读；EXIF 转正后按像素裁剪，动画仅取首帧，透明区域铺白底，输出 JPEG。
调用方负责对源文件和 cache_dir 做工作区路径隔离。POSIX 缓存逐级使用目录句柄
及 no-follow；不具备这些能力的平台跳过磁盘缓存，不以路径检查代替安全写入。
本模块不联网；resolver 是调用方明确提供的可信来源解析器，只能返回原始字节。
只承诺识别结构化图片及文本中的 image data URI，无法识别任意命令输出伪装的
base64。文本中嵌入的图片 data URI 和过长的纯 base64 文本会被拒绝。
"""

from __future__ import annotations

import base64
import binascii
import hashlib
import io
import json
import math
import os
import re
import stat
import uuid
import warnings
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from PIL import Image, ImageOps, UnidentifiedImageError


_CACHE_VERSION = 1
_OPEN_FORMATS = ("JPEG", "PNG", "WEBP", "GIF", "BMP", "TIFF")
# MPO 由 JPEG 插件识别，不是独立的打开插件。
_INPUT_FORMATS = (*_OPEN_FORMATS, "MPO")
_IMAGE_MIMES = frozenset(
    {"image/jpeg", "image/jpg", "image/png", "image/webp", "image/gif", "image/bmp", "image/tiff"}
)
_DATA_IMAGE = re.compile(r"data\s*:\s*image\s*/", re.IGNORECASE)
_BASE64_TEXT = re.compile(r"[A-Za-z0-9+/\r\n]+={0,2}\Z")
Crop = tuple[int, int, int, int]
Resolver = Callable[[str], bytes]


class ImageContextError(ValueError):
    """图片来源、内容或预览不能满足模型图片边界。"""

    def __init__(self, message: str, *, code: str = "invalid_image", size_bytes: int | None = None):
        super().__init__(message)
        self.code = code
        self.size_bytes = size_bytes


@dataclass(frozen=True)
class ImagePreview:
    """已重新编码的预览；data 是编码文件字节，mime 为实际输出类型。"""

    mime: str
    width: int
    height: int
    data: bytes

    @property
    def data_uri(self) -> str:
        """返回可直接用于 OpenAI 图片内容的 data URI。"""
        encoded = base64.b64encode(self.data).decode("ascii")
        return f"data:{self.mime};base64,{encoded}"


class ImageContextGateway:
    """限制原始输入、解码像素和模型预览，不修改源文件。

    max_input_bytes 默认 32 MiB，max_pixels 默认四千万像素；解码前检查头部尺寸。
    max_bytes 指 JPEG 文件本身的字节数，不包括 base64 和 JSON 封装的开销。
    crop 为 EXIF 转正后的 (left, top, right, bottom)，右、下边界不包含在内。
    不接受越界裁剪、矢量格式、未知格式或无法完整解码的首帧。
    """

    def __init__(
        self,
        max_edge: int = 2048,
        max_bytes: int = 2 * 1024 * 1024,
        max_pixels: int = 40_000_000,
        max_input_bytes: int = 32 * 1024 * 1024,
    ) -> None:
        for name, value in (
            ("max_edge", max_edge),
            ("max_bytes", max_bytes),
            ("max_pixels", max_pixels),
            ("max_input_bytes", max_input_bytes),
        ):
            if type(value) is not int or value <= 0:
                raise ValueError(f"{name} 必须是正整数")
        self.max_edge = max_edge
        self.max_bytes = max_bytes
        self.max_pixels = max_pixels
        self.max_input_bytes = max_input_bytes

    def preview_bytes(self, raw: bytes, crop: Crop | None = None) -> ImagePreview:
        """从受限字节生成预览；不根据文件名或声明 MIME 判断格式。"""
        raw = self._check_raw(raw)
        crop = self._check_crop(crop)
        with self._decode(raw, crop) as image:
            return self._encode(image)

    def preview_file(
        self,
        path: str | os.PathLike[str],
        cache_dir: str | os.PathLike[str] | None,
        crop: Crop | None = None,
    ) -> ImagePreview:
        """只读源文件，并在安全可用的隔离缓存中原子保存预览。

        缓存键包含源内容 SHA256、裁剪、所有限制和编码版本。命中仍校验源图片、
        裁剪及缓存的实际编码、尺寸和字节数；损坏缓存视为未命中。
        cache_dir=None 可关闭缓存；缓存不可安全读写时仍返回即时生成的预览。
        """
        crop = self._check_crop(crop)
        raw, origin = self._read_source(path)
        parameters = json.dumps(
            [_CACHE_VERSION, self.max_edge, self.max_bytes, self.max_pixels, self.max_input_bytes, crop],
            separators=(",", ":"),
        ).encode("ascii")
        key = hashlib.sha256(hashlib.sha256(raw).digest() + parameters).hexdigest() + ".jpg"
        with self._decode(raw, crop) as image, self._cache_directory(cache_dir) as directory:
            cached = self._read_cache(directory, key, image.size)
            if cached is not None:
                return cached
            preview = self._encode(image)
            self._write_cache(directory, key, preview.data, origin)
            return preview

    def _check_raw(self, raw: bytes) -> bytes:
        if not isinstance(raw, (bytes, bytearray)):
            raise ImageContextError("图片来源必须提供原始字节")
        if not raw or len(raw) > self.max_input_bytes:
            raise ImageContextError("原始图片为空或超过输入字节上限")
        return bytes(raw)

    @staticmethod
    def _check_crop(crop: Crop | None) -> Crop | None:
        if crop is None:
            return None
        if not isinstance(crop, (tuple, list)) or len(crop) != 4:
            raise ImageContextError("裁剪必须包含 left、top、right、bottom 四个像素整数")
        if any(type(value) is not int for value in crop):
            raise ImageContextError("裁剪坐标必须是像素整数")
        left, top, right, bottom = crop
        if left < 0 or top < 0 or right <= left or bottom <= top:
            raise ImageContextError("裁剪区域必须非空且坐标非负")
        return left, top, right, bottom

    def _check_dimensions(self, size: tuple[int, int]) -> None:
        width, height = size
        if width <= 0 or height <= 0 or width * height > self.max_pixels:
            raise ImageContextError("图片解码像素超过上限或尺寸无效")

    def _read_source(self, path: str | os.PathLike[str]) -> tuple[bytes, tuple[int, int]]:
        descriptor = None
        try:
            descriptor = os.open(path, os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NONBLOCK", 0))
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or info.st_size > self.max_input_bytes:
                raise ImageContextError("图片来源必须是未超过输入上限的普通文件")
            with os.fdopen(descriptor, "rb") as source:
                descriptor = None
                raw = source.read(self.max_input_bytes + 1)
            return self._check_raw(raw), (info.st_dev, info.st_ino)
        except OSError as exc:
            raise ImageContextError("无法读取图片文件") from exc
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _decode(self, raw: bytes, crop: Crop | None) -> Image.Image:
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw), formats=_OPEN_FORMATS) as probe:
                    if probe.format not in _INPUT_FORMATS:
                        raise ImageContextError("不支持该图片的实际编码格式")
                    self._check_dimensions(probe.size)
                    probe.verify()
                with Image.open(io.BytesIO(raw), formats=_OPEN_FORMATS) as source:
                    self._check_dimensions(source.size)
                    source.seek(0)
                    source.load()
                    with ImageOps.exif_transpose(source) as oriented:
                        self._check_dimensions(oriented.size)
                        if crop is not None:
                            if crop[2] > oriented.width or crop[3] > oriented.height:
                                raise ImageContextError("裁剪区域超出 EXIF 转正后的图片范围")
                            selected = oriented.crop(crop)
                        else:
                            selected = oriented.copy()
                    with selected:
                        if selected.mode in ("RGBA", "LA", "PA") or "transparency" in selected.info:
                            with selected.convert("RGBA") as rgba:
                                result = Image.new("RGB", rgba.size, "white")
                                with rgba.getchannel("A") as alpha:
                                    result.paste(rgba, mask=alpha)
                        else:
                            result = selected.convert("RGB")
                        result.info.clear()
                        return result
        except ImageContextError:
            raise
        except UnidentifiedImageError as exc:
            raise ImageContextError(f"图片格式未知或不受支持（{len(raw)} 字节）", code="unsupported_image", size_bytes=len(raw)) from exc
        except (Image.DecompressionBombError, Image.DecompressionBombWarning) as exc:
            raise ImageContextError(f"图片解码像素超过安全上限（{len(raw)} 字节）", code="image_too_large", size_bytes=len(raw)) from exc
        except (OSError, SyntaxError, ValueError) as exc:
            truncated = any(hint in str(exc).lower() for hint in ("truncated", "unexpected end", "not enough image data", "end of file"))
            reason = "图片数据被截断或不完整" if truncated else "图片数据损坏或不完整"
            raise ImageContextError(f"{reason}，无法完整解码（{len(raw)} 字节）", code="incomplete_image" if truncated else "corrupt_image", size_bytes=len(raw)) from exc
        except Exception as exc:
            raise ImageContextError(f"图片无法安全解码（{len(raw)} 字节）", size_bytes=len(raw)) from exc

    def _encode(self, image: Image.Image) -> ImagePreview:
        working = image.copy()
        try:
            working.thumbnail((self.max_edge, self.max_edge), Image.Resampling.LANCZOS)
            while True:
                for quality in (92, 85, 75, 65, 55, 45, 35, 25, 15):
                    output = io.BytesIO()
                    working.save(output, format="JPEG", quality=quality, optimize=True)
                    data = output.getvalue()
                    if len(data) <= self.max_bytes:
                        return ImagePreview("image/jpeg", working.width, working.height, data)
                if working.size == (1, 1):
                    raise ImageContextError("图片无法在编码字节上限内安全压缩")
                size = (max(1, math.floor(working.width * 0.75)), max(1, math.floor(working.height * 0.75)))
                smaller = working.resize(size, Image.Resampling.LANCZOS)
                working.close()
                working = smaller
        except ImageContextError:
            raise
        except Exception as exc:
            raise ImageContextError("图片无法安全编码为预览") from exc
        finally:
            working.close()

    @staticmethod
    @contextmanager
    def _cache_directory(path: str | os.PathLike[str] | None):
        """逐级拒绝符号链接；缺少目录句柄操作能力时禁用持久缓存。"""
        supported = getattr(os, "supports_dir_fd", set())
        required = (os.open, os.mkdir, os.rename, os.unlink, os.stat)
        if path is None or not hasattr(os, "O_NOFOLLOW") or not hasattr(os, "O_DIRECTORY") or not all(
            function in supported for function in required
        ):
            yield None
            return
        descriptor = None
        flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW
        try:
            absolute = Path(os.path.abspath(path))
            descriptor = os.open(absolute.anchor, flags)
            for part in absolute.parts[1:]:
                try:
                    child = os.open(part, flags, dir_fd=descriptor)
                except FileNotFoundError:
                    try:
                        os.mkdir(part, mode=0o700, dir_fd=descriptor)
                    except FileExistsError:
                        pass
                    child = os.open(part, flags, dir_fd=descriptor)
                os.close(descriptor)
                descriptor = child
        except (OSError, ValueError):
            if descriptor is not None:
                os.close(descriptor)
            descriptor = None
        try:
            yield descriptor
        finally:
            if descriptor is not None:
                os.close(descriptor)

    def _read_cache(self, directory: int | None, key: str, source_size: tuple[int, int]) -> ImagePreview | None:
        if directory is None:
            return None
        descriptor = None
        try:
            flags = os.O_RDONLY | os.O_NOFOLLOW | getattr(os, "O_NONBLOCK", 0)
            descriptor = os.open(key, flags, dir_fd=directory)
            info = os.fstat(descriptor)
            if not stat.S_ISREG(info.st_mode) or not 0 < info.st_size <= self.max_bytes:
                return None
            with os.fdopen(descriptor, "rb") as source:
                descriptor = None
                raw = source.read(self.max_bytes + 1)
            if not raw or len(raw) > self.max_bytes or not raw.endswith(b"\xff\xd9"):
                return None
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(io.BytesIO(raw), formats=("JPEG",)) as probe:
                    if probe.format != "JPEG" or getattr(probe, "n_frames", 1) != 1:
                        return None
                    self._check_dimensions(probe.size)
                    if max(probe.size) > self.max_edge or any(a > b for a, b in zip(probe.size, source_size)):
                        return None
                    if probe.mode != "RGB" or any(name in probe.info for name in ("exif", "icc_profile", "xmp", "comment")):
                        return None
                    size = probe.size
                    probe.verify()
                with Image.open(io.BytesIO(raw), formats=("JPEG",)) as decoded:
                    decoded.load()
            return ImagePreview("image/jpeg", size[0], size[1], raw)
        except Exception:
            return None
        finally:
            if descriptor is not None:
                os.close(descriptor)

    @staticmethod
    def _write_cache(directory: int | None, key: str, data: bytes, origin: tuple[int, int]) -> None:
        if directory is None:
            return
        temporary = f".{uuid.uuid4().hex}.tmp"
        descriptor = None
        try:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW
            descriptor = os.open(temporary, flags, 0o600, dir_fd=directory)
            with os.fdopen(descriptor, "wb") as output:
                descriptor = None
                output.write(data)
                output.flush()
                os.fsync(output.fileno())
            try:
                existing = os.stat(key, dir_fd=directory, follow_symlinks=False)
            except FileNotFoundError:
                existing = None
            if existing is not None and (existing.st_dev, existing.st_ino) == origin:
                return
            os.replace(temporary, key, src_dir_fd=directory, dst_dir_fd=directory)
        except OSError:
            pass
        finally:
            if descriptor is not None:
                os.close(descriptor)
            try:
                os.unlink(temporary, dir_fd=directory)
            except OSError:
                pass


def sanitize_model_payload(
    payload: Any,
    gateway: ImageContextGateway | None = None,
    resolver: Resolver | None = None,
) -> Any:
    """复制并递归规范化 Chat messages、Responses input 及嵌套工具结果。

    支持 image_url、input_image、Anthropic image/source 和 MCP image/data。
    结构化图片统一重新编码；保持各自协议外形，输出实际 JPEG MIME。
    data URI 只接受明确的受支持图片 MIME 和严格 base64；其他来源只有 resolver
    成功返回 bytes 才可用。resolver 应自行限定可信本地路径、来源和读取上限，
    本函数不请求网络，也不会自行读取 URL 或文件路径。

    普通文本中的图片 data URI 直接拒绝；看起来是 JSON 的工具文本也递归检查。
    未知 image 类型、图片字段结构及过长纯 base64 文本拒绝。图片保留字段具有
    严格语义，不支持把任意供应商图片扩展字段透传给模型。
    """
    gateway = gateway if gateway is not None else ImageContextGateway()
    active: set[int] = set()
    text_base64_limit = max(4096, 4 * ((gateway.max_bytes + 2) // 3))

    def decode_base64(value: Any, media_type: Any) -> ImagePreview:
        if not isinstance(media_type, str) or media_type.lower() not in _IMAGE_MIMES:
            raise ImageContextError("图片 MIME 未知或不受支持")
        if not isinstance(value, str) or len(value) > 4 * ((gateway.max_input_bytes + 2) // 3):
            raise ImageContextError("图片 base64 类型无效或超过输入上限")
        try:
            raw = base64.b64decode("".join(value.split()), validate=True)
        except (ValueError, binascii.Error) as exc:
            raise ImageContextError("图片 base64 编码无效") from exc
        return gateway.preview_bytes(raw)

    def from_reference(reference: Any) -> ImagePreview:
        if not isinstance(reference, str) or not reference:
            raise ImageContextError("图片来源必须是非空字符串")
        if reference[:5].lower() == "data:":
            if len(reference) > 4 * ((gateway.max_input_bytes + 2) // 3) + 128:
                raise ImageContextError("图片 data URI 超过输入上限")
            header, separator, encoded = reference.partition(",")
            if not separator or len(header) > 128 or not header.lower().endswith(";base64"):
                raise ImageContextError("图片 data URI 必须使用 base64")
            media_type = header[5:-7]
            return decode_base64(encoded, media_type)
        if len(reference) > 8192:
            raise ImageContextError("图片来源标识过长")
        if resolver is None:
            raise ImageContextError("远程或未知图片来源被拒绝，需要可信 resolver")
        try:
            raw = resolver(reference)
        except Exception as exc:
            raise ImageContextError("可信 resolver 无法解析图片来源") from exc
        return gateway.preview_bytes(raw)

    def normalize_url(value: Any, depth: int) -> Any:
        if isinstance(value, str):
            return from_reference(value).data_uri
        if not isinstance(value, Mapping) or "url" not in value:
            raise ImageContextError("image_url 必须是 URL 字符串或包含 url 的对象")
        if not set(value) <= {"url", "detail"}:
            raise ImageContextError("image_url 对象包含未知字段")
        result = {key: walk(item, depth + 1) for key, item in value.items() if key != "url"}
        result["url"] = from_reference(value["url"]).data_uri
        return result

    def image_block(block: Mapping[str, Any], depth: int) -> dict[str, Any]:
        kind = block["type"]
        if kind == "image_url":
            if "image_url" not in block:
                raise ImageContextError("image_url 内容块缺少图片来源")
            result = extras(block, {"image_url"}, depth)
            result["image_url"] = normalize_url(block["image_url"], depth)
            return result
        if kind == "input_image":
            sources = [name for name in ("image_url", "file_id") if block.get(name) is not None]
            if len(sources) != 1:
                raise ImageContextError("input_image 必须且只能包含一个图片来源")
            source = block[sources[0]]
            if sources[0] == "file_id" and resolver is None:
                raise ImageContextError("图片 file_id 需要可信 resolver")
            result = extras(block, {"image_url", "file_id"}, depth)
            result["image_url"] = from_reference(source).data_uri
            return result
        sources = [name for name in ("source", "data") if name in block]
        if len(sources) != 1:
            raise ImageContextError("image 内容块必须且只能包含 source 或 data")
        if sources[0] == "data":
            if "mimeType" not in block:
                raise ImageContextError("image/data 内容块缺少 mimeType")
            preview = decode_base64(block["data"], block["mimeType"])
            result = extras(block, {"data", "mimeType"}, depth)
            result.update(data=base64.b64encode(preview.data).decode("ascii"), mimeType=preview.mime)
            return result
        source = block["source"]
        if not isinstance(source, Mapping):
            raise ImageContextError("image/source 必须是对象")
        if source.get("type") == "base64" and set(source) <= {"type", "media_type", "data"}:
            preview = decode_base64(source.get("data"), source.get("media_type"))
        elif source.get("type") == "url" and set(source) <= {"type", "url"}:
            preview = from_reference(source.get("url"))
        else:
            raise ImageContextError("未知 image/source 类型或字段")
        result = extras(block, {"source"}, depth)
        result["source"] = {
            "type": "base64",
            "media_type": preview.mime,
            "data": base64.b64encode(preview.data).decode("ascii"),
        }
        return result

    def extras(block: Mapping[str, Any], excluded: set[str], depth: int) -> dict[str, Any]:
        result = {}
        for key, item in block.items():
            if key in excluded:
                continue
            if key in {"source", "data", "base64", "url", "file_id", "image_url", "image", "images", "input_image", "mimeType", "mime_type", "media_type"}:
                raise ImageContextError("图片内容块存在未知或重复的图片字段")
            result[key] = walk(item, depth + 1)
        return result

    def schema_value(value: Any, depth: int = 0) -> Any:
        if depth > 64:
            raise ImageContextError("工具定义嵌套过深")
        if isinstance(value, Mapping):
            if any(not isinstance(key, str) or _DATA_IMAGE.search(key) for key in value):
                raise ImageContextError("工具定义的字段名无效")
            return {key: schema_value(item, depth + 1) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [schema_value(item, depth + 1) for item in value]
        if isinstance(value, str):
            if _DATA_IMAGE.search(value) or (len(value.strip()) > text_base64_limit and _BASE64_TEXT.fullmatch(value.strip())):
                raise ImageContextError("工具定义包含内嵌图片或过长 base64")
            return value
        if value is None or type(value) in (bool, int, float):
            return value
        raise ImageContextError("工具定义必须是 JSON 兼容结构")

    def walk(value: Any, depth: int = 0) -> Any:
        if depth > 64:
            raise ImageContextError("模型消息嵌套过深")
        if isinstance(value, str):
            stripped = value.strip()
            if stripped.startswith(("{", "[")):
                try:
                    decoded = json.loads(stripped)
                except (ValueError, RecursionError):
                    decoded = None
                if isinstance(decoded, (dict, list)):
                    normalized = walk(decoded, depth + 1)
                    if normalized != decoded:
                        return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"))
                    return value
            if _DATA_IMAGE.search(value):
                raise ImageContextError("文本中嵌入的图片 data URI 必须改用结构化图片内容块")
            if len(stripped) > text_base64_limit and _BASE64_TEXT.fullmatch(stripped):
                raise ImageContextError("文本包含过长的疑似 base64 数据，请使用结构化图片内容块")
            return value
        if value is None or type(value) in (bool, int, float):
            return value
        if not isinstance(value, (Mapping, list, tuple)):
            raise ImageContextError("模型消息必须是 JSON 兼容结构")
        identity = id(value)
        if identity in active:
            raise ImageContextError("模型消息包含循环引用")
        active.add(identity)
        try:
            if isinstance(value, (list, tuple)):
                return [walk(item, depth + 1) for item in value]
            if any(not isinstance(key, str) for key in value):
                raise ImageContextError("模型消息对象的键必须是字符串")
            if any(_DATA_IMAGE.search(key) for key in value):
                raise ImageContextError("模型消息的对象键包含图片 data URI")
            kind = value.get("type")
            if kind in ("image_url", "input_image", "image"):
                return image_block(value, depth)
            if isinstance(kind, str) and "image" in kind.lower():
                raise ImageContextError("未知图片内容块类型")
            for field in ("mimeType", "mime_type", "media_type"):
                mime = value.get(field)
                if isinstance(mime, str) and mime.lower().startswith("image/"):
                    raise ImageContextError("图片 MIME 必须位于受支持的结构化图片内容块")
            result = {}
            for key, item in value.items():
                if depth == 0 and key in ("tools", "tool_choice", "response_format", "text"):
                    result[key] = schema_value(item)
                    continue
                if key.lower().startswith("image/"):
                    preview = decode_base64(item, key)
                    result[preview.mime] = base64.b64encode(preview.data).decode("ascii")
                    continue
                if key == "metadata":
                    result[key] = schema_value(item)
                    continue
                if key == "image_url":
                    result[key] = normalize_url(item, depth)
                elif key == "input_image":
                    raise ImageContextError("未知 input_image 字段结构")
                elif key in ("image", "images"):
                    raise ImageContextError("未知图片字段结构")
                else:
                    result[key] = walk(item, depth + 1)
            return result
        finally:
            active.remove(identity)

    return walk(payload)
