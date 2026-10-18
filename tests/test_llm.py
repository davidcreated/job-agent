"""Provider routing and response handling. No network: the HTTP layer is stubbed.

Run:  .venv/bin/python tests/test_llm.py
"""

import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
import llm  # noqa: E402

FAILS = []


def check(name, got, want):
    if got != want:
        FAILS.append(name)
        print(f"  FAIL {name}\n     got:  {got!r}\n     want: {want!r}")
    else:
        print(f"  ok   {name}")


SCHEMA = {"type": "object", "properties": {"ok": {"type": "boolean"}}}
sent = {}


def fake_post(url, headers, body, retries=4):
    sent.update(url=url, headers=headers, body=body)
    if "anthropic" in url:
        return {"content": [{"type": "tool_use", "input": {"ok": True}}]}
    return {"choices": [{"message": {"content": '```json\n{"ok": true}\n```'}}]}


llm._post = fake_post
for k in ("LLM_PROVIDER", "LLM_MODEL", "LLM_BASE_URL"):
    os.environ.pop(k, None)

print("\nrouting")
check("fifteen providers", len(llm.PROVIDERS), 15)
os.environ["LLM_PROVIDER"] = "openai"
os.environ["OPENAI_API_KEY"] = "k"
out = llm.generate("hi", schema=SCHEMA, system="s")
check("openai path hits chat/completions", sent["url"].endswith("/chat/completions"), True)
check("fenced JSON is parsed", out["ok"], True)
check("result is tagged with provider:model", out["_model"].startswith("openai:"), True)
check("schema is sent as a prompt hint",
      "JSON Schema" in sent["body"]["messages"][-1]["content"], True)

os.environ["LLM_PROVIDER"] = "anthropic"
os.environ["ANTHROPIC_API_KEY"] = "k"
out = llm.generate("hi", schema=SCHEMA)
check("anthropic returns tool_use input", out["ok"], True)
check("anthropic forces the tool call", sent["body"]["tool_choice"]["type"], "tool")

print("\nguards")
os.environ["LLM_PROVIDER"] = "nope"
try:
    llm.current()
    check("unknown provider is rejected", "no error", "LLMError")
except llm.LLMError:
    check("unknown provider is rejected", True, True)

os.environ["LLM_PROVIDER"] = "groq"
os.environ["LLM_MODEL"] = "gemini-3.8-flash"
check("a Gemini model name never leaks into another provider",
      llm.current()[1], llm.PROVIDERS["groq"]["model"])

os.environ["LLM_PROVIDER"] = "mistral"
os.environ.pop("LLM_MODEL")
os.environ.pop("MISTRAL_API_KEY", None)
try:
    llm.generate("hi")
    check("missing key fails loudly", "no error", "LLMError")
except llm.LLMError as e:
    check("missing key fails loudly", "MISTRAL_API_KEY" in str(e), True)

os.environ["LLM_PROVIDER"] = "ollama"
check("ollama needs no key", llm.generate("hi") is not None, True)

print()
sys.exit(1 if FAILS else 0)
