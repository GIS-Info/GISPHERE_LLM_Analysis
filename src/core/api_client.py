import base64
import logging
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Union

import requests

from .observability import traced
from .model_telemetry import model_post

from .config import (
    API_BASE_URL,
    GEMINI_BASE_URL,
    DOCUMENT_AI_TIMEOUT,
    TEXT_MODEL_CHAIN,
    VISION_MODEL_CHAIN,
    MODEL_COOLDOWN_SECONDS,
    TEXT_REASONING_EFFORT,
    VISION_REASONING_EFFORT,
    check_api_key,
)

logger = logging.getLogger(__name__)


class NewAPIClient:
    """Small client for the New API endpoints used by this project."""

    # 进程级共享的死模型熔断表：model -> 解禁时间戳。
    # 类属性而非实例属性，确保 llm_agent 与 document_ai 各自的 client 实例共享同一熔断状态。
    _blocked_until: Dict[str, float] = {}
    # 仅鉴权/封禁类错误触发长熔断；429(限流)/超时等临时错误不计入。
    _AUTH_FAIL_CODES = (401, 403)

    @classmethod
    def _is_model_blocked(cls, model: str) -> bool:
        """模型是否仍在熔断冷却期内（到期自动清除）。"""
        unblock_ts = cls._blocked_until.get(model)
        if unblock_ts is None:
            return False
        if time.time() >= unblock_ts:
            cls._blocked_until.pop(model, None)
            return False
        return True

    @classmethod
    def _trip_model(cls, model: str) -> None:
        """将模型加入熔断表，冷却 MODEL_COOLDOWN_SECONDS 秒。"""
        cls._blocked_until[model] = time.time() + MODEL_COOLDOWN_SECONDS
        logger.warning(
            f"模型 {model} 触发鉴权熔断（401/403），{MODEL_COOLDOWN_SECONDS // 60} 分钟内将跳过"
        )

    @staticmethod
    def _status_code_of(exc: Exception) -> Optional[int]:
        """从异常中尽力取出 HTTP 状态码（requests.HTTPError 携带 response）。"""
        response = getattr(exc, "response", None)
        return getattr(response, "status_code", None) if response is not None else None

    def __init__(
        self,
        api_key: Optional[str] = None,
        api_base_url: str = API_BASE_URL,
        gemini_base_url: str = GEMINI_BASE_URL,
        timeout: int = DOCUMENT_AI_TIMEOUT,
    ):
        self.api_key = api_key or check_api_key()
        if not self.api_key:
            raise RuntimeError("No API key found. Put it in keys/api_key.txt.")

        self.api_base_url = api_base_url.rstrip("/")
        self.gemini_base_url = gemini_base_url.rstrip("/")
        self.timeout = timeout
        # 记录最近一次成功调用所用模型，便于上层日志
        self.last_model: Optional[str] = None

    @property
    def headers(self) -> Dict[str, str]:
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }

    # ──────────────────────────────────────────────────────────────────
    # 统一 /chat/completions 路由 + 价格升序模型链回退
    # ──────────────────────────────────────────────────────────────────
    def _chat_once(
        self,
        model: str,
        messages: List[Dict[str, Any]],
        max_output_tokens: int,
        temperature: Optional[float],
        json_mode: bool,
        timeout: Optional[int],
        reasoning_effort: Optional[str] = None,
    ) -> str:
        """调用模型；根据明确的参数兼容错误自动调整并重试。"""

        # 每个客户端记住各模型不接受的可选参数。
        if not hasattr(self, "_unsupported_params"):
            self._unsupported_params = {}

        cache_key = (self.api_base_url, model)
        excluded = self._unsupported_params.setdefault(
            cache_key, set()
        )

        body: Dict[str, Any] = {
            "model": model,
            "messages": messages,
            "max_tokens": max_output_tokens,
        }

        if temperature is not None:
            body["temperature"] = temperature
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        if reasoning_effort:
            body["reasoning_effort"] = reasoning_effort

        # 保留当前已知的 Claude 兼容处理。
        if model.lower().startswith("claude-"):
            for name in (
                "temperature",
                "response_format",
                "reasoning_effort",
            ):
                body.pop(name, None)

        for name in excluded:
            body.pop(name, None)

        # 只允许自动移除这些可选参数。
        # 不调整 model、messages、max_tokens 等核心字段。
        adjustable = {
            "temperature",
            "response_format",
            "reasoning_effort",
        }

        while True:
            response, text = model_post(
                name="chat-completion", url=f"{self.api_base_url}/chat/completions",
                body=body, headers=self.headers, timeout=timeout or self.timeout,
                extract_text=self.extract_chat_text, model=model, input=messages,
                parameters={k: v for k, v in body.items() if k not in ("model", "messages")},
                require_text=True,
            )
            if response.ok:
                return text.strip()

            logger.error(
                "模型请求失败：model=%s status=%s response=%s",
                model,
                response.status_code,
                response.text[:3000],
            )

            # 仅处理明确指出参数不支持的 400 错误。
            try:
                payload = response.json()
                error = payload.get("error", {})
                if not isinstance(error, dict):
                    error = {}
            except (ValueError, AttributeError):
                error = {}

            param = error.get("param")
            code = error.get("code")
            message = str(error.get("message", "")).lower()

            explicitly_unsupported = (
                code in {
                    "unsupported_parameter",
                    "unsupported_value",
                }
                or (
                    code is None
                    and (
                        "not supported" in message
                        or "does not support" in message
                        or "unsupported parameter" in message
                    )
                )
            )

            if (
                response.status_code == 400
                and isinstance(param, str)
                and param in adjustable
                and param in body
                and explicitly_unsupported
            ):
                body.pop(param)
                excluded.add(param)

                logger.warning(
                    "模型 %s 不接受参数 %s，移除后重试；"
                    "本次运行后续请求将省略该参数",
                    model,
                    param,
                )
                continue

            # 其他错误交给原来的模型回退机制。
            response.raise_for_status()

    @traced("model-fallback-chain")
    def _complete_with_chain(
        self,
        messages: List[Dict[str, Any]],
        model_chain: List[str],
        max_output_tokens: int,
        temperature: Optional[float],
        json_mode: bool,
        timeout: Optional[int],
        reasoning_effort: Optional[str] = None,
    ) -> Optional[str]:
        """按价格升序模型链逐个尝试，任一成功即返回；全部失败返回 None。

        先过滤掉仍在鉴权熔断期内的模型；若整条链都被熔断，则保留原链兜底（不至于无模型可用）。
        """
        live_chain = [m for m in model_chain if not self._is_model_blocked(m)]
        if not live_chain:
            logger.warning("所有模型均在熔断期内，回退为尝试完整链")
            chain_to_use = list(model_chain)
        else:
            chain_to_use = live_chain
            skipped = [m for m in model_chain if m not in live_chain]
            if skipped:
                logger.info(f"熔断跳过模型: {skipped}")

        last_error: Optional[Exception] = None
        for idx, model in enumerate(chain_to_use):
            try:
                if idx > 0:
                    logger.warning(f"模型回退 -> {model}（第 {idx + 1}/{len(chain_to_use)} 个）")
                else:
                    logger.info(f"调用 /chat/completions，首选模型: {model}")
                text = self._chat_once(
                    model, messages, max_output_tokens, temperature, json_mode, timeout,
                    reasoning_effort,
                )
                self.last_model = model
                logger.info(f"✅ /chat/completions 调用成功（模型: {model}）")
                return text
            except Exception as e:
                last_error = e
                status = self._status_code_of(e)
                if status in self._AUTH_FAIL_CODES:
                    self._trip_model(model)
                logger.error(f"/chat/completions 调用失败（模型: {model}）: {e}")
        logger.error(f"模型链全部失败: {last_error}")
        return None

    def complete_text(
        self,
        prompt: str,
        instructions: Optional[str] = None,
        model_chain: Optional[List[str]] = None,
        max_output_tokens: int = 3000,
        temperature: Optional[float] = 0.1,
        json_mode: bool = True,
        timeout: Optional[int] = None,
        reasoning_effort: Optional[str] = "__default__",
    ) -> Optional[str]:
        """纯文本补全：走 TEXT_MODEL_CHAIN（价格升序）回退。

        reasoning_effort 缺省用配置项 TEXT_REASONING_EFFORT（默认 None=不下发）；
        显式传 None 可强制不下发，传 low/medium/high 覆盖。
        """
        effort = TEXT_REASONING_EFFORT if reasoning_effort == "__default__" else reasoning_effort
        messages: List[Dict[str, Any]] = []
        if instructions:
            messages.append({"role": "system", "content": instructions})
        messages.append({"role": "user", "content": prompt})
        return self._complete_with_chain(
            messages,
            model_chain or list(TEXT_MODEL_CHAIN),
            max_output_tokens,
            temperature,
            json_mode,
            timeout,
            effort,
        )

    def complete_vision(
        self,
        text: str,
        image_data_url: str,
        model_chain: Optional[List[str]] = None,
        max_output_tokens: int = 4096,
        temperature: Optional[float] = 0.1,
        json_mode: bool = False,
        timeout: Optional[int] = None,
        reasoning_effort: Optional[str] = "__default__",
    ) -> Optional[str]:
        """图片/文档（VLM）补全：走 VISION_MODEL_CHAIN（价格升序）回退。

        reasoning_effort 缺省用配置项 VISION_REASONING_EFFORT（默认 None=不下发）。
        """
        effort = VISION_REASONING_EFFORT if reasoning_effort == "__default__" else reasoning_effort
        messages = [
            {
                "role": "user",
                "content": [
                    {"type": "text", "text": text},
                    {"type": "image_url", "image_url": {"url": image_data_url}},
                ],
            }
        ]
        return self._complete_with_chain(
            messages,
            model_chain or list(VISION_MODEL_CHAIN),
            max_output_tokens,
            temperature,
            json_mode,
            timeout,
            effort,
        )

    @staticmethod
    def extract_chat_text(payload: Dict[str, Any]) -> str:
        """从 /chat/completions 响应中取文本内容。"""
        choices = payload.get("choices", [])
        if not choices:
            return ""
        message = choices[0].get("message", {})
        content = message.get("content", "")
        if isinstance(content, list):
            # 兼容部分网关把 content 拆成分片数组
            parts = [seg.get("text", "") for seg in content if isinstance(seg, dict)]
            return "".join(parts)
        return content or ""

    # ──────────────────────────────────────────────────────────────────
    # 以下三个方法走 /responses 与原生 Gemini 端点，当前主流程统一改用
    # complete_text / complete_vision（/chat/completions）。保留它们是为后续
    # 可能切回 /responses 路径或直连 Gemini 时复用，请勿删除。
    # ──────────────────────────────────────────────────────────────────
    def create_text_response(
        self,
        model: str,
        prompt: str,
        instructions: Optional[str] = None,
        max_output_tokens: int = 3000,
        temperature: Optional[float] = 0.1,
        json_mode: bool = True,
        timeout: Optional[int] = None,
    ) -> str:
        """[保留备用] /responses 文本补全，主流程当前使用 complete_text。"""
        body: Dict[str, Any] = {
            "model": model,
            "input": prompt,
            "max_output_tokens": max_output_tokens,
        }
        if instructions:
            body["instructions"] = instructions
        if temperature is not None:
            body["temperature"] = temperature
        if json_mode:
            body["text"] = {"format": {"type": "json_object"}}

        response, text = model_post(
            name="responses-text", url=f"{self.api_base_url}/responses",
            body=body, headers=self.headers, timeout=timeout or self.timeout,
            extract_text=self.extract_response_text, model=model,
            input={"instructions": instructions, "input": prompt},
            parameters={k: v for k, v in body.items() if k not in ("model", "input", "instructions")},
        )
        response.raise_for_status()
        return text

    def create_image_response(
        self,
        model: str,
        text: str,
        image_data_url: str,
        max_output_tokens: int = 4096,
        temperature: Optional[float] = 0.1,
        timeout: Optional[int] = None,
    ) -> str:
        """[保留备用] /responses 图片补全，主流程当前使用 complete_vision。"""
        body: Dict[str, Any] = {
            "model": model,
            "input": [
                {
                    "role": "user",
                    "content": [
                        {"type": "input_text", "text": text},
                        {"type": "input_image", "image_url": image_data_url},
                    ],
                }
            ],
            "max_output_tokens": max_output_tokens,
        }
        if temperature is not None:
            body["temperature"] = temperature

        response, result = model_post(
            name="responses-vision", url=f"{self.api_base_url}/responses",
            body=body, headers=self.headers, timeout=timeout or self.timeout,
            extract_text=self.extract_response_text, model=model, input=body["input"],
            parameters={k: v for k, v in body.items() if k not in ("model", "input")},
        )
        response.raise_for_status()
        return result

    def generate_gemini_content(
        self,
        model: str,
        text: str,
        image_data_url: str,
        max_output_tokens: int = 4096,
        temperature: float = 0.1,
        timeout: Optional[int] = None,
    ) -> str:
        """[保留备用] 直连 Gemini generateContent，主流程当前使用 complete_vision。"""
        mime_type, encoded = self._split_data_url(image_data_url)
        body = {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": text},
                        {
                            "inlineData": {
                                "mimeType": mime_type,
                                "data": encoded,
                            }
                        },
                    ],
                }
            ],
            "generationConfig": {
                "maxOutputTokens": max_output_tokens,
                "temperature": temperature,
            },
        }

        response, result = model_post(
            name="gemini-content", url=f"{self.gemini_base_url}/models/{model}:generateContent",
            body=body, headers=self.headers, timeout=timeout or self.timeout,
            extract_text=self.extract_gemini_text, protocol="gemini", model=model,
            input=body["contents"], parameters=body["generationConfig"],
        )
        response.raise_for_status()
        return result

    @staticmethod
    def image_path_to_data_url(image_path: Union[str, Path], media_type: str) -> str:
        with open(image_path, "rb") as f:
            encoded = base64.b64encode(f.read()).decode("utf-8")
        return f"data:{media_type};base64,{encoded}"

    @staticmethod
    def extract_response_text(payload: Dict[str, Any]) -> str:
        output_texts: List[str] = []
        for item in payload.get("output", []):
            for content in item.get("content", []):
                if "text" in content:
                    output_texts.append(content["text"])
        return "\n".join(output_texts).strip()

    @staticmethod
    def extract_gemini_text(payload: Dict[str, Any]) -> str:
        texts: List[str] = []
        for candidate in payload.get("candidates", []):
            content = candidate.get("content", {})
            for part in content.get("parts", []):
                if "text" in part:
                    texts.append(part["text"])
        return "\n".join(texts).strip()

    @staticmethod
    def _split_data_url(data_url: str) -> tuple[str, str]:
        prefix, encoded = data_url.split(",", 1)
        media_type = prefix.removeprefix("data:").split(";")[0]
        return media_type, encoded


