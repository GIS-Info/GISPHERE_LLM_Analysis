"""Offline regression tests: no model requests or telemetry leave this process."""
import os
import importlib.util
import unittest
from unittest.mock import MagicMock, patch

import requests

from src.core import observability as tracing
from src.core.api_client import NewAPIClient


class TracingTests(unittest.TestCase):
    @property
    def generations(self):
        return [entry for entry in self.spans if entry[0].get("as_type") == "generation"]

    def setUp(self):
        self.client = MagicMock()
        self.spans = []

        def start(**kwargs):
            span = MagicMock()
            self.spans.append((kwargs, span))
            manager = MagicMock()
            manager.__enter__.return_value = span
            manager.__exit__.return_value = False
            return manager

        self.client.start_as_current_observation.side_effect = start
        self.patcher = patch.object(tracing, "get_tracing_client", return_value=self.client)
        self.patcher.start()
        self.addCleanup(self.patcher.stop)
        self.env = patch.dict(os.environ, {"LANGFUSE_CAPTURE_CONTENT": "true"})
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_disabled_tracing_preserves_result(self):
        with patch.object(tracing, "get_tracing_client", return_value=None):
            with tracing.observation("disabled") as span:
                span.update(output="ok")

    def test_explicit_disable_does_not_initialize_sdk(self):
        self.patcher.stop()
        with patch.object(tracing, "_initialized", False), patch.object(tracing, "_client", None):
            with patch.dict(os.environ, {"LANGFUSE_TRACING_ENABLED": "false"}):
                self.assertIsNone(tracing.get_tracing_client())
        self.patcher.start()

    def test_tracing_failures_do_not_repeat_business_work(self):
        for failure in ("start", "update", "exit"):
            with self.subTest(failure=failure):
                manager = MagicMock()
                manager.__enter__.return_value.update.side_effect = RuntimeError("telemetry") if failure == "update" else None
                manager.__exit__.side_effect = RuntimeError("telemetry") if failure == "exit" else None
                self.client.start_as_current_observation.side_effect = RuntimeError("telemetry") if failure == "start" else None
                self.client.start_as_current_observation.return_value = manager
                work = MagicMock(return_value="ok")
                self.assertEqual(tracing.traced("work")(work)(), "ok")
                work.assert_called_once()

    def test_business_exception_propagates(self):
        with self.assertRaisesRegex(ValueError, "business"):
            with tracing.observation("work"):
                raise ValueError("business")
        self.assertTrue(any(c.kwargs.get("level") == "ERROR"
                            for c in self.spans[0][1].update.call_args_list))

    def test_content_redaction_and_capture_switch(self):
        with tracing.observation("vision", input=[{"image": "data:image/png;base64,SECRET"}]) as span:
            span.update(output="text")
        self.assertNotIn("SECRET", str(self.spans[0][1].update.call_args_list))
        with patch.dict(os.environ, {"LANGFUSE_CAPTURE_CONTENT": "false"}):
            with tracing.observation("private", input="secret", model="model") as span:
                span.update(output="secret", usage_details={"input": 3})
        calls = self.spans[1][1].update.call_args_list
        self.assertTrue(all("input" not in c.kwargs and "output" not in c.kwargs for c in calls))
        self.assertTrue(any(c.kwargs.get("usage_details") == {"input": 3} for c in calls))

    def test_partial_success_is_warning(self):
        result = (True, {"field": "value"}, "stage2 failed")
        self.assertEqual(tracing.traced("analysis")(lambda: result)(), result)
        self.assertTrue(any(c.kwargs.get("level") == "WARNING"
                            for c in self.spans[0][1].update.call_args_list))

    @staticmethod
    def response(status, payload):
        response = requests.Response()
        response.status_code = status
        response._content = __import__("json").dumps(payload).encode()
        return response

    @patch("src.core.api_client.requests.post")
    def test_compatibility_retry_and_usage(self, post):
        post.side_effect = [
            self.response(400, {"error": {"param": "temperature", "code": "unsupported_parameter"}}),
            self.response(200, {"choices": [{"message": {"content": "ok"}}],
                                "usage": {"prompt_tokens": 12, "completion_tokens": 5, "total_tokens": 17}}),
        ]
        client = NewAPIClient(api_key="test-only")
        self.assertEqual(client.complete_text("hello", model_chain=["test-model"]), "ok")
        self.assertEqual(post.call_count, 2)
        self.assertEqual(len(self.generations), 2)
        self.assertTrue(any(c.kwargs.get("level") == "ERROR" for c in self.generations[0][1].update.call_args_list))
        self.assertTrue(any(c.kwargs.get("usage_details") == {"input": 12, "output": 5, "total": 17}
                            for c in self.generations[1][1].update.call_args_list))
        self.assertNotIn("temperature", post.call_args.kwargs["json"])

    @patch("src.core.api_client.requests.post")
    def test_nonstandard_usage_does_not_break_completion(self, post):
        post.return_value = self.response(200, {"choices": [{"message": {"content": "ok"}}],
                                                "usage": "unavailable"})
        client = NewAPIClient(api_key="test-only")
        self.assertEqual(client.complete_text("hello", model_chain=["test-model"]), "ok")
        post.assert_called_once()

    @patch("src.core.api_client.requests.post")
    def test_model_fallback_preserves_success(self, post):
        post.side_effect = [self.response(500, {"error": {}}),
                            self.response(200, {"choices": [{"message": {"content": "fallback"}}]})]
        client = NewAPIClient(api_key="test-only")
        self.assertEqual(client.complete_text("hello", model_chain=["first", "second"]), "fallback")
        self.assertEqual(client.last_model, "second")
        self.assertEqual(len(self.generations), 2)


@unittest.skipUnless(importlib.util.find_spec("langfuse"), "Langfuse SDK not installed")
class RealSDKTests(unittest.TestCase):
    def test_nested_spans_and_generation_fields_with_local_exporter(self):
        from langfuse import Langfuse
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        exporter = InMemorySpanExporter()
        client = Langfuse(public_key="pk-lf-offline-test", secret_key="sk-lf-offline-test",
                          span_exporter=exporter)
        try:
            with patch.object(tracing, "get_tracing_client", return_value=client):
                with tracing.observation("row"):
                    with tracing.observation("model", as_type="generation", model="test-model",
                                             model_parameters={"temperature": 0.1,
                                                               "response_format": {"type": "json_object"}},
                                             input="hello") as generation:
                        generation.update(output="world", usage_details={"input": 2, "output": 1})
            client.flush()
            spans = {span.name: span for span in exporter.get_finished_spans()}
            self.assertEqual(set(spans), {"row", "model"})
            self.assertEqual(spans["model"].parent.span_id, spans["row"].context.span_id)
            self.assertEqual(spans["model"].attributes["langfuse.observation.model.name"], "test-model")
            self.assertIn("hello", str(spans["model"].attributes))
            self.assertIn("world", str(spans["model"].attributes))
            self.assertIn("langfuse.observation.usage_details", spans["model"].attributes)
        finally:
            client.shutdown()


if __name__ == "__main__":
    unittest.main()
