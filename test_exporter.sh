#!/bin/bash
# Test script for Solana Validator Exporter
# Verifies the exporter is running and metrics are being collected

set -e

EXPORTER_URL="${EXPORTER_URL:-http://localhost:8080}"

echo "=========================================="
echo "Solana Validator Exporter - Test Script"
echo "=========================================="
echo ""

# Color codes
GREEN='\033[0;32m'
RED='\033[0;31m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Helper functions
pass() {
    echo -e "${GREEN}✓ PASS${NC}: $1"
}

fail() {
    echo -e "${RED}✗ FAIL${NC}: $1"
    exit 1
}

warn() {
    echo -e "${YELLOW}⚠ WARN${NC}: $1"
}

# Test 1: Exporter is running
echo "Test 1: Checking if exporter is running..."
if curl -s -f "${EXPORTER_URL}/" > /dev/null 2>&1; then
    pass "Exporter is running on ${EXPORTER_URL}"
else
    fail "Exporter is not reachable at ${EXPORTER_URL}"
fi

# Test 2: Health endpoint
echo ""
echo "Test 2: Checking health endpoint..."
HEALTH=$(curl -s "${EXPORTER_URL}/health")
if echo "$HEALTH" | grep -q "healthy"; then
    pass "Health endpoint is responding"
else
    fail "Health endpoint is not responding correctly"
fi

# Test 3: Metrics endpoint
echo ""
echo "Test 3: Checking metrics endpoint..."
METRICS=$(curl -s "${EXPORTER_URL}/metrics")
if [ -n "$METRICS" ]; then
    pass "Metrics endpoint is responding"
else
    fail "Metrics endpoint returned no data"
fi

# Test 4: Core metrics present
echo ""
echo "Test 4: Checking for core metrics..."

check_metric() {
    local metric=$1
    local desc=$2
    if echo "$METRICS" | grep -q "^${metric}"; then
        pass "$desc (${metric})"
        return 0
    else
        warn "$desc (${metric}) - not found"
        return 1
    fi
}

check_metric "solana_epoch_number" "Epoch number"
check_metric "solana_slot_height" "Slot height"
check_metric "solana_network_tps" "Network TPS"

# Test 5: Validator-specific metrics
echo ""
echo "Test 5: Checking validator-specific metrics..."

check_metric "solana_validator_identity_balance_sol" "Identity balance"
check_metric "solana_validator_activated_stake_sol" "Activated stake"
check_metric "solana_validator_delinquent" "Delinquency status"

# Test 6: SOL price and USD metrics
echo ""
echo "Test 6: Checking SOL price and USD metrics..."

check_metric "solana_sol_price_usd" "SOL price USD"
check_metric "solana_validator_identity_balance_usd" "Identity balance USD"
check_metric "solana_validator_vote_balance_usd" "Vote balance USD"
check_metric "solana_validator_activated_stake_usd" "Activated stake USD"

# Test 7: Client type metric
echo ""
echo "Test 7: Checking client type metric..."

check_metric "solana_node_client_info" "Validator client type"

# Test 8: Skip rate metric (critical)
echo ""
echo "Test 8: Checking skip rate metric..."

if check_metric "solana_validator_skip_rate_percent" "Skip rate"; then
    SKIP_RATE=$(echo "$METRICS" | grep "^solana_validator_skip_rate_percent" | awk '{print $2}')
    echo "   Current skip rate: ${SKIP_RATE}%"

    # Check if skip rate is concerning
    if (( $(echo "$SKIP_RATE > 5.0" | bc -l) )); then
        warn "Skip rate is high (>${SKIP_RATE}%) - investigate performance"
    fi
fi

# Test 9: Delinquency check
echo ""
echo "Test 9: Checking delinquency status..."

if echo "$METRICS" | grep -q "^solana_validator_delinquent"; then
    DELINQUENT=$(echo "$METRICS" | grep "^solana_validator_delinquent" | awk '{print $2}')
    if [ "$DELINQUENT" = "0" ]; then
        pass "Validator is not delinquent"
    else
        fail "Validator is DELINQUENT - immediate action required!"
    fi
else
    warn "Delinquency metric not found"
fi

# Test 10: Balance checks
echo ""
echo "Test 10: Checking account balances..."

if echo "$METRICS" | grep -q "^solana_validator_identity_balance_sol"; then
    IDENTITY_BAL=$(echo "$METRICS" | grep "^solana_validator_identity_balance_sol" | awk '{print $2}')
    echo "   Identity balance: ${IDENTITY_BAL} SOL"

    if (( $(echo "$IDENTITY_BAL < 10.0" | bc -l) )); then
        warn "Identity balance is low (<10 SOL) - consider topping up"
    fi
fi

if echo "$METRICS" | grep -q "^solana_validator_vote_balance_sol"; then
    VOTE_BAL=$(echo "$METRICS" | grep "^solana_validator_vote_balance_sol" | awk '{print $2}')
    echo "   Vote balance: ${VOTE_BAL} SOL"

    if (( $(echo "$VOTE_BAL < 1.0" | bc -l) )); then
        warn "Vote balance is low (<1 SOL) - validator may stop voting"
    fi
fi

# Test 11: Scrape performance
echo ""
echo "Test 11: Checking scrape performance..."

if echo "$METRICS" | grep -q "^solana_exporter_scrape_duration_seconds"; then
    SCRAPE_TIME=$(echo "$METRICS" | grep "^solana_exporter_scrape_duration_seconds" | awk '{print $2}')
    echo "   Last scrape took: ${SCRAPE_TIME}s"

    if (( $(echo "$SCRAPE_TIME > 10.0" | bc -l) )); then
        warn "Scrape time is slow (>${SCRAPE_TIME}s) - consider using faster RPC"
    else
        pass "Scrape performance is good"
    fi
fi

# Summary
echo ""
echo "=========================================="
echo "Test Summary"
echo "=========================================="
echo ""
echo "All critical tests passed!"
echo ""
echo "Metrics endpoint: ${EXPORTER_URL}/metrics"
echo "Health endpoint: ${EXPORTER_URL}/health"
echo ""
echo "Next steps:"
echo "  1. Configure Prometheus to scrape this exporter"
echo "  2. Import Grafana dashboards from grafana/ directory"
echo "  3. Set up alerts in Prometheus"
echo ""
