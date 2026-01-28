#!/usr/bin/env python3
"""
Production-grade Solana Validator Prometheus Exporter

Monitors Solana validator health, performance, and economics via RPC API.
Exposes metrics in Prometheus format on /metrics endpoint.

Usage:
    # Set environment variables
    export SOLANA_RPC_URL="https://api.mainnet-beta.solana.com"
    export SOLANA_IDENTITY_KEY="your_validator_identity_pubkey"
    export SOLANA_VOTE_KEY="your_validator_vote_pubkey"

    # Optional: local validator RPC (for health checks)
    export SOLANA_LOCAL_RPC_URL="http://localhost:8899"

    # Run exporter
    uvicorn exporter:app --host 0.0.0.0 --port 8080
"""

import os
import time
import asyncio
import logging
from typing import Optional, Dict, Any, List
from datetime import datetime

import httpx
from fastapi import FastAPI, Response

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# ------------------------
# CONFIGURATION
# ------------------------
class Config:
    """Exporter configuration from environment variables"""

    # RPC endpoints
    RPC_URL: str = os.getenv("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
    LOCAL_RPC_URL: Optional[str] = os.getenv("SOLANA_LOCAL_RPC_URL")  # Optional

    # Validator keys
    IDENTITY_KEY: str = os.getenv("SOLANA_IDENTITY_KEY", "")
    VOTE_KEY: str = os.getenv("SOLANA_VOTE_KEY", "")

    # HTTP client settings
    TIMEOUT: float = float(os.getenv("SOLANA_RPC_TIMEOUT", "10.0"))
    MAX_CONNECTIONS: int = int(os.getenv("SOLANA_MAX_CONNECTIONS", "20"))

    @classmethod
    def validate(cls):
        """Validate required configuration"""
        if not cls.IDENTITY_KEY:
            logger.warning("SOLANA_IDENTITY_KEY not set - some metrics will be unavailable")
        if not cls.VOTE_KEY:
            logger.warning("SOLANA_VOTE_KEY not set - some metrics will be unavailable")
        if not cls.LOCAL_RPC_URL:
            logger.info("SOLANA_LOCAL_RPC_URL not set - local health checks disabled")

        logger.info(f"RPC URL: {cls.RPC_URL}")
        logger.info(f"Local RPC URL: {cls.LOCAL_RPC_URL or 'disabled'}")
        logger.info(f"Identity Key: {cls.IDENTITY_KEY[:20]}..." if cls.IDENTITY_KEY else "Identity Key: not set")
        logger.info(f"Vote Key: {cls.VOTE_KEY[:20]}..." if cls.VOTE_KEY else "Vote Key: not set")

Config.validate()

# ------------------------
# APP SETUP
# ------------------------
app = FastAPI(title="Solana Validator Exporter", version="1.0.0")

# Global HTTP client with connection pooling
http_client: Optional[httpx.AsyncClient] = None

@app.on_event("startup")
async def startup_event():
    """Initialize HTTP client on startup"""
    global http_client
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(Config.TIMEOUT, connect=5.0),
        limits=httpx.Limits(max_keepalive_connections=10, max_connections=Config.MAX_CONNECTIONS)
    )
    logger.info("Exporter started successfully")

@app.on_event("shutdown")
async def shutdown_event():
    """Clean up HTTP client on shutdown"""
    if http_client:
        await http_client.aclose()
    logger.info("Exporter shutdown complete")

# ------------------------
# RPC CLIENT
# ------------------------
async def rpc_call(url: str, method: str, params: Optional[List] = None) -> Dict[str, Any]:
    """
    Make an async RPC call with error handling

    Args:
        url: RPC endpoint URL
        method: RPC method name
        params: Optional method parameters

    Returns:
        RPC response dict, or empty dict on error
    """
    try:
        response = await http_client.post(
            url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params or []}
        )
        response.raise_for_status()
        data = response.json()

        # Check for RPC errors
        if "error" in data:
            logger.error(f"RPC error for {method}: {data['error']}")
            return {}

        return data
    except httpx.TimeoutException:
        logger.warning(f"Timeout calling {method} on {url}")
        return {}
    except httpx.HTTPError as e:
        logger.error(f"HTTP error calling {method}: {e}")
        return {}
    except Exception as e:
        logger.error(f"Unexpected error calling {method}: {e}")
        return {}

def extract_result(response: Dict[str, Any]) -> Any:
    """Extract result from RPC response"""
    if isinstance(response, dict) and "result" in response:
        return response["result"]
    return None

# ------------------------
# METRICS COLLECTION
# ------------------------
async def fetch_all_metrics() -> Dict[str, Any]:
    """
    Fetch all metrics concurrently using async/await

    Returns:
        Dictionary containing all metric data
    """
    rpc_url = Config.RPC_URL
    local_rpc_url = Config.LOCAL_RPC_URL

    # Prepare RPC calls
    rpc_calls = []
    call_names = []

    # Local health check (if local RPC available)
    if local_rpc_url:
        rpc_calls.append(rpc_call(local_rpc_url, "getHealth"))
        call_names.append("health")

    # Cluster-wide calls (always via main RPC)
    rpc_calls.extend([
        rpc_call(rpc_url, "getVersion"),
        rpc_call(rpc_url, "getEpochInfo", [{"commitment": "finalized"}]),
        rpc_call(rpc_url, "getSlot", [{"commitment": "finalized"}]),
        rpc_call(rpc_url, "getRecentPerformanceSamples", [5]),
    ])
    call_names.extend(["version", "epoch_info", "slot", "performance"])

    # Validator-specific calls (only if keys are configured)
    if Config.IDENTITY_KEY:
        rpc_calls.extend([
            rpc_call(rpc_url, "getBalance", [Config.IDENTITY_KEY, {"commitment": "finalized"}]),
            rpc_call(rpc_url, "getLeaderSchedule", [None, {"commitment": "finalized", "identity": Config.IDENTITY_KEY}]),
        ])
        call_names.extend(["identity_balance", "leader_schedule"])

    if Config.VOTE_KEY:
        rpc_calls.extend([
            rpc_call(rpc_url, "getBalance", [Config.VOTE_KEY, {"commitment": "finalized"}]),
            rpc_call(rpc_url, "getVoteAccounts", [{"commitment": "finalized", "votePubkey": Config.VOTE_KEY}]),
        ])
        call_names.extend(["vote_balance", "vote_accounts"])

    # Block production for skip rate (only if identity key set)
    if Config.IDENTITY_KEY:
        rpc_calls.append(
            rpc_call(rpc_url, "getBlockProduction", [{"commitment": "finalized", "identity": Config.IDENTITY_KEY}])
        )
        call_names.append("block_production")

    # Execute all calls concurrently
    results = await asyncio.gather(*rpc_calls, return_exceptions=True)

    # Map results to names
    metrics = {}
    for name, result in zip(call_names, results):
        if isinstance(result, Exception):
            logger.error(f"Exception fetching {name}: {result}")
            metrics[name] = None
        else:
            metrics[name] = extract_result(result)

    return metrics

def format_prometheus_metrics(data: Dict[str, Any]) -> str:
    """
    Format metrics data into Prometheus text format

    Args:
        data: Dictionary of metric data from fetch_all_metrics()

    Returns:
        Prometheus-formatted metrics string
    """
    lines = []

    # Helper to add metric
    def add_metric(name: str, value: float, help_text: str = "", labels: Dict[str, str] = None):
        if help_text:
            lines.append(f"# HELP {name} {help_text}")
            lines.append(f"# TYPE {name} gauge")

        if labels:
            label_str = ",".join(f'{k}="{v}"' for k, v in labels.items())
            lines.append(f"{name}{{{label_str}}} {value}")
        else:
            lines.append(f"{name} {value}")

    # ============================================
    # NODE HEALTH & VERSION
    # ============================================
    if data.get("health") is not None:
        health_value = 1 if data["health"] == "ok" else 0
        add_metric("solana_node_health", health_value, "Node health status (1=healthy, 0=down)")

    if data.get("version"):
        version_info = data["version"]
        version_str = version_info.get("solana-core", "unknown")
        add_metric("solana_node_version_info", 1, "Solana version info", {"version": version_str})

    # ============================================
    # EPOCH & SLOT INFO
    # ============================================
    if data.get("epoch_info"):
        epoch = data["epoch_info"]
        add_metric("solana_epoch_number", epoch.get("epoch", 0), "Current epoch number")
        add_metric("solana_epoch_slot_index", epoch.get("slotIndex", 0), "Current slot within epoch")
        add_metric("solana_epoch_slots_total", epoch.get("slotsInEpoch", 0), "Total slots in current epoch")
        add_metric("solana_slot_height", epoch.get("absoluteSlot", 0), "Current absolute slot")
        add_metric("solana_block_height", epoch.get("blockHeight", 0), "Current block height")
        add_metric("solana_transactions_total", epoch.get("transactionCount", 0), "Total transactions since genesis")

        # Calculate epoch progress
        slot_index = epoch.get("slotIndex", 0)
        slots_in_epoch = epoch.get("slotsInEpoch", 1)
        progress = (slot_index / slots_in_epoch * 100) if slots_in_epoch > 0 else 0
        add_metric("solana_epoch_progress_percent", progress, "Epoch completion percentage")

    # Cluster slot (for comparison with local validator)
    if data.get("slot") is not None:
        add_metric("solana_cluster_slot", data["slot"], "Latest cluster slot")

    # ============================================
    # NETWORK PERFORMANCE
    # ============================================
    if data.get("performance"):
        perf_samples = data["performance"]
        if perf_samples and len(perf_samples) > 0:
            # Use most recent sample
            sample = perf_samples[0]

            # TPS calculation
            num_tx = sample.get("numTransactions", 0)
            sample_period = sample.get("samplePeriodSecs", 1)
            tps = num_tx / sample_period if sample_period > 0 else 0
            add_metric("solana_network_tps", tps, "Network transactions per second")

            # Average slot time
            num_slots = sample.get("numSlots", 1)
            avg_slot_ms = (1000 * sample_period / num_slots) if num_slots > 0 else 0
            add_metric("solana_network_slot_time_ms", avg_slot_ms, "Average time per slot in milliseconds")

    # ============================================
    # VALIDATOR BALANCES
    # ============================================
    if data.get("identity_balance") and data["identity_balance"].get("value") is not None:
        lamports = data["identity_balance"]["value"]
        sol = lamports / 1_000_000_000
        add_metric("solana_validator_identity_balance_sol", sol, "Validator identity account balance (SOL)")

    if data.get("vote_balance") and data["vote_balance"].get("value") is not None:
        lamports = data["vote_balance"]["value"]
        sol = lamports / 1_000_000_000
        add_metric("solana_validator_vote_balance_sol", sol, "Validator vote account balance (SOL)")

    # ============================================
    # VALIDATOR STAKE & STATUS
    # ============================================
    if data.get("vote_accounts"):
        vote_accts = data["vote_accounts"]

        # Check current (active) validators
        current = vote_accts.get("current", [])
        if current and len(current) > 0:
            validator = current[0]

            # Active stake
            activated_stake = validator.get("activatedStake", 0)
            stake_sol = activated_stake / 1_000_000_000
            add_metric("solana_validator_activated_stake_sol", stake_sol, "Active stake delegated to validator (SOL)")

            # Last vote
            last_vote = validator.get("lastVote", 0)
            add_metric("solana_validator_last_vote_slot", last_vote, "Last voted slot")

            # Root slot
            root_slot = validator.get("rootSlot", 0)
            add_metric("solana_validator_root_slot", root_slot, "Root slot")

            # Commission
            commission = validator.get("commission", 0)
            add_metric("solana_validator_commission_percent", commission, "Validator commission percentage")

            # Delinquent status (in current = not delinquent)
            add_metric("solana_validator_delinquent", 0, "Validator delinquency status (0=active, 1=delinquent)")

        # Check delinquent validators
        delinquent = vote_accts.get("delinquent", [])
        if delinquent and len(delinquent) > 0:
            # Our validator is delinquent!
            add_metric("solana_validator_delinquent", 1, "Validator delinquency status (0=active, 1=delinquent)")

    # ============================================
    # LEADER SCHEDULE & SLOTS
    # ============================================
    if data.get("leader_schedule"):
        leader_schedule = data["leader_schedule"]
        if Config.IDENTITY_KEY in leader_schedule:
            assigned_slots = len(leader_schedule[Config.IDENTITY_KEY])
            add_metric("solana_validator_leader_slots_assigned", assigned_slots, "Number of leader slots assigned this epoch")
        else:
            add_metric("solana_validator_leader_slots_assigned", 0, "Number of leader slots assigned this epoch")

    # ============================================
    # BLOCK PRODUCTION & SKIP RATE
    # ============================================
    if data.get("block_production"):
        block_prod = data["block_production"]
        if block_prod and "value" in block_prod:
            value = block_prod["value"]
            by_identity = value.get("byIdentity", {})

            if Config.IDENTITY_KEY in by_identity:
                stats = by_identity[Config.IDENTITY_KEY]
                leader_slots = stats[0] if len(stats) > 0 else 0
                blocks_produced = stats[1] if len(stats) > 1 else 0
                blocks_skipped = leader_slots - blocks_produced

                add_metric("solana_validator_leader_slots_total", leader_slots, "Total leader slots")
                add_metric("solana_validator_blocks_produced", blocks_produced, "Blocks successfully produced")
                add_metric("solana_validator_blocks_skipped", blocks_skipped, "Blocks skipped (missed)")

                # Calculate skip rate
                skip_rate = (blocks_skipped / leader_slots * 100) if leader_slots > 0 else 0
                add_metric("solana_validator_skip_rate_percent", skip_rate, "Skip rate percentage")

    # ============================================
    # EXPORTER METADATA
    # ============================================
    add_metric("solana_exporter_build_info", 1, "Exporter version info", {
        "version": "1.0.0",
        "python": "3.8+"
    })

    return "\n".join(lines)

# ------------------------
# HTTP ENDPOINTS
# ------------------------
@app.get("/")
async def root():
    """Root endpoint with basic info"""
    return {
        "name": "Solana Validator Exporter",
        "version": "1.0.0",
        "metrics_path": "/metrics",
        "health_path": "/health"
    }

@app.get("/health")
async def health():
    """Health check endpoint"""
    return {"status": "healthy", "timestamp": datetime.utcnow().isoformat()}

@app.get("/metrics")
async def metrics():
    """
    Prometheus metrics endpoint

    Returns metrics in Prometheus text format
    """
    start_time = time.time()

    try:
        # Fetch all metrics
        data = await fetch_all_metrics()

        # Format as Prometheus metrics
        metrics_output = format_prometheus_metrics(data)

        # Add scrape duration
        duration = time.time() - start_time
        metrics_output += f"\n# HELP solana_exporter_scrape_duration_seconds Time spent scraping metrics\n"
        metrics_output += f"# TYPE solana_exporter_scrape_duration_seconds gauge\n"
        metrics_output += f"solana_exporter_scrape_duration_seconds {duration:.3f}\n"

        # Add scrape timestamp
        metrics_output += f"# HELP solana_exporter_scrape_timestamp_seconds Unix timestamp of last scrape\n"
        metrics_output += f"# TYPE solana_exporter_scrape_timestamp_seconds gauge\n"
        metrics_output += f"solana_exporter_scrape_timestamp_seconds {time.time():.0f}\n"

        logger.info(f"Metrics scraped successfully in {duration:.2f}s")

        return Response(content=metrics_output, media_type="text/plain; charset=utf-8")

    except Exception as e:
        logger.error(f"Error generating metrics: {e}", exc_info=True)
        return Response(
            content=f"# Error generating metrics: {str(e)}\n",
            media_type="text/plain; charset=utf-8",
            status_code=500
        )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
