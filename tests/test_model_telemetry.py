"""Offline coverage for every model protocol and browser-use's real SDK path."""
import asyncio
import importlib.util
import json
import os
import unittest
from unittest.mock import MagicMock, patch

import requests

from src.core import observability as tracing
from src.core.api_client import NewAPIClient
from src.core.llm_agent import LLMAgent
from src.core.model_telemetry import create_browser_model


class ModelTelemetryTests(unittest.TestCase):
    def setUp(self):
        self.spans = []
        client = MagicMock()

        def start(**kwargs):
            span = MagicMock()
            self.spans.append((kwargs, span))
            manager = MagicMock()
            manager.__enter__.return_value = span
            manager.__exit__.return_value = False
            return manager

        client.start_as_current_observation.side_effect = start
        patcher = patch.object(tracing, "get_tracing_client", return_value=client)
        patcher.start()
        self.addCleanup(patcher.stop)
        env = patch.dict(os.environ, {"LANGFUSE_CAPTURE_CONTENT": "true"})
        env.start()
        self.addCleanup(env.stop)

    @staticmethod
    def response(payload, status=200):
        response = requests.Response()
        response.status_code = status
        response._content = json.dumps(payload).encode()
        return response

    @property
    def generation(self):
        return next(span for config, span in reversed(self.spans) if config["as_type"] == "generation")

    def last_update(self, field):
        return next(call.kwargs[field] for call in reversed(self.generation.update.call_args_list)
                    if field in call.kwargs)

    @patch("src.core.model_telemetry.requests.post")
    def test_responses_text_usage(self, post):
        post.return_value = self.response({"output": [{"content": [{"text": "ok"}]}],
            "usage": {"input_tokens": 20, "output_tokens": 10, "total_tokens": 30,
                      "input_tokens_details": {"cached_tokens": 8},
                      "output_tokens_details": {"reasoning_tokens": 4}}})
        self.assertEqual(NewAPIClient(api_key="offline").create_text_response("model", "hello"), "ok")
        self.assertEqual(self.last_update("usage_details"), {"input": 12, "input_cached_tokens": 8,
                         "output": 6, "output_reasoning_tokens": 4, "total": 30})

    @patch("src.core.model_telemetry.requests.post")
    def test_responses_empty_and_truncated_preserve_business_result(self, post):
        client = NewAPIClient(api_key="offline")
        post.return_value = self.response({"output": []})
        self.assertEqual(client.create_text_response("model", "hello"), "")
        self.assertEqual(self.last_update("level"), "ERROR")
        post.return_value = self.response({"status": "incomplete",
            "incomplete_details": {"reason": "max_output_tokens"},
            "output": [{"content": [{"text": "partial"}]}]})
        self.assertEqual(client.create_text_response("model", "hello"), "partial")
        self.assertEqual(self.last_update("level"), "WARNING")

    @patch("src.core.model_telemetry.requests.post")
    def test_responses_vision_redacts_inline_image(self, post):
        post.return_value = self.response({"output": [{"content": [{"text": "image text"}]}]})
        self.assertEqual(NewAPIClient(api_key="offline").create_image_response(
            "model", "read", "data:image/png;base64,PRIVATE_IMAGE"), "image text")
        self.assertNotIn("PRIVATE_IMAGE", str(self.generation.update.call_args_list))
        self.assertIn("PRIVATE_IMAGE", post.call_args.kwargs["json"]["input"][0]["content"][1]["image_url"])

    @patch("src.core.model_telemetry.requests.post")
    def test_gemini_usage_and_media_redaction(self, post):
        post.return_value = self.response({"candidates": [{"content": {"parts": [{"text": "ok"}]}}],
            "usageMetadata": {"promptTokenCount": 20, "cachedContentTokenCount": 5,
                              "candidatesTokenCount": 7, "thoughtsTokenCount": 3, "totalTokenCount": 30}})
        self.assertEqual(NewAPIClient(api_key="offline").generate_gemini_content(
            "model", "read", "data:image/png;base64,PRIVATE_IMAGE"), "ok")
        self.assertNotIn("PRIVATE_IMAGE", str(self.generation.update.call_args_list))
        self.assertEqual(self.last_update("usage_details"), {"input": 15, "input_cached_tokens": 5,
                         "output": 7, "output_reasoning_tokens": 3, "total": 30})

    @patch("src.core.model_telemetry.requests.post")
    def test_ollama_generate_and_reset(self, post):
        post.return_value = self.response({"response": "ok", "prompt_eval_count": 4, "eval_count": 2})
        agent = LLMAgent.__new__(LLMAgent)
        self.assertEqual(agent._call_ollama("hello"), "ok")
        self.assertEqual(self.last_update("usage_details"), {"input": 4, "output": 2, "total": 6})
        agent._reset_ollama_context()
        self.assertEqual(self.spans[-1][0]["name"], "ollama-context-reset")
        self.assertEqual(post.call_count, 2)

    @patch("src.core.model_telemetry.requests.post")
    def test_http_error_keeps_reason_but_redacts_key(self, post):
        post.return_value = self.response({"error": {"code": "quota", "message": "quota exhausted sk-secret123"}}, 429)
        self.assertIsNone(NewAPIClient(api_key="offline").complete_text("hello", model_chain=["model"]))
        message = self.last_update("status_message")
        self.assertIn("quota exhausted", message)
        self.assertNotIn("sk-secret123", message)

    @patch("src.core.model_telemetry.requests.post")
    def test_timeout_keeps_reason(self, post):
        post.side_effect = requests.Timeout("offline timeout detail")
        self.assertIsNone(NewAPIClient(api_key="offline").complete_text("hello", model_chain=["model"]))
        self.assertIn("offline timeout detail", self.last_update("status_message"))

    @patch("src.core.model_telemetry.requests.post")
    def test_content_disabled_still_records_usage(self, post):
        post.return_value = self.response({"response": "private output", "prompt_eval_count": 3, "eval_count": 1})
        with patch.dict(os.environ, {"LANGFUSE_CAPTURE_CONTENT": "false"}):
            self.assertEqual(LLMAgent.__new__(LLMAgent)._call_ollama("private prompt"), "private output")
        for call in self.generation.update.call_args_list:
            self.assertNotIn("input", call.kwargs)
            self.assertNotIn("output", call.kwargs)
        self.assertEqual(self.last_update("usage_details")["total"], 4)

    @patch("src.core.model_telemetry.requests.post")
    def test_usage_parser_failure_never_repeats_model_call(self, post):
        post.return_value = self.response({"choices": [{"message": {"content": "ok"}}], "usage": "unknown"})
        self.assertEqual(NewAPIClient(api_key="offline").complete_text("hello", model_chain=["model"]), "ok")
        post.assert_called_once()

    @unittest.skipUnless(importlib.util.find_spec("browser_use"), "browser-use not installed")
    def test_browser_real_sdk_text_and_structured_calls(self):
        import httpx
        from browser_use.llm.messages import UserMessage, SystemMessage
        from pydantic import BaseModel

        class Answer(BaseModel):
            answer: str

        async def run():
            sent = []

            def handler(request):
                sent.append(json.loads(request.content))
                return httpx.Response(200, json={"id": "offline", "object": "chat.completion",
                    "created": 0, "model": "test-model", "choices": [{"index": 0,
                    "message": {"role": "assistant", "content": '{"answer":"ok"}'}, "finish_reason": "stop"}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 3, "total_tokens": 13,
                              "prompt_tokens_details": {"cached_tokens": 6},
                              "completion_tokens_details": {"reasoning_tokens": 1}}})

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
                for purpose, output_format in (("browser-navigation", None), ("browser-extraction", Answer)):
                    model = create_browser_model(purpose=purpose, model="test-model", api_key="offline",
                        base_url="https://offline.invalid/v1", http_client=http_client,
                        dont_force_structured_output=True, add_schema_to_system_prompt=True)
                    result = await model.ainvoke([SystemMessage(content="read"), UserMessage(content="hello")],
                                                output_format=output_format)
                    self.assertEqual(result.completion if output_format is None else result.completion.answer,
                                     '{"answer":"ok"}' if output_format is None else "ok")
            self.assertEqual(len(sent), 2)
            self.assertIn("json_schema", sent[1]["messages"][0]["content"])

        asyncio.run(run())
        generations = [(config, span) for config, span in self.spans if config["as_type"] == "generation"]
        self.assertEqual([c["name"] for c, _ in generations], ["browser-navigation", "browser-extraction"])
        self.assertEqual(self.last_update("usage_details"), {"input": 4, "input_cached_tokens": 6,
                         "output": 2, "output_reasoning_tokens": 1, "total": 13})


@unittest.skipUnless(importlib.util.find_spec("langfuse") and importlib.util.find_spec("browser_use"),
                     "Langfuse/browser-use not installed")
class RealSDKBrowserTests(unittest.TestCase):
    def test_async_browser_generations_keep_row_parent_and_report_errors(self):
        import httpx
        from browser_use.llm.messages import UserMessage
        from langfuse import Langfuse
        from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

        exporter = InMemorySpanExporter()
        client = Langfuse(public_key="pk-lf-browser-offline", secret_key="sk-lf-browser-offline",
                          span_exporter=exporter)
        requests_sent = []

        async def run():
            def handler(request):
                requests_sent.append(request)
                if len(requests_sent) == 2:
                    return httpx.Response(429, json={"error": {"message": "offline quota exceeded", "type": "quota"}})
                return httpx.Response(200, json={"id": "offline", "created": 0, "object": "chat.completion",
                    "model": "model", "choices": [{"index": 0, "finish_reason": "stop",
                    "message": {"role": "assistant", "content": "ok"}}],
                    "usage": {"prompt_tokens": 2, "completion_tokens": 1, "total_tokens": 3}})

            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http_client:
                model = create_browser_model(purpose="browser-navigation", model="model", api_key="offline",
                    base_url="https://offline.invalid/v1", http_client=http_client, max_retries=0)
                await model.ainvoke([UserMessage(content="hello")])
                with self.assertRaises(Exception):
                    await model.ainvoke([UserMessage(content="hello again")])

        try:
            with patch.object(tracing, "get_tracing_client", return_value=client):
                with tracing.observation("process-row"):
                    asyncio.run(run())
            client.flush()
            spans = exporter.get_finished_spans()
            root = next(s for s in spans if s.name == "process-row")
            children = [s for s in spans if s.name == "browser-navigation"]
            self.assertEqual(len(children), 2)
            self.assertTrue(all(s.parent.span_id == root.context.span_id for s in children))
            self.assertEqual(children[0].attributes["langfuse.observation.type"], "generation")
            self.assertEqual(children[1].attributes["langfuse.observation.level"], "ERROR")
            self.assertIn("quota exceeded", children[1].attributes["langfuse.observation.status_message"])
            self.assertEqual(len(requests_sent), 2)
        finally:
            client.shutdown()


if __name__ == "__main__":
    unittest.main()
