#!/usr/bin/env bash
# scripts/smoke-tests.sh
# Post-deploy smoke tests — run against a live environment after every deploy.
# Exit 0 = healthy, exit 1 = deployment failed, triggers Helm --atomic rollback.

set -euo pipefail

BASE_URL="${1:?Usage: smoke-tests.sh <base-url>}"
MAX_RETRIES=10
RETRY_DELAY=6   # seconds — total wait: 60s for app to stabilise

# ── Colours ────────────────────────────────────────────────────────────────
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m'

pass() { echo -e "${GREEN}✓${NC} $*"; }
fail() { echo -e "${RED}✗${NC} $*"; exit 1; }
info() { echo -e "${YELLOW}→${NC} $*"; }

# ── Retry helper ───────────────────────────────────────────────────────────
retry() {
  local -r desc="$1"; shift
  local attempt=1
  until "$@"; do
    if [[ $attempt -ge $MAX_RETRIES ]]; then
      fail "FAILED after $MAX_RETRIES attempts: $desc"
    fi
    info "Attempt $attempt/$MAX_RETRIES failed for '$desc' — retrying in ${RETRY_DELAY}s..."
    sleep "$RETRY_DELAY"
    ((attempt++))
  done
  pass "$desc"
}

# ── Test Helpers ───────────────────────────────────────────────────────────
check_http() {
  local desc="$1" url="$2" expected_status="${3:-200}"
  local status
  status=$(curl -sf -o /dev/null -w "%{http_code}" \
    --max-time 10 \
    --retry 0 \
    "$url" 2>/dev/null) || status="000"
  [[ "$status" == "$expected_status" ]] || {
    echo "  Expected HTTP $expected_status, got HTTP $status for $url"
    return 1
  }
}

check_json_field() {
  local desc="$1" url="$2" jq_query="$3" expected="$4"
  local actual
  actual=$(curl -sf --max-time 10 "$url" | jq -r "$jq_query" 2>/dev/null) || return 1
  [[ "$actual" == "$expected" ]] || {
    echo "  JSON field '$jq_query': expected '$expected', got '$actual'"
    return 1
  }
}

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo "  Smoke Tests → $BASE_URL"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"

# 1. Readiness probe must be healthy
retry "Readiness probe" \
  check_json_field "readiness" "$BASE_URL/health/ready" ".status" "ready"

# 2. Liveness probe must respond
retry "Liveness probe" \
  check_http "liveness" "$BASE_URL/health/live" "200"

# 3. Correct HTTP status codes on key endpoints
retry "API root returns 200" \
  check_http "api-root" "$BASE_URL/api/v1/" "200"

retry "Unknown route returns 404 (not 500)" \
  check_http "404-check" "$BASE_URL/this-route-does-not-exist" "404"

retry "Metrics endpoint is reachable" \
  check_http "metrics" "$BASE_URL/metrics" "200"

# 4. Verify version label in response headers or JSON
DEPLOYED_VERSION=$(curl -sf --max-time 10 "$BASE_URL/health/ready" | jq -r ".version" 2>/dev/null)
if [[ -n "${EXPECTED_VERSION:-}" ]]; then
  retry "Version matches expected ($EXPECTED_VERSION)" \
    [[ "$DEPLOYED_VERSION" == "$EXPECTED_VERSION" ]]
else
  info "EXPECTED_VERSION not set — deployed version is '$DEPLOYED_VERSION' (skipping version check)"
fi

# 5. TLS certificate must be valid and not expiring within 7 days
if [[ "$BASE_URL" == https://* ]]; then
  DOMAIN="${BASE_URL#https://}"
  EXPIRY_DAYS=$(echo | openssl s_client -servername "$DOMAIN" -connect "${DOMAIN}:443" 2>/dev/null \
    | openssl x509 -noout -checkend $((7 * 86400)) 2>&1 || echo "EXPIRING")
  if echo "$EXPIRY_DAYS" | grep -q "will expire"; then
    fail "TLS certificate expires within 7 days"
  else
    pass "TLS certificate valid (>7 days)"
  fi
fi

# 6. Check Prometheus scrape target is up
if [[ -n "${PROMETHEUS_URL:-}" ]]; then
  retry "Prometheus target is up" \
    check_json_field "prom-target" \
      "$PROMETHEUS_URL/api/v1/query?query=up{job=\"myapp\"}" \
      ".data.result[0].value[1]" "1"
fi

echo ""
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
echo -e "  ${GREEN}All smoke tests passed${NC}"
echo "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━"
