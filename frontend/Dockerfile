# BuraPay front end: built with Node, served as static files by Caddy.
#
# Two stages, so the runtime image carries no build toolchain — what ships is a static
# file server and a directory of hashed assets. TLS and routing belong to the VPS's
# Traefik; this container speaks plain HTTP on port 80 inside the Docker network and
# publishes nothing.

FROM node:22-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build


FROM caddy:2.8-alpine AS runtime

COPY Caddyfile /etc/caddy/Caddyfile
COPY --from=build /app/dist /srv

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget --quiet --tries=1 --spider http://127.0.0.1/healthz || exit 1

CMD ["caddy", "run", "--config", "/etc/caddy/Caddyfile", "--adapter", "caddyfile"]
