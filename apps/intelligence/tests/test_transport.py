import json
import os
import unittest
from decimal import Decimal
from unittest.mock import patch

from intelligence.llm.config import ProviderConfig
from intelligence.llm.transport import ChatCompletionTransport
from intelligence.llm.types import ProviderError


class FakeResponse:
    def __init__(self, payload):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        return False

    def read(self, amount=-1):
        return self.payload[:amount]


class FakeOpener:
    def __init__(self, payload):
        self.payload = payload
        self.request = None
        self.timeout = None

    def open(self, request, timeout):
        self.request = request
        self.timeout = timeout
        return FakeResponse(self.payload)


def make_config(profile="openrouter", model="google/gemma-4-26b-a4b-it:free"):
    return ProviderConfig(profile, "https://openrouter.ai/api/v1", "TEST_LLM_KEY", model,
                          "team", "USD", Decimal("20"), Decimal("0"), Decimal("0"), 4.5, 256)


class TransportTests(unittest.TestCase):
    def test_sends_json_nonstream_request_and_parses_usage(self):
        opener = FakeOpener({
            "id": "resp_1", "model": "google/gemma-4-26b-a4b-it:free",
            "choices": [{"message": {"content": "{\"ok\":true}"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 12, "completion_tokens": 4,
                      "prompt_tokens_details": {"cached_tokens": 3}, "cost": "0.0001"},
        })
        with patch.dict(os.environ, {"TEST_LLM_KEY": "not-a-real-key"}):
            completion = ChatCompletionTransport(make_config(), opener).complete(
                [{"role": "user", "content": "hello"}], json_mode=True)
        sent = json.loads(opener.request.data)
        self.assertFalse(sent["stream"])
        self.assertEqual(sent["response_format"], {"type": "json_object"})
        self.assertEqual(sent["max_tokens"], 256)
        self.assertEqual(sent["model"], "google/gemma-4-26b-a4b-it:free")
        self.assertEqual(sent["usage"], {"include": True})
        self.assertEqual(opener.timeout, 4.5)
        self.assertEqual(completion.usage.prompt_tokens, 12)
        self.assertEqual(completion.usage.cached_tokens, 3)
        self.assertEqual(completion.usage.reported_cost, Decimal("0.0001"))

    def test_missing_usage_stays_unknown(self):
        opener = FakeOpener({"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]})
        with patch.dict(os.environ, {"TEST_LLM_KEY": "x"}):
            result = ChatCompletionTransport(make_config(), opener).complete([{"role": "user", "content": "x"}])
        self.assertIsNone(result.usage)
        self.assertIsNone(result.model)

    def test_hackathon_request_omits_openrouter_usage_flag(self):
        config = ProviderConfig("hackathon", "https://changellenge.up.railway.app/v1", "TEST_LLM_KEY",
            "google/gemma-4-26b-a4b-it", "team", "RUB", Decimal("500"), Decimal("0"), Decimal("0"), 4.5, 256)
        opener = FakeOpener({"choices": [{"message": {"content": "ok"}}]})
        with patch.dict(os.environ, {"TEST_LLM_KEY": "x"}):
            ChatCompletionTransport(config, opener).complete([{"role": "user", "content": "hello"}])
        sent = json.loads(opener.request.data)
        self.assertNotIn("usage", sent)

    def test_model_mismatch_is_reported_as_uncertain(self):
        opener = FakeOpener({"model": "another/model", "choices": [{"message": {"content": "ok"}}]})
        with patch.dict(os.environ, {"TEST_LLM_KEY": "x"}):
            with self.assertRaises(ProviderError) as caught:
                ChatCompletionTransport(make_config(), opener).complete([{"role": "user", "content": "x"}])
        self.assertEqual(caught.exception.code, "model_mismatch")
        self.assertTrue(caught.exception.billing_uncertain)

    def test_no_key_is_actionable_without_sending(self):
        opener = FakeOpener({})
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaises(ProviderError) as caught:
                ChatCompletionTransport(make_config(), opener).complete([{"role": "user", "content": "x"}])
        self.assertEqual(caught.exception.code, "missing_api_key")
        self.assertIsNone(opener.request)

    def test_refusal_and_truncation_are_actionable_uncertain_errors(self):
        for choice, code in [
            ({"message": {"content": "", "refusal": "policy"}, "finish_reason": "stop"}, "model_refusal"),
            ({"message": {"content": "partial"}, "finish_reason": "length"}, "truncated_response"),
        ]:
            opener = FakeOpener({"choices": [choice]})
            with patch.dict(os.environ, {"TEST_LLM_KEY": "x"}):
                with self.assertRaises(ProviderError) as caught:
                    ChatCompletionTransport(make_config(), opener).complete([{"role": "user", "content": "x"}])
            self.assertEqual(caught.exception.code, code)
            self.assertTrue(caught.exception.billing_uncertain)

    def test_refusal_and_truncation_errors_keep_known_usage(self):
        usage = {"prompt_tokens": 10, "completion_tokens": 20, "cost": "0.000003"}
        for choice, code in [
            ({"message": {"content": "", "refusal": "policy"}, "finish_reason": "stop"}, "model_refusal"),
            ({"message": {"content": "partial"}, "finish_reason": "length"}, "truncated_response"),
        ]:
            opener = FakeOpener({"choices": [choice], "usage": usage})
            with patch.dict(os.environ, {"TEST_LLM_KEY": "x"}):
                with self.assertRaises(ProviderError) as caught:
                    ChatCompletionTransport(make_config(), opener).complete([{"role": "user", "content": "x"}])
            self.assertEqual(caught.exception.code, code)
            self.assertEqual(caught.exception.usage.prompt_tokens, 10)
            self.assertEqual(caught.exception.usage.reported_cost, Decimal("0.000003"))

    def test_malformed_json_error_keeps_usage_but_not_content(self):
        opener = FakeOpener({
            "choices": [{"message": {"content": "private malformed output"}, "finish_reason": "stop"}],
            "usage": {"prompt_tokens": 3, "completion_tokens": 7},
        })
        with patch.dict(os.environ, {"TEST_LLM_KEY": "x"}):
            with self.assertRaises(ProviderError) as caught:
                ChatCompletionTransport(make_config(), opener).complete(
                    [{"role": "user", "content": "x"}], json_mode=True)
        self.assertEqual(caught.exception.usage.prompt_tokens, 3)
        self.assertNotIn("private malformed output", str(caught.exception))

    def test_openrouter_cost_survives_missing_or_invalid_token_counts(self):
        for raw_usage in (
            {"cost": "0.000007"},
            {"prompt_tokens": 3, "completion_tokens": None, "cost": "0.000007"},
        ):
            opener = FakeOpener({"choices": [{"message": {"content": "ok"}}], "usage": raw_usage})
            with patch.dict(os.environ, {"TEST_LLM_KEY": "x"}):
                result = ChatCompletionTransport(make_config(), opener).complete([{"role": "user", "content": "x"}])
            self.assertEqual(result.usage.prompt_tokens, None)
            self.assertEqual(result.usage.completion_tokens, None)
            self.assertEqual(result.usage.reported_cost, Decimal("0.000007"))

    def test_json_mode_requires_finite_json_object(self):
        for content in ("[1, 2]", '{"n": NaN}', '{"n": Infinity}'):
            opener = FakeOpener({
                "choices": [{"message": {"content": content}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 2, "completion_tokens": 3, "cost": "0.000002"},
            })
            with patch.dict(os.environ, {"TEST_LLM_KEY": "x"}):
                with self.assertRaises(ProviderError) as caught:
                    ChatCompletionTransport(make_config(), opener).complete(
                        [{"role": "user", "content": "x"}], json_mode=True)
            self.assertEqual(caught.exception.code, "malformed_json_output")
            self.assertEqual(caught.exception.usage.reported_cost, Decimal("0.000002"))

    def test_json_mode_rejects_malformed_model_output(self):
        opener = FakeOpener({"choices": [{"message": {"content": "not json"}, "finish_reason": "stop"}]})
        with patch.dict(os.environ, {"TEST_LLM_KEY": "x"}):
            with self.assertRaises(ProviderError) as caught:
                ChatCompletionTransport(make_config(), opener).complete(
                    [{"role": "user", "content": "x"}], json_mode=True)
        self.assertEqual(caught.exception.code, "malformed_json_output")
        self.assertTrue(caught.exception.billing_uncertain)

    def test_rejects_invalid_request_before_opening(self):
        opener = FakeOpener({})
        with patch.dict(os.environ, {"TEST_LLM_KEY": "x"}):
            with self.assertRaises(ValueError):
                ChatCompletionTransport(make_config(), opener).complete([{"role": "user", "content": "x", "url": "https://example.com"}])
        self.assertIsNone(opener.request)


if __name__ == "__main__":
    unittest.main()
