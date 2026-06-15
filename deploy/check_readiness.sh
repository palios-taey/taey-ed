#!/usr/bin/env bash
# Quick Mira-side readiness check. Verifies the full stack with one command.
# Usage:  ./deploy/check_readiness.sh
#
# Output is human-readable; exit code 0 if everything's green, 1 otherwise.
set -u

HOSTNAME="https://taey-ed-api.taey.ai"
# Readiness-check test account — provide via env, never hardcode (public repo).
TEST_EMAIL="${TEST_EMAIL:?set TEST_EMAIL to the readiness-check account}"
TEST_PW="${TEST_PW:?set TEST_PW to the readiness-check account password}"

green="\033[32m" red="\033[31m" yellow="\033[33m" dim="\033[2m" reset="\033[0m"
fail=0

pass() { printf "  ${green}✓${reset} %s\n" "$1"; }
warn() { printf "  ${yellow}⚠${reset} %s\n" "$1"; }
fail() { printf "  ${red}✗${reset} %s\n" "$1"; fail=1; }
note() { printf "  ${dim}…${reset} ${dim}%s${reset}\n" "$1"; }

section() { printf "\n${dim}── %s ──${reset}\n" "$1"; }

# ── 1. systemd ──
section "systemd services"
for unit in cloudflared-taey-ed taey-ed-api taey-ed-worker; do
    state=$(systemctl is-active "$unit.service" 2>/dev/null)
    if [ "$state" = "active" ]; then
        pass "$unit: active"
    else
        fail "$unit: $state"
    fi
done

# ── 2. tunnel route ──
section "tunnel routing"
code=$(curl -s -m 5 -o /dev/null -w "%{http_code}" "$HOSTNAME/health" || echo "000")
if [ "$code" = "200" ]; then
    pass "GET /health → 200 through Cloudflare"
else
    fail "GET /health → $code (expected 200)"
fi

# ── 3. auth round-trip ──
section "auth round-trip"
# Try login first; signup if not yet present
LOGIN=$(curl -s -m 5 -X POST "$HOSTNAME/auth/login" \
    -H 'Content-Type: application/json' \
    -d "{\"email\":\"$TEST_EMAIL\",\"password\":\"$TEST_PW\"}" 2>/dev/null)
ACCESS=$(echo "$LOGIN" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('access_token',''))" 2>/dev/null)

if [ -z "$ACCESS" ]; then
    # Sign up the readiness-check user
    curl -s -m 5 -X POST "$HOSTNAME/auth/signup" \
        -H 'Content-Type: application/json' \
        -d "{\"email\":\"$TEST_EMAIL\",\"password\":\"$TEST_PW\"}" > /dev/null 2>&1
    LOGIN=$(curl -s -m 5 -X POST "$HOSTNAME/auth/login" \
        -H 'Content-Type: application/json' \
        -d "{\"email\":\"$TEST_EMAIL\",\"password\":\"$TEST_PW\"}" 2>/dev/null)
    ACCESS=$(echo "$LOGIN" | python3 -c "import sys,json; print(json.loads(sys.stdin.read()).get('access_token',''))" 2>/dev/null)
fi

if [ -n "$ACCESS" ]; then
    pass "login → access JWT (${#ACCESS} chars)"
else
    fail "login failed"
fi

# ── 4. embed endpoint with Bearer ──
section "embed endpoint"
if [ -n "$ACCESS" ]; then
    EMBED_OUT=$(curl -s -m 10 -X POST "$HOSTNAME/api/v1/embed" \
        -H "Authorization: Bearer $ACCESS" \
        -H 'Content-Type: application/json' \
        -d '{"texts":"readiness check"}' 2>/dev/null)
    DIM=$(echo "$EMBED_OUT" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); print(d.get('dimension', 0))" 2>/dev/null)
    VEC_LEN=$(echo "$EMBED_OUT" | python3 -c "import sys,json; d=json.loads(sys.stdin.read()); v=d.get('embeddings',[[]])[0]; print(len(v))" 2>/dev/null)
    if [ "$DIM" = "4096" ] && [ "$VEC_LEN" = "4096" ]; then
        pass "embed → reported dim=$DIM, actual vec_len=$VEC_LEN (consistent)"
    else
        fail "embed dim mismatch: reported=$DIM actual=$VEC_LEN"
    fi
else
    note "skipped (no access token)"
fi

# ── 5. size cap ──
section "request body size cap (25MB)"
# Send 26MB; should 413
BIGFILE=$(mktemp)
python3 -c "
import sys
body = b'{\"texts\":[\"' + b'x' * (26 * 1024 * 1024) + b'\"]}'
sys.stdout.buffer.write(body)
" > "$BIGFILE"
CODE=$(curl -s -m 10 -o /dev/null -w "%{http_code}" -X POST "$HOSTNAME/api/v1/embed" \
    -H 'Content-Type: application/json' \
    --data-binary @"$BIGFILE" 2>/dev/null)
rm -f "$BIGFILE"
if [ "$CODE" = "413" ]; then
    pass "26MB POST → 413 (cap enforced)"
else
    warn "26MB POST → $CODE (expected 413; Cloudflare may have rejected earlier)"
fi

# ── 6. embedding service ──
section "upstream embedding service"
if curl -s -m 3 http://127.0.0.1:8089/v1/models > /dev/null; then
    pass "Qwen3-Embedding-8B on 127.0.0.1:8089 reachable"
else
    fail "embedding service on 127.0.0.1:8089 unreachable"
fi

# ── 7. worker liveness ──
section "consultation worker"
WORKER_LOG="$(cd "$(dirname "$0")/.." && pwd)/logs/worker.log"
if [ -f "$WORKER_LOG" ]; then
    RECENT=$(tail -200 "$WORKER_LOG" 2>/dev/null | grep -c "consultation worker starting\|picked up job\|processing\|completed")
    if [ "$RECENT" -gt 0 ]; then
        pass "worker log shows recent activity ($RECENT relevant lines)"
    else
        warn "worker log silent (may just be idle)"
    fi
fi

# ── summary ──
section "summary"
if [ "$fail" = "0" ]; then
    printf "${green}READY${reset} for end-to-end test\n"
    exit 0
else
    printf "${red}NOT READY${reset} — see failures above\n"
    exit 1
fi
