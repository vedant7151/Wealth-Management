# Multi-stage: build the static Vite bundle, then serve it from a
# minimal non-root nginx image. Build-time VITE_* vars are baked into the
# bundle (Vite inlines them at build time, not read at container start --
# see docs/DEPLOY.md for why that means the API base URL/Clerk key must
# be passed as --build-arg, not a runtime env var).

FROM node:22.11.0-slim@sha256:f035ba7ffee18f67200e2eb8018e0f13c954ec16338f264940f701997e3c12da AS builder

WORKDIR /app
COPY package.json package-lock.json ./
RUN npm ci

COPY . .

ARG VITE_API_BASE_URL=http://localhost:8010
ARG VITE_CLERK_PUBLISHABLE_KEY=
ENV VITE_API_BASE_URL=${VITE_API_BASE_URL} \
    VITE_CLERK_PUBLISHABLE_KEY=${VITE_CLERK_PUBLISHABLE_KEY}
RUN npm run build


FROM nginxinc/nginx-unprivileged:1.31-alpine@sha256:b5047b9b76845eac124358373c6852377c52e4b05834a47647e955dd91cb8f1e AS runtime

COPY --from=builder /app/dist /usr/share/nginx/html
COPY nginx.conf /etc/nginx/conf.d/default.conf

EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=3s --start-period=5s --retries=3 \
    CMD wget -q -O- http://127.0.0.1:8080/ >/dev/null || exit 1
