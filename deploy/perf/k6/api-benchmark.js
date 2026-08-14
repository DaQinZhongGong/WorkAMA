/**
 * WorkAMA 平台 API 多端点基准对比脚本（k6）。
 *
 * 测试目标：对 5 个关键端点在固定请求数下采集延迟分布与吞吐，
 * 生成横向对比表格，定位延迟热点端点。
 *
 * 端点集：
 *   1) GET  /healthz                              无鉴权，健康检查
 *   2) GET  /api/v1/agents                        鉴权，Agent 列表
 *   3) POST /api/v1/memory-vectors/recall         鉴权，记忆召回（body: query+limit）
 *   4) GET  /api/v1/workflows-v2                  鉴权，工作流列表
 *   5) GET  /api/v1/knowledge/golden-sets?limit=5 鉴权，金标集列表
 *
 * 模型：单 VU 串行，每端点固定 100 请求，避免并发互相干扰，
 * 确保延迟分布反映端点自身开销而非资源争用。
 *
 * 环境变量：
 *   K6_BASE_URL      默认 http://platform-api:8000
 *   K6_BENCH_REQUESTS 每端点请求数，默认 100
 *
 * 执行示例：
 *   docker run --rm -i --network=workama_default `
 *     -v "$(pwd)/deploy/perf/k6:/scripts" `
 *     -w /scripts grafana/k6 run - < api-benchmark.js
 */

import http from 'k6/http';
import { check } from 'k6';
import { Trend } from 'k6/metrics';

const baseUrl = (__ENV.K6_BASE_URL || 'http://platform-api:8000').replace(/\/$/, '');
const email = __ENV.K6_TEST_EMAIL || 'tester@workama.example.com';
const password = __ENV.K6_TEST_PASSWORD || 'WorkAMA-Test-2026!';
const requestsPerEndpoint = parseInt(__ENV.K6_BENCH_REQUESTS || '100', 10);

// 每端点一个独立 Trend，便于分维度聚合
const metrics = {
  healthz: new Trend('bench_healthz_ms', true),
  agents: new Trend('bench_agents_ms', true),
  memory_recall: new Trend('bench_memory_recall_ms', true),
  workflows: new Trend('bench_workflows_ms', true),
  golden_sets: new Trend('bench_golden_sets_ms', true),
};

export const options = {
  discardResponseBodies: true,
  // 单 VU 串行执行 N 次迭代，每次遍历 5 端点，故每端点采集 N 个样本
  vus: 1,
  iterations: requestsPerEndpoint,
  thresholds: {
    checks: ['rate>0.95'],
  },
};

// 端点定义表：name / method / path / body / metric
// 注：任务描述原路径 /api/v1/agents、/api/v1/workflows-v2 在当前部署中返回 404，
// 经探测校正为 /api/v1/assistants、/api/v1/workflows（见 baseline-report.md 路径校正一节）。
const endpoints = [
  { name: 'healthz', method: 'GET', path: '/healthz', body: null, metric: metrics.healthz },
  { name: 'assistants', method: 'GET', path: '/api/v1/assistants', body: null, metric: metrics.agents },
  {
    name: 'memory-recall',
    method: 'POST',
    path: '/api/v1/memory-vectors/recall',
    body: JSON.stringify({ query: 'test', limit: 5 }),
    metric: metrics.memory_recall,
  },
  { name: 'workflows', method: 'GET', path: '/api/v1/workflows', body: null, metric: metrics.workflows },
  { name: 'golden-sets', method: 'GET', path: '/api/v1/knowledge/golden-sets?limit=5', body: null, metric: metrics.golden_sets },
];

export function setup() {
  const loginRes = http.post(
    `${baseUrl}/api/v1/auth/login`,
    JSON.stringify({ email, password }),
    // discardResponseBodies 全局开启会丢弃 body，此处单独保留以便解析 token
    { headers: { 'Content-Type': 'application/json' }, responseType: 'text' }
  );
  if (loginRes.status !== 200) {
    console.error(`benchmark setup 登录失败: HTTP ${loginRes.status} body=${loginRes.body}`);
    return { token: '' };
  }
  try {
    const parsed = JSON.parse(loginRes.body);
    return { token: parsed.access_token || '' };
  } catch (e) {
    console.error(`benchmark setup 解析 token 失败: ${e} body=${loginRes.body}`);
    return { token: '' };
  }
}

export default function (data) {
  const authHeaders = data.token ? { Authorization: `Bearer ${data.token}` } : {};

  // 遍历端点，每个串行发 N 次请求
  for (const ep of endpoints) {
    const params = { headers: authHeaders, tags: { endpoint: ep.name } };
    let res;
    if (ep.method === 'POST') {
      params.headers['Content-Type'] = 'application/json';
      res = http.post(`${baseUrl}${ep.path}`, ep.body, params);
    } else {
      res = http.get(`${baseUrl}${ep.path}`, params);
    }

    const ok = check(res, {
      [`${ep.name} status 2xx`]: (r) => r.status >= 200 && r.status < 300,
    });
    if (!ok) {
      console.warn(`endpoint=${ep.name} HTTP=${res.status}（仍记录延迟用于诊断）`);
    }
    ep.metric.add(res.timings.duration);
  }
}

/**
 * 输出对比表格到 stdout，便于直接复制进报告。
 */
export function handleSummary(data) {
  const pick = (name) => {
    const m = data.metrics[name];
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

  const rows = [
    { metric: 'bench_healthz_ms', endpoint: 'healthz' },
    { metric: 'bench_agents_ms', endpoint: 'agents' },
    { metric: 'bench_memory_recall_ms', endpoint: 'memory-recall' },
    { metric: 'bench_workflows_ms', endpoint: 'workflows' },
    { metric: 'bench_golden_sets_ms', endpoint: 'golden-sets' },
  ];

  const table = rows.map((r) => {
    const s = pick(r.metric);
    return {
      endpoint: r.endpoint,
      count: s ? s.count : 0,
      p50_ms: s ? s.p50 : null,
      p90_ms: s ? s.p90 : null,
      p95_ms: s ? s.p95 : null,
      p99_ms: s ? s.p99 : null,
      avg_ms: s ? s.avg : null,
      min_ms: s ? s.min : null,
      max_ms: s ? s.max : null,
    };
  });

  console.log('WORKAMA_BENCH_TABLE=' + JSON.stringify(table));
  // 打印人类可读表格
  console.log('\n========== WorkAMA API 基准对比 ==========');
  console.log(
    'endpoint'.padEnd(16) +
      'count'.padStart(8) +
      'p50'.padStart(10) +
      'p90'.padStart(10) +
      'p95'.padStart(10) +
      'p99'.padStart(10) +
      'avg'.padStart(10) +
      'min'.padStart(10) +
      'max'.padStart(10)
  );
  for (const r of table) {
    console.log(
      r.endpoint.padEnd(16) +
        String(r.count).padStart(8) +
        (r.p50_ms != null ? r.p50_ms.toFixed(2).padStart(10) : 'NA'.padStart(10)) +
        (r.p90_ms != null ? r.p90_ms.toFixed(2).padStart(10) : 'NA'.padStart(10)) +
        (r.p95_ms != null ? r.p95_ms.toFixed(2).padStart(10) : 'NA'.padStart(10)) +
        (r.p99_ms != null ? r.p99_ms.toFixed(2).padStart(10) : 'NA'.padStart(10)) +
        (r.avg_ms != null ? r.avg_ms.toFixed(2).padStart(10) : 'NA'.padStart(10)) +
        (r.min_ms != null ? r.min_ms.toFixed(2).padStart(10) : 'NA'.padStart(10)) +
        (r.max_ms != null ? r.max_ms.toFixed(2).padStart(10) : 'NA'.padStart(10))
    );
  }
  console.log('=========================================\n');
  return {};
}
