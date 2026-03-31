// scripts/load-test.js
// k6 load test — validates performance SLO after every deploy to staging/prod.
// Run: docker run --rm -v $PWD/scripts:/scripts grafana/k6 run /scripts/load-test.js
//
// Thresholds enforce the SLOs defined in monitoring/alerts/slo-alerts.yaml:
//   - P99 latency < 500ms
//   - Error rate < 0.1%
//   - P95 latency < 200ms

import http from "k6/http";
import { check, group, sleep } from "k6";
import { Rate, Trend, Counter } from "k6/metrics";
import { randomIntBetween } from "https://jslib.k6.io/k6-utils/1.4.0/index.js";

// ── Config ──────────────────────────────────────────────────────────────────
const BASE_URL = __ENV.BASE_URL || "http://localhost:8080";
const THINK_TIME_MIN = 0.5;
const THINK_TIME_MAX = 2.0;

// ── Custom Metrics ────────────────────────────────────────────────────────
const errorRate = new Rate("error_rate");
const apiDuration = new Trend("api_duration", true);   // true = display as ms
const requestCount = new Counter("total_requests");

// ── Test Scenarios ────────────────────────────────────────────────────────
export const options = {
  scenarios: {
    // Baseline: steady low load
    baseline: {
      executor: "constant-vus",
      vus: 5,
      duration: "1m",
      tags: { scenario: "baseline" },
    },
    // Ramp: simulate traffic spike
    ramp: {
      executor: "ramping-vus",
      startTime: "1m",
      startVUs: 5,
      stages: [
        { duration: "30s", target: 50 },   // ramp up
        { duration: "1m",  target: 50 },   // hold
        { duration: "30s", target: 5  },   // ramp down
      ],
      tags: { scenario: "ramp" },
    },
  },

  // SLO enforcement — CI fails if these are breached
  thresholds: {
    // Availability SLO: < 0.1% errors
    "error_rate":                                 ["rate<0.001"],
    // Latency SLO: P99 < 500ms, P95 < 200ms
    "http_req_duration{url_type:api}":            ["p(99)<500", "p(95)<200"],
    // Health endpoint must always be fast
    "http_req_duration{url_type:health}":         ["p(99)<100"],
    // No HTTP failures at all on health probes
    "http_req_failed{url_type:health}":           ["rate==0"],
  },

  // k6 Cloud output (optional — configure K6_CLOUD_TOKEN in env)
  ext: {
    loadimpact: {
      projectID: __ENV.K6_PROJECT_ID,
      name: `Smoke — ${__ENV.ENVIRONMENT || "staging"} — ${new Date().toISOString()}`,
    },
  },
};

// ── Shared Headers ────────────────────────────────────────────────────────
const headers = {
  "Content-Type": "application/json",
  "Accept": "application/json",
};

// ── Helpers ───────────────────────────────────────────────────────────────
function tag(urlType) {
  return { tags: { url_type: urlType } };
}

function assertSuccess(res, name) {
  const ok = check(res, {
    [`${name}: status 200`]: (r) => r.status === 200,
    [`${name}: has body`]:   (r) => r.body && r.body.length > 0,
  });
  errorRate.add(!ok);
  requestCount.add(1);
  apiDuration.add(res.timings.duration);
  return ok;
}

// ── Main VU Loop ──────────────────────────────────────────────────────────
export default function () {
  group("Health probes", () => {
    const live  = http.get(`${BASE_URL}/health/live`,  tag("health"));
    const ready = http.get(`${BASE_URL}/health/ready`, tag("health"));
    check(live,  { "liveness: 200":  (r) => r.status === 200 });
    check(ready, { "readiness: 200": (r) => r.status === 200 });
  });

  group("API endpoints", () => {
    // List items
    const list = http.get(`${BASE_URL}/api/v1/items`, { headers, ...tag("api") });
    assertSuccess(list, "GET /items");

    sleep(randomIntBetween(THINK_TIME_MIN * 10, THINK_TIME_MAX * 10) / 10);

    // Create item
    const payload = JSON.stringify({
      name: `load-test-item-${__VU}-${__ITER}`,
      value: randomIntBetween(1, 100),
    });
    const create = http.post(`${BASE_URL}/api/v1/items`, payload, { headers, ...tag("api") });
    assertSuccess(create, "POST /items");

    // Parse the ID and fetch by ID
    if (create.status === 201) {
      const item = create.json();
      if (item && item.id) {
        sleep(0.1);
        const get = http.get(`${BASE_URL}/api/v1/items/${item.id}`, { headers, ...tag("api") });
        assertSuccess(get, "GET /items/:id");
      }
    }
  });

  group("Error handling", () => {
    // Verify the app returns proper 404 (not 500) for unknown routes
    const notFound = http.get(`${BASE_URL}/api/v1/items/nonexistent-id-9999`, tag("api"));
    check(notFound, { "404 not 500": (r) => r.status === 404 });
  });

  sleep(randomIntBetween(THINK_TIME_MIN * 10, THINK_TIME_MAX * 10) / 10);
}

// ── Teardown: print summary ───────────────────────────────────────────────
export function handleSummary(data) {
  const p99  = data.metrics.http_req_duration?.values?.["p(99)"]?.toFixed(1) ?? "n/a";
  const p95  = data.metrics.http_req_duration?.values?.["p(95)"]?.toFixed(1) ?? "n/a";
  const errs = ((data.metrics.error_rate?.values?.rate ?? 0) * 100).toFixed(3);
  const reqs = data.metrics.total_requests?.values?.count ?? 0;

  console.log(`\n──────────────────────────────────────`);
  console.log(`  Load Test Summary`);
  console.log(`  Total requests : ${reqs}`);
  console.log(`  Error rate     : ${errs}%  (SLO: <0.1%)`);
  console.log(`  P95 latency    : ${p95}ms  (SLO: <200ms)`);
  console.log(`  P99 latency    : ${p99}ms  (SLO: <500ms)`);
  console.log(`──────────────────────────────────────\n`);

  return {
    stdout: JSON.stringify(data, null, 2),
  };
}
