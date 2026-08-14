FROM node:24-alpine

WORKDIR /test
COPY quality/sdk/node/package.json quality/sdk/node/package-lock.json ./
RUN npm ci --omit=dev
COPY quality/sdk/node/smoke.mjs ./smoke.mjs

CMD ["node", "smoke.mjs"]
