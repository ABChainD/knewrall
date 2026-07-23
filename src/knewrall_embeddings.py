"""
Knewrall Embeddings Adapter

Provider-agnostic adapter that converts text to embedding vectors using an
OpenAI-compatible API endpoint.  Supported providers:

  - OpenRouter  (default for testing)  OPENROUTER_API_KEY
  - Google AI   (Gemini embeddings)    GOOGLE_API_KEY
  - OpenAI-compatible (any base_url)   KNEWRALL_EMBED_API_KEY

API keys are read **only** from environment variables — never from files or
the config.json, so they're never accidentally synced.

Each provider also has a dedicated "*_EMBEDS" variable (e.g.
OPENROUTER_API_KEY_EMBEDS) checked *before* its shared key. This lets a
Knewrall install use its own quota-isolated key for embeddings without
touching whatever key other tools/agents in the same workspace already use
for that provider for unrelated (chat/coding) calls.

If no key is found or the request fails the adapter returns None so callers
can degrade gracefully to literal-only search.
"""

import json
import logging
import os
import time
import urllib.error
import urllib.request
from typing import List, Optional, Tuple

logger = logging.getLogger(__name__)

# ── Provider definitions ──────────────────────────────────────────────────────

_PROVIDERS = {
    "openrouter": {
        "base_url": "https://openrouter.ai/api/v1",
        "endpoint": "/embeddings",
        "key_env": "OPENROUTER_API_KEY",
        "default_model": "openai/text-embedding-3-small",
        "extra_headers": {
            "HTTP-Referer": "https://github.com/knewrall",
            "X-Title": "Knewrall",
        },
    },
    "openai": {
        "base_url": "https://api.openai.com/v1",
        "endpoint": "/embeddings",
        "key_env": "OPENAI_API_KEY",
        "default_model": "text-embedding-3-small",
        "extra_headers": {},
    },
    "google": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta",
        "endpoint": "/models/{model}:embedContent",
        "key_env": "GOOGLE_API_KEY",
        "default_model": "text-embedding-004",
        "extra_headers": {},
    },
}


class EmbeddingAdapter:
    """
    Thin, stdlib-only HTTP client for embedding text via an OpenAI-compatible
    or Google AI API.  No heavy SDK dependencies.

    Usage:
        adapter = EmbeddingAdapter(provider="openrouter",
                                   model="openai/text-embedding-3-small")
        vecs = adapter.embed(["Hello world", "Another text"])
        # vecs: list of float lists, or None on failure
    """

    def __init__(self,
                 provider: str = "openrouter",
                 model: Optional[str] = None,
                 base_url: Optional[str] = None,
                 api_key: Optional[str] = None,
                 timeout: int = 30,
                 max_retries: int = 3,
                 batch_size: int = 64):
        pdef = _PROVIDERS.get(provider)
        if pdef is None:
            raise ValueError(
                f"Unknown provider {provider!r}. "
                f"Choose from: {list(_PROVIDERS)}"
            )
        self.provider = provider
        self.model = model or pdef["default_model"]
        self.base_url = (base_url or pdef["base_url"]).rstrip("/")
        self.endpoint_tmpl = pdef["endpoint"]
        self.extra_headers: dict = pdef.get("extra_headers", {})
        self.timeout = timeout
        self.max_retries = max_retries
        self.batch_size = batch_size

        # Resolve API key: explicit arg > provider's dedicated embeds-only var
        # (e.g. OPENROUTER_API_KEY_EMBEDS) > generic fallback > provider's shared key.
        # The dedicated var is checked first so an install can scope a separate,
        # quota-isolated key to Knewrall embeddings without ever reading whatever
        # key other tools already have set for that provider's shared variable.
        key_env_embeds = f"{pdef['key_env']}_EMBEDS"
        self.api_key = (
            api_key
            or os.environ.get(key_env_embeds)
            or os.environ.get("KNEWRALL_EMBED_API_KEY")
            or os.environ.get(pdef["key_env"])
        )
        if not self.api_key:
            logger.warning(
                f"No API key found for provider {provider!r}. "
                f"Set {key_env_embeds} (recommended, dedicated), "
                f"KNEWRALL_EMBED_API_KEY, or {pdef['key_env']} to enable embeddings."
            )

    def is_available(self) -> bool:
        return bool(self.api_key)

    # ── Request helpers ───────────────────────────────────────────────────

    def _build_url(self) -> str:
        endpoint = self.endpoint_tmpl.replace("{model}", self.model)
        return self.base_url + endpoint

    def _request(self, payload: dict, timeout: Optional[int] = None,
                 max_retries: Optional[int] = None) -> Optional[dict]:
        """
        timeout/max_retries override this adapter's instance defaults for a
        single call — lets an interactive caller (query-time embed) ask for a
        fast-fail (short timeout, few/no retries) while a bulk/offline caller
        (indexing) keeps the generous instance defaults, without needing a
        second adapter instance or config.
        """
        timeout = self.timeout if timeout is None else timeout
        max_retries = self.max_retries if max_retries is None else max_retries

        url = self._build_url()
        body = json.dumps(payload).encode()
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.api_key}",
            **self.extra_headers,
        }
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")

        delay = 1.0
        for attempt in range(1, max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    return json.loads(resp.read().decode())
            except urllib.error.HTTPError as e:
                body_txt = e.read().decode(errors="replace")
                if e.code == 429 or e.code >= 500:
                    logger.warning(
                        f"HTTP {e.code} from {url} (attempt {attempt}/{max_retries}): "
                        f"{body_txt[:200]}"
                    )
                    if attempt < max_retries:
                        time.sleep(delay)
                        delay *= 2
                    continue
                logger.error(f"HTTP {e.code} from embeddings API: {body_txt[:400]}")
                return None
            except Exception as e:
                logger.warning(f"Embedding request failed (attempt {attempt}): {e}")
                if attempt < max_retries:
                    time.sleep(delay)
                    delay *= 2
        return None

    # ── OpenAI-compatible path ────────────────────────────────────────────

    def _embed_openai(self, texts: List[str], timeout: Optional[int] = None,
                      max_retries: Optional[int] = None) -> Optional[List[List[float]]]:
        """Call an OpenAI-compatible /embeddings endpoint."""
        resp = self._request({"model": self.model, "input": texts},
                              timeout=timeout, max_retries=max_retries)
        if resp is None:
            return None
        try:
            data = resp["data"]
            ordered = sorted(data, key=lambda x: x["index"])
            return [item["embedding"] for item in ordered]
        except (KeyError, TypeError) as e:
            logger.error(f"Unexpected embeddings response shape: {e} — {str(resp)[:200]}")
            return None

    # ── Google AI path ────────────────────────────────────────────────────

    def _embed_google(self, texts: List[str], timeout: Optional[int] = None,
                      max_retries: Optional[int] = None) -> Optional[List[List[float]]]:
        """
        Google's embedding API handles one text at a time with embedContent,
        or a batch with batchEmbedContents.  We use the batch endpoint.

        Has its own retry loop (Google's request shape differs from the
        OpenAI-compatible path, so it can't share _request()) — accepts the
        same timeout/max_retries overrides as _embed_openai for parity.
        """
        timeout = self.timeout if timeout is None else timeout
        max_retries = self.max_retries if max_retries is None else max_retries

        batch_url = (
            self.base_url
            + f"/models/{self.model}:batchEmbedContents?key={self.api_key}"
        )
        payload = {
            "requests": [
                {"model": f"models/{self.model}", "content": {"parts": [{"text": t}]}}
                for t in texts
            ]
        }
        body = json.dumps(payload).encode()
        req = urllib.request.Request(
            batch_url,
            data=body,
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        delay = 1.0
        for attempt in range(1, max_retries + 1):
            try:
                with urllib.request.urlopen(req, timeout=timeout) as resp:
                    data = json.loads(resp.read().decode())
                    return [item["values"] for item in data.get("embeddings", [])]
            except urllib.error.HTTPError as e:
                body_txt = e.read().decode(errors="replace")
                if e.code == 429 or e.code >= 500:
                    if attempt < max_retries:
                        time.sleep(delay)
                        delay *= 2
                    continue
                logger.error(f"Google AI HTTP {e.code}: {body_txt[:400]}")
                return None
            except Exception as e:
                logger.warning(f"Google AI request failed (attempt {attempt}): {e}")
                if attempt < max_retries:
                    time.sleep(delay)
                    delay *= 2
        return None

    # ── Public API ────────────────────────────────────────────────────────

    def embed(self, texts: List[str], timeout: Optional[int] = None,
             max_retries: Optional[int] = None) -> Optional[List[List[float]]]:
        """
        Embed a list of text strings.  Automatically batches.

        timeout/max_retries override this adapter's instance defaults for
        this call only — e.g. an interactive caller can pass a short
        timeout/no retries to fail fast, while a bulk/offline caller omits
        them and gets the instance's generous defaults.

        Returns:
            List of float vectors in the same order as input, or None on failure.
            Falls back to None if no API key is configured.
        """
        if not texts:
            return []
        if not self.api_key:
            logger.debug("Embedding skipped: no API key.")
            return None

        all_vectors: List[List[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i: i + self.batch_size]
            if self.provider == "google":
                vecs = self._embed_google(batch, timeout=timeout, max_retries=max_retries)
            else:
                vecs = self._embed_openai(batch, timeout=timeout, max_retries=max_retries)
            if vecs is None:
                return None
            all_vectors.extend(vecs)

        return all_vectors

    def embed_one(self, text: str, timeout: Optional[int] = None,
                 max_retries: Optional[int] = None) -> Optional[List[float]]:
        result = self.embed([text], timeout=timeout, max_retries=max_retries)
        if result and len(result) == 1:
            return result[0]
        return None

    def detect_dim(self) -> Optional[int]:
        """Embed a dummy string to detect the output dimension."""
        vec = self.embed_one("dim-probe")
        return len(vec) if vec else None


# ── Convenience factory ───────────────────────────────────────────────────────

def adapter_from_config(config: dict) -> EmbeddingAdapter:
    """
    Build an EmbeddingAdapter from the 'embeddings' section of config.json.

    config = {
        "provider": "openrouter",
        "model": "openai/text-embedding-3-small",
        "dim": 1536,
        ...
    }
    """
    emb = config.get("embeddings", {})
    return EmbeddingAdapter(
        provider=emb.get("provider", "openrouter"),
        model=emb.get("model"),
        base_url=emb.get("base_url"),
    )
