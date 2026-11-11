"""独立图片生成接口和委派内图片工具；不隐式复用文本模型密钥。"""
import base64
import hashlib
import json
import os
from pathlib import Path
from typing import Protocol
from uuid import uuid4

import httpx
from pydantic import Field

from ..execution import ImageSettings
from ..llm import LLMConfigurationError, LLMConnectionError, LLMHTTPError, LLMResponseError
from ..models import ImageRef
from ..platforms.content import local_image_path
from .tools import AgentTool, ToolArgs, ToolResult


def image_type(data):
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "png"
    if data.startswith(b"\xff\xd8\xff"):
        return "jpeg"
    if data.startswith(b"RIFF") and data[8:12] == b"WEBP":
        return "webp"
    raise ValueError("只支持 PNG、JPEG、WebP 图片")


def image_bytes(source):
    path = local_image_path(source)
    if path is None or not path.is_file() or path.stat().st_size > 10 * 1024 * 1024:
        raise ValueError("需要有效的本地图片，单张不超过 10MB")
    data = path.read_bytes()
    return data, image_type(data)


class ImageProvider(Protocol):
    supports_edit: bool

    def generate(self, prompt: str, reference: str | None = None) -> ImageRef: ...


class OpenAIImageProvider:
    def __init__(self, config: ImageSettings, output_dir: Path, *, transport=None):
        self.config, self.output_dir, self.transport = config, Path(output_dir), transport
        self.supports_edit = config.supports_edit

    def generate(self, prompt, reference=None):
        if reference and not self.supports_edit:
            raise LLMConfigurationError("当前图片接口未声明编辑能力")
        data, kind = image_bytes(reference) if reference else (None, None)
        identity = json.dumps([self.config.base_url, self.config.model, self.config.size,
                               self.config.response_format, prompt,
                               hashlib.sha256(data).hexdigest() if data else None], ensure_ascii=False)
        target = self.output_dir / (hashlib.sha256(identity.encode()).hexdigest() + ".png")
        if target.is_file():
            image_bytes(str(target))
            return ImageRef(description=prompt, source=str(target.resolve()))
        payload = {"model": self.config.model, "prompt": prompt, "n": 1, "size": self.config.size,
                   "output_format": "png"}
        if self.config.response_format:
            payload.pop("output_format")
            payload["response_format"] = self.config.response_format
        try:
            with httpx.Client(timeout=self.config.timeout, trust_env=self.config.trust_env,
                              transport=self.transport, follow_redirects=False) as client:
                headers = {"Authorization": "Bearer " + self.config.key()}
                if data:
                    response = client.post(self.config.endpoint("/images/edits"), headers=headers, data=payload,
                                           files={"image": ("reference." + kind, data, "image/" + kind)})
                else:
                    response = client.post(self.config.endpoint("/images/generations"), headers=headers, json=payload)
        except httpx.HTTPError:
            raise LLMConnectionError("图片服务连接失败或超时") from None
        if not 200 <= response.status_code < 300:
            raise LLMHTTPError(response.status_code)
        try:
            encoded = response.json()["data"][0]["b64_json"]
            if not isinstance(encoded, str) or len(encoded) > 20 * 1024 * 1024:
                raise ValueError
            output = base64.b64decode(encoded, validate=True)
            if image_type(output) != "png":
                raise ValueError("接口应按请求返回 PNG 图片")
        except (KeyError, IndexError, TypeError, ValueError):
            raise LLMResponseError("图片接口未返回有效的 base64 图片；请检查模型及接口兼容性") from None
        self.output_dir.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix("." + uuid4().hex + ".tmp")
        try:
            temporary.write_bytes(output)
            os.replace(temporary, target)
        finally:
            temporary.unlink(missing_ok=True)
        return ImageRef(description=prompt, source=str(target.resolve()))


class ImageArgs(ToolArgs):
    image_id: str = Field(min_length=1)


class GenerateArgs(ToolArgs):
    prompt: str = Field(min_length=1, max_length=8000)
    reference_image_id: str | None = None


class ImageTools:
    def __init__(self, images, *, provider=None, supports_vision=False):
        self.images = {f"image-{index}": image for index, image in enumerate(images)}
        self.provider, self.supports_vision = provider, supports_vision
        self.generated = []

    def catalog(self):
        return [{"image_id": key, **value.model_dump()} for key, value in self.images.items()]

    def capabilities(self):
        return {"image_input": self.supports_vision, "image_generation": self.provider is not None,
                "image_editing": bool(self.provider and self.provider.supports_edit)}

    def _image(self, identifier):
        if identifier not in self.images:
            raise PermissionError("只能访问本次委派提供或生成的图片 ID")
        return self.images[identifier]

    def inspect(self, args):
        data, kind = image_bytes(self._image(args.image_id).source)
        return ToolResult(json.dumps({"image_id": args.image_id}),
                          (f"data:image/{kind};base64," + base64.b64encode(data).decode(),))

    def generate(self, args):
        reference = self._image(args.reference_image_id).source if args.reference_image_id else None
        if len(self.generated) >= 4:
            raise ValueError("本次委派最多生成四张图片")
        image = self.provider.generate(args.prompt, reference)
        identifier = "generated-" + str(len(self.generated))
        self.generated.append(image)
        self.images[identifier] = image
        return json.dumps({"image_id": identifier, **image.model_dump()}, ensure_ascii=False)

    def tools(self):
        tools = []
        if self.supports_vision:
            tools.append(AgentTool("inspect_image", "查看本次提供或生成的图片，仅接受目录内 image_id", ImageArgs, self.inspect))
        if self.provider:
            tools.append(AgentTool("generate_image", "根据提示词创作图片；提供 reference_image_id 时按反馈修改已有图片",
                                   GenerateArgs, self.generate))
        return tuple(tools)

    def validate(self, content):
        allowed = {image.source for image in self.images.values()}
        if any(image.source not in allowed for image in content.images):
            raise ValueError("images 只能使用输入图片或本次 generate_image 返回的 source，不能虚构图片地址")
        return content
