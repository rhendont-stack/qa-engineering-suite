// Written by k6_script.py from the qa-performance skill's assets/k6-template.js. Do not edit the copy: change the workload
// and write the script again, so that what runs is what the workload and its limits say.
//
// The workload and the options below are data. The base URL comes from the run (-e BASE_URL=...) and must be the loopback
// address of the service the same container run started; this file holds no address at all. The summary goes where the
// run says (-e SUMMARY_PATH=/qa-evidence/<file>), which is the k6 report the kernel stores and reads.
import http from 'k6/http';
import { check, sleep } from 'k6';
import exec from 'k6/execution';

const WORKLOAD = __QA_WORKLOAD__;

export const options = __QA_OPTIONS__;

const BASE_URL = __ENV.BASE_URL;
const LOOPBACK = /^http:\/\/127\.0\.0\.1:[0-9]{1,5}$/;
if (typeof BASE_URL !== 'string' || !LOOPBACK.test(BASE_URL)) {
  throw new Error('refused: BASE_URL must be the run service on the loopback address 127.0.0.1 with a port, and nothing else');
}

// Each request appears as often as its weight; iteration i of the test sends entry i modulo the length. The order is fixed,
// so two runs of one workload send the same mix.
const SEQUENCE = [];
WORKLOAD.requests.forEach(function (request, index) {
  for (let n = 0; n < request.weight; n += 1) {
    SEQUENCE.push(index);
  }
});

export default function () {
  const request = WORKLOAD.requests[SEQUENCE[exec.scenario.iterationInTest % SEQUENCE.length]];
  const params = { headers: request.headers, tags: { name: request.name }, timeout: WORKLOAD.request_timeout };
  const response = http.request(request.method, BASE_URL + request.path, request.body, params);
  const checks = {};
  checks[request.name + ' answers ' + request.expect] = function (r) {
    return r.status === request.expect;
  };
  check(response, checks);
  if (WORKLOAD.think_time_s > 0) {
    sleep(WORKLOAD.think_time_s);
  }
}

function brief(data) {
  const lines = ['k6 summary (the whole object is the run k6 report):'];
  ['http_req_duration', 'http_req_failed', 'http_reqs', 'iterations', 'vus_max', 'checks'].forEach(function (name) {
    const metric = data.metrics[name];
    if (metric) {
      lines.push('  ' + name + ' ' + JSON.stringify(metric.values));
    }
  });
  if (data.state && typeof data.state.testRunDurationMs === 'number') {
    lines.push('  run duration ms ' + data.state.testRunDurationMs);
  }
  return lines.join('\n') + '\n';
}

export function handleSummary(data) {
  const out = { stdout: brief(data) };
  if (typeof __ENV.SUMMARY_PATH === 'string' && __ENV.SUMMARY_PATH !== '') {
    out[__ENV.SUMMARY_PATH] = JSON.stringify(data, null, 2) + '\n';
  }
  return out;
}
