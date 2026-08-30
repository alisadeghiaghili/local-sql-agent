"""Endpoint trust — whether an LLM endpoint may see schema/business-rule/row data.

Before the OpenAI-only refactor, :mod:`llm.router` decided this by
**class**: a backend was "local" unless its ``(module, class name)`` pair
matched a small blocklist of hosted-provider classes (``OpenAIBackend``,
``AnthropicBackend``). That was coherent when every hosted transport had
its own class and every local transport was :class:`~llm.ollama_backend.OllamaBackend`.

Once :class:`~llm.providers.OpenAIBackend` is the *only* real transport —
a user's local ``gpt-oss`` server and OpenAI's own hosted API are both
instances of the exact same class, differing only in ``base_url`` — a
class-keyed blocklist breaks in both directions: it cannot tell them
apart at all (either both look "remote" and the local deployment is
refused, or the blocklist is dropped and the hosted API silently looks
"local"). Trust has to be a property of the **endpoint** (its
``base_url``), not of the Python class that happens to speak to it.

This module supplies only the *default* — loopback / private-network /
``.local`` addresses are trusted by default, anything else is not — and
nothing here is ever the last word: an explicit ``trusted=`` on an
:class:`~llm.endpoints.EndpointConfig` (or on
:class:`~llm.providers.OpenAIBackend` directly) always overrides it. A
deployment that runs its "local" model on a reachable LAN host with a
public-looking hostname, or that wants a loopback endpoint treated as
untrusted for defense in depth, is never stuck with the heuristic.
"""

from __future__ import annotations

import ipaddress
from urllib.parse import urlparse

__all__ = ["default_trust_for_url"]


def default_trust_for_url(base_url: str) -> bool:
    """Best-effort default trust for an endpoint with no explicit override.

    Trusted by default: loopback addresses (``127.0.0.0/8``, ``::1``),
    RFC 1918 / RFC 4193 private ranges, ``localhost``, and any ``*.local``
    hostname (mDNS/Bonjour convention for a LAN host) — the shapes a
    self-hosted ``gpt-oss``/vLLM/llama.cpp server actually takes. Anything
    else, including an unresolvable/public hostname such as
    ``api.openai.com``, defaults to untrusted. A malformed *base_url*
    (empty string, no host) also defaults to untrusted rather than
    raising — an endpoint this function cannot even parse is not one it
    can vouch for.

    Parameters
    ----------
    base_url:
        The endpoint's base URL, e.g. ``"http://localhost:8000/v1"``.

    Returns
    -------
    bool

    Examples
    --------
    >>> default_trust_for_url("http://localhost:8000/v1")
    True
    >>> default_trust_for_url("http://127.0.0.1:11434/v1")
    True
    >>> default_trust_for_url("http://192.168.1.50:8000/v1")
    True
    >>> default_trust_for_url("http://gpu-box.local:8000/v1")
    True
    >>> default_trust_for_url("https://api.openai.com/v1")
    False
    >>> default_trust_for_url("")
    False
    """
    try:
        host = urlparse(base_url).hostname or ""
    except ValueError:
        return False
    if not host:
        return False
    if host == "localhost" or host.endswith(".local"):
        return True
    try:
        addr = ipaddress.ip_address(host)
    except ValueError:
        return False
    return addr.is_loopback or addr.is_private
