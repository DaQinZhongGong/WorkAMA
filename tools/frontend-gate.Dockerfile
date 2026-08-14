FROM node:24-alpine

WORKDIR /workspace
RUN corepack enable
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml turbo.json ./
COPY apps ./apps
COPY packages ./packages
RUN --mount=type=cache,id=workama-pnpm-store,target=/root/.local/share/pnpm/store pnpm install --frozen-lockfile --ignore-scripts
RUN pnpm frontend:verify
