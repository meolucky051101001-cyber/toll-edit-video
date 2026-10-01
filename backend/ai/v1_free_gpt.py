"""One bounded attempt at OpenAI's open-weight GPT model on a free endpoint."""
import json
import logging
import os
import re
import requests

logger = logging.getLogger(__name__)
MODEL = "openai/gpt-oss-120b:free"

def translate_free_gpt(texts, target_lang="vi", prior_context=None):
    key = os.getenv("OPENROUTER_API_KEY", "").strip()
    if not key or key.upper().startswith(("YOUR_", "PASTE_")):
        logger.warning("Free GPT unavailable: configure OPENROUTER_API_KEY in V1 backend/.env")
        return None
    from .translation import build_translation_prompt, _contains_cjk
    try:
        response = requests.post(
            "https://openrouter.ai/api/v1/chat/completions",
            headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"},
            json={"model": MODEL,
                  "messages": [{"role": "user", "content": build_translation_prompt(
                      texts, target_lang, prior_context, with_vision=False)}],
                  "reasoning": {"effort": "low"}, "max_tokens": 8192},
            timeout=(5.0, 20.0))
        if response.status_code != 200:
            logger.warning("Free GPT %s failed HTTP %s", MODEL, response.status_code)
            return None
        result = response.json()
        choice = result["choices"][0]
        if choice.get("finish_reason") != "stop":
            raise ValueError("Incomplete GPT generation")
        raw = choice["message"]["content"].strip()
        match = re.search(r'\[.*\]', raw, re.DOTALL)
        translated = json.loads(match.group(0) if match else raw)
        if (not isinstance(translated, list) or len(translated) != len(texts)
                or not all(isinstance(item, str) and item.strip() for item in translated)
                or (target_lang.lower().startswith("vi") and any(_contains_cjk(item) for item in translated))):
            raise ValueError("Invalid GPT translation")
        logger.info("Translation successful: provider=OpenRouter requested_model=%s actual_model=%s", MODEL, result.get("model", MODEL))
        return translated
    except Exception as exc:
        # Never log request headers, keys, or untrusted API error payloads.
        logger.warning("Free GPT %s failed: %s", MODEL, type(exc).__name__)
        return None
