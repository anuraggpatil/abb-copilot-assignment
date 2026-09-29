# Build the GUI, then serve the static bundle with nginx.
#
# UNVERIFIED: never built — `docker` is not installed on the machine this was written on.
# See docs/known-limitations.md.

FROM node:22-alpine AS build

WORKDIR /app

# Manifests first so a source edit does not reinstall node_modules. `npm ci` rather than
# `npm install`: the lockfile is the contract, and `install` would quietly update it.
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

# Vite inlines `import.meta.env.VITE_*` at build time, so the backend URL is baked into the
# bundle and cannot be changed by an environment variable on the running container. It must be
# the URL **the browser** uses — the host's `localhost:8080`, not compose's internal
# `http://copilot-backend:8080`, which the browser cannot resolve. That is the whole reason
# this is a build arg rather than a runtime env var.
ARG VITE_BACKEND_URL=http://localhost:8080
ENV VITE_BACKEND_URL=$VITE_BACKEND_URL

# `tsc -b && vite build` — the type check is part of the build on purpose: a type error that
# only shows up in CI is a type error that ships to a demo.
RUN npm run build


FROM nginx:1.27-alpine AS serve

COPY --from=build /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 80

# nginx's own stub status would need a module; a request for the served index page is a truer
# health signal anyway — it proves the bundle is actually there.
HEALTHCHECK --interval=15s --timeout=3s --start-period=5s --retries=3 \
    CMD wget --quiet --spider http://localhost/ || exit 1
