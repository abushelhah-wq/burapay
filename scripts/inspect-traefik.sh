#!/usr/bin/env bash
#
# Read the VPS's existing Traefik setup and print the values BuraPay's .env needs.
#
#   ./scripts/inspect-traefik.sh
#
# Read-only. It inspects containers and networks and prints a suggested .env block; it
# changes nothing, restarts nothing, and writes no file. Run it on the VPS before the
# first `docker compose up`, because guessing any of these values wrong is the one way
# this deployment can disturb something else on the host.

set -uo pipefail

bold() { printf '\033[1m%s\033[0m\n' "$1"; }
warn() { printf '\033[33m!  %s\033[0m\n' "$1"; }
ok()   { printf '\033[32m✓  %s\033[0m\n' "$1"; }

if ! command -v docker >/dev/null 2>&1; then
  warn "docker is not on PATH. Run this on the VPS, not on your laptop."
  exit 1
fi

if ! docker info >/dev/null 2>&1; then
  warn "Cannot reach the Docker daemon."
  echo "   Either it is not running, or this user is not in the docker group."
  echo "   Try: sudo ./scripts/inspect-traefik.sh"
  exit 1
fi

# --------------------------------------------------------------------------- #
bold "1. Is Traefik running here?"
# --------------------------------------------------------------------------- #

TRAEFIK_ID="$(docker ps --filter "ancestor=traefik" --format '{{.ID}}' 2>/dev/null | head -1)"
if [ -z "$TRAEFIK_ID" ]; then
  # Not every Traefik container is started from an image literally named "traefik".
  TRAEFIK_ID="$(docker ps --format '{{.ID}} {{.Image}} {{.Names}}' 2>/dev/null \
                 | grep -i traefik | awk '{print $1}' | head -1)"
fi

if [ -z "$TRAEFIK_ID" ]; then
  warn "No running Traefik container found."
  echo "   BuraPay expects Traefik to already be running and to own its own network."
  echo "   If Traefik lives elsewhere (host install, another host, Kubernetes), read the"
  echo "   values below off that installation instead."
  echo
else
  NAME="$(docker inspect -f '{{.Name}}' "$TRAEFIK_ID" | sed 's|^/||')"
  IMAGE="$(docker inspect -f '{{.Config.Image}}' "$TRAEFIK_ID")"
  ok "Found Traefik: $NAME ($IMAGE)"
  echo

  # ------------------------------------------------------------------------- #
  bold "2. Which network should BuraPay join?"
  # ------------------------------------------------------------------------- #
  echo "   Networks the Traefik container is attached to:"
  docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}     - {{$k}}{{"\n"}}{{end}}' \
    "$TRAEFIK_ID"
  # Traefik is usually on its own bridge plus, sometimes, the host bridge. The one
  # that is not "bridge"/"host"/"none" is the one other applications join.
  SUGGESTED_NET="$(docker inspect -f '{{range $k, $v := .NetworkSettings.Networks}}{{$k}}{{"\n"}}{{end}}' \
                   "$TRAEFIK_ID" | grep -vE '^(bridge|host|none)$' | head -1)"
  echo

  # ------------------------------------------------------------------------- #
  bold "3. Which entrypoint terminates HTTPS, and which resolver issues the certs?"
  # ------------------------------------------------------------------------- #
  echo "   Traefik's own command line and environment:"
  docker inspect -f '{{range .Config.Cmd}}     {{.}}{{"\n"}}{{end}}' "$TRAEFIK_ID" 2>/dev/null \
    | grep -iE 'entrypoint|certificatesresolver|acme|providers' || echo "     (nothing on the command line)"
  docker inspect -f '{{range .Config.Env}}     {{.}}{{"\n"}}{{end}}' "$TRAEFIK_ID" 2>/dev/null \
    | grep -iE 'TRAEFIK_(ENTRYPOINTS|CERTIFICATESRESOLVERS)' || true
  echo
  echo "   Static config files mounted into the container (traefik.yml / traefik.toml"
  echo "   usually name the entrypoints and resolvers — read whichever is listed):"
  docker inspect -f '{{range .Mounts}}     {{.Source}} -> {{.Destination}}{{"\n"}}{{end}}' \
    "$TRAEFIK_ID" 2>/dev/null || true
  echo
  echo "   Entrypoints and resolvers already in use by other applications on this host"
  echo "   (the safest thing BuraPay can do is use exactly the same ones):"
  docker ps -q | while read -r cid; do
    docker inspect -f '{{range $k, $v := .Config.Labels}}{{$k}}={{$v}}{{"\n"}}{{end}}' "$cid" 2>/dev/null
  done | grep -E 'entrypoints=|certresolver=' | sed 's/^/     /' | sort -u -t= -k2 | head -20
  echo

  SUGGESTED_ENTRY="$(docker ps -q | while read -r cid; do
      docker inspect -f '{{range $k, $v := .Config.Labels}}{{$k}}={{$v}}{{"\n"}}{{end}}' "$cid" 2>/dev/null
    done | grep -oE 'entrypoints=[a-zA-Z0-9_-]+' | cut -d= -f2 | sort | uniq -c | sort -rn \
      | head -1 | awk '{print $2}')"
  SUGGESTED_RESOLVER="$(docker ps -q | while read -r cid; do
      docker inspect -f '{{range $k, $v := .Config.Labels}}{{$k}}={{$v}}{{"\n"}}{{end}}' "$cid" 2>/dev/null
    done | grep -oE 'certresolver=[a-zA-Z0-9_-]+' | cut -d= -f2 | sort | uniq -c | sort -rn \
      | head -1 | awk '{print $2}')"

  # ------------------------------------------------------------------------- #
  bold "4. Is there already a global HTTP → HTTPS redirect?"
  # ------------------------------------------------------------------------- #
  if docker inspect -f '{{range .Config.Cmd}}{{.}} {{end}}' "$TRAEFIK_ID" 2>/dev/null \
       | grep -qi 'redirections'; then
    ok "Yes — Traefik redirects HTTP to HTTPS globally. BuraPay adds no redirect of its own."
  else
    warn "No global redirect found on Traefik's command line."
    echo "   It may still be configured in a mounted traefik.yml, or on a per-router"
    echo "   middleware. Check before adding one: a duplicate redirect is a redirect loop."
  fi
  echo

  # ------------------------------------------------------------------------- #
  bold "5. Does anything already claim this domain?"
  # ------------------------------------------------------------------------- #
  DOMAIN_TO_CHECK="${DOMAIN:-busrapay.com}"
  CLASH="$(docker ps -q | while read -r cid; do
      NAME="$(docker inspect -f '{{.Name}}' "$cid" | sed 's|^/||')"
      docker inspect -f '{{range $k, $v := .Config.Labels}}{{$v}}{{"\n"}}{{end}}' "$cid" 2>/dev/null \
        | grep -F "$DOMAIN_TO_CHECK" | sed "s|^|     $NAME: |"
    done)"
  if [ -n "$CLASH" ]; then
    warn "Another container already routes $DOMAIN_TO_CHECK:"
    echo "$CLASH"
    echo "   Two routers matching the same host is how an existing application gets"
    echo "   taken off the air. Resolve this before starting BuraPay."
  else
    ok "Nothing else routes $DOMAIN_TO_CHECK."
  fi
  echo

  # ------------------------------------------------------------------------- #
  bold "Suggested .env values — check them against the output above"
  # ------------------------------------------------------------------------- #
  echo "DOMAIN=$DOMAIN_TO_CHECK"
  echo "TRAEFIK_NETWORK=${SUGGESTED_NET:-<read it from section 2>}"
  echo "TRAEFIK_ENTRYPOINT=${SUGGESTED_ENTRY:-<read it from section 3>}"
  echo "TRAEFIK_CERT_RESOLVER=${SUGGESTED_RESOLVER:-<read it from section 3>}"
  echo
  echo "These are inferred from what the other containers on this host do. They are a"
  echo "starting point, not an answer — confirm each one against the sections above."
fi

# --------------------------------------------------------------------------- #
bold "All Docker networks on this host"
# --------------------------------------------------------------------------- #
docker network ls
