#!/usr/bin/env python3
"""
Assert the deployment contract that keeps BuraPay off the rest of the VPS's toes.

    docker compose config > /tmp/compose.yml
    python3 scripts/check_traefik_wiring.py /tmp/compose.yml

Run against the *rendered* compose configuration, not the source file, so what is
checked is what Docker would actually create. CI runs it on every push; it is worth
running by hand after any change to networks, labels or ports.

Each check exists because getting it wrong has a specific, bad consequence:

* PostgreSQL on the Traefik network is a database on the internet.
* A frontend router outranking the API router sends /api to the SPA, which answers
  index.html with a 200 — so every API call "succeeds" and returns HTML.
* A StripPrefix middleware here would double the prefix, because FastAPI already
  serves /api itself. That is the /api/api/v1/... failure, and it is silent until
  someone opens the site.
* PostgreSQL on the routable network is a database on the internet.
* A Traefik network declared non-external would mean this project creates the network
  Traefik already owns, so Traefik never sees the containers and the site 404s.
"""

from __future__ import annotations

import sys
from pathlib import Path
from typing import Any, Dict

try:
    import yaml
except ImportError:  # pragma: no cover - dependency guard
    print("PyYAML is needed: pip install pyyaml", file=sys.stderr)
    raise SystemExit(2) from None

FRONTEND_ROUTER = "traefik.http.routers.burapay-web"
BACKEND_ROUTER = "traefik.http.routers.burapay-api"


def labels_of(service: Dict[str, Any]) -> Dict[str, str]:
    """Compose renders labels as a mapping; a hand-written list form is also valid."""
    raw = service.get("labels", {})
    if isinstance(raw, list):
        pairs = (item.split("=", 1) for item in raw)
        return {key: value for key, value in pairs}
    return {str(key): str(value) for key, value in raw.items()}


def check(config: Dict[str, Any]) -> list[str]:
    problems: list[str] = []
    services = config.get("services", {})
    networks = config.get("networks", {})

    def fail(message: str) -> None:
        problems.append(message)

    # -- the database is not on the internet ------------------------------ #
    postgres = services.get("postgres")
    if postgres is None:
        fail("no postgres service")
    else:
        attached = set(postgres.get("networks") or {})
        if attached != {"internal"}:
            fail(f"postgres must be on the internal network alone, found {sorted(attached)}")
        if labels_of(postgres).get("traefik.enable", "").lower() != "false":
            fail("postgres must carry traefik.enable=false")
        if postgres.get("ports"):
            fail("postgres must not publish a host port")

    # -- the two public services -------------------------------------------- #
    # `web` is what Traefik reaches, whether it runs in host network mode (it connects
    # to the container's address on that bridge) or in a shared network (the override
    # adds it alongside). Either way both services must be on `web`.
    expected = {"backend": {"internal", "web"}, "frontend": {"web"}}
    for name in ("backend", "frontend"):
        service = services.get(name)
        if service is None:
            fail(f"no {name} service")
            continue
        attached = set(service.get("networks") or {})
        # The shared-network override adds "traefik" to both; that is allowed.
        if not expected[name] <= attached:
            fail(f"{name} must join {sorted(expected[name])}, found {sorted(attached)}")
        if "internal" in attached and name == "frontend":
            fail("the frontend serves static files and has no business on the "
                 "database network")
        if service.get("ports"):
            fail(f"{name} must not publish a host port; Traefik is the only way in")
        service_labels = labels_of(service)
        if service_labels.get("traefik.enable", "").lower() != "true":
            fail(f"{name} must carry traefik.enable=true")
        if not service_labels.get("traefik.docker.network"):
            fail(f"{name} is on two networks and must name traefik.docker.network, "
                 "or Traefik may route to an address it cannot reach")

    # -- routing --------------------------------------------------------- #
    backend_labels = labels_of(services.get("backend", {}))
    frontend_labels = labels_of(services.get("frontend", {}))
    try:
        api_priority = int(backend_labels[f"{BACKEND_ROUTER}.priority"])
        web_priority = int(frontend_labels[f"{FRONTEND_ROUTER}.priority"])
        if api_priority <= web_priority:
            fail(f"the API router must outrank the frontend router "
                 f"({api_priority} is not greater than {web_priority}); otherwise /api "
                 "is answered by the single-page application with an HTML 200")
    except KeyError as exc:
        fail(f"missing router priority label: {exc}")
    except ValueError:
        fail("router priorities must be integers")

    api_rule = backend_labels.get(f"{BACKEND_ROUTER}.rule", "")
    if "PathPrefix(`/api`)" not in api_rule:
        fail(f"the API router must match PathPrefix(`/api`), found {api_rule!r}")

    # -- no path rewriting anywhere -------------------------------------- #
    for name, service in services.items():
        for key, value in labels_of(service).items():
            if "stripprefix" in f"{key}{value}".lower():
                fail(f"{name} declares a StripPrefix middleware. FastAPI already serves "
                     "/api through root_path, so stripping it here produces /api/api/…")

    # -- networks --------------------------------------------------------- #
    if networks.get("internal", {}).get("internal") is not True:
        fail("the internal network must be marked internal: true, so the database has "
             "no route to or from the internet")
    if networks.get("web", {}).get("internal") is True:
        fail("the web network must NOT be internal — Traefik has to reach it")
    if not networks.get("web", {}).get("name"):
        fail("the web network needs an explicit name, or the traefik.docker.network "
             "label cannot be predicted from the directory this was cloned into")
    # Only present when the shared-network override is in play; when it is, this
    # project must never create the network Traefik already owns.
    if "traefik" in networks and networks["traefik"].get("external") is not True:
        fail("a declared traefik network must be external")

    # The label has to name a network the service is actually on, or Traefik routes to
    # an address it cannot reach.
    for name in ("backend", "frontend"):
        service = services.get(name, {})
        label = labels_of(service).get("traefik.docker.network")
        attached_names = {networks.get(key, {}).get("name") or key
                          for key in (service.get("networks") or {})}
        if label and label not in attached_names:
            fail(f"{name} names traefik.docker.network={label!r}, but it is only on "
                 f"{sorted(attached_names)}")

    return problems


def main() -> int:
    if len(sys.argv) != 2:
        print(__doc__)
        return 2
    config = yaml.safe_load(Path(sys.argv[1]).read_text())
    problems = check(config)
    if problems:
        print("Traefik wiring is wrong:", file=sys.stderr)
        for problem in problems:
            print(f"  - {problem}", file=sys.stderr)
        return 1
    print("Traefik wiring OK: database isolated on an internal network, no host port "
          "published, API router prioritised over the frontend, no path rewriting, "
          "and every traefik.docker.network label names a network the service is on.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
