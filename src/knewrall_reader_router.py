"""Config-driven, standalone text-completion transports for the ARL."""

from __future__ import annotations

import json
import hashlib
import os
import platform
import sys
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

from .paths import get_root


@dataclass(frozen=True)
class Provider:
    name: str
    transport: str
    model: str
    options: Dict[str, Any]


@dataclass
class ProviderStatus:
    available: bool
    reason: str
    resolved: Optional[str] = None


@dataclass
class Completion:
    text: str = ""
    error: Optional[str] = None
    provider: str = ""
    model: str = ""
    transport: str = ""
    elapsed_s: float = 0.0
    detail: Optional[str] = None


_probe_cache: Dict[str, Any] = {}


def _assistant_config(config: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    if config is not None:
        return config.get("assistant", config)
    try:
        with open(get_root() / ".knewrall" / "config.json", "r", encoding="utf-8") as fh:
            return json.load(fh).get("assistant", {})
    except (OSError, json.JSONDecodeError):
        return {}


def _providers(config: Optional[Dict[str, Any]] = None) -> Dict[str, Provider]:
    assistant = _assistant_config(config)
    return {
        name: Provider(name, data.get("transport", "cli"), data.get("model", ""), data)
        for name, data in assistant.get("providers", {}).items()
    }


def detect(config: Optional[Dict[str, Any]] = None, *, refresh: bool = False) -> Dict[str, ProviderStatus]:
    assistant = _assistant_config(config)
    cache_seconds = float(assistant.get("detect_cache_seconds", 3600))
    cache_path = get_root() / ".knewrall" / "reader_probe.json"
    now = time.time()
    key_material = {
        "providers": assistant.get("providers", {}),
        "platform": sys.platform,
        "machine": platform.node(),
    }
    config_key = hashlib.sha256(json.dumps(key_material, sort_keys=True).encode()).hexdigest()
    if not refresh and _probe_cache.get("config_key") == config_key and now - _probe_cache.get("at", 0) < cache_seconds:
        return _probe_cache["result"]
    if not refresh:
        cached = _read_probe_cache(cache_path, config_key, cache_seconds, now)
        if cached is not None:
            _probe_cache.update(config_key=config_key, at=now, result=cached)
            return cached
    result: Dict[str, ProviderStatus] = {}
    for name, provider in _providers(config).items():
        if provider.transport == "cli":
            binary = provider.options.get("binary", name)
            resolved = shutil.which(binary)
            result[name] = ProviderStatus(bool(resolved), f"{resolved}" if resolved else f"binary not found: {binary}", resolved)
        elif provider.transport == "http":
            keys = provider.options.get("key_env", [])
            key = next((os.environ.get(k) for k in keys if os.environ.get(k)), None)
            result[name] = ProviderStatus(bool(key or not keys), "key available" if key else ("no key configured" if keys else "no key required"))
        else:
            result[name] = ProviderStatus(False, f"unsupported transport: {provider.transport}")
    _probe_cache.update(config_key=config_key, at=now, result=result)
    try:
        cache_path.parent.mkdir(parents=True, exist_ok=True)
        cache_path.write_text(json.dumps({"at": now, "config_key": config_key, "result": {k: vars(v) for k, v in result.items()}}, indent=2) + "\n", encoding="utf-8")
    except OSError:
        pass
    return result


def _read_probe_cache(path: Path, config_key: str, ttl: float, now: float) -> Optional[Dict[str, ProviderStatus]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("config_key") != config_key or now - float(data["at"]) >= ttl:
            return None
        return {name: ProviderStatus(**status) for name, status in data["result"].items()}
    except (OSError, ValueError, KeyError, TypeError):
        return None


def resolve(config: Optional[Dict[str, Any]] = None, *, provider: Optional[str] = None) -> Optional[Provider]:
    assistant = _assistant_config(config)
    providers = _providers(config)
    if provider:
        status = detect(config).get(provider)
        return providers.get(provider) if status and status.available else None
    order = [assistant["provider"]] if assistant.get("provider") else []
    order += [name for name in assistant.get("fallback_order", []) if name not in order]
    if not order:
        order = list(providers)
    statuses = detect(config)
    for name in order:
        if name in providers and statuses.get(name, ProviderStatus(False, "missing")).available:
            return providers[name]
    return None


def _first_error(stderr: bytes) -> str:
    return stderr.decode("utf-8", errors="replace").strip().splitlines()[0] if stderr.strip() else "provider returned no output"


def complete(provider: Provider, prompt: str, *, deadline_s: Optional[float] = None) -> Completion:
    started = time.monotonic()
    result = Completion(provider=provider.name, model=provider.model, transport=provider.transport)
    try:
        if provider.transport == "cli":
            options = provider.options
            via = options.get("prompt_via", "stdin")
            if via == "argv":
                limit = int(options.get("max_prompt_chars", 7500))
                if len(prompt) > limit:
                    result.error = "content_too_large"
                    return result
                args = [str(a).replace("{model}", provider.model).replace("{prompt}", prompt) for a in options.get("args", ["run", "-m", "{model}"])]
                input_data = None
            else:
                args = [str(a).replace("{model}", provider.model) for a in options.get("args", ["run", "-m", "{model}"])]
                input_data = prompt.encode("utf-8")
            resolved = shutil.which(options.get("binary", provider.name))
            if not resolved:
                result.error = "no_provider"
                return result
            proc = subprocess.run([resolved, *args], input=input_data, capture_output=True,
                                  timeout=deadline_s or options.get("timeout_seconds", 60))
            if proc.returncode != 0 or not proc.stdout.strip():
                result.error = "cli_error"
                result.detail = _first_error(proc.stderr)
            else:
                result.text = proc.stdout.decode("utf-8", errors="replace")
        elif provider.transport == "http":
            options = provider.options
            keys = options.get("key_env", [])
            key = next((os.environ.get(k) for k in keys if os.environ.get(k)), None)
            headers = {"Content-Type": "application/json"}
            if key:
                headers["Authorization"] = f"Bearer {key}"
            payload = json.dumps({"model": provider.model, "temperature": options.get("temperature", 0), "messages": [{"role": "user", "content": prompt}]}).encode()
            request = urllib.request.Request(options.get("base_url", "") + options.get("endpoint", "/chat/completions"), data=payload, headers=headers)
            deadline = started + (deadline_s or options.get("timeout_seconds", 45))
            retries = int(options.get("max_retries", 0))
            for attempt in range(retries + 1):
                try:
                    with urllib.request.urlopen(request, timeout=max(0.1, deadline - time.monotonic())) as response:
                        body = json.loads(response.read().decode("utf-8"))
                    result.text = body["choices"][0]["message"]["content"]
                    if not isinstance(result.text, str) or not result.text.strip():
                        result.text = ""
                        result.error = "http_error"
                        result.detail = "provider returned no output"
                    break
                except urllib.error.HTTPError as exc:
                    if exc.code not in (429, 500, 502, 503, 504) or attempt >= retries:
                        result.error = "rate_limited" if exc.code == 429 else "http_error"
                        result.detail = str(exc)
                        break
                    retry_after = exc.headers.get("Retry-After")
                    delay = float(retry_after) if retry_after else min(2 ** attempt, 8)
                    if time.monotonic() + delay >= deadline:
                        result.error = "timeout"
                        break
                    time.sleep(delay)
        else:
            result.error = "unsupported_transport"
    except subprocess.TimeoutExpired:
        result.error = "timeout"
    except urllib.error.HTTPError as exc:
        result.error = "rate_limited" if exc.code == 429 else "http_error"
        result.detail = str(exc)
    except json.JSONDecodeError as exc:
        result.error = "parse_error"
        result.detail = str(exc)
    except Exception as exc:
        result.error = "http_error" if provider.transport == "http" else "cli_error"
        result.detail = str(exc)
    finally:
        result.elapsed_s = time.monotonic() - started
    return result
