"""
AI-powered emoji-to-word generator.
Uses free LLM APIs to generate searchable words for any emoji.
Supports multiple providers in parallel for richer results.

Providers:
  - Gemini (Google) — free 1500 req/day. Get key at: https://aistudio.google.com/apikey
  - Groq — free tier. Get key at: https://console.groq.com/keys  (optional)
  - HuggingFace — free Inference API. Get key at: https://huggingface.co/settings/tokens  (optional)

Caches results to avoid repeated API calls.
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
from pathlib import Path
from typing import Optional

logger = logging.getLogger("stickerbot.ai")

# ── Cache ────────────────────────────────────────────────────────────

_CACHE_FILE = Path(__file__).parent / "ai_words_cache.json"
_cache: dict[str, list[str]] = {}


def _load_cache() -> None:
    """Load cached AI words from disk."""
    global _cache
    if _CACHE_FILE.exists():
        try:
            _cache = json.loads(_CACHE_FILE.read_text(encoding="utf-8"))
        except Exception:
            _cache = {}


def _save_cache() -> None:
    """Save cache to disk."""
    _CACHE_FILE.write_text(json.dumps(_cache, ensure_ascii=False, indent=2), encoding="utf-8")


# Load cache on import
_load_cache()


# ── Prompt ───────────────────────────────────────────────────────────

PROMPT = """For the emoji "{emoji}", generate 8-12 searchable keywords.
Include BOTH Russian and English words (at least 4 of each).
Keywords: single words, lowercase, no punctuation.
Return ONLY a comma-separated list, nothing else.

Example for 😂: смех,смешно,ржака,лол,угар,хохот,laugh,lol,funny,haha,rofl
Example for ❤️: любовь,сердце,люблю,обожаю,like,love,heart,valentine"""


# ── Gemini ───────────────────────────────────────────────────────────

async def _gemini_words(emoji: str, api_key: str) -> list[str]:
    """Generate words using Google Gemini API."""
    try:
        import google.generativeai as genai
    except ImportError:
        logger.warning("google-generativeai not installed. Install: pip install google-generativeai")
        return []

    try:
        genai.configure(api_key=api_key)
        model = genai.GenerativeModel("gemini-2.0-flash")
        prompt = PROMPT.format(emoji=emoji)
        response = await asyncio.to_thread(
            model.generate_content, prompt,
            generation_config={"temperature": 0.4, "max_output_tokens": 100}
        )
        text = (response.text or "").strip().lower()
        words = [w.strip() for w in text.replace("\n", ",").split(",") if w.strip()]
        logger.debug("Gemini: %s → %s", emoji, words[:5])
        return words[:15]
    except Exception as e:
        logger.warning("Gemini error for %s: %s", emoji, e)
        return []


# ── Groq (optional) ──────────────────────────────────────────────────

async def _groq_words(emoji: str, api_key: str) -> list[str]:
    """Generate words using Groq API (Llama 3)."""
    try:
        from groq import AsyncGroq
    except ImportError:
        return []

    try:
        client = AsyncGroq(api_key=api_key)
        prompt = PROMPT.format(emoji=emoji)
        response = await client.chat.completions.create(
            model="llama-3.1-8b-instant",
            messages=[{"role": "user", "content": prompt}],
            temperature=0.3,
            max_tokens=100,
        )
        text = (response.choices[0].message.content or "").strip().lower()
        words = [w.strip() for w in text.replace("\n", ",").split(",") if w.strip()]
        logger.debug("Groq: %s → %s", emoji, words[:5])
        return words[:15]
    except Exception as e:
        logger.debug("Groq error for %s: %s", emoji, e)
        return []


# ── HuggingFace (optional) ───────────────────────────────────────────

async def _hf_words(emoji: str, api_key: str) -> list[str]:
    """Generate words using HuggingFace Inference API."""
    try:
        import aiohttp
    except ImportError:
        return []

    try:
        prompt = PROMPT.format(emoji=emoji)
        async with aiohttp.ClientSession() as session:
            async with session.post(
                "https://api-inference.huggingface.co/models/mistralai/Mistral-7B-Instruct-v0.3",
                headers={"Authorization": f"Bearer {api_key}"},
                json={"inputs": prompt, "parameters": {"max_new_tokens": 100, "temperature": 0.3}},
                timeout=aiohttp.ClientTimeout(total=15),
            ) as resp:
                if resp.status != 200:
                    return []
                data = await resp.json()
                text = (data[0].get("generated_text", "") if isinstance(data, list) else "").strip().lower()
                # Remove the prompt from response
                if prompt.lower() in text:
                    text = text.split(prompt.lower(), 1)[-1]
                words = [w.strip() for w in text.replace("\n", ",").split(",") if w.strip()]
                logger.debug("HF: %s → %s", emoji, words[:5])
                return words[:15]
    except Exception:
        return []


# ── Orchestrator ─────────────────────────────────────────────────────

async def generate_words(emoji: str) -> list[str]:
    """
    Generate search words for an emoji using all available AI providers.
    Results are merged, deduplicated, and cached.

    Returns empty list if no AI providers are configured or all fail.
    """
    # Check cache first
    if emoji in _cache:
        return _cache[emoji]

    # Collect API keys
    providers = []

    gemini_key = os.environ.get("GEMINI_API_KEY", "")
    groq_key = os.environ.get("GROQ_API_KEY", "")
    hf_key = os.environ.get("HF_API_KEY", "")

    # Run all available providers in parallel
    tasks = []
    if gemini_key:
        tasks.append(_gemini_words(emoji, gemini_key))
    if groq_key:
        tasks.append(_groq_words(emoji, groq_key))
    if hf_key:
        tasks.append(_hf_words(emoji, hf_key))

    if not tasks:
        logger.debug("No AI providers configured for emoji: %s", emoji)
        return []

    results = await asyncio.gather(*tasks, return_exceptions=True)

    # Merge and deduplicate
    all_words: list[str] = []
    seen: set[str] = set()
    for result in results:
        if isinstance(result, list):
            for word in result:
                if word and word not in seen:
                    seen.add(word)
                    all_words.append(word)

    # Cache
    _cache[emoji] = all_words
    _save_cache()

    if all_words:
        logger.info("AI: %s → %d words %s", emoji, len(all_words), all_words[:5])

    return all_words


def get_cached_words(emoji: str) -> list[str]:
    """Get previously cached AI words (no API call)."""
    return _cache.get(emoji, [])
