# Standard library imports
import asyncio
import functools
import json
import os
import time
from datetime import datetime
from functools import wraps
from typing import Any, Dict, List, Optional, Union
from urllib.parse import urlparse
import base64
import io
import httpx

# Third-party imports
import fastapi_poe as fp
import tiktoken
from fastapi import (
    Depends,
    FastAPI,
    File,
    Form,
    HTTPException,
    Query,
    Request,
    Response,
    UploadFile,
)
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import HTMLResponse, FileResponse
from fastapi.openapi.utils import get_openapi
from fastapi.responses import JSONResponse, StreamingResponse
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from fastapi_poe.client import get_bot_response
from pydantic import BaseModel, Field

# Fake tool calling import
from fake_tool_calling import FakeToolCallHandler

# Create the FastAPI app
app = FastAPI(
    title="Poe-API OpenAI Proxy",
    version="1.0.0",
    description="A proxy server for Poe API that provides OpenAI-compatible endpoints",
)


# Add CORS middleware configuration
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)


class ChatMessage(BaseModel):
    role: str  # role: the role of the message, either system, user, or assistant
    content: str


class ChatCompletionMessage(BaseModel):
    role: str
    content: Optional[Any] = None
    name: Optional[str] = None


class ChatCompletionRequest(BaseModel):
    model: str
    messages: list[ChatCompletionMessage]
    temperature: Optional[float] = 1.0
    top_p: Optional[float] = 1.0
    seed: Optional[int] = None
    response_format: Optional[Dict[str, str]] = None
    n: Optional[int] = 1
    stream: Optional[bool] = False
    stop: Optional[Union[str, list[str]]] = None
    max_tokens: Optional[int] = None
    presence_penalty: Optional[float] = 0
    frequency_penalty: Optional[float] = 0
    logit_bias: Optional[Dict[int, float]] = None
    user: Optional[str] = None
    # Tool calling support
    tools: Optional[List[Dict[str, Any]]] = None
    tool_choice: Optional[Union[str, Dict[str, Any]]] = None


class EmbeddingRequest(BaseModel):
    model: str
    input: Union[str, list[str]]
    encoding_format: Optional[str] = "float"
    user: Optional[str] = None


class ModerationRequest(BaseModel):
    input: Union[str, list[str]]
    model: Optional[str] = "text-moderation-latest"


class ImageGenerationRequest(BaseModel):
    prompt: str
    model: Optional[str] = None
    n: Optional[int] = 1
    size: Optional[str] = None  # Ignored, Poe bots determine dimensions
    response_format: Optional[str] = "url"


class ErrorResponse(BaseModel):
    message: str
    type: str
    param: Optional[str] = None
    code: Optional[str] = None


# Add a custom exception class for Poe API errors
class PoeAPIError(Exception):
    """Custom exception for Poe API errors."""

    def __init__(self, message, error_data=None, status_code=500, error_id=None):
        self.message = message
        self.error_data = error_data
        self.status_code = status_code
        self.error_id = error_id
        super().__init__(self.message)


def create_error_response(
    message: str, error_type: str, status_code: int, param: Optional[str] = None
) -> HTTPException:
    error_types = {
        400: "invalid_request_error",
        401: "authentication_error",
        403: "permission_error",
        404: "not_found_error",
        429: "rate_limit_error",
        500: "server_error",
    }
    error = {
        "message": message,
        "type": error_type or error_types.get(status_code, "server_error"),
    }
    if param:
        error["param"] = param
    return HTTPException(status_code=status_code, detail=error)


def normalize_model(model: str):
    # trim any whitespace from the model name
    model = model.strip()

    # No validation and normalization - we pass through the model name as provided
    # Model validation is handled by the Poe API service
    return model


# Custom HTTP Bearer authentication that returns 401 like OpenAI
class CustomHTTPBearer(HTTPBearer):
    async def __call__(
        self, request: Request
    ) -> Optional[HTTPAuthorizationCredentials]:
        authorization = request.headers.get("Authorization")
        if not authorization:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": {
                        "message": "Authentication error: No token provided",
                        "type": "authentication_error",
                    }
                },
                headers={"WWW-Authenticate": "Bearer"},
            )

        try:
            scheme, credentials = authorization.split()
            if scheme.lower() != "bearer":
                raise HTTPException(
                    status_code=401,
                    detail={
                        "error": {
                            "message": f"Authentication error: Invalid scheme '{scheme}' - must be 'Bearer'",
                            "type": "authentication_error",
                        }
                    },
                    headers={"WWW-Authenticate": "Bearer"},
                )
        except ValueError:
            raise HTTPException(
                status_code=401,
                detail={
                    "error": {
                        "message": "Authentication error: Malformed Authorization header",
                        "type": "authentication_error",
                    }
                },
                headers={"WWW-Authenticate": "Bearer"},
            )

        return HTTPAuthorizationCredentials(scheme=scheme, credentials=credentials)


security = CustomHTTPBearer(bearerFormat="Bearer", description="Your API key")


async def get_api_key(
    credentials: HTTPAuthorizationCredentials = Depends(security),
) -> str:
    """Extracts and validates the API key from the authorization header"""
    return credentials.credentials


def normalize_role(role: str):
    if role == "user":
        return "user"
    elif role == "assistant":
        return "bot"
    elif role == "system":
        return "system"
    else:
        return role


def parse_poe_error(error: Exception) -> tuple[str, dict, str, str]:
    """
    Parse error information from Poe API errors.

    Returns a tuple of:
        - Error message
        - Error data (JSON object or None)
        - Error type
        - Error ID (if available)
    """
    error_message = str(error)
    error_data = None
    error_type = "server_error"
    error_id = None

    try:
        error_str = str(error)

        # Case 1: Error is a JSON object string
        if error_str.startswith("{") and error_str.endswith("}"):
            error_data = json.loads(error_str)
            error_message = error_data.get("text", error_str)
            error_type = "poe_api_error"

        # Case 2: Error is BotError format with embedded JSON
        elif "BotError('" in error_str and "')" in error_str:
            json_part = error_str.split("BotError('", 1)[1].rsplit("')", 1)[0]
            try:
                error_data = json.loads(json_part)
                error_message = error_data.get("text", error_str)
                error_type = "poe_api_error"
            except json.JSONDecodeError:
                pass

        # Extract error_id if available
        if "error_id:" in error_message:
            try:
                error_id = error_message.split("error_id:", 1)[1].strip().rstrip(")")
            except Exception:
                pass

        # Determine error type based on message content
        if isinstance(error, ValueError) and "Model" in error_str:
            error_type = "model_not_found"
        elif "Internal server error" in error_message:
            error_type = "poe_server_error"

    except json.JSONDecodeError:
        pass
    except Exception:
        # If any unexpected error occurs during parsing, use the original error message
        pass

    return error_message, error_data, error_type, error_id


async def process_base64_image(data_url: str, api_key: str) -> fp.Attachment:
    """Convert base64 data URL to Poe attachment"""
    try:
        # Parse data URL: data:image/jpeg;base64,/9j/4AAQ...
        if not data_url.startswith("data:"):
            raise ValueError("Invalid data URL format")

        header, data = data_url.split(";base64,", 1)
        mime_type = header[5:]  # Remove 'data:'

        # Decode base64 data
        file_data = base64.b64decode(data)

        # Determine file extension from MIME type
        extension_map = {
            "image/jpeg": "jpg",
            "image/png": "png",
            "image/webp": "webp",
            "image/gif": "gif",
            "application/pdf": "pdf",
        }

        extension = extension_map.get(mime_type, "bin")
        file_name = f"uploaded_file.{extension}"

        # Upload to Poe using raw bytes
        attachment = await fp.upload_file(
            file=file_data, file_name=file_name, api_key=api_key
        )

        return attachment

    except Exception as e:
        raise ValueError(f"Failed to process base64 image: {str(e)}")


async def process_image_url(url: str, api_key: str) -> fp.Attachment:
    """Convert image URL to Poe attachment"""
    try:
        # Upload via URL (Poe will download it)
        attachment = await fp.upload_file(file_url=url, api_key=api_key)

        return attachment

    except Exception as e:
        raise ValueError(f"Failed to process image URL: {str(e)}")


async def convert_openai_content_to_poe(
    content: List[Dict], api_key: str
) -> tuple[str, List[fp.Attachment]]:
    """
    Convert OpenAI message content array to Poe format.
    Returns (text_content, attachments_list)
    """
    text_parts = []
    attachments = []

    for comp in content:
        if isinstance(comp, dict):
            if comp.get("type") == "text" and "text" in comp:
                text_parts.append(comp["text"])

            elif comp.get("type") == "image_url":
                image_url_obj = comp.get("image_url", {})
                url = image_url_obj.get("url", "")

                if url:
                    try:
                        if url.startswith("data:"):
                            # Base64 encoded image
                            attachment = await process_base64_image(url, api_key)
                            attachments.append(attachment)
                            text_parts.append(f"[Uploaded Image: {attachment.name}]")
                        else:
                            # External URL
                            attachment = await process_image_url(url, api_key)
                            attachments.append(attachment)
                            text_parts.append(f"[Image from URL: {attachment.name}]")
                    except ValueError as e:
                        # Fallback to text representation if upload fails
                        text_parts.append(f"[Image (upload failed): {url}]")
                        print(f"Warning: {e}")

            elif comp.get("type") == "image":
                # Handle legacy "image" type
                image_url = comp.get("image_url", "")
                if image_url:
                    try:
                        if image_url.startswith("data:"):
                            attachment = await process_base64_image(image_url, api_key)
                            attachments.append(attachment)
                            text_parts.append(f"[Uploaded Image: {attachment.name}]")
                        else:
                            attachment = await process_image_url(image_url, api_key)
                            attachments.append(attachment)
                            text_parts.append(f"[Image from URL: {attachment.name}]")
                    except ValueError as e:
                        text_parts.append(f"[Image: {image_url}]")
                        print(f"Warning: {e}")

    return " ".join(text_parts), attachments


def count_tokens(text: str, model: str = None) -> int:
    """Count the number of tokens in a string using the tiktoken library

    Uses cl100k_base tokenizer for all models for consistency and simplicity.
    """
    try:
        # Use cl100k_base tokenizer for all models (used by OpenAI and compatible with Claude)
        encoding = tiktoken.get_encoding("cl100k_base")
        return len(encoding.encode(text))
    except Exception as e:
        # Return an approximation if tiktoken fails
        return len(text) // 4


def count_message_tokens(
    messages: list[fp.ProtocolMessage], model: str = None
) -> Dict[str, int]:
    """Count tokens in a list of messages and return prompt and completion token counts

    Uses a consistent approach for all models.
    """
    prompt_tokens = 0
    completion_tokens = 0

    for msg in messages:
        # Count each message based on its role
        msg_content = msg.content if hasattr(msg, "content") else ""
        token_count = count_tokens(msg_content)

        if msg.role == "bot" or msg.role == "assistant":
            completion_tokens += token_count
        else:
            prompt_tokens += token_count

    # Add a small overhead for formatting (consistent with OpenAI's approach)
    prompt_tokens += 3

    return {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }


@app.post("/chat/completions")
@app.post("/v1/chat/completions")
@app.post("//v1/chat/completions")
async def chat_completions(
    request: ChatCompletionRequest, api_key: str = Depends(get_api_key)
):

    try:
        # Handle tool calling requests
        if request.tools:
            handler = FakeToolCallHandler()
            return await handler.process_request(request, api_key)

        # Validate model first
        model = normalize_model(request.model)

        # Validate messages
        if not request.messages:
            raise HTTPException(
                status_code=400,
                detail={
                    "error": {
                        "message": "Messages array cannot be empty",
                        "type": "invalid_request_error",
                        "param": "messages",
                    }
                },
            )

        # Prepare messages for the API call
        messages = []
        for msg in request.messages:
            role = normalize_role(msg.role)
            content = msg.content or ""
            attachments = []

            if isinstance(content, list):
                # Handle multimodal content with files/images
                try:
                    text_content, file_attachments = (
                        await convert_openai_content_to_poe(content, api_key)
                    )
                    content = text_content
                    attachments = file_attachments
                except Exception as e:
                    # Fallback to simple text extraction if file processing fails
                    print(f"Warning: File processing failed: {e}")
                    parts = []
                    for comp in content:
                        if isinstance(comp, dict):
                            if comp.get("type") == "text" and "text" in comp:
                                parts.append(comp["text"])
                            elif comp.get("type") == "image_url":
                                parts.append(
                                    f"[Image: {comp.get('image_url', {}).get('url', '')}]"
                                )
                            elif comp.get("type") == "image":
                                parts.append(f"[Image: {comp.get('image_url', '')}]")
                    content = " ".join(parts)

            # Create ProtocolMessage with or without attachments
            if attachments:
                messages.append(
                    fp.ProtocolMessage(
                        role=role, content=content, attachments=attachments
                    )
                )
            else:
                messages.append(fp.ProtocolMessage(role=role, content=content))

        # If streaming is requested, use StreamingResponse
        if request.stream:
            headers = {
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "Transfer-Encoding": "chunked",
                "X-Accel-Buffering": "no",
            }
            return StreamingResponse(
                stream_openai_format(request.model, messages, api_key),
                headers=headers,
                media_type="text/event-stream",
            )

        # For non-streaming, accumulate the full response
        response = await generate_poe_bot_response_with_files(
            request.model, messages, api_key
        )

        # Calculate token counts
        token_counts = count_message_tokens(messages)
        response_tokens = count_tokens(
            response["content"] if isinstance(response, dict) else ""
        )
        token_counts["completion_tokens"] = response_tokens
        token_counts["total_tokens"] = token_counts["prompt_tokens"] + response_tokens

        # Set finish reason to stop
        finish_reason = "stop"

        completion_response = {
            "id": "chatcmpl-" + os.urandom(12).hex(),
            "object": "chat.completion",
            "created": int(time.time()),
            "model": request.model,
            "choices": [
                {
                    "index": 0,
                    "message": {
                        "role": "assistant",
                        "content": (
                            response["content"]
                            if isinstance(response, dict)
                            else str(response)
                        ),
                    },
                    "finish_reason": finish_reason,
                }
            ],
            "usage": token_counts,
        }

        return completion_response

    except Exception as e:

        # Default error values
        status_code = 500
        error_message = str(e)
        error_type = "server_error"

        # Handle different error types
        if isinstance(e, HTTPException):
            raise e
        elif isinstance(e, PoeAPIError):
            # Use the structured error data from Poe
            error_message = e.message
            error_type = "poe_api_error"
            status_code = e.status_code
            # Include the original error data if available
            if e.error_data:
                error_detail = {
                    "error": {
                        "message": error_message,
                        "type": error_type,
                        "poe_error": e.error_data,
                    }
                }
                # Add error_id if available
                if e.error_id:
                    error_detail["error"]["error_id"] = e.error_id
                raise HTTPException(status_code=status_code, detail=error_detail)
        elif isinstance(e, ValueError):
            if "Model" in str(e):
                status_code = 404
                error_type = "invalid_request_error"
                error_message = str(e)
            else:
                status_code = 400
                error_type = "invalid_request_error"
        else:
            # Use the helper function to parse error information
            error_message, error_data, error_type, error_id = parse_poe_error(e)

            if error_data:
                error_detail = {
                    "error": {
                        "message": error_message,
                        "type": error_type,
                        "poe_error": error_data,
                    }
                }

                if error_id:
                    error_detail["error"]["error_id"] = error_id

                raise HTTPException(status_code=status_code, detail=error_detail)

        # Default error response
        raise HTTPException(
            status_code=status_code,
            detail={"error": {"message": error_message, "type": error_type}},
        )


async def stream_response(
    model: str, messages: list[fp.ProtocolMessage], api_key: str, format_type: str
):
    """Common streaming function for all response types"""
    model = normalize_model(model)
    first_chunk = True
    accumulated_response = ""

    # Calculate prompt tokens before starting stream
    token_counts = count_message_tokens(messages)

    try:
        async for message in get_bot_response(
            messages=messages, bot_name=model, api_key=api_key, skip_system_prompt=True
        ):
            chunk = await create_stream_chunk(
                message.text, model, format_type, first_chunk
            )
            accumulated_response += message.text  # Accumulate the full response text
            yield f"data: {json.dumps(chunk)}\n\n".encode("utf-8")
            first_chunk = False
            await asyncio.sleep(0)  # Allow event loop to process

        # Calculate completion tokens from accumulated response
        completion_tokens = count_tokens(accumulated_response)
        token_counts["completion_tokens"] = completion_tokens
        token_counts["total_tokens"] = token_counts["prompt_tokens"] + completion_tokens

        # Send final message with token counts
        final_chunk = await create_final_chunk(model, format_type, token_counts)
        yield f"data: {json.dumps(final_chunk)}\n\n".encode("utf-8")

        if format_type in ["completion", "chat"]:
            yield b"data: [DONE]\n\n"

    except Exception as e:

        # Use the helper function to parse error information
        error_message, error_data, error_type, error_id = parse_poe_error(e)

        # Add token counts to error response if available
        if accumulated_response:
            # Calculate completion tokens from accumulated response
            completion_tokens = count_tokens(accumulated_response)
            token_counts["completion_tokens"] = completion_tokens
            token_counts["total_tokens"] = (
                token_counts["prompt_tokens"] + completion_tokens
            )

        error_data = {
            "error": {"message": error_message, "type": error_type, "code": error_type}
        }

        # Add error_id if available
        if error_id:
            error_data["error"]["error_id"] = error_id

        # Add token counts if available and we had some response before the error
        if accumulated_response:
            error_data["usage"] = token_counts

        yield f"data: {json.dumps(error_data)}\n\n".encode("utf-8")
        if format_type in ["completion", "chat"]:
            yield b"data: [DONE]\n\n"


async def create_stream_chunk(
    message_text: str,
    model: str,
    format_type: str,
    is_first_chunk: bool = False,
    is_replace_response: bool = False,
):
    """Common function to create streaming response chunks"""
    chunk_id = os.urandom(12).hex()
    timestamp = int(time.time())

    if format_type == "completion":
        return {
            "id": f"cmpl-{chunk_id}",
            "object": "text_completion",
            "created": timestamp,
            "model": model,
            "choices": [
                {
                    "text": message_text,
                    "index": 0,
                    "logprobs": None,
                    "finish_reason": None,
                }
            ],
        }
    elif format_type == "chat":
        return {
            "id": f"chatcmpl-{chunk_id}",
            "object": "chat.completion.chunk",
            "created": timestamp,
            "model": model,
            "choices": [
                {
                    "index": 0,
                    "delta": {
                        **({"role": "assistant"} if is_first_chunk else {}),
                        # When is_replace_response is True, we need to explicitly include content
                        # to signal to the client to replace previous content
                        "content": message_text,
                    },
                    "finish_reason": None,
                    "logprobs": None,
                }
            ],
        }
    else:  # poe format
        return {
            "response": message_text,
            "done": False,
            "is_replace": is_replace_response,
        }


async def create_final_chunk(
    model: str, format_type: str, token_counts: Optional[Dict[str, int]] = None
):
    """Common function to create final streaming chunks"""
    chunk_id = os.urandom(12).hex()
    timestamp = int(time.time())

    if format_type == "completion":
        result = {
            "id": f"cmpl-{chunk_id}",
            "object": "text_completion",
            "created": timestamp,
            "model": model,
            "choices": [
                {"text": "", "index": 0, "logprobs": None, "finish_reason": "stop"}
            ],
        }
        if token_counts:
            result["usage"] = token_counts
        return result
    elif format_type == "chat":
        result = {
            "id": f"chatcmpl-{chunk_id}",
            "object": "chat.completion.chunk",
            "created": timestamp,
            "model": model,
            "choices": [
                {"index": 0, "logprobs": None, "delta": {}, "finish_reason": "stop"}
            ],
        }
        if token_counts:
            result["usage"] = token_counts
        return result
    else:  # poe format
        result = {"response": "", "done": True}
        if token_counts:
            result["usage"] = token_counts
        return result


async def stream_response_with_replace(
    model: str, messages: list[fp.ProtocolMessage], api_key: str, format_type: str
):
    """Common streaming function for all response types with replace support"""
    model = normalize_model(model)
    first_chunk = True
    accumulated_response = ""

    # Calculate prompt tokens before starting stream
    token_counts = count_message_tokens(messages)

    try:
        async for message in get_bot_response(
            messages=messages,
            bot_name=model,
            api_key=api_key,
            skip_system_prompt=True,
        ):
            # Check if message should replace previous content
            is_replace_response = getattr(message, "is_replace_response", False)

            # If this is a replace message, reset accumulated response
            if is_replace_response:
                accumulated_response = ""  # Reset accumulated response

            # Handle attachment URLs
            message_text = message.text
            if message.attachment:
                message_text += f"\n{message.attachment.url}"

            chunk = await create_stream_chunk(
                message_text, model, format_type, first_chunk, is_replace_response
            )

            # Accumulate the text (this starts fresh if we just reset)
            accumulated_response += message_text

            yield f"data: {json.dumps(chunk)}\n\n".encode("utf-8")
            first_chunk = False
            await asyncio.sleep(0)  # Allow event loop to process

        # Calculate completion tokens from accumulated response
        completion_tokens = count_tokens(accumulated_response)
        token_counts["completion_tokens"] = completion_tokens
        token_counts["total_tokens"] = token_counts["prompt_tokens"] + completion_tokens

        # Send final message with token counts
        final_chunk = await create_final_chunk(model, format_type, token_counts)
        yield f"data: {json.dumps(final_chunk)}\n\n".encode("utf-8")

        if format_type in ["completion", "chat"]:
            yield b"data: [DONE]\n\n"

    except Exception as e:

        # Use the helper function to parse error information
        error_message, error_data, error_type, error_id = parse_poe_error(e)

        # Add token counts to error response if available
        if accumulated_response:
            # Calculate completion tokens from accumulated response
            completion_tokens = count_tokens(accumulated_response)
            token_counts["completion_tokens"] = completion_tokens
            token_counts["total_tokens"] = (
                token_counts["prompt_tokens"] + completion_tokens
            )

        error_data = {
            "error": {"message": error_message, "type": error_type, "code": error_type}
        }

        # Add error_id if available
        if error_id:
            error_data["error"]["error_id"] = error_id

        # Add token counts if available and we had some response before the error
        if accumulated_response:
            error_data["usage"] = token_counts

        yield f"data: {json.dumps(error_data)}\n\n".encode("utf-8")
        if format_type in ["completion", "chat"]:
            yield b"data: [DONE]\n\n"


# Only keep one stream_openai_format function


async def stream_completions_format(
    model: str, messages: list[fp.ProtocolMessage], api_key: str
):
    async for chunk in stream_response(model, messages, api_key, "completion"):
        yield chunk


async def stream_completions_format_with_files(
    model: str, messages: list[fp.ProtocolMessage], api_key: str
):
    model = normalize_model(model)
    first_chunk = True
    accumulated_response = ""

    token_counts = count_message_tokens(messages)

    try:
        async for message in get_bot_response(
            messages=messages, bot_name=model, api_key=api_key, skip_system_prompt=True
        ):
            message_text = message.text

            if message.attachment:
                message_text += f"\n{message.attachment.url}"

            chunk = await create_stream_chunk(
                message_text, model, "completion", first_chunk
            )
            accumulated_response += message_text
            yield f"data: {json.dumps(chunk)}\n\n".encode("utf-8")
            first_chunk = False
            await asyncio.sleep(0)

        completion_tokens = count_tokens(accumulated_response)
        token_counts["completion_tokens"] = completion_tokens
        token_counts["total_tokens"] = token_counts["prompt_tokens"] + completion_tokens

        final_chunk = await create_final_chunk("completion", "completion", token_counts)
        yield f"data: {json.dumps(final_chunk)}\n\n".encode("utf-8")
        yield b"data: [DONE]\n\n"

    except Exception as e:
        error_message, error_data, error_type, error_id = parse_poe_error(e)

        if accumulated_response:
            completion_tokens = count_tokens(accumulated_response)
            token_counts["completion_tokens"] = completion_tokens
            token_counts["total_tokens"] = (
                token_counts["prompt_tokens"] + completion_tokens
            )

        error_data = {
            "error": {"message": error_message, "type": error_type, "code": error_type}
        }

        if error_id:
            error_data["error"]["error_id"] = error_id

        if accumulated_response:
            error_data["usage"] = token_counts

        yield f"data: {json.dumps(error_data)}\n\n".encode("utf-8")
        yield b"data: [DONE]\n\n"


# Mount the static directory
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", response_class=HTMLResponse)
async def root():
    # Serve the static HTML file
    return FileResponse("static/index.html")


@app.get("/v1/", response_class=HTMLResponse)
async def v1_root():
    # Also serve the same HTML file for the /v1/ endpoint
    return FileResponse("static/index.html")


# Simple exception handler without logging
@app.exception_handler(Exception)
async def global_exception_handler(request: Request, exc: Exception):
    # For HTTPExceptions, return their predefined responses
    if isinstance(exc, HTTPException):
        return JSONResponse(
            status_code=exc.status_code,
            content=(
                {"error": exc.detail}
                if not isinstance(exc.detail, dict)
                else exc.detail
            ),
        )

    # For other exceptions, return a 500 error
    return JSONResponse(
        status_code=500,
        content={
            "error": {
                "message": f"An unexpected error occurred: {str(exc)}",
                "type": "server_error",
            }
        },
    )


@app.get("/models")
@app.get("/v1/models")
@app.get("//v1/models")  # Handle double slash case like other endpoints
async def list_models_openai():
    # Return list of available models in OpenAI-compatible format
    model_configs = [
        {"id": "Claude-Sonnet-4", "context_window": 200000},
        {"id": "Claude-Opus-4", "context_window": 200000},
        {"id": "Claude-3.7-Sonnet", "context_window": 200000},
        {"id": "Claude-3.5-Sonnet", "context_window": 200000},
        # GPT models
        {"id": "GPT-4o", "context_window": 128000},
        {"id": "GPT-4o-mini", "context_window": 128000},
        # Gemini models
        {"id": "Gemini-2.0-Flash", "context_window": 1000000},
        {"id": "Gemini-2.5-Pro-Exp", "context_window": 1000000},
    ]

    # Create timestamp for all models
    creation_time = int(datetime.now().timestamp())

    # Convert model configs to OpenAI-compatible model objects with limited capabilities
    model_objects = []
    for config in model_configs:
        model_id = config["id"]
        context_window = config["context_window"]

        model_objects.append(
            {
                "id": model_id,
                "object": "model",
                "created": creation_time,
                "owned_by": "poe-api-bridge",
                "context_window": context_window,
                "permission": [
                    {
                        "id": f"modelperm-{model_id.lower().replace('-', '')}",
                        "object": "model_permission",
                        "created": creation_time,
                        "allow_create_engine": False,
                        "allow_sampling": False,
                        "allow_logprobs": False,
                        "allow_search_indices": False,
                        "allow_view": True,
                        "allow_fine_tuning": False,
                        "organization": "*",
                        "group": None,
                        "is_blocking": False,
                    }
                ],
            }
        )

    return {
        "object": "list",
        "data": model_objects,
    }


@app.post("/completions")
@app.post("/v1/completions")
@app.post("//v1/completions")
async def completions(request: Request, api_key: str = Depends(get_api_key)):
    body = await request.json()

    messages = [fp.ProtocolMessage(role="user", content=body.get("prompt", ""))]
    model = body.get("model")
    stream = body.get("stream", False)

    if stream:
        return StreamingResponse(
            stream_completions_format_with_files(model, messages, api_key),
            media_type="text/event-stream",
        )

    # For non-streaming requests, accumulate the full response
    response = await generate_poe_bot_response_with_files(model, messages, api_key)

    # Calculate token counts
    prompt_tokens = count_tokens(body.get("prompt", ""))
    completion_tokens = count_tokens(response.get("content", ""))
    token_usage = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": prompt_tokens + completion_tokens,
    }

    return {
        "id": "cmpl-" + os.urandom(12).hex(),
        "object": "text_completion",
        "created": int(time.time()),
        "model": model,
        "choices": [
            {
                "text": response.get("content", ""),
                "index": 0,
                "logprobs": None,
                "finish_reason": "stop",
            }
        ],
        "usage": token_usage,
    }


async def get_first_file_from_bot(
    model: str, messages: list[fp.ProtocolMessage], api_key: str
):
    first_file = None
    async for message in get_bot_response(
        messages=messages,
        bot_name=model,
        api_key=api_key,
        skip_system_prompt=True,
    ):
        if message.attachment and not first_file:
            first_file = message.attachment
            break  # Only need the first file
    return first_file


@app.post("/images/generations")
@app.post("/v1/images/generations")
@app.post("//v1/images/generations")
async def image_generations(
    request: ImageGenerationRequest, api_key: str = Depends(get_api_key)
):
    try:
        model = normalize_model(request.model or "Imagen-3-Fast")
        num_images = max(1, min(request.n or 1, 10))  # Limit to reasonable range

        messages = [fp.ProtocolMessage(role="user", content=request.prompt)]

        # Generate multiple images by making multiple requests
        data = []
        successful_generations = 0

        for i in range(num_images):
            try:
                file_result = await get_first_file_from_bot(model, messages, api_key)

                if file_result:
                    if request.response_format == "b64_json":
                        async with httpx.AsyncClient() as client:
                            img_response = await client.get(file_result.url)
                            img_base64 = base64.b64encode(img_response.content).decode()
                            data.append({"b64_json": img_base64})
                    else:
                        data.append({"url": file_result.url})

                    successful_generations += 1
                else:
                    print(f"Warning: Failed to generate image {i+1}/{num_images}")

            except Exception as e:
                print(f"Warning: Error generating image {i+1}/{num_images}: {e}")
                continue

        if successful_generations > 0:
            return {"created": int(time.time()), "data": data}

    except Exception as e:
        error_message, error_data, error_type, error_id = parse_poe_error(e)
        raise HTTPException(
            status_code=500,
            detail={"error": {"message": error_message, "type": error_type}},
        )

    # If we get here, no file was returned
    raise HTTPException(
        status_code=500,
        detail={
            "error": {
                "message": "Failed to generate image - no file returned from bot",
                "type": "image_generation_error",
            }
        },
    )


@app.post("/images/edits")
@app.post("/v1/images/edits")
@app.post("//v1/images/edits")
async def image_edits(
    image: UploadFile = File(...),
    prompt: str = Form(...),
    model: Optional[str] = Form(None),
    n: Optional[int] = Form(1),
    size: Optional[str] = Form(None),
    response_format: Optional[str] = Form("url"),
    mask: Optional[UploadFile] = File(None),
    api_key: str = Depends(get_api_key),
):
    try:
        model = normalize_model(model or "StableDiffusionXL")
        num_images = max(1, min(n or 1, 10))  # Limit to reasonable range

        # Read the image file and convert to base64
        image_content = await image.read()
        image_b64 = base64.b64encode(image_content).decode()

        # Create OpenAI-style multimodal content
        openai_content = [
            {"type": "text", "text": prompt},
            {
                "type": "image_url",
                "image_url": {"url": f"data:image/jpeg;base64,{image_b64}"},
            },
        ]

        # Convert OpenAI content to Poe format using existing function
        poe_content, attachments = await convert_openai_content_to_poe(
            openai_content, api_key
        )

        # Create ProtocolMessage with converted content and attachments
        if attachments:
            messages = [
                fp.ProtocolMessage(
                    role="user", content=poe_content, attachments=attachments
                )
            ]
        else:
            messages = [fp.ProtocolMessage(role="user", content=poe_content)]

        # Generate multiple images by making multiple requests
        data = []
        successful_generations = 0

        for i in range(num_images):
            try:
                file_result = await get_first_file_from_bot(model, messages, api_key)

                if file_result:
                    if response_format == "b64_json":
                        async with httpx.AsyncClient() as client:
                            img_response = await client.get(file_result.url)
                            img_base64 = base64.b64encode(img_response.content).decode()
                            data.append({"b64_json": img_base64})
                    else:
                        data.append({"url": file_result.url})

                    successful_generations += 1
                else:
                    print(f"Warning: Failed to generate image {i+1}/{num_images}")

            except Exception as e:
                print(f"Warning: Error generating image {i+1}/{num_images}: {e}")
                continue

        if successful_generations > 0:
            return {"created": int(time.time()), "data": data}

    except Exception as e:
        error_message, error_data, error_type, error_id = parse_poe_error(e)
        raise HTTPException(
            status_code=500,
            detail={"error": {"message": error_message, "type": error_type}},
        )

    # If we get here, no file was returned
    raise HTTPException(
        status_code=500,
        detail={
            "error": {
                "message": "Failed to edit image - no file returned from bot",
                "type": "image_edit_error",
            }
        },
    )


async def generate_poe_bot_response_with_files(
    model, messages: list[fp.ProtocolMessage], api_key: str
):
    model = normalize_model(model)
    accumulated_text = ""
    received_files = []

    try:
        response = {"role": "assistant", "content": ""}

        async for message in get_bot_response(
            messages=messages,
            bot_name=model,
            api_key=api_key,
            skip_system_prompt=True,
        ):
            is_replace_response = getattr(message, "is_replace_response", False)

            if is_replace_response:
                accumulated_text = ""

            # Just accumulate text, handle attachments separately
            accumulated_text += message.text

            # Collect attachments separately
            if message.attachment:
                received_files.append(message.attachment)

            response["content"] = accumulated_text

    except Exception as e:
        error_message, error_data, error_type, error_id = parse_poe_error(e)

        if error_data:
            raise PoeAPIError(
                f"Poe API Error: {error_message}",
                error_data=error_data,
                error_id=error_id,
            )

        raise

    # Add all attachment URLs at the end
    if received_files:
        for attachment in received_files:
            response["content"] += f"\n{attachment.url}\n"

    return response


async def generate_poe_bot_response(
    model, messages: list[fp.ProtocolMessage], api_key: str
):
    model = normalize_model(model)
    accumulated_text = ""

    try:
        response = {"role": "assistant", "content": ""}

        async for message in get_bot_response(
            messages=messages,
            bot_name=model,
            api_key=api_key,
            skip_system_prompt=True,
        ):
            # Check if message should replace previous content
            is_replace_response = getattr(message, "is_replace_response", False)

            # If this is a replace message, reset accumulated response
            if is_replace_response:
                accumulated_text = ""  # Reset accumulated text

            # Accumulate the text (will start fresh if is_replace_response was True)
            accumulated_text += message.text
            response["content"] = accumulated_text

    except Exception as e:
        # Use the helper function to parse error information
        error_message, error_data, error_type, error_id = parse_poe_error(e)

        if error_data:
            raise PoeAPIError(
                f"Poe API Error: {error_message}",
                error_data=error_data,
                error_id=error_id,
            )

        # If we couldn't parse a structured error, just raise the original
        raise

    return response


async def stream_openai_format(
    model: str, messages: list[fp.ProtocolMessage], api_key: str
):
    async for chunk in stream_response_with_replace(model, messages, api_key, "chat"):
        yield chunk


@app.get("/openapi.json")
async def get_openapi_json():
    if app.openapi_schema:
        return app.openapi_schema

    openapi_schema = get_openapi(
        title="Poe-API OpenAI Bridge",
        version="1.0.0",
        description="A proxy server for Poe API that provides OpenAI-compatible endpoints",
        routes=app.routes,
    )

    # Customize the schema as needed
    openapi_schema["info"]["x-logo"] = {"url": "https://poe.com/favicon.ico"}

    app.openapi_schema = openapi_schema
    return app.openapi_schema
