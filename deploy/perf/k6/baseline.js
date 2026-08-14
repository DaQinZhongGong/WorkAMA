/**
 * WorkAMA 平台 API 基线压测脚本（k6）。
 *
 * 测试目标：建立 platform-api 在稳态并发下的延迟与错误率基线，
 * 作为后续性能优化与回归对照的参照。
 *
 * 测试端点：
 *   - GET /healthz              （无鉴权，平台健康检查）
 *   - GET /api/v1/assistants    （鉴权，Agent/Assistant 列表）
 *
 * 注：任务描述原路径 /api/v1/agents 在当前部署返回 404，已校正为
 * /api/v1/assistants（见 deploy/perf/baseline-report.md 路径校正一节）。
 *
 * 阶段模型：
 *   1) warmup     2 VU  30s     预热连接池与 JIT
 *   2) ramp-up    2→20 VU 60s   渐进加压
 *   3) steady    20 VU 120s    稳态压测（核心采样窗口）
 *   4) ramp-down 20→0 VU 30s   渐进降压
 *
 * 阈值（基线宽松，区别于网关层 P99<30ms 的生产硬指标）：
 *   - http_req_duration p(99) < 500ms
 *   - http_req_failed   rate  < 5%
 *
 * 环境变量：
 *   K6_BASE_URL  默认 http://platform-api:8000（在 workama_default 网络内通过容器别名访问）
 *   K6_TEST_EMAIL 默认 tester@workama.example.com
 *   K6_TEST_PASSWORD 默认 WorkAMA-Test-2026!
 *
 * 执行示例（在宿主机，挂载脚本到 k6 容器并加入 workama_default 网络）：
 *   docker run --rm -i --network=workama_default `
 *     -v "$(pwd)/deploy/perf/k6:/scripts" `
 *     -w /scripts grafana/k6 run - < baseline.js
 */

import http from 'k6/http';
import { check, sleep } from 'k6';
import { Trend, Counter, Rate } from 'k6/metrics';

// 目标地址，去掉尾部斜杠
const baseUrl = (__ENV.K6_BASE_URL || 'http://platform-api:8000').replace(/\/$/, '');
const email = __ENV.K6_TEST_EMAIL || 'tester@workama.example.com';
const password = __ENV.K6_TEST_PASSWORD || 'WorkAMA-Test-2026!';

// 自定义指标：分端点延迟与鉴权失败计数
const healthzDuration = new Trend('healthz_duration_ms', true);
const agentsDuration = new Trend('agents_duration_ms', true);
const authFailures = new Counter('auth_failures');
const errorRate = new Rate('custom_error_rate');

export const options = {
  // 忽略响应体，降低内存压力，聚焦延迟采样
  discardResponseBodies: true,
  tags: {
    workama_project: 'platform-api',
    baseline: 'true',
    phase: 'perf-baseline',
  },
  // 阶段化加压：warmup -> ramp-up -> steady -> ramp-down
  stages: [
    { duration: '30s', target: 2 },    // 预热：避免冷启动噪声
    { duration: '60s', target: 20 },   // 渐进加压到 20 VU
    { duration: '120s', target: 20 },  // 稳态压测主窗口
    { duration: '30s', target: 0 },    // 降压，观察恢复
  ],
  thresholds: {
    // 基线阈值：宽松于网关层生产要求（30ms）
    http_req_duration: ['p(99)<500'],
    http_req_failed: ['rate<0.05'],
    checks: ['rate>0.95'],
    custom_error_rate: ['rate<0.05'],
  },
};

/**
 * 预执行一次登录，获取 access_token 供所有 VU 复用。
 * k6 的 setup() 在全局唯一执行一次，结果传入 default 函数。
 */
export function setup() {
  const loginRes = http.post(
    `${baseUrl}/api/v1/auth/login`,
    JSON.stringify({ email, password }),
    // discardResponseBodies 全局开启会丢弃 body，此处单独保留以便解析 token
    { headers: { 'Content-Type': 'application/json' }, responseType: 'text' }
  );

  const ok = check(loginRes, {
    'login status is 200': (r) => r.status === 200,
  });

  if (!ok || loginRes.status !== 200) {
    authFailures.add(1);
    console.error(`登录失败: HTTP ${loginRes.status} body=${loginRes.body}`);
    return { token: '' };
  }

  let token = '';
  try {
    const parsed = JSON.parse(loginRes.body);
    token = parsed.access_token || '';
  } catch (e) {
    console.error(`解析 token 失败: ${e} body=${loginRes.body}`);
    return { token: '' };
  }
  if (!token) {
    console.error('登录响应未包含 access_token 字段');
    return { token: '' };
  }

  console.log('setup: 登录成功，token 已获取');
  return { token };
}

/**
 * 默认场景：每个 VU 在每次迭代中交替请求 healthz 与 agents，
 * 模拟前端轮询 + 业务请求的混合负载。
 */
export default function (data) {
  // 1) 健康检查（无鉴权）
  const healthRes = http.get(`${baseUrl}/healthz`, {
    tags: { endpoint: 'healthz' },
  });
  const healthOk = check(healthRes, {
    'healthz status 200': (r) => r.status === 200,
  });
  healthzDuration.add(healthRes.timings.duration);
  if (!healthOk || healthRes.status >= 400) {
    errorRate.add(1);
  } else {
    errorRate.add(0);
  }

  // 2) Assistant 列表（鉴权）
  const agentsRes = http.get(`${baseUrl}/api/v1/assistants`, {
    headers: data.token ? { Authorization: `Bearer ${data.token}` } : {},
    tags: { endpoint: 'assistants' },
  });
  const agentsOk = check(agentsRes, {
    'assistants status 200': (r) => r.status === 200,
  });
  agentsDuration.add(agentsRes.timings.duration);
  if (!agentsOk || agentsRes.status >= 400) {
    errorRate.add(1);
  } else {
    errorRate.add(0);
  }

  // 迭代间隔：模拟用户思考时间，避免无脑打满
  sleep(0.3);
}

/**
 * 输出每个端点的趋势摘要，便于在 k6 summary 中对比。
 * 同时写入挂载文件 /out/baseline-summary.json（容器内路径），
 * 避免 PowerShell stderr 包装吞掉 k6 默认 summary 表格。
 */
export function handleSummary(data) {
  const fmt = (m) => {
    if (!m || !m.values) return null;
    const v = m.values;
    return {
      p50: v['p(50)'],
      p90: v['p(90)'],
      p95: v['p(95)'],
      p99: v['p(99)'],
      avg: v['avg'],
      min: v['min'],
      max: v['max'],
      count: v.count,
    };
  };
  const summary = {
    generated_at: new Date().toISOString(),
    base_url: baseUrl,
    healthz_ms: fmt(data.metrics.healthz_duration_ms),
    assistants_ms: fmt(data.metrics.agents_duration_ms),
    http_req_duration_ms: fmt(data.metrics.http_req_duration),
    http_req_failed_rate: data.metrics.http_req_failed
      ? data.metrics.http_req_failed.values.rate
      : null,
    checks_rate: data.metrics.checks ? data.metrics.checks.values.rate : null,
    custom_error_rate: data.metrics.custom_error_rate
      ? data.metrics.custom_error_rate.values.rate
      : null,
    vus_max: data.metrics.vus_max ? data.metrics.vus_max.value : null,
    iterations: data.metrics.iterations ? data.metrics.iterations.value : null,
    rps: data.metrics.http_reqs ? data.metrics.http_reqs.values.rate : null,
  };
  const text = 'WORKAMA_BASELINE_SUMMARY=' + JSON.stringify(summary);
  console.log(text);
  // 同时写入挂载目录（运行时需 -v deploy/perf/out:/out）
  return {
    '/out/baseline-summary.json': JSON.stringify(summary, null, 2),
    stdout: text + '\n',
  };
}
