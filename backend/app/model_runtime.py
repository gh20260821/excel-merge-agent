from __future__ import annotations

from pathlib import Path
import re
from typing import Any

import ai
import openai
from pydantic import BaseModel

from .domain import MergeSpec, PlannerProvenance
from .model_config import ModelConnectionRepository, ModelProfile
from .planner import create_model_plan


class ProbeAnswer(BaseModel):
    status: str
    detail: str


class ProbeConfiguration(BaseModel):
    action: str
    nested: dict[str, str]


@ai.tool
async def model_capability_probe(configuration: ProbeConfiguration) -> dict[str, Any]:
    """Submit the requested nested capability probe configuration."""
    return {"received": configuration.model_dump()}


capability_agent = ai.Agent(tools=[model_capability_probe])


def safe_provider_error(exc: Exception) -> str:
    """Return an actionable provider error without reflecting arbitrary response data."""
    message = str(exc).lower()
    status_match = re.search(r"(?:error code|status(?: code)?)[^0-9]{0,4}([45][0-9]{2})", message)
    status = status_match.group(1) if status_match else None
    if status == "402" or any(term in message for term in (
        "insufficient balance", "insufficient_balance", "insufficient quota",
        "insufficient_quota", "billing hard limit",
    )):
        return "The provider reports insufficient balance or quota for this API account."
    if status == "401" or any(term in message for term in (
        "invalid api key", "incorrect api key", "invalid_api_key", "authentication failed",
    )):
        return "The provider rejected the API key. Check that the key is valid for this endpoint."
    if status == "403" or any(term in message for term in (
        "permission denied", "access denied", "forbidden",
    )):
        return "The API account does not have permission to use this endpoint or model."
    if status == "404" or any(term in message for term in (
        "model not found", "model_not_found", "unknown model",
    )):
        return "The configured endpoint or model was not found. Check both values."
    if status == "429" or any(term in message for term in (
        "rate limit", "rate_limit", "too many requests",
    )):
        return "The provider rate limit was reached. Wait briefly or check the account limits."
    if "timeout" in message or "timed out" in message:
        return "The provider did not respond before the configured timeout."
    if any(term in message for term in (
        "connection error", "connecterror", "name or service not known", "nodename nor servname",
    )):
        return "The provider endpoint could not be reached. Check the address and network connection."
    if status:
        return f"The provider rejected the connection test (HTTP {status})."
    return f"The connection test failed ({type(exc).__name__})."


def provider_extra_body(profile: ModelProfile) -> dict[str, Any] | None:
    # Current DeepSeek reasoning models reject forced tool_choice while thinking is enabled.
    # Planning requires one exact typed tool, so the adapter disables thinking for tool turns.
    if profile.provider == "deepseek":
        return {"thinking": {"type": "disabled"}}
    return None


class ModelRuntime:
    def __init__(self, repository: ModelConnectionRepository,
                 profile_id: str | None = None) -> None:
        self.repository = repository
        registry = repository.registry()
        self.profile_id = profile_id or registry.default_profile
        self.profile = registry.profile(self.profile_id)
        api_key = repository.resolve_api_key(self.profile)
        self.client = openai.AsyncOpenAI(
            api_key=api_key,
            base_url=str(self.profile.base_url),
            timeout=self.profile.timeout,
        )
        protocol = ai.providers.openai.OpenAIChatCompletionsProtocol()
        provider = ai.get_provider(
            "openai",
            base_url=str(self.profile.base_url),
            api_key=api_key,
            client=self.client,
            protocol=protocol,
        )
        self.model = ai.Model(id=self.profile.model, provider=provider)
        self.extra_body = provider_extra_body(self.profile)

    async def close(self) -> None:
        await self.client.close()

    def request_params(self) -> ai.InferenceRequestParams | None:
        return ai.InferenceRequestParams(extra_body=self.extra_body) if self.extra_body else None

    async def probe(self) -> dict[str, Any]:
        text_parts: list[str] = []
        async with ai.stream(
            self.model,
            [ai.user_message("Reply with exactly: merge-agent-ready")],
            params=self.request_params(),
        ) as stream:
            async for event in stream:
                if isinstance(event, ai.events.TextDelta):
                    text_parts.append(event.chunk)

        structured_supported = False
        structured_error: str | None = None
        try:
            async with ai.stream(
                self.model,
                [ai.user_message("Return status ready and a short detail.")],
                output_type=ProbeAnswer,
                params=self.request_params(),
            ) as stream:
                async for _ in stream:
                    pass
            structured_supported = isinstance(stream.output, ProbeAnswer)
        except Exception as exc:
            structured_error = type(exc).__name__

        forced_tool_supported = False
        forced_tool_error: str | None = None
        try:
            params = ai.InferenceRequestParams(
                tool_calling=ai.ToolCallingParams(
                    tool_choice=ai.ToolRef("model_capability_probe"),
                    parallel_tool_calls=False,
                ),
                extra_body=self.extra_body,
            )
            async with capability_agent.run(
                self.model,
                [ai.user_message(
                    "Call model_capability_probe with action='validate' and nested={mode:'exact'}."
                )],
                params=params,
            ) as result:
                async for event in result:
                    if isinstance(event, ai.events.ToolCallResult) and event.exception is None:
                        forced_tool_supported = True
        except Exception as exc:
            forced_tool_error = type(exc).__name__

        return {
            "connected": bool("".join(text_parts).strip()),
            "streaming": bool(text_parts),
            "structured_output": structured_supported,
            "structured_output_error": structured_error,
            "forced_nested_tools": forced_tool_supported,
            "forced_nested_tools_error": forced_tool_error,
            "planning_compatible": forced_tool_supported,
            "profile_id": self.profile_id,
            "provider": self.profile.provider,
            "model": self.profile.model,
            "api_mode": self.profile.api_mode,
            "reasoning_disabled_for_tools": self.profile.provider == "deepseek",
        }

    async def explain_plan(self, summary: str) -> str:
        parts: list[str] = []
        messages = [
            ai.system_message(
                "You explain deterministic Excel merge plans. Be concise, mention conflicts, "
                "and never claim that execution already happened."
            ),
            ai.user_message(summary),
        ]
        async with ai.stream(self.model, messages, params=self.request_params()) as stream:
            async for event in stream:
                if isinstance(event, ai.events.TextDelta):
                    parts.append(event.chunk)
        return "".join(parts).strip()

    async def plan_merge(self, template_path: str, evidence: dict[str, Any]) -> tuple[MergeSpec, PlannerProvenance]:
        return await create_model_plan(
            self.model,
            self.profile.model,
            Path(template_path),
            evidence,
            extra_body=self.extra_body,
            profile_id=self.profile_id,
            provider=self.profile.provider,
        )


class ModelRuntimeFactory:
    def __init__(self, repository: ModelConnectionRepository) -> None:
        self.repository = repository

    def create(self, profile_id: str | None = None) -> ModelRuntime:
        return ModelRuntime(self.repository, profile_id)

    async def probe(self, profile_id: str | None = None) -> dict[str, Any]:
        runtime = self.create(profile_id)
        try:
            return await runtime.probe()
        finally:
            await runtime.close()
