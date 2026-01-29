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
from fastapi.responses import JSONResponse

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

    # SOL price (CoinGecko free API)
    COINGECKO_URL: str = "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"

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

async def fetch_sol_price() -> Optional[float]:
    """Fetch current SOL/USD price from CoinGecko"""
    try:
        response = await http_client.get(Config.COINGECKO_URL)
        response.raise_for_status()
        data = response.json()
        return data.get("solana", {}).get("usd")
    except Exception as e:
        logger.warning(f"Failed to fetch SOL price: {e}")
        return None

def detect_client_type(version_str: str) -> str:
    """Detect validator client type from version string"""
    version_lower = version_str.lower()
    if "jito" in version_lower:
        return "Jito"
    elif "firedancer" in version_lower or "fd_" in version_lower:
        return "Firedancer"
    else:
        return "Agave"

# ------------------------
# BLOCK PRODUCTION DATA
# ------------------------
async def fetch_block_details(slot: int) -> Optional[Dict[str, Any]]:
    """
    Fetch detailed block data for a specific slot.
    Returns dict with status: "produced", "skipped", or "unavailable"
    """
    try:
        response = await rpc_call(
            Config.RPC_URL,
            "getBlock",
            [slot, {"encoding": "json", "transactionDetails": "full", "rewards": True, "maxSupportedTransactionVersion": 0}]
        )

        # Check for RPC errors
        if "error" in response:
            error_code = response["error"].get("code", 0)
            error_msg = response["error"].get("message", "")

            # -32009: Slot was skipped (validator actually missed it)
            if error_code == -32009 or "skipped" in error_msg.lower():
                return {
                    "slot": slot,
                    "status": "skipped",
                    "votes": None, "non_votes": None,
                    "fees_sol": None, "compute_units": None, "cu_percent": None,
                    "explorer_url": f"https://solscan.io/block/{slot}"
                }
            # -32004: Block not available (RPC pruned the data)
            elif error_code == -32004 or "not available" in error_msg.lower():
                return {
                    "slot": slot,
                    "status": "no data",
                    "votes": "-", "non_votes": "-",
                    "fees_sol": "-", "compute_units": "-", "cu_percent": "-",
                    "explorer_url": f"https://solscan.io/block/{slot}"
                }
            else:
                logger.warning(f"RPC error for slot {slot}: {error_msg}")
                return None

        block = extract_result(response)
        if not block:
            return None

        transactions = block.get("transactions", [])
        votes = 0
        non_votes = 0
        total_fees = 0
        total_cu = 0

        VOTE_PROGRAM = "Vote111111111111111111111111111111111111111"

        for tx in transactions:
            meta = tx.get("meta", {})
            total_fees += meta.get("fee", 0)
            total_cu += meta.get("computeUnitsConsumed", 0)

            message = tx.get("transaction", {}).get("message", {})
            account_keys = message.get("accountKeys", []) or message.get("staticAccountKeys", [])

            if VOTE_PROGRAM in account_keys:
                votes += 1
            else:
                non_votes += 1

        return {
            "slot": slot,
            "status": "produced",
            "votes": votes,
            "non_votes": non_votes,
            "fees_sol": round(total_fees / 1_000_000_000, 6),
            "compute_units": total_cu,
            "cu_percent": round((total_cu / 48_000_000 * 100), 1) if total_cu > 0 else 0,
            "explorer_url": f"https://solscan.io/block/{slot}"
        }
    except Exception as e:
        logger.warning(f"Failed to fetch block {slot}: {e}")
        return None

async def fetch_leader_slots_data() -> Dict[str, Any]:
    """
    Fetch last 4 completed slots and next 4 upcoming slots with countdown.
    Returns data for Grafana Infinity plugin table.
    """
    if not Config.IDENTITY_KEY:
        return {"error": "IDENTITY_KEY not configured", "slots": []}

    # Fetch current slot, epoch info, and leader schedule
    current_slot_resp, epoch_resp, leader_schedule_resp = await asyncio.gather(
        rpc_call(Config.RPC_URL, "getSlot", [{"commitment": "finalized"}]),
        rpc_call(Config.RPC_URL, "getEpochInfo", [{"commitment": "finalized"}]),
        rpc_call(Config.RPC_URL, "getLeaderSchedule", [None, {"commitment": "finalized", "identity": Config.IDENTITY_KEY}])
    )

    current_slot = extract_result(current_slot_resp) or 0
    epoch_info = extract_result(epoch_resp) or {}
    leader_schedule = extract_result(leader_schedule_resp) or {}

    our_slots = leader_schedule.get(Config.IDENTITY_KEY, [])
    if not our_slots:
        return {"current_slot": current_slot, "slots": [], "next_leader_in": None}

    # Calculate absolute slot numbers
    epoch_start_slot = epoch_info.get("absoluteSlot", 0) - epoch_info.get("slotIndex", 0)
    absolute_slots = sorted([epoch_start_slot + s for s in our_slots])

    # Split into completed and upcoming
    completed = [s for s in absolute_slots if s <= current_slot][-4:]  # Last 4
    upcoming = [s for s in absolute_slots if s > current_slot][:4]     # Next 4

    # Calculate countdown to next leader slot
    next_leader_in = (upcoming[0] - current_slot) if upcoming else None

    # Fetch details for completed slots concurrently
    slots_data = []

    if completed:
        block_results = await asyncio.gather(*[fetch_block_details(s) for s in completed])
        for slot, result in zip(completed, block_results):
            if result:
                # fetch_block_details now returns status directly (produced/skipped/no data)
                slots_data.append(result)
            else:
                # Only None if unexpected error occurred
                slots_data.append({
                    "slot": slot, "status": "error", "votes": "-", "non_votes": "-",
                    "fees_sol": "-", "compute_units": "-", "cu_percent": "-",
                    "explorer_url": f"https://solscan.io/block/{slot}"
                })

    # Add upcoming slots
    for s in upcoming:
        slots_data.append({
            "slot": s, "status": "upcoming", "votes": "-", "non_votes": "-",
            "fees_sol": "-", "compute_units": "-", "cu_percent": "-",
            "explorer_url": None
        })

    # Sort: upcoming first (by slot desc), then completed (by slot desc)
    upcoming_data = [s for s in slots_data if s["status"] == "upcoming"]
    completed_data = [s for s in slots_data if s["status"] != "upcoming"]
    upcoming_data.sort(key=lambda x: x["slot"], reverse=True)
    completed_data.sort(key=lambda x: x["slot"], reverse=True)

    return {
        "current_slot": current_slot,
        "next_leader_slot": upcoming[0] if upcoming else None,
        "next_leader_in": next_leader_in,
        "slots": upcoming_data + completed_data
    }

async def fetch_inflation_rewards() -> Dict[str, Any]:
    """
    Fetch inflation rewards for the vote account.
    Returns rewards for current epoch and previous epoch.
    """
    if not Config.VOTE_KEY:
        return {"current_epoch": None, "previous_epoch": None}

    try:
        # Get current epoch info first
        epoch_resp = await rpc_call(Config.RPC_URL, "getEpochInfo", [{"commitment": "finalized"}])
        epoch_info = extract_result(epoch_resp) or {}
        current_epoch = epoch_info.get("epoch", 0)

        # Fetch rewards for previous epoch (current epoch rewards aren't finalized yet)
        rewards_resp = await rpc_call(
            Config.RPC_URL,
            "getInflationReward",
            [[Config.VOTE_KEY], {"epoch": current_epoch - 1}]
        )
        rewards = extract_result(rewards_resp)

        prev_reward = None
        if rewards and len(rewards) > 0 and rewards[0]:
            reward_data = rewards[0]
            prev_reward = {
                "epoch": reward_data.get("epoch", current_epoch - 1),
                "amount_lamports": reward_data.get("amount", 0),
                "amount_sol": reward_data.get("amount", 0) / 1_000_000_000,
                "post_balance_lamports": reward_data.get("postBalance", 0),
                "commission": reward_data.get("commission"),
                "effective_slot": reward_data.get("effectiveSlot", 0)
            }

        # Also try to get epoch before that for comparison
        rewards_resp_2 = await rpc_call(
            Config.RPC_URL,
            "getInflationReward",
            [[Config.VOTE_KEY], {"epoch": current_epoch - 2}]
        )
        rewards_2 = extract_result(rewards_resp_2)

        prev_prev_reward = None
        if rewards_2 and len(rewards_2) > 0 and rewards_2[0]:
            reward_data = rewards_2[0]
            prev_prev_reward = {
                "epoch": reward_data.get("epoch", current_epoch - 2),
                "amount_lamports": reward_data.get("amount", 0),
                "amount_sol": reward_data.get("amount", 0) / 1_000_000_000,
            }

        return {
            "current_epoch": current_epoch,
            "last_epoch_reward": prev_reward,
            "prev_epoch_reward": prev_prev_reward
        }
    except Exception as e:
        logger.warning(f"Failed to fetch inflation rewards: {e}")
        return {"current_epoch": None, "last_epoch_reward": None, "prev_epoch_reward": None}


async def fetch_epoch_fees() -> Dict[str, Any]:
    """
    Calculate total fees earned from all blocks produced this epoch.
    Uses getBlockProduction to get slot range, then fetches block data.
    """
    if not Config.IDENTITY_KEY:
        return {"total_fees_sol": 0, "blocks_with_fees": 0}

    try:
        # Get block production data with slot range
        block_prod_resp = await rpc_call(
            Config.RPC_URL,
            "getBlockProduction",
            [{"commitment": "finalized", "identity": Config.IDENTITY_KEY}]
        )
        block_prod = extract_result(block_prod_resp)

        if not block_prod or "value" not in block_prod:
            return {"total_fees_sol": 0, "blocks_with_fees": 0}

        value = block_prod["value"]
        by_identity = value.get("byIdentity", {})

        if Config.IDENTITY_KEY not in by_identity:
            return {"total_fees_sol": 0, "blocks_with_fees": 0}

        # Get the slot range for this epoch's block production
        slot_range = value.get("range", {})
        first_slot = slot_range.get("firstSlot", 0)
        last_slot = slot_range.get("lastSlot", 0)

        # Get our produced slots from leader schedule
        leader_schedule_resp = await rpc_call(
            Config.RPC_URL,
            "getLeaderSchedule",
            [None, {"commitment": "finalized", "identity": Config.IDENTITY_KEY}]
        )
        leader_schedule = extract_result(leader_schedule_resp) or {}
        our_slot_offsets = leader_schedule.get(Config.IDENTITY_KEY, [])

        if not our_slot_offsets:
            return {"total_fees_sol": 0, "blocks_with_fees": 0}

        # Get epoch info to calculate absolute slots
        epoch_resp = await rpc_call(Config.RPC_URL, "getEpochInfo", [{"commitment": "finalized"}])
        epoch_info = extract_result(epoch_resp) or {}
        epoch_start_slot = epoch_info.get("absoluteSlot", 0) - epoch_info.get("slotIndex", 0)
        current_slot = epoch_info.get("absoluteSlot", 0)

        # Calculate absolute slots we've already passed
        our_completed_slots = [
            epoch_start_slot + offset
            for offset in our_slot_offsets
            if epoch_start_slot + offset <= current_slot
        ]

        # Limit to last 100 slots to avoid too many RPC calls
        slots_to_check = our_completed_slots[-100:] if len(our_completed_slots) > 100 else our_completed_slots

        # Fetch fees from produced blocks (sample last 20 for efficiency)
        sample_slots = slots_to_check[-20:] if len(slots_to_check) > 20 else slots_to_check

        total_fees = 0
        blocks_with_fees = 0

        async def get_block_fees(slot):
            try:
                resp = await rpc_call(
                    Config.RPC_URL,
                    "getBlock",
                    [slot, {"encoding": "json", "transactionDetails": "full", "rewards": False, "maxSupportedTransactionVersion": 0}]
                )
                block = extract_result(resp)
                if block:
                    fees = sum(tx.get("meta", {}).get("fee", 0) for tx in block.get("transactions", []))
                    return fees
                return 0
            except:
                return 0

        # Fetch fees concurrently
        fee_results = await asyncio.gather(*[get_block_fees(s) for s in sample_slots])

        for fees in fee_results:
            if fees > 0:
                total_fees += fees
                blocks_with_fees += 1

        # Extrapolate to full epoch if we sampled
        total_completed = len(our_completed_slots)
        sampled = len(sample_slots)
        if sampled > 0 and total_completed > sampled:
            avg_fees_per_block = total_fees / sampled
            estimated_total = avg_fees_per_block * total_completed
        else:
            estimated_total = total_fees

        return {
            "total_fees_sol": round(estimated_total / 1_000_000_000, 6),
            "sampled_fees_sol": round(total_fees / 1_000_000_000, 6),
            "blocks_sampled": sampled,
            "blocks_completed": total_completed,
            "avg_fee_per_block_sol": round((total_fees / sampled / 1_000_000_000), 6) if sampled > 0 else 0
        }
    except Exception as e:
        logger.warning(f"Failed to fetch epoch fees: {e}")
        return {"total_fees_sol": 0, "blocks_with_fees": 0}


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

    # Execute all RPC calls + SOL price fetch + inflation rewards + epoch fees concurrently
    rpc_calls.append(fetch_sol_price())
    call_names.append("sol_price")

    rpc_calls.append(fetch_inflation_rewards())
    call_names.append("inflation_rewards")

    rpc_calls.append(fetch_epoch_fees())
    call_names.append("epoch_fees")

    results = await asyncio.gather(*rpc_calls, return_exceptions=True)

    # Map results to names
    metrics = {}
    for name, result in zip(call_names, results):
        if isinstance(result, Exception):
            logger.error(f"Exception fetching {name}: {result}")
            metrics[name] = None
        elif name in ("sol_price", "inflation_rewards", "epoch_fees"):
            # These return values directly, not RPC responses
            metrics[name] = result
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
        client_type = detect_client_type(version_str)
        add_metric("solana_node_version_info", 1, "Solana version info", {"version": version_str})
        add_metric("solana_node_client_info", 1, "Validator client type", {"client": client_type})

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
    # SOL PRICE & USD CONVERSIONS
    # ============================================
    sol_price = data.get("sol_price")
    if sol_price is not None:
        add_metric("solana_sol_price_usd", sol_price, "Current SOL price in USD")

        # USD-converted balances
        if data.get("identity_balance") and data["identity_balance"].get("value") is not None:
            identity_sol = data["identity_balance"]["value"] / 1_000_000_000
            add_metric("solana_validator_identity_balance_usd", identity_sol * sol_price, "Identity account balance in USD")

        if data.get("vote_balance") and data["vote_balance"].get("value") is not None:
            vote_sol = data["vote_balance"]["value"] / 1_000_000_000
            add_metric("solana_validator_vote_balance_usd", vote_sol * sol_price, "Vote account balance in USD")

        if data.get("vote_accounts"):
            current = data["vote_accounts"].get("current", [])
            if current and len(current) > 0:
                stake_sol = current[0].get("activatedStake", 0) / 1_000_000_000
                add_metric("solana_validator_activated_stake_usd", stake_sol * sol_price, "Active stake in USD")

    # ============================================
    # INFLATION REWARDS
    # ============================================
    if data.get("inflation_rewards"):
        rewards = data["inflation_rewards"]
        current_epoch = rewards.get("current_epoch")

        if current_epoch:
            add_metric("solana_validator_current_epoch", current_epoch, "Current epoch number")

        last_reward = rewards.get("last_epoch_reward")
        if last_reward:
            add_metric("solana_validator_last_epoch_reward_sol", last_reward["amount_sol"],
                      "Inflation reward earned last epoch (SOL)")
            add_metric("solana_validator_last_epoch_reward_epoch", last_reward["epoch"],
                      "Epoch number for last reward")

            # Add USD value if SOL price available
            sol_price = data.get("sol_price")
            if sol_price:
                add_metric("solana_validator_last_epoch_reward_usd", last_reward["amount_sol"] * sol_price,
                          "Inflation reward earned last epoch (USD)")

        prev_reward = rewards.get("prev_epoch_reward")
        if prev_reward:
            add_metric("solana_validator_prev_epoch_reward_sol", prev_reward["amount_sol"],
                      "Inflation reward earned 2 epochs ago (SOL)")

    # ============================================
    # EPOCH FEES (Transaction Fees Earned)
    # ============================================
    if data.get("epoch_fees"):
        fees = data["epoch_fees"]
        total_fees = fees.get("total_fees_sol", 0)
        add_metric("solana_validator_epoch_fees_total_sol", total_fees,
                  "Estimated total transaction fees earned this epoch (SOL)")

        avg_fee = fees.get("avg_fee_per_block_sol", 0)
        add_metric("solana_validator_avg_fee_per_block_sol", avg_fee,
                  "Average transaction fee per block (SOL)")

        blocks_completed = fees.get("blocks_completed", 0)
        add_metric("solana_validator_blocks_completed_epoch", blocks_completed,
                  "Number of blocks completed this epoch")

        # Add USD value if SOL price available
        sol_price = data.get("sol_price")
        if sol_price and total_fees > 0:
            add_metric("solana_validator_epoch_fees_total_usd", total_fees * sol_price,
                      "Estimated total transaction fees earned this epoch (USD)")

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

@app.get("/blocks")
async def blocks():
    """
    Block production endpoint for Grafana Infinity plugin

    Returns JSON with upcoming and completed leader slots
    """
    try:
        data = await fetch_leader_slots_data()
        return JSONResponse(content=data)
    except Exception as e:
        logger.error(f"Error fetching block data: {e}", exc_info=True)
        return JSONResponse(
            content={"error": str(e)},
            status_code=500
        )

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8080)
