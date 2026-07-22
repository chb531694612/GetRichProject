from __future__ import annotations

import io
import json
import unittest
from pathlib import Path
from unittest.mock import patch

from score_fourfold.ai_analyzer import AIAnalysisError, probe_deepseek

from .helpers import make_settings


class _Response:
    def __init__(self, payload: dict):
        self._stream = io.BytesIO(json.dumps(payload).encode("utf-8"))

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self):
        return self._stream.read()


class AIAnalyzerTests(unittest.TestCase):
    def test_probe_uses_authenticated_chat_completion(self):
        settings = make_settings(Path("data"), deepseek_api_key="secret", ai_analysis_enabled=True)
        with patch(
            "score_fourfold.ai_analyzer.urllib.request.urlopen",
            return_value=_Response({"choices": [{"message": {"content": "AI连接正常"}}]}),
        ) as mocked:
            self.assertEqual(probe_deepseek(settings), "AI连接正常")
        request = mocked.call_args.args[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")

    def test_probe_rejects_missing_key_and_empty_response(self):
        with self.assertRaisesRegex(AIAnalysisError, "not configured"):
            probe_deepseek(make_settings(Path("data")))
        settings = make_settings(Path("data"), deepseek_api_key="secret")
        with patch(
            "score_fourfold.ai_analyzer.urllib.request.urlopen",
            return_value=_Response({"choices": [{"message": {"content": ""}}]}),
        ):
            with self.assertRaisesRegex(AIAnalysisError, "empty content"):
                probe_deepseek(settings)


if __name__ == "__main__":
    unittest.main()
