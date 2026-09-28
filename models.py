"""Map a raw model id to the lab that trained it and a key-sized name.

Feeds report whatever string their harness uses: ``claude-opus-5-5[1m]``,
``openai-codex/gpt-6-astra``, ``cursor-grok-4.6-high-fast``. The deck wants
two things from that: whose mark to draw, and a name that fits one line.
"""
from __future__ import annotations

import re

#: First token of a model name -> provider id (``logos/providers/<id>.svg``).
FAMILY_PROVIDER = {
    "claude": "anthropic", "opus": "anthropic", "sonnet": "anthropic",
    "haiku": "anthropic", "fable": "anthropic",
    "gpt": "openai", "o1": "openai", "o3": "openai", "o4": "openai",
    "codex": "openai", "chatgpt": "openai",
    "grok": "xai",
    "gemini": "google", "gemma": "google",
    "deepseek": "deepseek",
    "mimo": "xiaomi",
    "glm": "zai",
    "kimi": "moonshot",
    "qwen": "qwen", "qwq": "qwen",
    "minimax": "minimax",
    "mistral": "mistral", "devstral": "mistral", "codestral": "mistral",
    "magistral": "mistral",
    "llama": "meta",
    "nemotron": "nvidia",
    "composer": "cursor",
}

#: Router/vendor path prefixes that name the lab when the model token does not.
VENDOR_PROVIDER = {
    "anthropic": "anthropic", "openai": "openai", "openai-codex": "openai",
    "x-ai": "xai", "xai": "xai", "google": "google", "deepseek": "deepseek",
    "moonshotai": "moonshot", "qwen": "qwen", "mistralai": "mistral",
    "meta-llama": "meta", "nvidia": "nvidia", "z-ai": "zai", "zai": "zai",
    "xiaomi": "xiaomi", "minimax": "minimax",
}

CASING = {
    "gpt": "GPT", "glm": "GLM", "mimo": "MiMo", "deepseek": "DeepSeek",
    "minimax": "MiniMax", "qwq": "QwQ", "o1": "o1", "o3": "o3", "o4": "o4",
}

#: Suffix tokens that describe how the model is served, not which model it is.
_NOISE = re.compile(
    r"^(\d+[kmb]|\d{8}|latest|preview|free|low|medium|high|xhigh|max|fast|thinking)$"
)

MAX_CHARS = 12


def _split(model: str) -> tuple[str, list[str]]:
    raw = re.sub(r"\[.*?\]", "", str(model or "").strip().lower())
    vendor, _, name = raw.rpartition("/")
    vendor = vendor.rsplit("/", 1)[-1]
    tokens = [t for t in name.split("-") if t]
    if tokens and tokens[0] == "cursor" and len(tokens) > 1:
        tokens = tokens[1:]
    return vendor, tokens


def provider(model: str) -> str:
    vendor, tokens = _split(model)
    for token in tokens[:2]:
        family = token if token in FAMILY_PROVIDER else re.sub(r"[\d.]+$", "", token)
        if family in FAMILY_PROVIDER:
            return FAMILY_PROVIDER[family]
    return VENDOR_PROVIDER.get(vendor, "")


def _word(token: str) -> str:
    if token in CASING:
        return CASING[token]
    if re.fullmatch(r"[vk][\d.]+", token):
        return token.upper()
    return token[:1].upper() + token[1:]


def short_name(model: str, limit: int = MAX_CHARS) -> str:
    """``claude-opus-5-5`` -> ``Opus 5.5``; ``gpt-6-astra-900k`` -> ``GPT-6 Astra``."""
    _, tokens = _split(model)
    if not tokens:
        return ""
    head = [tokens[0]]
    for token in tokens[1:]:
        if not _NOISE.match(token):
            head.append(token)
    tokens = head
    if tokens[0] == "claude" and len(tokens) > 1:
        tokens = tokens[1:]
        # Legacy ids put the version first: claude-3-5-sonnet.
        family = [t for t in tokens if not t.isdigit()]
        tokens = family[:1] + [t for t in tokens if t.isdigit()] + family[1:]
    words: list[str] = []
    for token in tokens:
        if (token.isdigit() and len(token) == 1 and words
                and re.fullmatch(r"\d+(\.\d+)*", words[-1])):
            words[-1] += "." + token
        else:
            words.append(token)
    if words[0] == "gpt" and len(words) > 1:
        words = ["GPT-" + words[1], *words[2:]]
        fallback = [words[0][4:], *words[1:]]
    else:
        fallback = words[1:]
    full = " ".join(map(_word, words))
    if len(full) > limit and fallback and provider(model):
        full = " ".join(map(_word, fallback))
    return full[:limit].rstrip()
