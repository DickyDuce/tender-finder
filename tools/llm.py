"""Tiny provider adapter. Groq (free tier) and Ollama (local) both speak the OpenAI API.

Config via environment / .env:
    LLM_PROVIDER = groq | ollama          (default: groq)
    GROQ_API_KEY = ...                    (required for groq)
    LLM_MODEL    = override default model
"""

import json
import os

from dotenv import load_dotenv
from openai import OpenAI

load_dotenv()

PROVIDERS = {
    "groq": {"base_url": "https://api.groq.com/openai/v1", "key_env": "GROQ_API_KEY", "model": "openai/gpt-oss-120b"},
    "ollama": {"base_url": "http://localhost:11434/v1", "key_env": None, "model": "llama3.1:8b"},
}


def provider_name():
    return (os.getenv("LLM_PROVIDER") or "groq").lower()


def is_configured():
    cfg = PROVIDERS.get(provider_name())
    return bool(cfg) and (cfg["key_env"] is None or bool(os.getenv(cfg["key_env"])))


def get_client():
    """Return (client, model) or raise RuntimeError if no provider is configured."""
    name = provider_name()
    if name not in PROVIDERS:
        raise RuntimeError(f"Unknown LLM_PROVIDER '{name}'. Use one of: {', '.join(PROVIDERS)}")
    cfg = PROVIDERS[name]
    key = os.getenv(cfg["key_env"]) if cfg["key_env"] else "ollama"
    if not key:
        raise RuntimeError(f"Set {cfg['key_env']} in .env (free key at https://console.groq.com/keys)")
    return OpenAI(base_url=cfg["base_url"], api_key=key), os.getenv("LLM_MODEL") or cfg["model"]


def _parse_json(text):
    """Parse a JSON object from model output, tolerating ```json fences or stray prose."""
    text = (text or "").strip()
    if text.startswith("```"):
        text = text.strip("`").removeprefix("json").strip()
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end == -1:
        raise ValueError("no JSON object in model output")
    return json.loads(text[start : end + 1])


def chat_json(system, user, retries=1):
    """Ask the model for a single JSON object. Retries once on unparseable output."""
    client, model = get_client()
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    last_err = None
    for _ in range(retries + 1):
        resp = client.chat.completions.create(
            model=model, messages=messages, temperature=0, response_format={"type": "json_object"}
        )
        content = resp.choices[0].message.content
        try:
            return _parse_json(content)
        except (ValueError, json.JSONDecodeError) as e:
            last_err = e
            messages += [
                {"role": "assistant", "content": content or ""},
                {"role": "user", "content": "That was not valid JSON. Reply with only the JSON object."},
            ]
    raise RuntimeError(f"model did not return valid JSON: {last_err}")
