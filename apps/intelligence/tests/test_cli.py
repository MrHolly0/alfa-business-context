import contextlib
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from intelligence.llm.cli import main


class CLITests(unittest.TestCase):
    def test_default_demo_is_offline_and_marks_synthetic_cost(self):
        output = io.StringIO()
        with contextlib.redirect_stdout(output):
            self.assertEqual(main([]), 0)
        data = json.loads(output.getvalue())
        self.assertEqual(data["mode"], "offline_fixture")
        self.assertFalse(data["network"])
        self.assertFalse(data["credentials_read"])
        self.assertTrue(data["cost_is_synthetic"])
        self.assertEqual(data["provider_cost_currency"], "USD")
        self.assertIsNone(data["projected_hackathon_cost_rub"])

    def test_compare_rejects_different_models_before_transport(self):
        config = {
            "default_profile": "openrouter",
            "profiles": {
                "openrouter": {
                    "base_url": "https://openrouter.ai/api/v1", "api_key_env": "OPENROUTER_API_KEY",
                    "model": "google/gemma-4-26b-a4b-it:free", "budget_scope": "openrouter-test",
                    "currency": "USD", "daily_limit": "1", "input_per_million": "0.1",
                    "output_per_million": "0.2", "timeout_seconds": 1, "max_output_tokens": 10,
                },
                "hackathon": {
                    "base_url": "https://changellenge.up.railway.app/v1", "api_key_env": "HACKATHON_API_KEY",
                    "model": "z-ai/glm-5.3-flash", "budget_scope": "hackathon-test",
                    "currency": "RUB", "daily_limit": "500", "input_per_million": "1",
                    "output_per_million": "2", "timeout_seconds": 1, "max_output_tokens": 10,
                },
            },
        }
        with tempfile.TemporaryDirectory() as directory:
            config_path = Path(directory) / "providers.json"
            config_path.write_text(json.dumps(config), encoding="utf-8")
            stderr = io.StringIO()
            with patch("intelligence.llm.cli.ChatCompletionTransport") as transport, contextlib.redirect_stderr(stderr):
                result = main(["run", "--config", str(config_path), "--compare-hackathon"])
            self.assertEqual(result, 2)
            self.assertIn("same model", stderr.getvalue())
            transport.assert_not_called()


if __name__ == "__main__":
    unittest.main()
