# Frontend container: build with Node, serve the static bundle with Caddy.
#
# A separate service from the API (§1): no HTML is templated by FastAPI, and
# this container holds no credentials and talks to nothing but the browser.

FROM node:22-alpine AS build
WORKDIR /app

COPY package.json package-lock.json* ./
# `npm ci` when a lockfile is present, `npm install` when it is not, so a fresh
# clone builds either way.
RUN if [ -f package-lock.json ]; then npm ci; else npm install; fi

COPY . .
RUN npm run build

FROM caddy:2-alpine
COPY --from=build /app/dist /srv
COPY Caddyfile /etc/caddy/Caddyfile
EXPOSE 8080
