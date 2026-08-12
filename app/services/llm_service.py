"""LLM abstraction (Phase 15 / Section 21).

The system talks to an interface, never to a vendor SDK. Two implementations
ship:

* `DeterministicLLMService` - no network, no key, used in demo mode and tests.
* `LangChainLLMService`     - a real chat model behind LangChain, selected by
                              `LLM_PROVIDER` and constructed lazily so that the
                              provider package is only needed when it is used.

The contract is narrow on purpose: the caller hands over a system prompt and a
JSON evidence bundle, and gets prose back. There is no path by which a model
response can become a decision - `generate_narrative` returns a string, and the
explanation service copies decision fields from the deterministic result.
"""

from __future__ import annotations

import json
from abc import ABC, abstractmethod
from typing import Any

from app.config import Settings, get_settings
from app.domain.exceptions import LlmError
from app.logging import get_logger

logger = get_logger(__name__)


class LLMService(ABC):
    """Vendor-neutral text generation interface."""

    name: str = "abstract"
    model: str | None = None

    @property
    def available(self) -> bool:
        return False

    @abstractmethod
    def generate_narrative(
        self, *, system_prompt: str, evidence: dict[str, Any], max_tokens: int | None = None
    ) -> str:
        """Turn a structured evidence bundle into prose."""


class DeterministicLLMService(LLMService):
    """A no-op implementation.

    It never claims to be available, which makes the explanation service fall
    back to its template generator. Keeping it as a real implementation (rather
    than `None`) means every call site has one code path, not two.
    """

    name = "deterministic"

    @property
    def available(self) -> bool:
        return False

    def generate_narrative(
        self, *, system_prompt: str, evidence: dict[str, Any], max_tokens: int | None = None
    ) -> str:
        raise LlmError("No LLM provider is configured; use the deterministic explainer.")


class LangChainLLMService(LLMService):
    """LangChain-backed chat model.

    The provider package is imported lazily so that an unused provider never
    becomes an install-time dependency.
    """

    name = "langchain"

    def __init__(self, settings: Settings | None = None) -> None:
        self.settings = settings or get_settings()
        self.model = self.settings.llm_model
        self._client: Any | None = None
        self._init_error: str | None = None

    @property
    def available(self) -> bool:
        return self.settings.llm_enabled

    def _build_client(self) -> Any:
        if self._client is not None:
            return self._client
        if self._init_error is not None:
            raise LlmError(self._init_error)

        provider = self.settings.llm_provider
        try:
            if provider == "anthropic":
                from langchain_anthropic import ChatAnthropic

                self._client = ChatAnthropic(
                    model=self.settings.llm_model,
                    api_key=self.settings.llm_api_key,
                    temperature=self.settings.llm_temperature,
                    max_tokens=self.settings.llm_max_tokens,
                    timeout=self.settings.llm_timeout_seconds,
                )
            elif provider == "openai":
                from langchain_openai import ChatOpenAI

                self._client = ChatOpenAI(
                    model=self.settings.llm_model,
                    api_key=self.settings.llm_api_key,
                    temperature=self.settings.llm_temperature,
                    max_tokens=self.settings.llm_max_tokens,
                    timeout=self.settings.llm_timeout_seconds,
                )
            else:
                raise LlmError(f"Unsupported LLM provider '{provider}'.")
        except ImportError as exc:
            self._init_error = (
                f"LLM provider '{provider}' is selected but its package is not installed: {exc}"
            )
            logger.warning("llm.provider_missing", provider=provider)
            raise LlmError(self._init_error) from exc
        return self._client

    def generate_narrative(
        self, *, system_prompt: str, evidence: dict[str, Any], max_tokens: int | None = None
    ) -> str:
        if not self.available:
            raise LlmError("LLM is disabled (demo mode, missing key, or provider 'none').")

        from langchain_core.messages import HumanMessage, SystemMessage

        client = self._build_client()
        payload = json.dumps(evidence, indent=2, default=str)
        messages = [
            SystemMessage(content=system_prompt),
            HumanMessage(
                content=(
                    "Write the explanation for the following access-governance evidence. "
                    "Use only the facts present in this JSON.\n\n"
                    f"```json\n{payload}\n```"
                )
            ),
        ]
        try:
            response = client.invoke(messages)
        except Exception as exc:
            logger.warning("llm.invoke_failed", error=str(exc), provider=self.settings.llm_provider)
            raise LlmError(f"LLM invocation failed: {exc}") from exc

        text = getattr(response, "content", None)
        if isinstance(text, list):  # some providers return content blocks
            text = "".join(
                block.get("text", "") if isinstance(block, dict) else str(block)
                for block in text
            )
        if not isinstance(text, str) or not text.strip():
            raise LlmError("LLM returned an empty response.")
        return text.strip()


def build_llm_service(settings: Settings | None = None) -> LLMService:
    """Select an implementation from configuration.

    Demo mode always yields the deterministic service, which is what keeps the
    governance workflow runnable and testable with no API key present.
    """
    settings = settings or get_settings()
    if settings.demo_mode or settings.llm_provider == "none" or not settings.llm_api_key:
        logger.info(
            "llm.deterministic_mode",
            demo_mode=settings.demo_mode,
            provider=settings.llm_provider,
        )
        return DeterministicLLMService()
    return LangChainLLMService(settings)
