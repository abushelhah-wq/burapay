#!/usr/bin/env python3
"""
Start the web application over HTTPS.

    python serve.py

Configuration comes from the environment or a .env file (see .env.example).

Two ways to serve TLS, and the app supports both:

1. **Directly** — set SSL_CERTFILE and SSL_KEYFILE and uvicorn terminates TLS itself.
   Fine for local development; generate a self-signed pair with scripts/make_cert.sh.

2. **Behind a reverse proxy** — leave the SSL_* variables unset and let Caddy, nginx
   or a load balancer terminate TLS. Set BEHIND_PROXY=1 so the app trusts
   X-Forwarded-Proto, and keep FORCE_HTTPS=1 so it still emits HSTS. This is what
   deploy/docker-compose.yml does, and it is the right answer in production because
   Caddy obtains and renews a real certificate automatically.

The app refuses to run hosted-checkout flows unless PUBLIC_BASE_URL is https, since
gateways reject non-HTTPS return and callback URLs.
"""

from __future__ import annotations

import os
import sys

try:
    from dotenv import load_dotenv
    load_dotenv(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"),
                override=False)
except ImportError:
    if os.path.exists(".env"):
        print("A .env file exists but python-dotenv is not installed, so it is being "
              "ignored.\n  Fix: python3 -m pip install -r requirements.txt "
              "--break-system-packages", file=sys.stderr)

import uvicorn  # noqa: E402  (imported after .env is loaded)

from app.config import SETTINGS  # noqa: E402


def main() -> int:
    tls = bool(SETTINGS.ssl_certfile and SETTINGS.ssl_keyfile)

    if tls:
        for path, label in ((SETTINGS.ssl_certfile, "SSL_CERTFILE"),
                            (SETTINGS.ssl_keyfile, "SSL_KEYFILE")):
            if not os.path.exists(path or ""):
                print(f"{label} points at {path!r}, which does not exist.\n"
                      f"  Generate a development pair:  ./scripts/make_cert.sh",
                      file=sys.stderr)
                return 1

    problem = SETTINGS.base_url_problem()
    scheme = "https" if tls else "http"
    print(f"busrapay starting on {scheme}://{SETTINGS.host}:{SETTINGS.port}")
    if problem:
        # Not fatal: the server-side flows work without it, and refusing to boot would
        # be unhelpful for someone who only wants to measure Direct/refund/capture.
        print(f"\n  WARNING: {problem}\n"
              f"  Server-side flows will work; hosted checkout will be refused with "
              f"this explanation.\n", file=sys.stderr)
    else:
        print(f"  Public base URL: {SETTINGS.public_base_url}")
    if not tls:
        print("  TLS is not terminated here. Set SSL_CERTFILE/SSL_KEYFILE, or run "
              "behind a proxy with BEHIND_PROXY=1.")

    uvicorn.run(
        "app.main:app",
        host=SETTINGS.host,
        port=SETTINGS.port,
        ssl_certfile=SETTINGS.ssl_certfile if tls else None,
        ssl_keyfile=SETTINGS.ssl_keyfile if tls else None,
        # Only honour X-Forwarded-* when explicitly told there is a proxy in front:
        # these headers are client-settable and trusting them unconditionally would
        # let a caller spoof the scheme past the HTTPS check.
        proxy_headers=SETTINGS.behind_proxy,
        forwarded_allow_ips="*" if SETTINGS.behind_proxy else None,
        log_level=os.environ.get("LOG_LEVEL", "info"),
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
