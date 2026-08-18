/**
 * WorkAMA 平台开销 P99 验收脚本（k6）。
 *
 * 与 baseline.js 同源，但显式把 p(99) 加入 thresholds，
 * 强制 k6 计算并在 summary 输出 p50/p90/p95/p99，便于对《800/830》的
 * "网关平台开销 P99 < 30ms" 验收线下定论。
 *
 * 端点：
 *   - GET /healthz           （无鉴权，豁免限流）
 *   - GET /api/v1/assistants （鉴权，落 default 限流桶）
 *
 * 注：本脚本用于"平台延迟预算"测量。压测时通过把 RATE_LIMIT_*_PER_MIN
 * 调高来隔离限流影响（限流是安全控制，与延迟预算正交）。
 */

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend, Rate } from 'k6/metrics';

const baseUrl = (__ENV.K6_BASE_URL || 'http://platform-api:8000').replace(/\/$/, '');
const email = __ENV.K6_TEST_EMAIL || 'tester@workama.example.com';
const password = __ENV.K6_TEST_PASSWORD || 'WorkAMA-Test-2026!';

const healthzDuration = new Trend('healthz_duration_ms', true);
const agentsDuration = new Trend('agents_duration_ms', true);
const errorRate = new Rate('custom_error_rate');

export const options = {
  discardResponseBodies: true,
  tags: { workama_project: 'platform-api', phase: 'p99-acceptance' },
  stages: [
    { duration: '30s', target: 2 },
    { duration: '60s', target: 20 },
    { duration: '120s', target: 20 },
    { duration: '30s', target: 0 },
  ],
  thresholds: {
    // 显式引用 p(99) 以强制 k6 计算并暴露该分位
    http_req_duration: ['p(99)<1000'],
    healthz_duration_ms: ['p(99)<1000'],
    agents_duration_ms: ['p(99)<1000'],
    http_req_failed: ['rate<0.05'],
    custom_error_rate: ['rate<0.05'],
  },
};

export function setup() {
  const loginRes = http.post(
    `${baseUrl}/api/v1/auth/login`,
    JSON.stringify({ email, password }),
    { headers: { 'Content-Type': 'application/json' }, responseType: 'text' }
  );
  if (loginRes.status !== 200) {
    console.error(`登录失败: HTTP ${loginRes.status}`);
    return { token: '' };
  }
  let token = '';
  try {
    token = JSON.parse(loginRes.body).access_token || '';
  } catch (e) {
    console.error(`解析 token 失败: ${e}`);
  }
  if (!token) console.error('登录响应未包含 access_token');
  else console.log('setup: 登录成功');
  return { token };
}

export default function (data) {
  const healthRes = http.get(`${baseUrl}/healthz`, { tags: { endpoint: 'healthz' } });
  check(healthRes, { 'healthz 200': (r) => r.status === 200 });
  healthzDuration.add(healthRes.timings.duration);
  errorRate.add(healthRes.status >= 400 ? 1 : 0);

  const agentsRes = http.get(`${baseUrl}/api/v1/assistants`, {
    headers: data.token ? { Authorization: `Bearer ${data.token}` } : {},
    tags: { endpoint: 'assistants' },
  });
  check(agentsRes, { 'assistants 200': (r) => r.status === 200 });
  agentsDuration.add(agentsRes.timings.duration);
  errorRate.add(agentsRes.status >= 400 ? 1 : 0);

  sleep(0.3);
}

export function handleSummary(data) {
  const fmt = (m) => {
    if (!m || !m.values) return null;
    const v = m.values;
    return {
      p50: v['p(50)'], p90: v['p(90)'], p95: v['p(95)'], p99: v['p(99)'],
      avg: v['avg'], min: v['min'], max: v['max'], count: v.count,
    };
  };
  const summary = {
    generated_at: new Date().toISOString(),
    base_url: baseUrl,
    healthz_ms: fmt(data.metrics.healthz_duration_ms),
    assistants_ms: fmt(data.metrics.agents_duration_ms),
    http_req_duration_ms: fmt(data.metrics.http_req_duration),
    http_req_failed_rate: data.metrics.http_req_failed ? data.metrics.http_req_failed.values.rate : null,
    custom_error_rate: data.metrics.custom_error_rate ? data.metrics.custom_error_rate.values.rate : null,
    rps: data.metrics.http_reqs ? data.metrics.http_reqs.values.rate : null,
    iterations: data.metrics.iterations ? data.metrics.iterations.value : null,
  };
  const text = 'WORKAMA_P99_SUMMARY=' + JSON.stringify(summary);
  console.log(text);
  return { '/out/p99-summary.json': JSON.stringify(summary, null, 2), stdout: text + '\n' };
}
