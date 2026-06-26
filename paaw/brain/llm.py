"""
LLM Wrapper

Unified interface to any LLM provider via LiteLLM.
Handles retries, streaming, and model selection.
"""

import asyncio
from collections.abc import AsyncGenerator
from typing import Any

import structlog
from litellm import acompletion, aembedding
from litellm.exceptions import InternalServerError, RateLimitError, ServiceUnavailableError
from tenacity import (
    retry, 
    retry_if_exception_type, 
    stop_after_attempt, 
    wait_exponential,
    before_sleep_log,
)

from paaw.config import settings
from paaw.models import ChatMessage, MessageRole

logger = structlog.get_logger()


class LLMError(Exception):
    """Base exception for LLM errors."""

    pass


class LLM:
    """
    Unified LLM interface using LiteLLM.

    Supports:
    - OpenAI (GPT-4, GPT-4o, etc.)
    - Anthropic (Claude)
    - Local models via Ollama
    - Any LiteLLM-supported provider
    """

    def __init__(
        self,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ):
        self.model = model or settings.llm.default_model
        self.temperature = temperature or settings.llm.temperature
        self.max_tokens = max_tokens or settings.llm.max_tokens

        # Set API keys if available
        self._setup_api_keys()

    def _setup_api_keys(self) -> None:
        """Set up API keys from settings."""
        import os

        if settings.llm.openai_api_key:
            os.environ["OPENAI_API_KEY"] = settings.llm.openai_api_key

        if settings.llm.anthropic_api_key:
            os.environ["ANTHROPIC_API_KEY"] = settings.llm.anthropic_api_key

        if settings.llm.ollama_base_url:
            os.environ["OLLAMA_API_BASE"] = settings.llm.ollama_base_url

    def _format_messages(
        self,
        messages: list[ChatMessage] | list[dict] | str,
        system_prompt: str | None = None,
    ) -> list[dict[str, Any]]:
        """Convert messages to LiteLLM format with tool call support."""
        formatted = []

        # Add system prompt if provided
        if system_prompt:
            formatted.append({"role": "system", "content": system_prompt})

        # Handle string input (simple chat)
        if isinstance(messages, str):
            formatted.append({"role": "user", "content": messages})
            return formatted

        # Convert ChatMessage list or dict list
        for msg in messages:
            # Handle plain dict (already formatted)
            if isinstance(msg, dict):
                formatted.append(msg)
                continue
            
            # Handle ChatMessage objects
            msg_dict: dict[str, Any] = {"role": msg.role.value, "content": msg.content}
            
            # Handle tool calls (assistant requesting tools)
            if msg.tool_calls:
                msg_dict["tool_calls"] = msg.tool_calls
            
            # Handle tool responses
            if msg.tool_call_id:
                msg_dict["tool_call_id"] = msg.tool_call_id
            
            formatted.append(msg_dict)

        return formatted

    @retry(
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2, min=2, max=30),
        retry=retry_if_exception_type((
            ConnectionError, 
            TimeoutError, 
            InternalServerError,  # Anthropic overload (529)
            RateLimitError,       # Rate limits (429)
            ServiceUnavailableError,  # Service unavailable (503)
        )),
        reraise=True,
        before_sleep=before_sleep_log(logger, "warning"),
    )
    async def chat(
        self,
        messages: list[ChatMessage] | str,
        system_prompt: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
        tools: list[dict[str, Any]] | None = None,
        return_full_response: bool = False,
    ) -> str | dict:
        """
        Send a chat completion request.

        Args:
            messages: Either a string (user message) or list of ChatMessages
            system_prompt: Optional system prompt to prepend
            model: Override default model
            temperature: Override default temperature
            max_tokens: Override default max_tokens
            tools: Optional list of tools for function calling
            return_full_response: If True, return dict with content and tool_calls

        Returns:
            The assistant's response text (or dict if return_full_response=True)
        """
        formatted_messages = self._format_messages(messages, system_prompt)

        try:
            logger.info(
                "LLM request starting",
                model=model or self.model,
                message_count=len(formatted_messages),
                has_tools=bool(tools),
            )

            kwargs: dict[str, Any] = {
                "model": model or self.model,
                "messages": formatted_messages,
                "temperature": temperature or self.temperature,
                "max_tokens": max_tokens or self.max_tokens,
                "timeout": settings.llm.timeout,
            }

            if tools:
                kwargs["tools"] = tools

            response = await acompletion(**kwargs)
            
            message = response.choices[0].message
            content = message.content or ""
            tool_calls = getattr(message, 'tool_calls', None)

            logger.info(
                "LLM response received",
                model=response.model,
                tokens_used=response.usage.total_tokens if response.usage else None,
                has_tool_calls=bool(tool_calls),
                response_length=len(content),
            )

            if return_full_response:
                return {
                    "content": content,
                    "tool_calls": tool_calls,
                    "raw_message": message,
                    "usage": {
                        "prompt_tokens": response.usage.prompt_tokens,
                        "completion_tokens": response.usage.completion_tokens,
                        "total_tokens": response.usage.total_tokens,
                    } if response.usage else None,
                }
            
            return content

        except (InternalServerError, RateLimitError, ServiceUnavailableError) as e:
            # These will be retried by tenacity
            logger.warning(
                "LLM request failed (will retry)", 
                error=str(e), 
                model=model or self.model,
                error_type=type(e).__name__,
            )
            raise  # Let tenacity handle retry
            
        except Exception as e:
            logger.error(
                "LLM request failed (no retry)", 
                error=str(e), 
                model=model or self.model,
                error_type=type(e).__name__,
            )
            raise LLMError(f"LLM request failed: {e}") from e

    async def chat_stream(
        self,
        messages: list[ChatMessage] | str,
        system_prompt: str | None = None,
        model: str | None = None,
        temperature: float | None = None,
        max_tokens: int | None = None,
    ) -> AsyncGenerator[str, None]:
        """
        Stream a chat completion response.

        Yields chunks of the response as they arrive.
        """
        formatted_messages = self._format_messages(messages, system_prompt)

        try:
            response = await acompletion(
                model=model or self.model,
                messages=formatted_messages,
                temperature=temperature or self.temperature,
                max_tokens=max_tokens or self.max_tokens,
                timeout=settings.llm.timeout,
                stream=True,
            )

            async for chunk in response:
                if chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content

        except Exception as e:
            logger.error("LLM streaming error", error=str(e))
            raise LLMError(f"LLM streaming failed: {e}") from e

    async def embed(
        self,
        text: str | list[str],
        model: str | None = None,
    ) -> list[list[float]]:
        """
        Generate embeddings for text (optional feature).
        
        NOTE: Embeddings are optional in PAAW. For Phase 1-2, we use
        graph structure + LLM classification for memory retrieval.
        Embeddings will be added in Phase 3 for semantic search.

        Args:
            text: Single string or list of strings to embed
            model: Embedding model to use

        Returns:
            List of embedding vectors
            
        Raises:
            LLMError: If no embedding model is configured
        """
        embedding_model = model or settings.llm.embedding_model
        
        if not embedding_model:
            raise LLMError(
                "No embedding model configured. "
                "Set LLM_EMBEDDING_MODEL in .env or use graph-based retrieval instead."
            )
        
        if isinstance(text, str):
            text = [text]

        try:
            response = await aembedding(
                model=embedding_model,
                input=text,
            )

            return [item["embedding"] for item in response.data]

        except Exception as e:
            logger.error("Embedding error", error=str(e))
            raise LLMError(f"Embedding failed: {e}") from e

    async def embed_single(self, text: str, model: str | None = None) -> list[float]:
        """Generate embedding for a single text."""
        embeddings = await self.embed(text, model)
        return embeddings[0]
    
    def has_embedding_support(self) -> bool:
        """Check if embedding model is configured."""
        return settings.llm.embedding_model is not None


# Convenience function for quick chats
async def quick_chat(message: str, system_prompt: str | None = None) -> str:
    """Quick one-off chat without creating an LLM instance."""
    llm = LLM()
    return await llm.chat(message, system_prompt=system_prompt)


async def call_llm(
    messages: list[dict],
    temperature: float = 0.7,
    max_tokens: int = 1500,
) -> dict:
    """
    Call LLM with messages and return response with content.
    
    This is a convenience function for Server Room that returns
    a dict with 'content' key for compatibility.
    """
    llm = LLM(temperature=temperature, max_tokens=max_tokens)
    
    # Format messages for the LLM
    formatted_messages = []
    for msg in messages:
        formatted_messages.append(msg)
    
    result = await llm.chat(
        messages=formatted_messages,
        temperature=temperature,
        max_tokens=max_tokens,
        return_full_response=True,
    )
    
    # Ensure we return a dict with 'content'
    if isinstance(result, dict):
        return result
    return {"content": result}
