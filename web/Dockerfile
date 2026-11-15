# syntax=docker/dockerfile:1
# Burn Rate front end: Vite build, served by unprivileged nginx on :8080.
# Build context is the REPOSITORY ROOT (one .dockerignore for both images keeps host node_modules out):
#
#   docker buildx build --platform linux/amd64 -t burnrate-web -f web/Dockerfile .
#   docker run --rm -p 8080:8080 burnrate-web        # http://localhost:8080
#
# The bundle talks to the API on its own origin (/api/...). In the cluster the gateway sends /api and /hooks
# to the back end and everything else here; this image never proxies.

ARG NODE_IMAGE=node:24-slim
ARG NGINX_IMAGE=nginxinc/nginx-unprivileged:1.30.6-alpine

# ---- build: static files are the same for every CPU, so build on the machine's own architecture ------------
# (an arm64 laptop building a linux/amd64 image would otherwise run tsc and Vite under emulation)
FROM --platform=$BUILDPLATFORM ${NODE_IMAGE} AS build
WORKDIR /web
COPY web/package.json web/package-lock.json ./
RUN --mount=type=cache,target=/root/.npm,sharing=locked \
    npm ci --no-audit --no-fund
COPY web/ ./
# "build" in package.json is `tsc --noEmit && vite build`; output goes to /web/dist
RUN npm run build \
 && test -f dist/index.html

# ---- runtime ---------------------------------------------------------------------------------------------------
FROM ${NGINX_IMAGE} AS runtime
COPY web/nginx.conf /etc/nginx/nginx.conf
COPY --from=build /web/dist /usr/share/nginx/html
# fail the build, not the pod, on a broken config
RUN nginx -t -c /etc/nginx/nginx.conf
USER 101:101
EXPOSE 8080
CMD ["nginx", "-g", "daemon off;"]
