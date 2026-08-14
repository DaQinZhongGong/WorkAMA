FROM node:24-alpine AS build

WORKDIR /app
RUN corepack enable
# Use a registry mirror (npmmirror.com) to avoid registry.npmjs.org timeouts.
RUN pnpm config set registry https://registry.npmmirror.com
COPY package.json pnpm-workspace.yaml pnpm-lock.yaml ./
COPY apps/miniapp/package.json ./apps/miniapp/package.json
COPY packages/i18n/package.json ./packages/i18n/package.json
RUN pnpm install --frozen-lockfile --filter @workama/miniapp... --ignore-scripts
COPY apps/miniapp ./apps/miniapp
COPY packages/i18n ./packages/i18n
ARG VITE_PLATFORM_API_URL=http://localhost:20200
ENV VITE_PLATFORM_API_URL=$VITE_PLATFORM_API_URL
RUN pnpm --filter @workama/miniapp test
RUN pnpm --filter @workama/miniapp build

FROM nginx:1.27-alpine
COPY apps/miniapp/nginx.conf /etc/nginx/conf.d/default.conf
COPY --from=build /app/apps/miniapp/dist /usr/share/nginx/html
EXPOSE 80
