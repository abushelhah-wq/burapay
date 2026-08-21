"""
busrapay — payment gateway response-time benchmark, as a web application.

Pick a gateway and a flow, pay with a sandbox test card, and every merchant-server
call is timed and recorded.

Why this has to run on HTTPS
----------------------------
It is not a preference. Hosted checkout hands the gateway a ``return_url`` and a
``callback_url``; gateway sandboxes reject non-HTTPS values for both, and browsers
will not carry a secure-context redirect back to a plaintext origin. The app
therefore refuses to start a hosted flow when PUBLIC_BASE_URL is not https, and
says why, rather than letting the gateway return an opaque rejection.
"""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

from fastapi import FastAPI, Form, Request
from fastapi.responses import (HTMLResponse, JSONResponse, PlainTextResponse,
                               RedirectResponse, Response)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from starlette.middleware.base import BaseHTTPMiddleware

from app.adapters import FLOW_LABELS, FLOWS, build_adapters, get_adapter
from app.adapters.base import Card, Charge
from app.config import SETTINGS, env
from app.storage import Store
from app.timing import GatewayError, NotConfigured, NotSupported, new_reference

app = FastAPI(title="busrapay gateway benchmark", docs_url="/api/docs",
              redoc_url=None)
app.mount("/static", StaticFiles(directory="app/static"), name="static")
templates = Jinja2Templates(directory="app/templates")
store = Store(SETTINGS.database_path)

#: Sandbox test card used by the Direct and stand-alone flows. Overridable per
#: gateway via env; never a real card, and never persisted.
DEFAULT_CARD = Card(
    number=env("TEST_CARD_NUMBER", "4111111111111111") or "4111111111111111",
    month=env("TEST_CARD_MONTH", "12") or "12",
    year=env("TEST_CARD_YEAR", "2030") or "2030",
    cvc=env("TEST_CARD_CVC", "123") or "123",
    holder=env("TEST_CARD_HOLDER", "Benchmark Runner") or "Benchmark Runner",
)


# --------------------------------------------------------------------------- #
# HTTPS enforcement
# --------------------------------------------------------------------------- #

class HTTPSRedirectAndHSTS(BaseHTTPMiddleware):
    """Send plaintext requests to https and set HSTS on the way back.

    Behind a reverse proxy the scheme arrives in X-Forwarded-Proto, which is
    client-settable and therefore only trusted when BEHIND_PROXY is on.
    """

    async def dispatch(self, request: Request, call_next):
        if SETTINGS.force_https:
            scheme = request.url.scheme
            if SETTINGS.behind_proxy:
                scheme = request.headers.get("x-forwarded-proto", scheme).split(",")[0].strip()
            if scheme != "https" and request.url.hostname not in ("localhost", "127.0.0.1"):
                return RedirectResponse(
                    request.url.replace(scheme="https"), status_code=307)

        response = await call_next(request)
        if SETTINGS.force_https:
            response.headers.setdefault(
                "Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "strict-origin-when-cross-origin")
        return response


app.add_middleware(HTTPSRedirectAndHSTS)


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def gateway_options() -> List[Dict[str, Any]]:
    return [{"name": name, "label": adapter.label,
             "configured": adapter.configured(),
             "missing": adapter.missing_config(),
             "notes": adapter.notes,
             "documented_calls": adapter.documented_calls}
            for name, adapter in build_adapters(SETTINGS.http_timeout).items()]


def flow_options() -> List[Dict[str, str]]:
    return [{"name": flow, "label": FLOW_LABELS[flow]} for flow in FLOWS]


def page_context(request: Request, **extra: Any) -> Dict[str, Any]:
    context = {
        "request": request,
        "gateways": gateway_options(),
        "flows": flow_options(),
        "settings": SETTINGS,
        "base_url_problem": SETTINGS.base_url_problem(),
        "default_amount": f"{SETTINGS.default_amount:.2f}",
        "default_currency": SETTINGS.default_currency,
    }
    context.update(extra)
    return context


def build_charge(gateway: str, amount: float, currency: str) -> Charge:
    reference = new_reference("busrapay")
    return Charge(amount=amount, currency=currency.upper(), reference=reference,
                  return_url=SETTINGS.url_for(f"/return/{gateway}"),
                  webhook_url=SETTINGS.url_for(f"/webhook/{gateway}"))


def failure(request: Request, message: str, *, status_code: int = 200,
            attempt: Optional[Dict[str, Any]] = None) -> HTMLResponse:
    return templates.TemplateResponse(
        request, "checkout.html",
        page_context(request, result={"status": "failed", "detail": message},
                     attempt=attempt),
        status_code=status_code)


# --------------------------------------------------------------------------- #
# Pages
# --------------------------------------------------------------------------- #

@app.get("/", response_class=HTMLResponse)
def index() -> RedirectResponse:
    return RedirectResponse("/checkout", status_code=307)


@app.get("/checkout", response_class=HTMLResponse)
def checkout_page(request: Request) -> HTMLResponse:
    return templates.TemplateResponse(request, "checkout.html", page_context(request))


@app.get("/healthz", response_class=PlainTextResponse)
def healthz() -> str:
    return "ok"


# --------------------------------------------------------------------------- #
# Running a checkout
# --------------------------------------------------------------------------- #

@app.post("/checkout", response_class=HTMLResponse)
def run_checkout(request: Request,
                 gateway: str = Form(...),
                 flow: str = Form(...),
                 amount: float = Form(...),
                 currency: str = Form(...),
                 card_number: str = Form(""),
                 card_month: str = Form(""),
                 card_year: str = Form(""),
                 card_cvc: str = Form("")) -> Response:
    try:
        adapter = get_adapter(gateway, SETTINGS.http_timeout)
    except KeyError as exc:
        return failure(request, str(exc), status_code=400)

    if flow not in FLOWS:
        return failure(request, f"Unknown flow {flow!r}.", status_code=400)

    try:
        adapter.require_configured()
        adapter.require_supports(flow)
    except (NotConfigured, NotSupported) as exc:
        return failure(request, str(exc))

    charge = build_charge(gateway, amount, currency)
    card = DEFAULT_CARD
    if SETTINGS.allow_direct_card_entry and card_number:
        card = Card(number=card_number.replace(" ", ""), month=card_month,
                    year=card_year, cvc=card_cvc, holder=DEFAULT_CARD.holder)

    if flow == "hosted_checkout":
        return start_hosted_checkout(request, adapter, charge)
    return run_server_flow(request, adapter, flow, charge, card)


def start_hosted_checkout(request: Request, adapter, charge: Charge) -> Response:
    """Leg 1: create the gateway session and hand the browser off to it."""
    problem = SETTINGS.base_url_problem()
    if problem:
        return failure(request,
                       f"Hosted checkout cannot run: {problem} The gateway needs to "
                       f"redirect the customer back here and deliver webhooks, and "
                       f"sandboxes reject non-HTTPS URLs for both.")

    attempt_id = store.start_attempt(
        gateway=adapter.name, flow="hosted_checkout", amount=charge.amount,
        currency=charge.currency, reference=charge.reference,
        location=env("MEASUREMENT_LOCATION"))

    with adapter.session_for("hosted_checkout") as session:
        try:
            started = adapter.start_hosted(session, charge)
        except (GatewayError, NotConfigured, NotSupported) as exc:
            store.record_calls(attempt_id, "start", session.calls)
            store.finish_attempt(attempt_id, status="failed", detail=str(exc))
            return failure(request, str(exc), attempt=store.get_attempt(attempt_id))
        finally:
            store.record_calls(attempt_id, "start", session.calls)

    store.update_context(attempt_id, {
        "adapter_context": started.context,
        "return_url": charge.return_url,
        "webhook_url": charge.webhook_url,
    })
    store.finish_attempt(attempt_id, status="pending",
                         detail="redirected to the gateway's payment page",
                         gateway_ref=started.gateway_ref,
                         context={"adapter_context": started.context,
                                  "return_url": charge.return_url,
                                  "webhook_url": charge.webhook_url})

    # The attempt id rides along so the return leg can find this row. Gateways vary in
    # which query parameters they preserve, so it is also stored in a cookie as a
    # fallback for the ones that drop unknown parameters.
    target = started.redirect_url
    separator = "&" if "?" in target else "?"
    if target.startswith("/"):
        target = f"{target}{separator}attempt={attempt_id}"
    response = RedirectResponse(target, status_code=303)
    response.set_cookie("attempt", attempt_id, max_age=3600, httponly=True,
                        samesite="lax", secure=SETTINGS.https_ready())
    return response


def run_server_flow(request: Request, adapter, flow: str, charge: Charge,
                    card: Card) -> Response:
    """Direct API and the stand-alone operations: one request, no browser leg."""
    attempt_id = store.start_attempt(
        gateway=adapter.name, flow=flow, amount=charge.amount, currency=charge.currency,
        reference=charge.reference, location=env("MEASUREMENT_LOCATION"))

    with adapter.session_for(flow) as session:
        try:
            outcome = adapter.run_standalone(flow, session, charge, card)
            status, detail, gateway_ref = outcome.status, outcome.detail, outcome.gateway_ref
        except (GatewayError, NotConfigured, NotSupported) as exc:
            status, detail, gateway_ref = "failed", str(exc), None
        finally:
            store.record_calls(attempt_id, flow, session.calls)

    store.finish_attempt(attempt_id, status=status, detail=detail, gateway_ref=gateway_ref)
    attempt = store.get_attempt(attempt_id)
    return templates.TemplateResponse(
        request, "checkout.html",
        page_context(request, result={"status": status, "detail": detail},
                     attempt=attempt, selected_gateway=adapter.name,
                     selected_flow=flow))


# --------------------------------------------------------------------------- #
# Gateway callbacks
# --------------------------------------------------------------------------- #

@app.get("/return/{gateway}", response_class=HTMLResponse)
def hosted_return(request: Request, gateway: str) -> Response:
    """Leg 2: the customer is back from the gateway — confirm the outcome server-side."""
    params = dict(request.query_params)
    attempt_id = params.get("attempt") or request.cookies.get("attempt")
    if not attempt_id:
        return failure(request,
                       "Returned from the gateway without an attempt id, so this "
                       "payment cannot be matched to a recorded attempt.")

    attempt = store.get_attempt(attempt_id)
    if attempt is None:
        return failure(request, f"No recorded attempt {attempt_id!r}.")

    try:
        adapter = get_adapter(attempt["gateway"], SETTINGS.http_timeout)
    except KeyError as exc:
        return failure(request, str(exc))

    charge = Charge(amount=attempt["amount"], currency=attempt["currency"],
                    reference=attempt["reference"],
                    return_url=attempt["context"].get("return_url", ""),
                    webhook_url=attempt["context"].get("webhook_url", ""))
    adapter_context = attempt["context"].get("adapter_context", {})

    with adapter.session_for("hosted_checkout") as session:
        try:
            outcome = adapter.confirm_hosted(session, charge, adapter_context, params)
            status, detail, gateway_ref = outcome.status, outcome.detail, outcome.gateway_ref
        except (GatewayError, NotConfigured, NotSupported) as exc:
            status, detail, gateway_ref = "failed", str(exc), attempt.get("gateway_ref")
        finally:
            store.record_calls(attempt_id, "confirm", session.calls)

    store.finish_attempt(attempt_id, status=status, detail=detail,
                         gateway_ref=gateway_ref or attempt.get("gateway_ref"))
    return templates.TemplateResponse(
        request, "checkout.html",
        page_context(request, result={"status": status, "detail": detail},
                     attempt=store.get_attempt(attempt_id),
                     selected_gateway=attempt["gateway"], selected_flow="hosted_checkout"))


@app.post("/webhook/{gateway}")
def webhook(gateway: str, payload: Dict[str, Any] | None = None) -> JSONResponse:
    """Accept gateway webhooks.

    Deliberately minimal and deliberately not used to decide an attempt's outcome:
    this benchmark measures merchant-initiated round trips, and a webhook is a push
    the merchant neither initiates nor can time. Acknowledged so gateways that retry
    on non-2xx stop retrying.
    """
    return JSONResponse({"received": True, "gateway": gateway}, status_code=200)


@app.get("/handoff/{gateway}/{token}", response_class=HTMLResponse)
def handoff(request: Request, gateway: str, token: str) -> HTMLResponse:
    """Mount a gateway's own widget for the flows that are not a plain redirect.

    Adyen's Sessions flow and HyperPay's Copy&Pay both hand back an identifier for a
    browser SDK rather than a URL to redirect to, so there is a page to host it.
    """
    attempt_id = request.query_params.get("attempt") or request.cookies.get("attempt", "")
    return templates.TemplateResponse(
        request, "handoff.html",
        {"request": request, "gateway": gateway, "token": token,
         "attempt_id": attempt_id, "settings": SETTINGS,
         "return_url": SETTINGS.url_for(f"/return/{gateway}?attempt={attempt_id}")})


# --------------------------------------------------------------------------- #
# Results
# --------------------------------------------------------------------------- #

@app.get("/results", response_class=HTMLResponse)
def results_page(request: Request) -> HTMLResponse:
    labels = {name: adapter.label
              for name, adapter in build_adapters(SETTINGS.http_timeout).items()}
    return templates.TemplateResponse(
        request, "results.html",
        {"request": request, "summary": store.summary(),
         "attempts": store.recent_attempts(100), "labels": labels,
         "flow_labels": FLOW_LABELS, "settings": SETTINGS})


@app.get("/api/summary")
def api_summary() -> JSONResponse:
    return JSONResponse({"summary": store.summary()})


@app.get("/api/attempts/{attempt_id}")
def api_attempt(attempt_id: str) -> JSONResponse:
    attempt = store.get_attempt(attempt_id)
    if attempt is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    return JSONResponse(attempt)


@app.get("/api/gateways")
def api_gateways() -> JSONResponse:
    return JSONResponse({"gateways": gateway_options(), "flows": flow_options()})
