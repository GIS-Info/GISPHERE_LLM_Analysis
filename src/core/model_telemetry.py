"""Instrument model HTTP calls with bounded recovery from TLS EOF failures."""
import logging
import time
import requests

from .observability import observation, record_http_error, record_model_response

logger = logging.getLogger(__name__)


def _post_with_tls_recovery(url, *, headers, body, timeout):
    """Retry only an abruptly closed TLS connection, never certificate failures.

    A lost response can mean the provider already processed the POST. Keep the
    retry budget small; every attempt uses the same model and request body.
    requests.post creates a fresh session/connection on each attempt.
    """
    for attempt in range(3):
        try:
            return requests.post(url, headers=headers, json=body, timeout=timeout)
        except requests.exceptions.SSLError as exc:
            detail = str(exc).lower()
            tls_eof = ('unexpected_eof_while_reading' in detail
                       or 'eof occurred in violation of protocol' in detail)
            if not tls_eof or 'certificate_verify_failed' in detail or attempt == 2:
                raise
            delay = 2 ** attempt
            logger.warning('模型 HTTPS 连接意外关闭，%s 秒后重连（%s/2）', delay, attempt + 1)
            time.sleep(delay)


def model_post(*, name, url, body, headers, timeout, extract_text, protocol="openai",
               model=None, input=None, parameters=None, require_text=False):
    with observation(name, as_type="generation", model=model or body.get("model"),
                     input=input, model_parameters=parameters or {},
                     metadata={"protocol": protocol}) as generation:
        response = _post_with_tls_recovery(url, headers=headers, body=body, timeout=timeout)
        if not response.ok:
            record_http_error(generation, response)
            return response, None
        payload = response.json()
        text = extract_text(payload)
        record_model_response(generation, payload, text, protocol)
        if not text or (isinstance(text, str) and not text.strip()):
            generation.update(level="ERROR", status_message="Empty model output")
            if require_text:
                raise RuntimeError("空响应内容")
        return response, text


def create_browser_model(*, purpose, **kwargs):
    """Trace the actual serialized OpenAI call made by browser-use.

    Only this model's client is wrapped; SDK retry and structured parsing stay intact.
    Each SDK call is one generation, including any internal HTTP retries.
    """
    from browser_use import ChatOpenAI
    from .api_client import NewAPIClient

    class TracedChatOpenAI(ChatOpenAI):
        def get_client(self):
            client = super().get_client()
            create = client.chat.completions.create

            async def traced_create(*args, **call_kwargs):
                with observation(purpose, as_type="generation",
                                 model=call_kwargs.get("model", self.model),
                                 input=call_kwargs.get("messages"),
                                 model_parameters={k: v for k, v in call_kwargs.items()
                                                   if k not in ("model", "messages")},
                                 metadata={"component": "browser-use"}) as generation:
                    response = await create(*args, **call_kwargs)
                    try:
                        payload = response.model_dump(mode="json")
                        output = NewAPIClient.extract_chat_text(payload)
                        record_model_response(generation, payload, output)
                        if not payload.get("choices") or not output:
                            generation.update(level="ERROR", status_message="Empty model completion")
                    except Exception as exc:
                        logger.debug("Browser model telemetry failed (%s)", type(exc).__name__)
                    return response

            client.chat.completions.create = traced_create
            return client

    return TracedChatOpenAI(**kwargs)
