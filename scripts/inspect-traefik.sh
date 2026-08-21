#!/usr/bin/env bash
#
# Report the Traefik settings this host actually uses.
#
# Run this ON THE VPS before deploying behind Traefik, and copy the values it
# prints into .env. They must not be guessed: an entrypoint or certificate
# resolver that does not exist produces a router Traefik silently ignores,
# which looks exactly like the application being down.
#
#   ./scripts/inspect-traefik.sh
#
# Read-only. It inspects containers and networks and changes nothing.

set -uo pipefail

bold() { printf '\033[1m%s\033[0m\n' "$*"; }
warn() { printf '\033[33m%s\033[0m\n' "$*"; }
fail() { printf '\033[31m%s\033[0m\n' "$*"; }

if ! command -v docker >/dev/null 2>&1; then
  fail "docker is not on PATH. Run this on the VPS, not on your laptop."
  exit 1
fi

bold "== Traefik containers =="
traefik_containers=$(docker ps --filter "ancestor=traefik" --format '{{.ID}}' 2>/dev/null)
if [ -z "$traefik_containers" ]; then
  # Some hosts run a retagged or forked image; fall back to a name match.
  traefik_containers=$(docker ps --format '{{.ID}} {{.Image}} {{.Names}}' \
    | grep -i traefik | awk '{print $1}')
fi

if [ -z "$traefik_containers" ]; then
  warn "No running Traefik container found."
  warn "If this host does not run Traefik, leave TRAEFIK_NETWORK unset and deploy"
  warn "with the plain docker-compose.yml -- the bundled Caddy will handle TLS."
  exit 0
fi

for cid in $traefik_containers; do
  name=$(docker inspect -f '{{.Name}}' "$cid" | sed 's|^/||')
  image=$(docker inspect -f '{{.Config.Image}}' "$cid")
  echo "container: $name  ($image)"

  echo
  bold "-- entrypoints (TRAEFIK_ENTRYPOINT) --"
  # Entrypoints are declared either as CLI flags or as env vars. Check both;
  # a host configured through traefik.yml will show neither, hence the hint.
  {
    docker inspect -f '{{range .Config.Cmd}}{{println .}}{{end}}' "$cid"
    docker inspect -f '{{range .Args}}{{println .}}{{end}}' "$cid"
    docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$cid"
  } 2>/dev/null | grep -iE 'entrypoint' | sed 's/^/  /' | sort -u

  echo "  (the name you want is the part after entryPoints. -- commonly"
  echo "   'websecure' for :443 and 'web' for :80)"

  echo
  bold "-- certificate resolvers (TRAEFIK_CERT_RESOLVER) --"
  {
    docker inspect -f '{{range .Config.Cmd}}{{println .}}{{end}}' "$cid"
    docker inspect -f '{{range .Args}}{{println .}}{{end}}' "$cid"
    docker inspect -f '{{range .Config.Env}}{{println .}}{{end}}' "$cid"
  } 2>/dev/null | grep -iE 'certificatesresolvers|certresolver|acme' \
    | sed 's/^/  /' | sort -u
  echo "  (the name you want is the part after certificatesResolvers. --"
  echo "   commonly 'letsencrypt' or 'le')"

  echo
  bold "-- networks (TRAEFIK_NETWORK) --"
  docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}  {{$k}}{{println}}{{end}}' "$cid"
  echo "  Pick the network your other proxied apps are also on -- usually not"
  echo "  'bridge' and not the compose-default network of Traefik's own stack."

  echo
  bold "-- published ports --"
  docker inspect -f '{{range $p, $c := .NetworkSettings.Ports}}  {{$p}} -> {{$c}}{{println}}{{end}}' "$cid" \
    | grep -v '\-> $' || echo "  (none published)"

  echo
  bold "-- how existing apps label themselves (copy these conventions) --"
  docker ps --format '{{.Names}}' | while read -r other; do
    labels=$(docker inspect -f \
      '{{range $k, $v := .Config.Labels}}{{if or (hasPrefix $k "traefik.http.routers") (hasPrefix $k "traefik.docker")}}    {{$k}}={{$v}}{{println}}{{end}}{{end}}' \
      "$other" 2>/dev/null)
    if [ -n "$labels" ]; then
      echo "  $other:"
      echo "$labels"
    fi
  done
  echo
done

bold "== Put these in .env =="
cat <<'HINT'
  TRAEFIK_NETWORK=<the shared network name above>
  TRAEFIK_ENTRYPOINT=<the https entrypoint name above>
  TRAEFIK_CERT_RESOLVER=<the certificate resolver name above>

Then deploy with:
  docker compose -f docker-compose.yml -f docker-compose.traefik-network.yml up -d

Leave TRAEFIK_NETWORK unset to use the bundled Caddy instead:
  docker compose up -d
HINT
