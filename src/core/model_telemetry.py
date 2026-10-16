"""Instrument model HTTP calls without changing routing or retry behavior."""
import logging
import requests

from .observability import observation, record_http_error, record_model_response

logger = logging.getLogger(__name__)


def model_post(*, name, url, body, headers, timeout, extract_text, protocol="openai",
               model=None, input=None, parameters=None, require_text=False):
    with observation(name, as_type="generation", model=model or body.get("model"),
                     input=input, model_parameters=parameters or {},
                     metadata={"protocol": protocol}) as generation:
        response = requests.post(url, headers=headers, json=body, timeout=timeout)
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
