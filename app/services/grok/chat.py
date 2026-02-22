"""
Grok Chat 服务
"""

import asyncio
import orjson
import tiktoken
from typing import Dict, List, Any
from dataclasses import dataclass

from curl_cffi.requests import AsyncSession

from app.core.logger import logger
from app.core.config import get_config
from app.core.exceptions import (
    AppException,
    UpstreamException,
    ValidationException,
    ErrorType
)
from app.services.grok.headers import build_grok_headers
from app.services.grok.model import ModelService
from app.services.grok.assets import UploadService
from app.services.grok.processor import StreamProcessor, CollectProcessor
from app.services.grok.retry import RetryConfig
from app.services.token import get_token_manager
from app.services.request_stats import request_stats


CHAT_API = "https://grok.com/rest/app-chat/conversations/new"
TIMEOUT = 120
BROWSER = "chrome136"

# 模块级共享 HTTP 连接池
_shared_session: AsyncSession | None = None


def _get_shared_session() -> AsyncSession:
    """懒初始化共享 AsyncSession，复用 TCP/TLS 连接"""
    global _shared_session
    if _shared_session is None:
        _shared_session = AsyncSession(impersonate=BROWSER, max_clients=64)
    return _shared_session


async def close_shared_session() -> None:
    """关闭共享 session，供应用 shutdown 时调用"""
    global _shared_session
    if _shared_session is not None:
        try:
            await _shared_session.close()
        except Exception:
            pass
        _shared_session = None


_enc = tiktoken.get_encoding("o200k_base")


async def _count_prompt_tokens(messages: List[Dict[str, Any]]) -> int:
    """Count prompt tokens from OpenAI messages using tiktoken (o200k_base).

    BPE encoding is CPU-intensive, so the actual work runs in a thread pool
    to avoid blocking the asyncio event loop on large prompts.
    """
    def _encode() -> int:
        total = 3  # base overhead (<|start|>assistant<|message|>)
        for msg in messages:
            total += 4  # per-message overhead (role, delimiters)
            content = msg.get("content", "")
            if isinstance(content, str):
                if content:
                    total += len(_enc.encode(content))
            elif isinstance(content, list):
                for item in content:
                    if item.get("type") == "text":
                        text = item.get("text", "")
                        if text:
                            total += len(_enc.encode(text))
        return total
    return await asyncio.to_thread(_encode)


@dataclass
class ChatRequest:
    """聊天请求数据"""
    model: str
    messages: List[Dict[str, Any]]
    stream: bool = None
    think: bool = None


class MessageExtractor:
    """消息内容提取器"""
    
    # 需要上传的类型
    UPLOAD_TYPES = {"image_url", "input_audio", "file"}
    # 视频模式不支持的类型
    VIDEO_UNSUPPORTED = {"input_audio", "file"}
    
    @staticmethod
    def extract(messages: List[Dict[str, Any]], is_video: bool = False) -> tuple[str, List[str]]:
        """
        从 OpenAI 消息格式提取内容
        
        Args:
            messages: OpenAI 格式消息列表
            is_video: 是否为视频模型
            
        Returns:
            (text, attachments): 拼接后的文本和需要上传的附件列表
            
        Raises:
            ValueError: 视频模型遇到不支持的内容类型
        """
        texts = []
        attachments = []  # 需要上传的附件 (URL 或 base64)

        # 先抽取每条消息的文本，保留角色信息用于合并
        extracted: List[Dict[str, str]] = []

        for msg in messages:
            role = msg.get("role", "")
            content = msg.get("content", "")
            parts = []

            # 简单字符串内容
            if isinstance(content, str):
                if content.strip():
                    parts.append(content)

            # 列表格式内容
            elif isinstance(content, list):
                for item in content:
                    item_type = item.get("type", "")

                    # 文本类型
                    if item_type == "text":
                        text = item.get("text", "")
                        if text.strip():
                            parts.append(text)

                    # 图片类型
                    elif item_type == "image_url":
                        image_data = item.get("image_url", {})
                        url = image_data.get("url", "") if isinstance(image_data, dict) else str(image_data)
                        if url:
                            attachments.append(("image", url))

                    # 音频类型
                    elif item_type == "input_audio":
                        if is_video:
                            raise ValueError("视频模型不支持 input_audio 类型")
                        audio_data = item.get("input_audio", {})
                        data = audio_data.get("data", "") if isinstance(audio_data, dict) else str(audio_data)
                        if data:
                            attachments.append(("audio", data))

                    # 文件类型
                    elif item_type == "file":
                        if is_video:
                            raise ValueError("视频模型不支持 file 类型")
                        file_data = item.get("file", {})
                        # file 可能是 URL 或 base64
                        url = file_data.get("url", "") or file_data.get("data", "")
                        if isinstance(file_data, str):
                            url = file_data
                        if url:
                            attachments.append(("file", url))

            if parts:
                extracted.append({"role": role, "text": "\n".join(parts)})

        # 合并文本
        last_user_index = None
        for i in range(len(extracted) - 1, -1, -1):
            if extracted[i]["role"] == "user":
                last_user_index = i
                break

        for i, item in enumerate(extracted):
            role = item["role"] or "user"
            text = item["text"]
            if i == last_user_index:
                texts.append(text)
            else:
                texts.append(f"{role}: {text}")

        # 换行拼接文本
        message = "\n\n".join(texts)
        return message, attachments
    
    @staticmethod
    def extract_text_only(messages: List[Dict[str, Any]]) -> str:
        """仅提取文本内容"""
        text, _ = MessageExtractor.extract(messages, is_video=True)
        return text


class ChatRequestBuilder:
    """请求构造器"""
    
    @staticmethod
    def build_headers(token: str) -> Dict[str, str]:
        """构造请求头"""
        return build_grok_headers(token)
    
    @staticmethod
    def build_payload(
        message: str, 
        model: str, 
        mode: str, 
        think: bool = None,
        file_attachments: List[str] = None,
        image_attachments: List[str] = None
    ) -> Dict[str, Any]:
        """
        构造请求体
        
        Args:
            message: 消息文本
            model: 模型名称
            mode: 模型模式
            think: 是否开启思考
            file_attachments: 文件附件 ID 列表
            image_attachments: 图片附件 URL 列表
        """
        temporary = get_config("grok.temporary", True)
        if think is None:
            think = get_config("grok.thinking", False)

        # Upstream payload expects image attachments merged into fileAttachments.
        merged_attachments: List[str] = []
        if file_attachments:
            merged_attachments.extend(file_attachments)
        if image_attachments:
            merged_attachments.extend(image_attachments)
        
        return {
            "temporary": temporary,
            "modelName": model,
            "modelMode": mode,
            "message": message,
            "fileAttachments": merged_attachments,
            "imageAttachments": [],
            "disableSearch": False,
            "enableImageGeneration": True,
            "returnImageBytes": False,
            "returnRawGrokInXaiRequest": False,
            "enableImageStreaming": True,
            "imageGenerationCount": 2,
            "forceConcise": False,
            "toolOverrides": {},
            "enableSideBySide": True,
            "sendFinalMetadata": True,
            "isReasoning": False,
            "disableTextFollowUps": False,
            "responseMetadata": {
                "modelConfigOverride": {"modelMap": {}},
                "requestModelDetails": {"modelId": model}
            },
            "disableMemory": False,
            "forceSideBySide": False,
            "isAsyncChat": False,
            "disableSelfHarmShortCircuit": False,
            "deviceEnvInfo": {
                "darkModeEnabled": False,
                "devicePixelRatio": 2,
                "screenWidth": 2056,
                "screenHeight": 1329,
                "viewportWidth": 2056,
                "viewportHeight": 1083
            }
        }


# ==================== Grok 服务 ====================

class GrokChatService:
    """Grok API 调用服务"""
    
    def __init__(self, proxy: str = None):
        self.proxy = proxy or get_config("grok.base_proxy_url", "")
    
    async def chat(
        self,
        token: str,
        message: str,
        model: str = "grok-3",
        mode: str = "MODEL_MODE_FAST",
        think: bool = None,
        stream: bool = None,
        file_attachments: List[str] = None,
        image_attachments: List[str] = None
    ):
        """
        发送聊天请求
        
        Args:
            token: 认证 Token
            message: 消息文本
            model: Grok 模型名称
            mode: 模型模式
            think: 是否开启思考
            stream: 是否流式
            file_attachments: 文件附件 ID 列表
            image_attachments: 图片附件 URL 列表
        
        Raises:
            UpstreamException: 当 Grok API 返回错误时
        """
        if stream is None:
            stream = get_config("grok.stream", True)
        
        headers = ChatRequestBuilder.build_headers(token)
        payload = ChatRequestBuilder.build_payload(
            message, model, mode, think, 
            file_attachments, image_attachments
        )
        proxies = {"http": self.proxy, "https": self.proxy} if self.proxy else None
        timeout = get_config("grok.timeout", TIMEOUT)
        
        # 建立连接（复用共享连接池）
        session = _get_shared_session()
        try:
            resp = await session.post(
                CHAT_API,
                headers=headers,
                data=orjson.dumps(payload),
                timeout=timeout,
                stream=True,
                proxies=proxies
            )

            if resp.status_code != 200:
                try:
                    content = await resp.text()
                    content = content[:1000]
                except Exception:
                    content = "Unable to read response content"

                logger.error(
                    f"Chat failed: {resp.status_code}, {content}",
                    extra={"status": resp.status_code, "token": token[:10] + "..."}
                )
                raise UpstreamException(
                    message=f"Grok API request failed: {resp.status_code}",
                    details={"status": resp.status_code}
                )

        except UpstreamException:
            raise
        except Exception as e:
            logger.error(f"Chat request error: {e}")
            raise UpstreamException(
                message=f"Chat connection failed: {str(e)}",
                details={"error": str(e)}
            )

        # 流式传输（只关闭 response，不关闭共享 session）
        async def stream_response():
            try:
                async for line in resp.aiter_lines():
                    yield line
            finally:
                try:
                    resp.close()
                except Exception:
                    pass
        
        return stream_response()
    
    async def chat_openai(self, token: str, request: ChatRequest):
        """OpenAI 兼容接口"""
        model_info = ModelService.get(request.model)
        if not model_info:
            raise ValidationException(f"Unknown model: {request.model}")
        
        grok_model = model_info.grok_model
        mode = model_info.model_mode
        is_video = model_info.is_video
        
        # 提取消息和附件
        try:
            message, attachments = MessageExtractor.extract(request.messages, is_video=is_video)
        except ValueError as e:
            raise ValidationException(str(e))
        
        # 处理附件上传
        file_ids = []
        image_ids = []
        
        if attachments:
            upload_service = UploadService()
            try:
                for attach_type, attach_data in attachments:
                    # 获取 ID
                    file_id, _ = await upload_service.upload(attach_data, token)
                    
                    if attach_type == "image":
                        # 图片 imageAttachments
                        image_ids.append(file_id)
                        logger.debug(f"Image uploaded: {file_id}")
                    else:
                        # 文件 fileAttachments
                        file_ids.append(file_id)
                        logger.debug(f"File uploaded: {file_id}")
            finally:
                await upload_service.close()
        
        stream = request.stream if request.stream is not None else get_config("grok.stream", True)
        think = request.think if request.think is not None else get_config("grok.thinking", False)
        
        response = await self.chat(
            token, message, grok_model, mode, think, stream,
            file_attachments=file_ids,
            image_attachments=image_ids
        )
        
        return response, stream, request.model


# ==================== Chat 业务服务 ====================

class ChatService:
    """Chat 业务服务"""
    
    @staticmethod
    async def completions(
        model: str,
        messages: List[Dict[str, Any]],
        stream: bool = None,
        thinking: str = None
    ):
        """
        Chat Completions 入口
        
        Args:
            model: 模型名称
            messages: 消息列表
            stream: 是否流式
            thinking: 思考模式
            
        Returns:
            AsyncGenerator 或 dict
        """
        # 解析参数
        think = None
        if thinking == "enabled":
            think = True
        elif thinking == "disabled":
            think = False

        is_stream = stream if stream is not None else get_config("grok.stream", True)

        chat_request = ChatRequest(
            model=model,
            messages=messages,
            stream=is_stream,
            think=think
        )

        # 获取 token 并请求 Grok（失败时自动换 token 重试）
        token_mgr = await get_token_manager()

        # prompt tokens 只依赖输入消息，与 API 调用并行计算
        prompt_tokens_task = asyncio.create_task(_count_prompt_tokens(messages))

        max_retry = RetryConfig.get_max_retry()
        excluded_tokens: set[str] = set()
        token = None
        reservation_id = None
        last_error = None

        service = GrokChatService()

        for attempt in range(max_retry + 1):
            # 选择并预占 token（排除已失败的）
            token, reservation_id = await token_mgr.reserve_token_for_model(
                model,
                exclude=excluded_tokens,
            )
            if not token:
                break

            try:
                response, _, model_name = await service.chat_openai(token, chat_request)
                last_error = None
                break
            except UpstreamException as e:
                try:
                    await token_mgr.release_token_reservation(token, reservation_id)
                except Exception:
                    pass
                status = e.details.get("status") if e.details else None
                await token_mgr.record_fail(token, status or 0, str(e))
                last_error = e

                # 对 chat 请求，只要上游返回非 200，就切换 token 重试。
                # 不再受 retry_status_codes 白名单限制，避免 5xx 直接失败。
                if isinstance(status, int) and status != 200 and attempt < max_retry:
                    excluded_tokens.add(token)
                    delay = 0.5 * (attempt + 1)
                    logger.warning(
                        "Retry {}/{}: token {} got {}, switching token in {}s",
                        attempt + 1, max_retry, token, status, delay
                    )
                    await asyncio.sleep(delay)
                    continue
                raise
            except AppException:
                prompt_tokens_task.cancel()
                try:
                    await token_mgr.release_token_reservation(token, reservation_id)
                except Exception:
                    pass
                try:
                    await request_stats.record_request(model, success=False)
                except Exception:
                    pass
                raise

        if token is None:
            prompt_tokens_task.cancel()
            try:
                await request_stats.record_request(model, success=False)
            except Exception:
                pass
            raise AppException(
                message="No available tokens. Please try again later.",
                error_type=ErrorType.RATE_LIMIT.value,
                code="rate_limit_exceeded",
                status_code=429
            )

        if last_error is not None:
            prompt_tokens_task.cancel()
            try:
                await request_stats.record_request(model, success=False)
            except Exception:
                pass
            raise last_error

        # 处理响应
        prompt_tokens = await prompt_tokens_task

        if is_stream:
            processor = StreamProcessor(model_name, token, think, prompt_tokens=prompt_tokens).process(response)

            async def _wrapped_stream():
                completed = False
                try:
                    async for chunk in processor:
                        yield chunk
                    completed = True
                finally:
                    # Only count as "success" when the stream ends naturally.
                    try:
                        if completed:
                            await token_mgr.sync_usage(token, model_name, consume_on_fail=True, is_usage=True)
                            await request_stats.record_request(model_name, success=True)
                        else:
                            await request_stats.record_request(model_name, success=False)
                    except Exception:
                        pass
                    finally:
                        try:
                            await token_mgr.release_token_reservation(token, reservation_id)
                        except Exception:
                            pass

            return _wrapped_stream()

        try:
            result = await CollectProcessor(model_name, token, prompt_tokens=prompt_tokens).process(response)
            try:
                await token_mgr.sync_usage(token, model_name, consume_on_fail=True, is_usage=True)
                await request_stats.record_request(model_name, success=True)
            except Exception:
                pass
            return result
        finally:
            try:
                await token_mgr.release_token_reservation(token, reservation_id)
            except Exception:
                pass


__all__ = [
    "GrokChatService",
    "ChatRequest",
    "ChatRequestBuilder",
    "MessageExtractor",
    "ChatService",
    "close_shared_session",
]
