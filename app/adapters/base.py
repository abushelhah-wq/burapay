"""
The adapter contract every gateway implements.

Two shapes exist because the two flows differ in a way that matters for a web app:

* **Hosted checkout** is inherently two-legged. The server creates a session and
  hands the browser a redirect URL; the customer pays on the gateway's page; the
  gateway sends the browser back. Only then can the server confirm the outcome.
  So it is ``start_hosted()`` then ``confirm_hosted()``, with the customer's time
  on the gateway's page sitting between them and deliberately excluded from the
  measurement — the merchant does not control it and cannot speed it up.

* **Direct / server-to-server** completes inside one request. It is ``direct()``.

The stand-alone operations (capture, refund, void, MIT) each set themselves up with
untimed prep calls and then time the single operation being measured.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional

import requests

from app.timing import (CallRecord, GatewayError, MeasuredSession, NotConfigured,
                        NotSupported, new_reference)

#: The flows the UI can run, in display order.
FLOWS = ("hosted_checkout", "direct_api", "capture", "refund", "void", "mit")

FLOW_LABELS: Dict[str, str] = {
    "hosted_checkout": "Hosted checkout (redirect)",
    "direct_api": "Direct API (server-to-server)",
    "capture": "Capture",
    "refund": "Refund",
    "void": "Void",
    "mit": "MIT / recurring charge",
}


@dataclass
class Charge:
    """What the customer asked for, plus the URLs the gateway needs to reach us."""

    amount: float
    currency: str
    reference: str = field(default_factory=lambda: new_reference("busrapay"))
    return_url: str = ""
    webhook_url: str = ""


@dataclass
class Card:
    """Test card details, only ever used by the Direct flow."""

    number: str
    month: str
    year: str
    cvc: str
    holder: str = "Benchmark Runner"


@dataclass
class Started:
    """Result of the first leg of a hosted checkout."""

    redirect_url: str
    gateway_ref: str
    #: Anything confirm_hosted() needs to look the payment up when the browser returns.
    context: Dict[str, Any] = field(default_factory=dict)


@dataclass
class Outcome:
    """The end state of an operation."""

    status: str          # succeeded | pending | failed
    detail: str = ""
    gateway_ref: Optional[str] = None
    raw: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == "succeeded"


class Adapter:
    """Base class for a gateway adapter.

    Subclasses set ``name``/``label``, implement ``configured()``, and implement the
    flows the gateway supports. A flow that a gateway genuinely lacks, or that needs
    a credential this deployment does not have, raises :class:`NotSupported` or
    :class:`NotConfigured` — the UI turns those into an explanation rather than a
    stack trace.
    """

    name: str = "adapter"
    label: str = "Adapter"
    #: What the vendor's own documentation says the minimum call count is, per flow.
    #: Shown next to the measured number so a divergence is visible immediately.
    documented_calls: Dict[str, str] = {}
    #: Caveats surfaced in the UI alongside results for this gateway.
    notes: List[str] = []
    #: Flows this adapter implements at all.
    supported_flows: tuple = FLOWS

    def __init__(self, timeout: float = 30.0) -> None:
        self.timeout = timeout

    # -- configuration ---------------------------------------------------- #

    def configured(self) -> bool:
        return not self.missing_config()

    def missing_config(self) -> List[str]:
        """Environment variables this gateway needs but does not have."""
        return []

    def build_session(self) -> requests.Session:
        """A session pre-loaded with this gateway's auth."""
        return requests.Session()

    def session_for(self, flow: str) -> MeasuredSession:
        return MeasuredSession(self.name, flow, timeout=self.timeout,
                               session=self.build_session())

    def require_configured(self) -> None:
        missing = self.missing_config()
        if missing:
            raise NotConfigured(
                f"{self.label} is missing {', '.join(missing)}. Set it in .env "
                f"(see .env.example) and restart.")

    def require_supports(self, flow: str) -> None:
        if flow not in self.supported_flows:
            raise NotSupported(f"{self.label} does not support {FLOW_LABELS.get(flow, flow)}.")

    # -- flows ------------------------------------------------------------ #

    def start_hosted(self, s: MeasuredSession, charge: Charge) -> Started:
        raise NotSupported(f"{self.label}: hosted checkout is not implemented.")

    def confirm_hosted(self, s: MeasuredSession, charge: Charge,
                       context: Dict[str, Any], params: Dict[str, str]) -> Outcome:
        raise NotSupported(f"{self.label}: hosted confirmation is not implemented.")

    def direct(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        raise NotSupported(f"{self.label}: direct API is not implemented.")

    def capture(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        raise NotSupported(f"{self.label}: capture is not implemented.")

    def refund(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        raise NotSupported(f"{self.label}: refund is not implemented.")

    def void(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        raise NotSupported(f"{self.label}: void is not implemented.")

    def mit(self, s: MeasuredSession, charge: Charge, card: Card) -> Outcome:
        raise NotSupported(f"{self.label}: MIT is not implemented.")

    # -- dispatch --------------------------------------------------------- #

    def run_standalone(self, flow: str, s: MeasuredSession, charge: Charge,
                       card: Card) -> Outcome:
        """Run one of the non-redirect flows."""
        handlers = {
            "direct_api": self.direct,
            "capture": self.capture,
            "refund": self.refund,
            "void": self.void,
            "mit": self.mit,
        }
        handler = handlers.get(flow)
        if handler is None:
            raise NotSupported(f"{FLOW_LABELS.get(flow, flow)} is not a server-side flow.")
        return handler(s, charge, card)
