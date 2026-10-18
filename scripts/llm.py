"""One interface to many LLM providers.

Everything else in the project calls `llm.generate(...)`. Which provider
answers is decided by, in order:

    1. LLM_PROVIDER / LLM_MODEL environment variables (or .env)
    2. scoring.provider / scoring.model in config/criteria.yaml
    3. gemini, the default

Gemini keeps its own client (scripts/gemini.py) because of its free-tier model
rotation. Anthropic has a native client here. The other twelve hosted
providers and local Ollama all speak the OpenAI chat-completions dialect, so
one code path covers them and adding another is a single PROVIDERS entry.

Model names change faster than this file will. The ones below are starting
points. List what your key can actually use with:

    python scripts/llm.py models <provider>
"""

from __future__ import annotations

import argparse
import json
import os
import random
import re
import sys
import time
from pathlib import Path

import requests

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gemini  # noqa: E402  (also loads .env)

ROOT = Path(__file__).resolve().parent.parent


class LLMError(RuntimeError):
    pass


# kind "openai" = OpenAI chat-completions compatible. base is the API root.
# key is the environment variable holding the API key (None = no key needed).
PROVIDERS: dict[str, dict] = {
    "gemini":     {"kind": "gemini", "key": "GEMINI_API_KEY",
                   "model": gemini.DEFAULT_MODEL, "label": "Google Gemini"},
    "anthropic":  {"kind": "anthropic", "key": "ANTHROPIC_API_KEY",
                   "base": "https://api.anthropic.com/v1",
                   "model": "claude-sonnet-5-5", "label": "Anthropic Claude"},
    "openai":     {"kind": "openai", "key": "OPENAI_API_KEY",
                   "base": "https://api.openai.com/v1",
                   "model": "gpt-6-sol", "label": "OpenAI GPT"},
    "xai":        {"kind": "openai", "key": "XAI_API_KEY",
                   "base": "https://api.x.ai/v1",
                   "model": "grok-4.7", "label": "xAI Grok"},
    "deepseek":   {"kind": "openai", "key": "DEEPSEEK_API_KEY",
                   "base": "https://api.deepseek.com",
                   "model": "deepseek-v4-pro", "label": "DeepSeek"},
    "mistral":    {"kind": "openai", "key": "MISTRAL_API_KEY",
                   "base": "https://api.mistral.ai/v1",
                   "model": "mistral-large-latest", "label": "Mistral"},
    "qwen":       {"kind": "openai", "key": "DASHSCOPE_API_KEY",
                   "base": "https://dashscope-intl.aliyuncs.com/compatible-mode/v1",
                   "model": "qwen-max", "label": "Alibaba Qwen"},
    "moonshot":   {"kind": "openai", "key": "MOONSHOT_API_KEY",
                   "base": "https://api.moonshot.ai/v1",
                   "model": "kimi-k3", "label": "Moonshot Kimi"},
    "zhipu":      {"kind": "openai", "key": "ZAI_API_KEY",
                   "base": "https://api.z.ai/api/paas/v4",
                   "model": "glm-5.3", "label": "Zhipu GLM"},
    "minimax":    {"kind": "openai", "key": "MINIMAX_API_KEY",
                   "base": "https://api.minimax.io/v1",
                   "model": "MiniMax-M2", "label": "MiniMax"},
    "cohere":     {"kind": "openai", "key": "COHERE_API_KEY",
                   "base": "https://api.cohere.ai/compatibility/v1",
                   "model": "command-a-03-2025", "label": "Cohere Command"},
    "groq":       {"kind": "openai", "key": "GROQ_API_KEY",
                   "base": "https://api.groq.com/openai/v1",
                   "model": "llama-3.3-70b-versatile", "label": "Meta Llama on Groq"},
    "together":   {"kind": "openai", "key": "TOGETHER_API_KEY",
                   "base": "https://api.together.xyz/v1",
                   "model": "meta-llama/Llama-3.3-70B-Instruct-Turbo",
                   "label": "Open models on Together"},
    "openrouter": {"kind": "openai", "key": "OPENROUTER_API_KEY",
                   "base": "https://openrouter.ai/api/v1",
                   "model": "openrouter/auto", "label": "OpenRouter (any model)"},
    "ollama":     {"kind": "openai", "key": None,
                   "base": "http://localhost:11434/v1",
                   "model": "llama3.3", "label": "Local models via Ollama"},
}


def _scoring_cfg() -> dict:
    try:
        import yaml
        cfg = yaml.safe_load((ROOT / "config" / "criteria.yaml").read_text())
        return cfg.get("scoring", {})
    except Exception:
        return {}


def current() -> tuple[str, str]:
    """The (provider, model) in effect."""
    cfg = _scoring_cfg()
    provider = (os.environ.get("LLM_PROVIDER") or cfg.get("provider")
                or "gemini").lower()
    if provider not in PROVIDERS:
        raise LLMError(f"unknown provider {provider!r}. "
                       f"choose one of: {', '.join(PROVIDERS)}")
    model = (os.environ.get("LLM_MODEL") or cfg.get("model")
             or PROVIDERS[provider]["model"])
    # A model name from another provider (the old default was a Gemini alias)
    # must not leak into this one.
    if provider != "gemini" and model.startswith("gemini"):
        model = PROVIDERS[provider]["model"]
    return provider, model


def available_models() -> list[str]:
    provider, model = current()
    if provider == "gemini":
        return gemini.available_models()
    return [f"{provider}:{model}"]


# ---------------------------------------------------------------------------
# Shared plumbing
# ---------------------------------------------------------------------------

def _parse_json(text: str):
    text = text.strip()
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", text)
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        m = re.search(r"[\{\[].*[\}\]]", text, re.S)
        if m:
            return json.loads(m.group(0))
        raise LLMError(f"unparseable JSON: {text[:300]}")


def _post(url: str, headers: dict, body: dict, retries: int = 4) -> dict:
    """POST with backoff on 429 and 5xx. Honors Retry-After."""
    last = ""
    for attempt in range(retries):
        try:
            r = requests.post(url, headers=headers, json=body, timeout=(15, 120))
        except requests.RequestException as exc:
            last = f"{type(exc).__name__}: {exc}"
            time.sleep(2 ** attempt + random.random())
            continue
        if r.status_code == 200:
            return r.json()
        last = f"{r.status_code}: {r.text[:300]}"
        if r.status_code == 429 or r.status_code >= 500:
            wait = r.headers.get("retry-after")
            time.sleep(min(float(wait) if wait and wait.replace('.', '').isdigit()
                           else 2 ** attempt + random.random(), 60))
            continue
        break  # 4xx other than 429 will not fix itself
    raise LLMError(last)


def _schema_hint(schema: dict) -> str:
    return ("\n\nReply with ONLY a JSON object that matches this JSON Schema. "
            "No prose, no code fences.\n" + json.dumps(schema))


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------

def _openai_compatible(p: dict, model: str, prompt: str, schema, system,
                       max_tokens: int, temperature: float):
    key = os.environ.get(p["key"]) if p["key"] else "ollama"
    if not key:
        raise LLMError(f"{p['key']} not set in .env")
    base = os.environ.get("LLM_BASE_URL") or p["base"]
    messages = []
    if system:
        messages.append({"role": "system", "content": system})
    messages.append({"role": "user",
                     "content": prompt + (_schema_hint(schema) if schema else "")})
    body = {"model": model, "messages": messages,
            "max_tokens": max_tokens, "temperature": temperature}
    if schema:
        body["response_format"] = {"type": "json_object"}
    try:
        data = _post(f"{base.rstrip('/')}/chat/completions",
                     {"Authorization": f"Bearer {key}"}, body)
    except LLMError as exc:
        # Some providers and some newer models reject temperature or
        # max_tokens. Retry once with the bare minimum before giving up.
        if "400" not in str(exc) and "422" not in str(exc):
            raise
        body.pop("temperature", None)
        body.pop("response_format", None)
        if "max_tokens" in str(exc):
            body["max_completion_tokens"] = body.pop("max_tokens")
        data = _post(f"{base.rstrip('/')}/chat/completions",
                     {"Authorization": f"Bearer {key}"}, body)
    try:
        return data["choices"][0]["message"]["content"] or ""
    except (KeyError, IndexError, TypeError):
        raise LLMError(f"unexpected response: {json.dumps(data)[:300]}")


def _anthropic(p: dict, model: str, prompt: str, schema, system,
               max_tokens: int, temperature: float):
    key = os.environ.get(p["key"])
    if not key:
        raise LLMError(f"{p['key']} not set in .env")
    body: dict = {"model": model, "max_tokens": max_tokens,
                  "temperature": temperature,
                  "messages": [{"role": "user", "content": prompt}]}
    if system:
        body["system"] = system
    if schema:
        # A forced tool call is the reliable way to get structured JSON.
        body["tools"] = [{"name": "respond", "description": "Return the result.",
                          "input_schema": schema}]
        body["tool_choice"] = {"type": "tool", "name": "respond"}
    data = _post(f"{p['base']}/messages",
                 {"x-api-key": key, "anthropic-version": "2023-06-01"}, body)
    blocks = data.get("content") or []
    if schema:
        for b in blocks:
            if b.get("type") == "tool_use":
                return b["input"]
        raise LLMError(f"no tool_use block: {json.dumps(data)[:300]}")
    return "".join(b.get("text", "") for b in blocks)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def generate(prompt: str, *, schema: dict | None = None, system: str | None = None,
             max_tokens: int = 8000, temperature: float = 0.4,
             model: str | None = None, **kwargs):
    """Drop-in for gemini.generate. Returns parsed JSON when `schema` is given
    (tagged with "_model"), otherwise text."""
    provider, default_model = current()
    model = model or default_model
    p = PROVIDERS[provider]

    if p["kind"] == "gemini":
        out = gemini.generate(prompt, schema=schema, system=system,
                              max_tokens=max_tokens, temperature=temperature,
                              model=None if model == p["model"] else model,
                              **kwargs)
        return out
    fn = _anthropic if p["kind"] == "anthropic" else _openai_compatible
    out = fn(p, model, prompt, schema, system, max_tokens, temperature)
    if not schema:
        return out
    parsed = out if isinstance(out, (dict, list)) else _parse_json(out)
    if isinstance(parsed, dict):
        parsed["_model"] = f"{provider}:{model}"
    return parsed


def list_models(provider: str) -> list[str]:
    p = PROVIDERS[provider]
    if p["kind"] == "gemini":
        return gemini.MODEL_POOL
    key = os.environ.get(p["key"]) if p["key"] else "ollama"
    if not key:
        raise LLMError(f"{p['key']} not set in .env")
    headers = ({"x-api-key": key, "anthropic-version": "2023-06-01"}
               if p["kind"] == "anthropic" else {"Authorization": f"Bearer {key}"})
    r = requests.get(f"{p['base']}/models", headers=headers, timeout=30)
    r.raise_for_status()
    return sorted(m["id"] for m in r.json().get("data", []))


def main() -> int:
    ap = argparse.ArgumentParser(description="Inspect and test LLM providers")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("providers", help="list supported providers")
    m = sub.add_parser("models", help="list models your key can use")
    m.add_argument("provider", choices=PROVIDERS)
    sub.add_parser("test", help="send one small request to the active provider")
    args = ap.parse_args()

    if args.cmd == "models":
        print("\n".join(list_models(args.provider)))
    elif args.cmd == "test":
        provider, model = current()
        print(f"{provider}:{model}")
        out = generate("Return a JSON object with the key ok set to true.",
                       schema={"type": "object",
                               "properties": {"ok": {"type": "boolean"}},
                               "required": ["ok"]}, max_tokens=200)
        print("ok" if out.get("ok") else f"unexpected: {out}")
    else:
        active = current()[0]
        for name, p in PROVIDERS.items():
            has = "set" if (p["key"] is None or os.environ.get(p["key"])) else "-"
            mark = "*" if name == active else " "
            print(f"{mark} {name:<11} {p['label']:<26} {p['model']:<40} key: {has}")
        print("\n* = active. Change with LLM_PROVIDER / LLM_MODEL in .env")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
