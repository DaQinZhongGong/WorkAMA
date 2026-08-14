FROM node:24-alpine
WORKDIR /app

# Install Chromium and system fonts for browser-based tests.
RUN apk add --no-cache chromium nss freetype harfbuzz ttf-freefont
ENV BROWSER_EXECUTABLE=/usr/bin/chromium

RUN corepack enable
# Use a registry mirror to avoid network timeouts when reaching registry.npmjs.org
# from inside Docker on constrained networks (npmmirror.com / 淘宝镜像).
RUN pnpm config set registry https://registry.npmmirror.com

COPY package.json pnpm-workspace.yaml pnpm-lock.yaml turbo.json ./
COPY apps/web/package.json ./apps/web/package.json
COPY apps/mobile/package.json ./apps/mobile/package.json
COPY apps/share/package.json ./apps/share/package.json
COPY apps/desktop/package.json ./apps/desktop/package.json
COPY apps/extension/package.json ./apps/extension/package.json
COPY packages/api-client/package.json ./packages/api-client/package.json
COPY packages/event-renderer/package.json ./packages/event-renderer/package.json
COPY packages/i18n/package.json ./packages/i18n/package.json
COPY packages/config/package.json ./packages/config/package.json
COPY packages/core-state/package.json ./packages/core-state/package.json
COPY packages/ui/package.json ./packages/ui/package.json
COPY apps/web ./apps/web
COPY apps/mobile ./apps/mobile
COPY apps/share ./apps/share
COPY apps/extension ./apps/extension
COPY apps/desktop ./apps/desktop
COPY packages/api-client ./packages/api-client
COPY packages/event-renderer ./packages/event-renderer
COPY packages/i18n ./packages/i18n
COPY packages/config ./packages/config
COPY packages/core-state ./packages/core-state
COPY packages/ui ./packages/ui
RUN --mount=type=cache,id=workama-pnpm,target=/pnpm/store \
    pnpm config set store-dir /pnpm/store && pnpm install --no-frozen-lockfile --ignore-scripts

ARG VITE_PLATFORM_API_URL=http://localhost:20200
ARG VITE_AGENT_WS_URL=ws://localhost:20201
ARG VITE_GRAFANA_URL=http://localhost:20230/d/workama-overview/workama-platform-overview?orgId=1&kiosk=1
ENV VITE_PLATFORM_API_URL=$VITE_PLATFORM_API_URL VITE_AGENT_WS_URL=$VITE_AGENT_WS_URL VITE_GRAFANA_URL=$VITE_GRAFANA_URL
RUN pnpm --filter @workama/web build

EXPOSE 3000
CMD ["pnpm", "--filter", "@workama/web", "preview"]
