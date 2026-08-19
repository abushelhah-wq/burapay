# nginx and TLS

`nginx.conf` is an nginx *template*: `${DOMAIN}` is substituted at container start by
the official image's entrypoint, which is why it is mounted into
`/etc/nginx/templates/` rather than `/etc/nginx/conf.d/`.

## First certificate

nginx will not start without a certificate at the path the config names, and certbot's
HTTP challenge needs nginx running — so the first issuance needs one manual step.

```bash
# 1. Point busrapay.com's A/AAAA record at this host, and open 80 and 443.
# 2. Bring up everything except nginx.
docker compose up -d postgres backend frontend

# 3. Issue the certificate in standalone mode, which binds port 80 itself.
docker compose run --rm --service-ports certbot certonly \
  --standalone -d busrapay.com -d www.busrapay.com \
  --email you@example.com --agree-tos --no-eff-email

# 4. Start the proxy.
docker compose up -d nginx
```

Renewal is automatic from then on: the `certbot` service wakes every twelve hours and
renews anything inside thirty days of expiry, writing challenge files into the shared
webroot volume that nginx serves.

## Certificates are not in the repository

`nginx/certs/` is mounted from the host and is git-ignored. Nothing that constitutes a
private key belongs in version control, and a certificate baked into an image outlives
every deployment that image is used for.

## Using an existing reverse proxy

If the VPS already terminates TLS for other applications, drop the `nginx` and
`certbot` services from `docker-compose.yml`, publish the `frontend` and `backend`
ports on localhost only, and point the existing proxy at them. The backend requires
three things from whatever sits in front of it:

* `X-Forwarded-Proto` set correctly — the application uses it to decide whether to
  mark cookies `Secure`;
* `/api/` forwarded with the prefix intact;
* HTTPS on the public origin, because every gateway sandbox rejects non-HTTPS return
  and webhook URLs.
