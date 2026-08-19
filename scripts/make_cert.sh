#!/usr/bin/env bash
# Generate a self-signed certificate for local HTTPS development.
#
# Browsers will warn about it — that is expected and correct, since nothing vouches
# for a certificate you signed yourself. Click through it for local testing.
#
# Gateway sandboxes will NOT click through it. They cannot deliver webhooks to a
# host with an untrusted certificate, and most reject a return_url they cannot
# verify. For hosted-checkout flows against a real sandbox you need either a real
# certificate (see deploy/docker-compose.yml, which gets one automatically via
# Caddy) or a tunnel such as ngrok/cloudflared that terminates TLS with a trusted cert.
set -euo pipefail

CERT_DIR="${CERT_DIR:-certs}"
HOSTNAME_="${1:-localhost}"
DAYS="${DAYS:-825}"

mkdir -p "$CERT_DIR"

openssl req -x509 -newkey rsa:2048 -nodes \
  -keyout "$CERT_DIR/key.pem" \
  -out "$CERT_DIR/cert.pem" \
  -days "$DAYS" \
  -subj "/CN=$HOSTNAME_" \
  -addext "subjectAltName=DNS:$HOSTNAME_,DNS:localhost,IP:127.0.0.1" 2>/dev/null

chmod 600 "$CERT_DIR/key.pem"

echo "Wrote $CERT_DIR/cert.pem and $CERT_DIR/key.pem for CN=$HOSTNAME_ (valid $DAYS days)."
echo
echo "Run the app with it:"
echo "  SSL_CERTFILE=$CERT_DIR/cert.pem SSL_KEYFILE=$CERT_DIR/key.pem \\"
echo "  PUBLIC_BASE_URL=https://$HOSTNAME_:8443 python serve.py"
