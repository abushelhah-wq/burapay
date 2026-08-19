# BuraPay front end: built with Node, served by nginx as static files.
#
# Two stages so the runtime image carries no build toolchain — the shipped image is
# nginx plus a directory of hashed assets.

FROM node:22-alpine AS build

WORKDIR /app

COPY package.json package-lock.json ./
RUN npm ci

COPY . .
RUN npm run build


FROM nginx:1.27-alpine AS runtime

# The SPA config: hashed assets are cached hard, index.html never is, and unknown
# paths fall through to index.html so client-side routes survive a page reload.
COPY nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/dist /usr/share/nginx/html

EXPOSE 80

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD wget --quiet --tries=1 --spider http://127.0.0.1/ || exit 1

CMD ["nginx", "-g", "daemon off;"]
