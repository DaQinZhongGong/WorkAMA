#!/bin/sh
# WorkAMA web 运行时配置注入（12-factor）：
# 把部署期环境变量 WEB_* 重写为 /config.js（window.__WORKAMA_CONFIG__），
# 使同一镜像可跨环境部署——改端点只需改 compose env，无需重新构建。
# 未设置的环境变量不写入对应键，前端按 config.ts 的优先级回退到
# VITE_* 构建期默认，行为与旧镜像完全一致。
set -eu

CONFIG_DIST="/app/apps/web/dist/config.js"

{
  printf 'window.__WORKAMA_CONFIG__={'
  sep=''
  for pair in "platformApiUrl:${WEB_PLATFORM_API_URL:-}" \
              "agentWsUrl:${WEB_AGENT_WS_URL:-}" \
              "grafanaUrl:${WEB_GRAFANA_URL:-}"; do
    key=${pair%%:*}
    val=${pair#*:}
    [ -n "$val" ] || continue
    # JSON 字符串安全：拒绝引号与反斜杠，避免注入破坏脚本
    case "$val" in
      *'"'*|*'\'*)
        echo "[web-entrypoint] unsafe characters in $key, skipped" >&2
        continue
        ;;
    esac
    [ -n "$sep" ] && printf '%s' "$sep"
    printf '"%s":"%s"' "$key" "$val"
    sep=','
  done
  printf '};\n'
} > "$CONFIG_DIST"

echo "[web-entrypoint] runtime config written to $CONFIG_DIST:"
cat "$CONFIG_DIST"

exec pnpm --filter @workama/web preview
