"""Z.ai (GLM) LLM client — OpenAI-compatible API."""

import logging
import os

from openai import AsyncOpenAI, RateLimitError

log = logging.getLogger(__name__)

ZAI_BASE_URL = os.getenv("ZAI_BASE_URL", "https://api.z.ai/api/paas/v4")
ZAI_MODEL = os.getenv("ZAI_MODEL", "glm-5.2")


def _load_zai_keys() -> list[str]:
    keys = []
    i = 1
    while True:
        k = os.getenv(f"ZAI_API_KEY_{i}")
        if not k:
            break
        keys.append(k)
        i += 1
    if not keys:
        fallback = os.getenv("ZAI_API_KEY")
        if fallback:
            keys.append(fallback)
    return keys


ZAI_KEYS = _load_zai_keys()
_zai_key_idx = 0


def current_zai_key() -> str | None:
    return ZAI_KEYS[_zai_key_idx] if ZAI_KEYS else None


def rotate_zai_key() -> str | None:
    global _zai_key_idx
    _zai_key_idx += 1
    if _zai_key_idx >= len(ZAI_KEYS):
        log.error("🔴 All Z.ai API keys exhausted")
        return None
    log.warning(f"🔁 Rotated to Z.ai key #{_zai_key_idx + 1}")
    return ZAI_KEYS[_zai_key_idx]


def _client(api_key: str) -> AsyncOpenAI:
    return AsyncOpenAI(api_key=api_key, base_url=ZAI_BASE_URL)


async def generate_text(
    prompt: str,
    *,
    max_tokens: int = 256,
    temperature: float = 0.7,
) -> str:
    """Generate text with Z.ai, rotating keys on rate limits."""
    while True:
        key = current_zai_key()
        if key is None:
            raise RuntimeError("No Z.ai API keys available")
        try:
            log.debug(f"🤖 Calling Z.ai ({ZAI_MODEL}) key #{_zai_key_idx + 1}")
            client = _client(key)
            resp = await client.chat.completions.create(
                model=ZAI_MODEL,
                messages=[{"role": "user", "content": prompt}],
                max_tokens=max_tokens,
                temperature=temperature,
            )
            text = resp.choices[0].message.content.strip()
            log.info(f"🤖 Z.ai generated text ({len(text)} chars)")
            return text
        except RateLimitError as e:
            log.warning(f"⚠️  Z.ai key #{_zai_key_idx + 1} rate limited (429): {e}")
            if rotate_zai_key() is None:
                raise RuntimeError("All Z.ai API keys exhausted") from e
