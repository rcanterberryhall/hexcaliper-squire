"""
llm.py — Unified LLM provider abstraction.

Routes analysis prompts to the configured provider:

  - **ollama** (default): Local Ollama via merLLM proxy at ``config.OLLAMA_URL``.
  - **ollama_cloud**: Ollama paid API at ``config.ESCALATION_API_URL``.
  - **claude**: Anthropic Claude API.

All callers use ``generate()`` which returns the raw response text.
The caller is responsible for JSON parsing.

Ollama calls use ``stream=True`` so that merLLM can display live token
activity in its GPU status cards.  Qwen3 ``<think>`` blocks are stripped
automatically so callers always receive the final answer text only.
"""
import json
import logging
import re

import requests

import config

log = logging.getLogger(__name__)

_THINK_RE = re.compile(r"<think>.*?</think>", re.DOTALL)

# Matches untagged chain-of-thought preambles that Qwen3 sometimes emits
# when ``think: false`` is ignored.  Looks for one or more reasoning
# paragraphs followed by a clear answer paragraph.
_UNTAGGED_COT_RE = re.compile(
    r"^(?:Okay|Alright|Let me|So,? |First|Hmm|Now|Looking|I need|I should|I'll|The user)"
    r".*?(?:\n\n)",
    re.DOTALL,
)


def _strip_think(text: str) -> str:
    """Remove Qwen3 ``<think>…</think>`` reasoning blocks from LLM output."""
    return _THINK_RE.sub("", text).strip()


def _strip_untagged_think(text: str) -> str:
    """Strip untagged chain-of-thought when the model ignores ``think: false``.

    Qwen3 sometimes emits its reasoning as plain text (no ``<think>`` tags)
    followed by the actual answer after a double-newline break.  This
    function repeatedly removes leading CoT paragraphs until the text starts
    with actual answer content.
    """
    stripped = text.strip()
    while True:
        m = _UNTAGGED_COT_RE.match(stripped)
        if not m:
            break
        stripped = stripped[m.end():].strip()
    return stripped if stripped else text.strip()


def _collect_stream(resp: requests.Response) -> str:
    """Read an Ollama NDJSON stream and return the concatenated response text.

    Strips Qwen3 thinking tags so callers always get the final answer only.
    When the model places its answer in the ``thinking`` NDJSON field (instead
    of ``response``), the thinking content is used as a fallback so output is
    never silently lost.
    """
    parts: list[str] = []
    think_parts: list[str] = []
    for line in resp.iter_lines():
        if not line:
            continue
        try:
            obj = json.loads(line)
        except json.JSONDecodeError:
            continue
        token = obj.get("response", "")
        if token:
            parts.append(token)
        think_token = obj.get("thinking", "")
        if think_token:
            think_parts.append(think_token)
    text = _strip_think("".join(parts))
    if not text.strip() and think_parts:
        # Model placed its answer in the thinking field — extract it.
        text = _strip_think("".join(think_parts))
    return text


def generate(
    prompt: str,
    *,
    format: str | None = "json",
    temperature: float = 0.1,
    num_predict: int = 768,
    num_ctx: int = 8192,
    timeout: int = 90,
    priority: str = "short",
) -> str:
    """
    Send a prompt to the configured LLM provider and return the response text.

    :param prompt: The full prompt string.
    :param format: Response format hint ("json" or None). Used by Ollama;
                   for Claude, the system prompt requests JSON output.
    :param temperature: Sampling temperature.
    :param num_predict: Max tokens to generate.
    :param num_ctx: Context window size (Ollama only).
    :param timeout: Request timeout in seconds.
    :param priority: merLLM priority bucket (``chat``, ``embeddings``,
        ``short``, ``feedback``, ``background``). Forwarded as the
        ``X-Priority`` header on Ollama calls. Defaults to ``short`` so
        any new call site that forgets to choose lands in a safe middle
        bucket instead of starving chat or jumping ahead of bulk work.
        Note: ``embeddings`` is auto-routed by merLLM at the
        ``/api/embeddings`` endpoint, so generate/chat callers should
        not pick it (merLLM#38).
    :return: Raw response text from the LLM.
    :raises requests.HTTPError: On non-2xx response.
    """
    provider = config.ESCALATION_PROVIDER or "ollama"

    if provider == "claude":
        text = _claude(prompt, temperature=temperature,
                       max_tokens=num_predict, timeout=timeout,
                       json_mode=format == "json")
    elif provider == "ollama_cloud":
        text = _ollama_cloud(prompt, format=format,
                             temperature=temperature,
                             num_predict=num_predict,
                             num_ctx=num_ctx, timeout=timeout)
    else:
        text = _ollama_local(prompt, format=format,
                             temperature=temperature,
                             num_predict=num_predict,
                             num_ctx=num_ctx, timeout=timeout,
                             priority=priority)

    # For free-text (non-JSON) responses, strip untagged chain-of-thought
    # that Qwen3 emits when it ignores the think:false option.
    if format != "json":
        text = _strip_untagged_think(text)
    return text


def _ollama_local(
    prompt: str, *, format: str | None, temperature: float,
    num_predict: int, num_ctx: int, timeout: int, priority: str,
) -> str:
    """Call Ollama via the local merLLM proxy (streaming)."""
    body: dict = {
        "model":   config.effective_model(),
        "prompt":  prompt,
        "stream":  True,
        "options": {"temperature": temperature, "num_predict": num_predict,
                    "num_ctx": num_ctx, "think": False},
    }
    if format:
        body["format"] = format

    resp = requests.post(
        config.OLLAMA_URL,
        headers=config.ollama_headers(priority=priority),
        json=body,
        timeout=timeout,
        stream=True,
    )
    resp.raise_for_status()
    return _collect_stream(resp)


def _ollama_cloud(
    prompt: str, *, format: str | None, temperature: float,
    num_predict: int, num_ctx: int, timeout: int,
) -> str:
    """Call Ollama paid cloud API (streaming)."""
    url = config.ESCALATION_API_URL or config.OLLAMA_URL
    headers = {"Content-Type": "application/json"}
    if config.ESCALATION_API_KEY:
        headers["Authorization"] = f"Bearer {config.ESCALATION_API_KEY}"

    body: dict = {
        "model":   config.effective_model(),
        "prompt":  prompt,
        "stream":  True,
        "options": {"temperature": temperature, "num_predict": num_predict,
                    "num_ctx": num_ctx, "think": False},
    }
    if format:
        body["format"] = format

    resp = requests.post(
        url if "/api/" in url else f"{url.rstrip('/')}/api/generate",
        headers=headers,
        json=body,
        timeout=timeout,
        stream=True,
    )
    resp.raise_for_status()
    return _collect_stream(resp)


def _claude(
    prompt: str, *, temperature: float, max_tokens: int,
    timeout: int, json_mode: bool,
) -> str:
    """Call the Anthropic Claude Messages API."""
    api_key = config.ESCALATION_API_KEY
    if not api_key:
        raise ValueError("ESCALATION_API_KEY is required for Claude provider")

    url = config.ESCALATION_API_URL or "https://api.anthropic.com"
    model = config.effective_model() or "claude-sonnet-4-20250514"

    system = "You are an analysis assistant. Return structured data only."
    if json_mode:
        system += " Always respond with valid JSON and nothing else."

    r = requests.post(
        f"{url.rstrip('/')}/v1/messages",
        headers={
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "content-type": "application/json",
        },
        json={
            "model": model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "system": system,
            "messages": [{"role": "user", "content": prompt}],
        },
        timeout=timeout,
    )
    r.raise_for_status()
    data = r.json()

    # Extract text from Claude's response format
    content = data.get("content", [])
    parts = [block["text"] for block in content if block.get("type") == "text"]
    return "\n".join(parts)
