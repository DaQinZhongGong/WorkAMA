FROM node:24-alpine

WORKDIR /workspace
RUN corepack enable
RUN apk add --no-cache chromium nss freetype harfbuzz ttf-freefont
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml turbo.json ./
COPY apps ./apps
COPY packages ./packages
RUN pnpm install --frozen-lockfile --ignore-scripts
ARG VITE_PLATFORM_API_URL=http://host.docker.internal:8000
ARG VITE_AGENT_WS_URL=ws://host.docker.internal:8001
ENV VITE_PLATFORM_API_URL=$VITE_PLATFORM_API_URL VITE_AGENT_WS_URL=$VITE_AGENT_WS_URL
RUN pnpm --filter @workama/web build
