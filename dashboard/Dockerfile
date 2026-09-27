# Patient dashboard, mode A (standalone): the BFF serving the React build.
# Build context = this folder (Railway: service root directory `dashboard`).
#
#   docker build -t dashboard-bff dashboard
#   docker run --rm -p 3000:3000 --env-file dashboard/.env -e COOKIE_SECURE=true dashboard-bff
#
# Runtime configuration is environment only (README "Configuration"); the BFF
# refuses to start when a required variable is missing or invalid. The two
# VITE_* values are baked into the web build, so they are build arguments
# (Railway passes a service variable to the build when its name is declared
# here as ARG).
#
# Base: Debian slim, pinned by digest. node:24-alpine (Alpine 3.24) was tried
# first; its binaries failed with "exec format error" on the Docker Desktop host
# used for testing, while Debian slim runs everywhere we tried.

FROM node:24-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6 AS build
WORKDIR /app
COPY package.json package-lock.json ./
COPY web/package.json web/package.json
COPY bff/package.json bff/package.json
RUN npm ci --no-audit --no-fund
COPY tsconfig.base.json ./
# The web type-check includes a test that imports scripts/register-smart-client.mjs.
COPY scripts scripts
COPY web web
COPY bff bff
# OpenEMR's web origin for the cards' edit links (unset = no links), and its
# date_display_format (0 = YYYY-MM-DD).
ARG VITE_OPENEMR_WEB_URL=""
ARG VITE_DATE_DISPLAY_FORMAT="0"
ENV VITE_OPENEMR_WEB_URL=$VITE_OPENEMR_WEB_URL \
    VITE_DATE_DISPLAY_FORMAT=$VITE_DATE_DISPLAY_FORMAT
RUN npm run build

FROM node:24-bookworm-slim@sha256:0e0ff40c39bc087845bfb27465a0df4ea419520094bc35842ff83dd8cbe6f9b6 AS runtime
ENV NODE_ENV=production \
    HOST=0.0.0.0 \
    PORT=3000
WORKDIR /app
COPY package.json package-lock.json ./
COPY web/package.json web/package.json
COPY bff/package.json bff/package.json
# Production dependencies of the BFF only (the web app is prebuilt static files).
RUN npm ci --omit=dev --workspace=bff --include-workspace-root=false --no-audit --no-fund \
    && npm cache clean --force
COPY --from=build /app/bff/dist bff/dist
COPY --from=build /app/web/dist web/dist
USER node
EXPOSE 3000
HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
    CMD ["node", "-e", "fetch('http://127.0.0.1:' + process.env.PORT + '/healthz').then(r => process.exit(r.ok ? 0 : 1), () => process.exit(1))"]
CMD ["node", "bff/dist/server.js"]
