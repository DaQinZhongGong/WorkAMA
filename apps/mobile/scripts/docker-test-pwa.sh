#!/bin/sh
# Run the real-browser PWA acceptance tests inside the WorkAMA web container.
# Assumes the container has Chromium at /usr/bin/chromium (see apps/web/Dockerfile).
set -e

cd /app/apps/mobile
pnpm build
MOBILE_PWA_BASE_URL=http://127.0.0.1:3100 BROWSER_EXECUTABLE=/usr/bin/chromium pnpm exec playwright test
