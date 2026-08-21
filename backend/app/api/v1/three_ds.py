"""
The 3-D Secure challenge document.

Getting a cardholder in front of their issuer sounds like it should be one redirect,
and it is not. Two constraints shape everything here.

**It cannot run in a frame.** A challenge *ends* by sending the browser back to this
application's return URL. Inside an iframe that return is refused by our own
``X-Frame-Options: DENY``, and the cardholder is left looking at an empty box no matter
what the issuer did. At the top level the header never applies.

**The markup is not ours.** Gateways hand back a body fragment — a form of hidden inputs
addressed to the issuer's access control server — under names like ``htmlBodyContent``.
Rendering that on our own host would give somebody else's markup our origin. So the
document is served under ``Content-Security-Policy: sandbox`` with no
``allow-same-origin``: it lands in an opaque origin, where it can post itself to the
issuer and navigate the window, and can read nothing of ours.

A fragment is also not a page. Hidden inputs render as nothing, and a fragment whose
gateway expects *you* to submit it produces a blank screen — which is exactly what a
bare ``HTMLResponse`` of the fragment gave. So it is wrapped in a real document that
submits the form itself, says what it is doing while it happens, and offers a button
when the automatic submit does not fire.
"""

from __future__ import annotations

import html
import re
from typing import Optional

#: A fragment with no form in it is not a challenge, whatever the gateway called it.
FORM_PATTERN = re.compile(r"<form[\s>]", re.IGNORECASE)

_STYLE = """
  :root { color-scheme: light dark; }
  body { margin: 0; padding: 3rem 1.5rem; font: 15px/1.6 system-ui, -apple-system,
         "Segoe UI", sans-serif; color: #1f2430; background: #f7f8fa; }
  main { max-width: 34rem; margin: 0 auto; }
  h1 { font-size: 1.25rem; margin: 0 0 .5rem; }
  p { margin: 0 0 .75rem; color: #55607a; }
  .card { background: #fff; border: 1px solid #e3e6ee; border-radius: 12px;
          padding: 1.5rem; }
  button { margin-top: .5rem; font: inherit; font-weight: 600; color: #fff;
           background: #4f46e5; border: 0; border-radius: 8px; padding: .6rem 1.1rem;
           cursor: pointer; }
  .quiet { font-size: 13px; color: #7a8399; }
  pre { overflow: auto; background: #10141f; color: #e7eaf3; padding: 1rem;
        border-radius: 8px; font-size: 12px; }
  @media (prefers-color-scheme: dark) {
    body { background: #10141f; color: #e7eaf3; }
    .card { background: #171c2b; border-color: #2a3145; }
    p { color: #a6afc4; }
  }
"""

_SCRIPT = """
  (function () {
    var status = document.getElementById('burapay-status');
    var form = document.querySelector('#burapay-challenge form');
    if (!form) { status.textContent = 'The bank sent no form to submit.'; return; }

    // The gateway's own fragment may or may not carry a submit script. Submitting it
    // here is safe either way: if it already navigated, this never runs.
    var submitted = false;
    function go() {
      if (submitted) return;
      submitted = true;
      try { form.submit(); } catch (e) {
        status.textContent = 'Your browser would not submit the form automatically. '
          + 'Use the button below.';
      }
    }
    document.getElementById('burapay-continue').addEventListener('click', go);
    // A tick, so the status text paints before the navigation starts.
    setTimeout(go, 50);
  })();
"""


def _document(title: str, body: str) -> str:
    return (
        "<!doctype html>\n<html lang=\"en\">\n<head>\n"
        "<meta charset=\"utf-8\">\n"
        "<meta name=\"viewport\" content=\"width=device-width, initial-scale=1\">\n"
        f"<title>{html.escape(title)}</title>\n"
        f"<style>{_STYLE}</style>\n</head>\n<body>\n{body}\n</body>\n</html>"
    )


def challenge_document(markup: str, *, gateway: str, amount: float,
                       currency: str) -> str:
    """Wrap an issuer's challenge fragment in a page that actually submits it."""
    where = html.escape(f"{amount:.2f} {currency} · {gateway}")
    return _document("Verify with your bank", f"""
<main>
  <div class="card">
    <h1>Verify with your bank</h1>
    <p>{where}</p>
    <p id="burapay-status">Taking you to your bank…</p>
    <button type="button" id="burapay-continue">Continue to your bank</button>
    <p class="quiet">The time you spend here is recorded as customer interaction, not
      as gateway latency.</p>
  </div>
</main>
<div id="burapay-challenge" hidden>{markup}</div>
<script>{_SCRIPT}</script>""")


def no_form_document(markup: Optional[str], *, gateway: str,
                     transaction_id: str) -> str:
    """What to show when the gateway called it a challenge but sent no form.

    Escaped and displayed rather than injected. A gateway that sends something
    unexpected here is a fact worth seeing, not something to render blindly and hope.
    """
    excerpt = html.escape((markup or "")[:2000]) or "(nothing was returned)"
    return _document("No challenge to show", f"""
<main>
  <div class="card">
    <h1>No challenge to show</h1>
    <p>{html.escape(gateway)} said this payment needed a 3-D Secure challenge, but what
      it sent contains no form to submit, so there is nowhere to send you.</p>
    <p>The payment is still pending. The full exchange is in the
      <a href="/logs?transaction={html.escape(transaction_id)}">call log</a>, under the
      authentication call.</p>
    <p class="quiet">What the gateway returned, as stored:</p>
    <pre>{excerpt}</pre>
  </div>
</main>""")
