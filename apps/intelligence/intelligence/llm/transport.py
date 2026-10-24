"""Single-attempt, OpenAI-compatible chat completions transport."""

from __future__ import annotations

import json
import http.client
import math
import os
import urllib.error
import urllib.request
from decimal import Decimal, InvalidOperation
from typing import Mapping, Sequence

from .config import OPENROUTER_BASE, ProviderConfig
from .types import Completion, ProviderError, Usage

MAX_RESPONSE_BYTES = 2_000_000


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class ChatCompletionTransport:
    def __init__(self, config: ProviderConfig, opener=None):
        self.config = config
        self._opener = opener or urllib.request.build_opener(_NoRedirect())

    def complete(self, messages: Sequence[Mapping[str, str]], *, json_mode: bool = False,
                 max_output_tokens: int | None = None) -> Completion:
        self.config.ensure_spend_allowed()
        if not isinstance(messages, Sequence) or isinstance(messages, (str, bytes)) or not messages:
            raise ValueError("messages must be a non-empty sequence")
        clean_messages = []
        for message in messages:
            if not isinstance(message, Mapping) or set(message) != {"role", "content"}:
                raise ValueError("each message must contain only role and content")
            if message["role"] not in {"system", "user", "assistant"} or not isinstance(message["content"], str):
                raise ValueError("message role/content is invalid")
            clean_messages.append({"role": message["role"], "content": message["content"]})
        token_limit = max_output_tokens if max_output_tokens is not None else self.config.max_output_tokens
        if isinstance(token_limit, bool) or not isinstance(token_limit, int) or token_limit < 1 or token_limit > self.config.max_output_tokens:
            raise ValueError("max_output_tokens must be within the configured limit")
        api_key = os.environ.get(self.config.api_key_env)
        if not api_key:
            raise ProviderError("missing_api_key", f"Set {self.config.api_key_env} in the environment")
        body = {"model": self.config.model, "messages": clean_messages, "stream": False, "max_tokens": token_limit}
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if self.config.profile == "openrouter" and self.config.reasoning_effort is not None:
            body["reasoning"] = {"effort": self.config.reasoning_effort}
        if self.config.profile == "openrouter":
            body["usage"] = {"include": True}
            if self.config.is_paid_openrouter:
                prices = (self.config.input_per_million, self.config.output_per_million)
                caps = [float(price) for price in prices]
                if any(not math.isfinite(cap) or cap <= 0 for cap in caps):
                    raise ValueError("paid OpenRouter price caps must be representable as finite JSON numbers")
                body["provider"] = {
                    "sort": "price",
                    "require_parameters": True,
                    "max_price": {"prompt": caps[0], "completion": caps[1], "request": 0},
                }
        headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json", "Accept": "application/json"}
        if self.config.base_url == OPENROUTER_BASE:
            headers["HTTP-Referer"] = "https://alfa-business-context.local"
            headers["X-Title"] = "Alfa Business Context"
        request = urllib.request.Request(self.config.base_url + "/chat/completions",
                                         data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                         headers=headers, method="POST")
        try:
            response = self._opener.open(request, timeout=self.config.timeout_seconds)
        except urllib.error.HTTPError as exc:
            status = exc.code
            raise ProviderError("provider_http_error", f"Provider returned HTTP {status}",
                                billing_uncertain=status >= 500 or status in {408, 429}, http_status=status) from None
        except (urllib.error.URLError, TimeoutError, OSError, http.client.HTTPException):
            raise ProviderError("provider_transport_error", "Provider request failed or timed out",
                                billing_uncertain=True) from None
        try:
            with response:
                raw = response.read(MAX_RESPONSE_BYTES + 1)
        except (OSError, TimeoutError, http.client.HTTPException):
            raise ProviderError("provider_read_error", "Provider response could not be read", billing_uncertain=True) from None
        if len(raw) > MAX_RESPONSE_BYTES:
            raise ProviderError("response_too_large", "Provider response exceeded the size limit", billing_uncertain=True)
        try:
            payload = json.loads(raw)
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise ProviderError("invalid_response", "Provider returned invalid JSON", billing_uncertain=True) from None
        completion = self._parse_completion(payload)
        if json_mode:
            try:
                parsed_content = json.loads(completion.content, parse_constant=self._reject_json_constant)
                if not isinstance(parsed_content, dict):
                    raise ValueError("JSON object required")
            except (json.JSONDecodeError, ValueError):
                raise ProviderError("malformed_json_output", "Model returned malformed JSON",
                                    billing_uncertain=True, usage=completion.usage) from None
        return completion

    @staticmethod
    def _reject_json_constant(value: str):
        raise ValueError("non-finite JSON numbers are not permitted")

    def _parse_completion(self, payload) -> Completion:
        if not isinstance(payload, dict):
            raise ProviderError("invalid_response", "Provider response must be a JSON object", billing_uncertain=True)
        raw_usage = payload.get("usage")
        usage = self._parse_usage(raw_usage) if isinstance(raw_usage, dict) else None
        choices = payload.get("choices")
        if not isinstance(choices, list) or len(choices) != 1 or not isinstance(choices[0], dict):
            raise ProviderError("invalid_response", "Provider response must contain one choice", billing_uncertain=True, usage=usage)
        choice = choices[0]
        message = choice.get("message")
        if not isinstance(message, dict):
            raise ProviderError("invalid_response", "Provider response has no message", billing_uncertain=True, usage=usage)
        if message.get("refusal"):
            raise ProviderError("model_refusal", "Model refused the request", billing_uncertain=True, usage=usage)
        finish_reason = choice.get("finish_reason")
        if finish_reason == "length":
            raise ProviderError("truncated_response", "Model output reached the token limit", billing_uncertain=True, usage=usage)
        content = message.get("content")
        if not isinstance(content, str):
            raise ProviderError("invalid_response", "Provider response content is not text", billing_uncertain=True, usage=usage)
        model = payload.get("model")
        if isinstance(model, str):
            expected_id = self.config.model.removesuffix(":free")
            if model not in {self.config.model, expected_id}:
                raise ProviderError("model_mismatch", "Provider returned a different model", billing_uncertain=True, usage=usage)
        else:
            model = None
        response_id = payload.get("id") if isinstance(payload.get("id"), str) else None
        return Completion(content, model, usage, finish_reason if isinstance(finish_reason, str) else None, response_id)

    def _parse_usage(self, raw: dict) -> Usage | None:
        prompt = raw.get("prompt_tokens")
        completion = raw.get("completion_tokens")
        valid_tokens = not any(isinstance(v, bool) or not isinstance(v, int) or v < 0
                               for v in (prompt, completion))
        if not valid_tokens:
            prompt = completion = None
        details = raw.get("prompt_tokens_details")
        cached = details.get("cached_tokens") if isinstance(details, dict) else None
        if (not valid_tokens or isinstance(cached, bool) or not isinstance(cached, int)
                or cached < 0 or cached > prompt):
            cached = None
        cost = None
        if self.config.profile == "openrouter" and "cost" in raw:
            try:
                if isinstance(raw["cost"], bool):
                    raise InvalidOperation
                cost = Decimal(str(raw["cost"]))
                if not cost.is_finite() or cost < 0:
                    cost = None
            except (InvalidOperation, ValueError):
                cost = None
        if prompt is None and cost is None:
            return None
        return Usage(prompt, completion, cached, cost, "USD" if cost is not None else None)
