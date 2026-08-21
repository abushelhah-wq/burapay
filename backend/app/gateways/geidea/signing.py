"""
Geidea request authentication and signing.

AUTHENTICATION -- doc-derived
    HTTP Basic, with the Merchant Public Key as username and the API Password
    as password. Recorded in ``docs/02_api_flow_comparison.md`` from
    https://docs.geidea.net/docs/pre-requisites.md

SIGNATURE -- INFERRED, NOT VERIFIED
    Geidea's session and refund calls carry a ``signature`` field. The field
    *name* is doc-derived; the value's construction is not. The implemented
    form is::

        base64( HMAC-SHA256( key = apiPassword,
                             msg = publicKey + amount + currency
                                   + merchantReferenceId + timestamp ) )

    with ``amount`` formatted to two decimal places and ``timestamp`` in
    ISO-8601. The previous release's README lists this concatenation order and
    the timestamp format as the two things that could not be confirmed against
    a worked example, and names them as the first suspects when a call is
    rejected for a bad signature.

    TODO(docs): confirm the exact payload order and timestamp format against
    https://docs.geidea.net/docs/pre-requisites.md and the API Reference page
    for Create Session. The host is currently unreachable from this
    environment (egress policy refuses docs.geidea.net), so this could not be
    checked during the build.

    Sending an incorrectly-constructed signature is safe in the sense that
    matters: Geidea rejects the request and the rejection is logged in full.
    It cannot move money to the wrong place. That is why this one inference is
    shipped, while an inferred *field name* on a capture or refund amount is
    not -- that could move the wrong amount.

    Both the payload order and the timestamp format are overridable from
    ``gateways.config_json`` so a deployment can correct them without a code
    change, exactly as the previous release intended.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
from decimal import Decimal
from typing import Any, Mapping, Sequence

from app.money import exponent
from app.timeutil import utcnow

#: Order of the fields concatenated to form the signed message. INFERRED.
DEFAULT_SIGNATURE_FIELDS: tuple[str, ...] = (
    "publicKey", "amount", "currency", "merchantReferenceId", "timestamp",
)

#: strftime format for the ``timestamp`` field. INFERRED.
DEFAULT_TIMESTAMP_FORMAT = "%m/%d/%Y %I:%M:%S %p"

#: The previous release defaulted to ISO-8601 instead. Both spellings appear in
#: Geidea sample material; neither could be confirmed. Kept here so a
#: deployment can switch with a config change rather than a code edit.
ALTERNATE_TIMESTAMP_FORMATS: tuple[str, ...] = (
    "%Y-%m-%dT%H:%M:%S",
    "%m/%d/%Y %I:%M:%S %p",
)


class BasicAuth:
    """
    HTTP Basic authentication, applied by the shared client (§0.3).

    Implemented here rather than handed to httpx so that signing and
    authentication are one strategy object the client applies uniformly, and so
    the credential never reaches a code path that might log it -- the shared
    client masks the ``Authorization`` header before it writes the log row.
    """

    def __init__(self, public_key: str, api_password: str) -> None:
        self._public_key = public_key
        self._api_password = api_password

    def apply(
        self, *, method: str, url: str, headers: dict[str, str], body: Any
    ) -> tuple[dict[str, str], Any]:
        token = base64.b64encode(
            f"{self._public_key}:{self._api_password}".encode()
        ).decode()
        headers = {
            "Content-Type": "application/json",
            **headers,
            "Authorization": f"Basic {token}",
        }
        return headers, body

    def __repr__(self) -> str:  # pragma: no cover - defensive
        # Never let a traceback print the API password.
        return "<GeideaBasicAuth public_key=***>"


def format_amount(amount_minor: int, currency: str) -> str:
    """
    Render minor units as the decimal string Geidea's JSON expects.

    Kept adapter-local on purpose: :func:`app.money.format_minor` is the
    presentation layer's function, and a gateway's wire format must not be
    coupled to how the UI happens to display money.
    """
    return str(Decimal(amount_minor).scaleb(-exponent(currency)).quantize(
        Decimal(1).scaleb(-exponent(currency))
    ))


def timestamp(fmt: str | None = None) -> str:
    return utcnow().strftime(fmt or DEFAULT_TIMESTAMP_FORMAT)


def build_signature(
    *,
    api_password: str,
    values: Mapping[str, str],
    fields: Sequence[str] = DEFAULT_SIGNATURE_FIELDS,
) -> str:
    """
    base64(HMAC-SHA256(apiPassword, concat(values[f] for f in fields))).

    INFERRED -- see the module docstring. A missing field contributes an empty
    string rather than raising, matching the concatenation semantics of the
    documented description.
    """
    message = "".join(str(values.get(name, "")) for name in fields)
    digest = hmac.new(
        api_password.encode("utf-8"), message.encode("utf-8"), hashlib.sha256
    ).digest()
    return base64.b64encode(digest).decode("utf-8")
