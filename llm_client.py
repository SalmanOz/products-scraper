"""Unified LLM access: NVIDIA NIM primary, Gemini fallback (order configurable).

Every generation path in this repo (blog articles, quality-gate judge,
product AI verdicts) previously died when a single provider was rate-limited
or down — and the blog runs unattended on a cron. Both providers behind one
client keeps every caller alive when one of them fails.

Env:
  NVIDIA_API_KEY                   — build.nvidia.com
  GEMINI_API_KEY / GOOGLE_API_KEY  — Google AI Studio
  LLM_PRIMARY                      — "nvidia" (default) or "gemini"
  NVIDIA_MODEL                     — override the default NIM model

Default NIM model is qwen/qwen3.5-397b — the strongest verified multilingual
writer on the catalog (Turkish quality + structure adherence, which the
[TITLE]/[SUMMARY]/[CONTENT] output contract depends on). If the configured
model ever 404s (catalog rotation), the client silently retries with the
long-lived meta/llama-3.3-70b-instruct before giving up on NVIDIA.

chat() returns the generated text or raises RuntimeError when every provider
failed. json_mode asks both providers for a JSON object response.
"""

import json
import logging
import os
import time

import requests

GEMINI_MODELS = ["gemini-2.5-flash", "gemini-2.0-flash"]
NVIDIA_URL = "https://integrate.api.nvidia.com/v1/chat/completions"
NVIDIA_DEFAULT_MODEL = "qwen/qwen3.5-397b"
NVIDIA_SAFE_MODEL = "meta/llama-3.3-70b-instruct"


def _gemini_chat(prompt, api_key, temperature, max_tokens, json_mode):
    """Gemini REST with per-model retry and MAX_TOKENS continuation (text
    mode only — a truncated JSON payload can't be continued reliably)."""
    gen_config = {"temperature": temperature, "maxOutputTokens": max_tokens}
    if json_mode:
        gen_config["responseMimeType"] = "application/json"

    for model in GEMINI_MODELS:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}"
        retry_delay = 5
        for attempt in range(3):
            try:
                contents = [{"role": "user", "parts": [{"text": prompt}]}]
                resp = requests.post(url, json={"contents": contents, "generationConfig": gen_config}, timeout=90)
                if resp.status_code in (429, 503) and attempt < 2:
                    logging.warning(f"[llm] {model} -> {resp.status_code}, retrying in {retry_delay}s")
                    time.sleep(retry_delay)
                    retry_delay *= 2
                    continue
                if resp.status_code != 200:
                    logging.warning(f"[llm] {model} failed: {resp.status_code} {resp.text[:150]}")
                    break

                candidate = resp.json()["candidates"][0]
                text = candidate["content"]["parts"][0]["text"]
                finish = candidate.get("finishReason", "STOP")

                cont = 0
                while finish == "MAX_TOKENS" and cont < 3 and not json_mode:
                    cont += 1
                    logging.info(f"[llm] {model} truncated, continuing ({cont}/3)")
                    contents.append({"role": "model", "parts": [{"text": text}]})
                    contents.append({"role": "user", "parts": [{"text": "Yazın yarım kaldı. En son kaldığın cümleden itibaren devam et, tamamen bitirene kadar."}]})
                    c_resp = requests.post(url, json={"contents": contents, "generationConfig": gen_config}, timeout=90)
                    if c_resp.status_code != 200:
                        break
                    c_cand = c_resp.json()["candidates"][0]
                    piece = c_cand["content"]["parts"][0]["text"]
                    text += " " + piece.strip()
                    finish = c_cand.get("finishReason", "STOP")
                return text
            except Exception as e:
                logging.warning(f"[llm] {model} exception: {e}")
                break
    return None


def _nvidia_chat(prompt, api_key, temperature, max_tokens, json_mode):
    """OpenAI-compatible NIM call with finish_reason=length continuation and a
    known-good model fallback when the configured model isn't in the catalog."""
    configured = os.getenv("NVIDIA_MODEL", NVIDIA_DEFAULT_MODEL)
    model_candidates = [configured] + ([NVIDIA_SAFE_MODEL] if configured != NVIDIA_SAFE_MODEL else [])
    headers = {"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"}

    for model in model_candidates:
        messages = [{"role": "user", "content": prompt}]
        payload = {"model": model, "messages": messages, "temperature": temperature,
                   "max_tokens": min(max_tokens, 8192)}
        if json_mode:
            payload["response_format"] = {"type": "json_object"}

        for attempt in range(3):
            try:
                resp = requests.post(NVIDIA_URL, headers=headers, json=payload, timeout=120)
                if resp.status_code == 429 and attempt < 2:
                    time.sleep(10 * (attempt + 1))
                    continue
                if resp.status_code == 404:
                    logging.warning(f"[llm] nvidia model '{model}' not found, trying next candidate")
                    break
                if resp.status_code != 200:
                    # Some NIM models reject response_format — retry once without it
                    if json_mode and "response_format" in payload:
                        logging.warning(f"[llm] nvidia {resp.status_code}, retrying without response_format")
                        payload.pop("response_format")
                        continue
                    logging.warning(f"[llm] nvidia {model} failed: {resp.status_code} {resp.text[:150]}")
                    break

                choice = resp.json()["choices"][0]
                text = choice["message"]["content"]
                cont = 0
                while choice.get("finish_reason") == "length" and cont < 3 and not json_mode:
                    cont += 1
                    messages.append({"role": "assistant", "content": text})
                    messages.append({"role": "user", "content": "Devam et, kaldığın yerden tamamla."})
                    c_resp = requests.post(NVIDIA_URL, headers=headers, json=payload, timeout=120)
                    if c_resp.status_code != 200:
                        break
                    choice = c_resp.json()["choices"][0]
                    text += " " + choice["message"]["content"].strip()
                return text
            except Exception as e:
                logging.warning(f"[llm] nvidia {model} exception: {e}")
                break
    return None


def chat(prompt, temperature=0.7, max_tokens=8192, json_mode=False):
    """Primary provider first (LLM_PRIMARY, default nvidia), the other as
    fallback. Raises RuntimeError if neither provider is configured or all
    attempts fail. A provider without credentials is simply skipped, so a
    single-key setup keeps working unchanged."""
    gemini_key = os.getenv("GEMINI_API_KEY") or os.getenv("GOOGLE_API_KEY")
    nvidia_key = os.getenv("NVIDIA_API_KEY")
    if not gemini_key and not nvidia_key:
        raise RuntimeError("No LLM credentials: set NVIDIA_API_KEY and/or GEMINI_API_KEY/GOOGLE_API_KEY.")

    providers = {
        "nvidia": (nvidia_key, _nvidia_chat),
        "gemini": (gemini_key, _gemini_chat),
    }
    primary = os.getenv("LLM_PRIMARY", "nvidia").strip().lower()
    if primary not in providers:
        primary = "nvidia"
    order = [primary] + [p for p in providers if p != primary]

    tried = []
    for name in order:
        key, fn = providers[name]
        if not key:
            continue
        text = fn(prompt, key, temperature, max_tokens, json_mode)
        if text:
            return text
        tried.append(name)
        logging.warning(f"[llm] provider '{name}' exhausted, trying next")

    raise RuntimeError(f"All LLM providers failed ({' + '.join(tried) or 'none configured'}).")
