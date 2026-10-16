"""Optional Langfuse tracing for the requests-based pipeline.

Telemetry failures must never retry or interrupt business operations.
"""
import atexit
import logging
import os
import re
import sys
from contextlib import contextmanager
from functools import wraps
from pathlib import Path

logger = logging.getLogger(__name__)
_client = None
_initialized = False


def get_tracing_client():
    global _client, _initialized
    if _initialized:
        return _client
    _initialized = True
    try:
        from dotenv import load_dotenv

        load_dotenv(Path(__file__).resolve().parents[2] / "keys" / "langfuse.env",
                    override=False)
        if os.getenv("LANGFUSE_TRACING_ENABLED", "true").lower() in (
            "0", "false", "no", "off"
        ):
            return None
        if not (os.getenv("LANGFUSE_PUBLIC_KEY") and os.getenv("LANGFUSE_SECRET_KEY")):
            return None
        from langfuse import Langfuse

        _client = Langfuse()
        atexit.register(flush_traces)
        logger.info("Langfuse tracing enabled")
    except Exception as exc:
        logger.warning("Langfuse unavailable; tracing disabled (%s)", type(exc).__name__)
    return _client


def _safe_content(value):
    """Exclude image payloads while preserving text prompts and outputs."""
    if isinstance(value, dict):
        return {key: ("[image/document data omitted]" if key in ("inlineData", "inline_data")
                      else _safe_content(item)) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_safe_content(item) for item in value]
    if isinstance(value, str) and value.startswith("data:"):
        return "[image/document data omitted]"
    return value


def error_summary(exc):
    message = str(exc)
    message = re.sub(r"\b(?:sk|pk)-[A-Za-z0-9_-]+", "[redacted key]", message)
    return f"{type(exc).__name__}: {message}"[:1500]


def record_model_response(span, payload, output, protocol="openai"):
    """Preserve provider usage and emit exclusive Langfuse token buckets."""
    try:
        usage = payload if protocol == "ollama" else payload.get(
            "usageMetadata" if protocol == "gemini" else "usage", {})
        if not isinstance(usage, dict):
            usage = {}

        def count(container, key):
            value = container.get(key)
            return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else None

        if protocol == "ollama":
            input_count, output_count = count(usage, "prompt_eval_count"), count(usage, "eval_count")
            cached = reasoning = None
            total = None
            raw_usage = {k: usage[k] for k in ("prompt_eval_count", "eval_count", "total_duration",
                         "load_duration", "prompt_eval_duration", "eval_duration") if k in usage}
        elif protocol == "gemini":
            input_count = count(usage, "promptTokenCount")
            output_count = count(usage, "candidatesTokenCount")
            cached, reasoning = count(usage, "cachedContentTokenCount"), count(usage, "thoughtsTokenCount")
            total = count(usage, "totalTokenCount")
            if output_count is not None and reasoning is not None:
                output_count += reasoning  # Gemini reports thoughts separately.
            raw_usage = usage
        else:
            input_count = count(usage, "prompt_tokens")
            if input_count is None:
                input_count = count(usage, "input_tokens")
            output_count = count(usage, "completion_tokens")
            if output_count is None:
                output_count = count(usage, "output_tokens")
            input_details = usage.get("prompt_tokens_details") or usage.get("input_tokens_details") or {}
            output_details = usage.get("completion_tokens_details") or usage.get("output_tokens_details") or {}
            cached = count(input_details, "cached_tokens") if isinstance(input_details, dict) else None
            reasoning = count(output_details, "reasoning_tokens") if isinstance(output_details, dict) else None
            total = count(usage, "total_tokens")
            raw_usage = usage

        buckets = {}
        for name, inclusive, detail_name, detail in (
            ("input", input_count, "input_cached_tokens", cached),
            ("output", output_count, "output_reasoning_tokens", reasoning),
        ):
            if inclusive is not None:
                part = min(detail, inclusive) if detail is not None else 0
                buckets[name] = inclusive - part
                if detail is not None:
                    buckets[detail_name] = part
        if total is not None:
            buckets["total"] = total
        elif input_count is not None and output_count is not None:
            buckets["total"] = input_count + output_count
        metadata = {"raw_usage": raw_usage, "usage_reported": bool(buckets)}
        if payload.get("model") or payload.get("modelVersion"):
            metadata["response_model"] = payload.get("model") or payload.get("modelVersion")
        if payload.get("id"):
            metadata["response_id"] = payload["id"]
        if payload.get("choices"):
            metadata["finish_reason"] = payload["choices"][0].get("finish_reason")
        elif payload.get("candidates"):
            metadata["finish_reason"] = payload["candidates"][0].get("finishReason")
        if payload.get("status"):
            metadata["response_status"] = payload["status"]
        if payload.get("incomplete_details"):
            metadata["incomplete_details"] = payload["incomplete_details"]
        span.update(output=output, usage_details=buckets, metadata=metadata)
        if metadata.get("finish_reason") in ("length", "MAX_TOKENS") or payload.get("status") == "incomplete":
            span.update(level="WARNING", status_message="Model output truncated")
        elif payload.get("status") == "failed":
            span.update(level="ERROR", status_message="Model response status: failed")
    except Exception as exc:
        logger.debug("Model telemetry parsing failed (%s)", type(exc).__name__)


def record_http_error(span, response):
    try:
        payload = response.json()
        error = payload.get("error", {}) if isinstance(payload, dict) else {}
        if isinstance(error, dict):
            detail = "; ".join(f"{key}={error[key]}" for key in ("code", "param", "message") if error.get(key))
        else:
            detail = str(error)
        span.update(level="ERROR", status_message=error_summary(
            RuntimeError(f"HTTP {response.status_code}: {detail}")))
    except Exception:
        span.update(level="ERROR", status_message=f"HTTP {response.status_code}")


class _Observation:
    def __init__(self, span=None):
        self.span = span

    def update(self, **kwargs):
        if self.span is None:
            return
        if os.getenv("LANGFUSE_CAPTURE_CONTENT", "true").lower() in (
            "0", "false", "no", "off"
        ):
            kwargs.pop("input", None)
            kwargs.pop("output", None)
        try:
            self.span.update(**_safe_content(kwargs))
        except Exception as exc:
            logger.debug("Langfuse update failed (%s)", type(exc).__name__)


@contextmanager
def observation(name, as_type="span", **kwargs):
    client = get_tracing_client()
    manager = None
    span = _Observation()
    if client is not None:
        try:
            manager = client.start_as_current_observation(name=name, as_type=as_type)
            span = _Observation(manager.__enter__())
            span.update(**kwargs)
        except Exception as exc:
            manager = None
            logger.debug("Langfuse span failed (%s)", type(exc).__name__)
    try:
        yield span
    except BaseException as exc:
        span.update(level="ERROR", status_message=error_summary(exc))
        raise
    finally:
        if manager is not None:
            try:
                manager.__exit__(*sys.exc_info())
            except Exception as exc:
                logger.debug("Langfuse span close failed (%s)", type(exc).__name__)


def traced(name):
    """Trace synchronous pipeline steps, including failures returned as values."""
    def decorate(func):
        @wraps(func)
        def wrapped(*args, **kwargs):
            with observation(name) as span:
                if name == "process-row":
                    row_index = kwargs.get("row_index", args[1] if len(args) > 1 else None)
                    span.update(input={"row_index": row_index}, metadata={"row_index": row_index})
                elif name == "model-fallback-chain":
                    models = kwargs.get("model_chain", args[2] if len(args) > 2 else [])
                    span.update(metadata={"model_chain": list(models)})
                result = func(*args, **kwargs)
                span.update(output=result)
                if result is None or result is False:
                    span.update(level="ERROR", status_message="Operation returned no result")
                elif isinstance(result, tuple) and result and isinstance(result[0], bool):
                    error = result[2] if len(result) > 2 else ""
                    if not result[0] or error:
                        span.update(level="WARNING" if result[0] else "ERROR",
                                    status_message=str(error or "Operation failed"))
                return result
        return wrapped
    return decorate


def flush_traces():
    if _client is not None:
        try:
            _client.flush()
        except Exception as exc:
            logger.debug("Langfuse flush failed (%s)", type(exc).__name__)
