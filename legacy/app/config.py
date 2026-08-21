"""
Application settings, read from the environment.

Every gateway credential is optional: a gateway with no credentials is reported as
"not configured" in the UI and cannot be selected, rather than failing at click time.

The one setting that is not optional in substance is PUBLIC_BASE_URL. Gateways
redirect the customer's browser back to it and POST webhooks to it, and they
require HTTPS for both. If it is not an https:// URL the app refuses to start the
hosted flows rather than handing a gateway a URL it will reject.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Dict, List, Optional
from urllib.parse import urlparse


def env(key: str, default: Optional[str] = None) -> Optional[str]:
    value = os.environ.get(key)
    if value is None:
        return default
    value = value.strip()
    return value or default


def env_bool(key: str, default: bool = False) -> bool:
    raw = (env(key) or "").lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "on")


def env_float(key: str, default: float) -> float:
    try:
        return float(env(key) or default)
    except ValueError:
        return default


def env_int(key: str, default: int) -> int:
    try:
        return int(env(key) or default)
    except ValueError:
        return default


@dataclass
class Settings:
    #: Public HTTPS origin this app is reachable at, e.g. https://busrapay.com.
    #: Used to build return_url and webhook_url for every gateway.
    public_base_url: str = field(default_factory=lambda: env("PUBLIC_BASE_URL", "") or "")
    #: Bind address for the built-in server.
    host: str = field(default_factory=lambda: env("HOST", "0.0.0.0") or "0.0.0.0")
    port: int = field(default_factory=lambda: env_int("PORT", 8443))
    #: TLS material for running uvicorn with HTTPS directly. Leave unset when TLS is
    #: terminated upstream by Caddy/nginx/a load balancer.
    ssl_certfile: Optional[str] = field(default_factory=lambda: env("SSL_CERTFILE"))
    ssl_keyfile: Optional[str] = field(default_factory=lambda: env("SSL_KEYFILE"))
    #: Redirect http:// to https:// and send HSTS. Turn off only when something
    #: upstream already does it, to avoid a redirect loop.
    force_https: bool = field(default_factory=lambda: env_bool("FORCE_HTTPS", True))
    #: Trust X-Forwarded-Proto/For. Required behind a reverse proxy, and must stay
    #: off when the app is exposed directly, since the headers are client-settable.
    behind_proxy: bool = field(default_factory=lambda: env_bool("BEHIND_PROXY", False))

    database_path: str = field(
        default_factory=lambda: env("DATABASE_PATH", "data/results.db") or "data/results.db")
    http_timeout: float = field(default_factory=lambda: env_float("HTTP_TIMEOUT_SECONDS", 30.0))
    default_amount: float = field(default_factory=lambda: env_float("TEST_AMOUNT", 10.00))
    default_currency: str = field(default_factory=lambda: env("TEST_CURRENCY", "SAR") or "SAR")

    #: Allows the Direct (server-to-server) flow to accept card numbers typed into
    #: the browser form. Off by default: that path puts the app in PCI scope, and it
    #: is only ever appropriate against sandbox credentials with test cards.
    allow_direct_card_entry: bool = field(
        default_factory=lambda: env_bool("ALLOW_DIRECT_CARD_ENTRY", False))

    def https_ready(self) -> bool:
        return self.public_base_url.lower().startswith("https://")

    def base_url_problem(self) -> Optional[str]:
        """Why the hosted flows cannot run, or None when the base URL is usable."""
        if not self.public_base_url:
            return ("PUBLIC_BASE_URL is not set. Gateways need an https:// address to "
                    "redirect the customer back to and to deliver webhooks.")
        parsed = urlparse(self.public_base_url)
        if parsed.scheme != "https":
            return (f"PUBLIC_BASE_URL is {self.public_base_url!r}, which is not https. "
                    "Gateway sandboxes reject non-HTTPS return and callback URLs.")
        if not parsed.netloc:
            return f"PUBLIC_BASE_URL is {self.public_base_url!r}, which has no host."
        return None

    def url_for(self, path: str) -> str:
        return f"{self.public_base_url.rstrip('/')}/{path.lstrip('/')}"


SETTINGS = Settings()
