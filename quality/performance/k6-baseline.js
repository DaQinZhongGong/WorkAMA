import http from 'k6/http';
import { check, sleep } from 'k6';

const baseUrl = (__ENV.K6_BASE_URL || 'http://host.docker.internal:8080').replace(/\/$/, '');
const endpoint = __ENV.K6_ENDPOINT || '/healthz';
const vus = Number(__ENV.K6_VUS || '1');
const duration = __ENV.K6_DURATION || '30s';
const project = __ENV.K6_PROJECT || 'workama';

export const options = {
  vus,
  duration,
  discardResponseBodies: true,
  tags: { workama_project: project, baseline: 'true' },
  thresholds: {
    checks: ['rate>0.99'],
    http_req_failed: ['rate<0.01'],
    http_req_duration: ['p(95)<500'],
  },
};

export default function () {
  const response = http.get(`${baseUrl}${endpoint}`, {
    tags: { endpoint },
  });
  check(response, {
    'response status is 2xx': (result) => result.status >= 200 && result.status < 300,
  });
  sleep(1);
}
