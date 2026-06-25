"""Batch Supermetrics queries across many accounts.

The user has 30+ Meta ad accounts connected in Supermetrics. A naive
per-account approach issues 60+ queries per cycle. Supermetrics supports
comma-separated `ds_accounts`, so we cluster N accounts into a single
query — verified empirically that 5 accounts in one request returns
87 rows correctly tagged with `Account ID` in ~9 seconds.

This module:
  1. Groups queries by `signature` — everything except `ds_accounts`
     (so all `ad_daily` queries with matching shape get batched together).
  2. Clusters accounts into batches of `CLUSTER_SIZE` and dispatches each.
  3. Skips accounts known to be **non-prioritised** in Supermetrics (so
     one bad apple doesn't tank the whole batch with a 500 error).
  4. Falls back to per-account queries if a batch fails — and marks the
     offending accounts as blocked for the rest of the run.

Why per-account fallback? Supermetrics returns the prioritised-account
error as a 500 for the WHOLE batch; we can't tell which account(s) in
the batch are blocked from the error alone. The fallback resolves it,
at the cost of one slow cycle the first time we see a new blocked
account.
"""

from __future__ import annotations

import logging
import os
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from typing import Dict, List, Optional, Set, Tuple

from clients import list_clients, get_client_config
from core import cache
from core.supermetrics import (
    QueryConfig,
    SupermetricsClient,
    SupermetricsError,
)


log = logging.getLogger(__name__)

CLUSTER_SIZE = int(os.environ.get("BATCH_CLUSTER_SIZE", "8"))
FALLBACK_PARALLELISM = int(os.environ.get("FALLBACK_PARALLELISM", "6"))

# Substring matches for Supermetrics error codes/messages that mean
# "this specific account is permanently unavailable for this user/plan".
# When we see one of these, we mark the account blocked so future cycles
# skip it. Transient errors (timeouts, 5xx without these codes) are
# retried.
_BLOCK_SIGNALS = (
    "prioritised",
    "prioritized",
    "QUERY_ERROR",                  # prioritised-account error code
    "QUERY_ACCOUNT_UNAVAILABLE",    # account explicitly unavailable
    "ACCOUNT_NOT_FOUND",
    "PERMISSION_DENIED",
    "no longer available",
)


def _is_block_signal(msg: str) -> bool:
    """True if the error msg means this account is permanently unavailable."""
    return any(s.lower() in msg.lower() for s in _BLOCK_SIGNALS)


def _extract_blocked_account(msg: str) -> Optional[str]:
    """If the batched error names a specific bad account, extract its ID.
    Example: 'QUERY_ACCOUNT_UNAVAILABLE: Facebook Ads account "act_581146904473020" is ...'
    Returns 'act_581146904473020' or None."""
    import re
    m = re.search(r'(act_\d+)', msg)
    return m.group(1) if m else None


# Signature key for grouping queries. We can batch queries together iff
# everything except ds_accounts matches.
QuerySig = Tuple[str, str, str, str, str, int]  # (ds_id, platform, type, date_range, fields, num_days)


def _signature(q: Dict) -> QuerySig:
    spec = q["query"]
    return (
        spec.get("ds_id", ""),
        q["platform"],
        q["type"],
        spec.get("date_range_type", ""),
        spec.get("fields", ""),
        int(spec.get("num_days", 0) or 0),
    )


def _ensure_account_id_field(fields: str) -> str:
    """Inject Account_id into the field list if missing — needed for
    multi-account responses so we can route rows back to clients."""
    parts = [p.strip() for p in fields.split(",")]
    lower = [p.lower() for p in parts]
    if not any(p in ("account_id", "accountid") for p in lower):
        parts = ["Account_id"] + parts
    return ",".join(parts)


def run_batched_cycle(client_id: Optional[str] = None,
                     blocked: Optional[Set[str]] = None,
                     cluster_size: int = CLUSTER_SIZE) -> Dict:
    """Run one batched pull cycle. Returns a summary dict.

    Args:
      client_id: if set, restrict to one client (useful for testing).
      blocked: pre-known blocked accounts to skip. Updated in-place
               as we discover more during this run.
    """
    cache.init_db()
    sm = SupermetricsClient()
    blocked = blocked if blocked is not None else set()

    clients = ([get_client_config(client_id)] if client_id
               else [get_client_config(c["id"]) for c in list_clients()])

    # Build (signature -> [(client_id, account, q_dict)]) groups
    groups: Dict[QuerySig, List[Tuple[str, str, Dict]]] = defaultdict(list)
    for cfg in clients:
        if cfg["id"].startswith("_"):
            continue
        for q in cfg.get("queries", []):
            spec = q.get("query") or {}
            accounts_raw = spec.get("ds_accounts", "")
            if not accounts_raw or accounts_raw.startswith("REPLACE"):
                continue
            for acc in accounts_raw.split(","):
                acc = acc.strip()
                if acc and not acc.startswith("list."):
                    groups[_signature(q)].append((cfg["id"], acc, q))

    summary: Dict = {
        "started_at":    datetime.utcnow().isoformat() + "Z",
        "batches":       [],
        "blocked_added": [],
    }

    for sig, items in groups.items():
        # Re-filter blocked between batches: when a fallback adds entries
        # to `blocked`, subsequent clusters in the same group skip them.
        remaining = [it for it in items if it[1] not in blocked]
        if not remaining:
            log.info("Signature %s: all %d accounts blocked, skipping",
                    sig[2], len(items))
            continue

        log.info("Signature %s: %d accounts to batch (%d total, %d blocked)",
                sig[2], len(remaining), len(items), len(items) - len(remaining))

        while remaining:
            cluster = remaining[:cluster_size]
            batch_result = _run_batch(sm, sig, cluster, blocked)
            summary["batches"].append(batch_result)
            summary["blocked_added"].extend(batch_result.get("newly_blocked", []))
            # Re-filter: drop newly-blocked accounts from the queue
            remaining = [it for it in remaining[cluster_size:]
                        if it[1] not in blocked]

    summary["finished_at"] = datetime.utcnow().isoformat() + "Z"
    summary["blocked_total"] = sorted(blocked)
    return summary


def _run_batch(sm: SupermetricsClient, sig: QuerySig,
              cluster: List[Tuple[str, str, Dict]],
              blocked: Set[str]) -> Dict:
    """Dispatch ONE batched query for a cluster of accounts. On failure
    that looks like a prioritised-account error, fall back to per-account
    queries to identify the bad apples, then update `blocked`."""
    (ds_id, platform, q_type, date_range_type, fields, num_days) = sig
    account_to_client = {acc: cid for (cid, acc, _) in cluster}
    accounts_str = ",".join(acc for (_, acc, _) in cluster)
    batched_fields = _ensure_account_id_field(fields)

    batched_query = {
        "ds_id":           ds_id,
        "ds_accounts":     accounts_str,
        "date_range_type": date_range_type,
        "fields":          batched_fields,
        "max_rows":        50000,
    }
    if num_days:
        batched_query["num_days"] = num_days

    qc = QueryConfig(name=f"batched_{q_type}_{len(cluster)}acc",
                     platform=platform, type=q_type, query=batched_query)
    started = datetime.utcnow().isoformat() + "Z"

    try:
        payload = sm.run_query(qc)
    except SupermetricsError as e:
        msg = str(e)
        # Fast path: if the error names a specific account, mark just it
        # blocked and let the next batch round retry the rest.
        named = _extract_blocked_account(msg)
        if named and _is_block_signal(msg):
            blocked.add(named)
            log.info("Batched %s/%d failed: account %s marked blocked",
                    q_type, len(cluster), named)
            # Re-batch immediately with the bad apple removed (recurse once)
            remaining = [it for it in cluster if it[1] != named]
            if remaining:
                return _run_batch(sm, sig, remaining, blocked)
            return {"type": q_type, "accounts": len(cluster),
                    "status": "partial", "newly_blocked": [named], "rows": 0}
        # Generic path: fall back per-account to identify the bad apples.
        log.warning("Batched %s/%d failed (%s) — fallback per-account",
                   q_type, len(cluster), msg[:120])
        return _run_per_account_fallback(sm, sig, cluster, blocked)

    # Success: normalize the batched response, route rows to clients,
    # upsert per client.
    if q_type == "ad_daily":
        rows = sm.normalize_ad_daily(payload, platform,
                                    account_to_client=account_to_client)
        rows_per_client = _split_by_client(rows)
        total = 0
        for cid, rs in rows_per_client.items():
            n = cache.upsert_ad_daily(rs)
            total += n
            cache.record_sync(cid, f"batched_{q_type}", started,
                              datetime.utcnow().isoformat() + "Z", n, "ok")
        return {"type": q_type, "accounts": len(cluster), "rows": total,
                "status": "ok"}
    elif q_type == "ad_status":
        rows = sm.normalize_ad_status(payload, platform,
                                     account_to_client=account_to_client)
        rows_per_client = _split_by_client(rows)
        total = 0
        for cid, rs in rows_per_client.items():
            n = cache.upsert_ads_status(rs)
            total += n
            cache.record_sync(cid, f"batched_{q_type}", started,
                              datetime.utcnow().isoformat() + "Z", n, "ok")
        return {"type": q_type, "accounts": len(cluster), "rows": total,
                "status": "ok"}
    else:
        log.warning("Unknown q_type in batcher: %s", q_type)
        return {"type": q_type, "status": "skipped"}


def _run_per_account_fallback(sm: SupermetricsClient, sig: QuerySig,
                             cluster: List[Tuple[str, str, Dict]],
                             blocked: Set[str]) -> Dict:
    """One Supermetrics query per account in the cluster, in parallel.
    Used after a batch fails — identifies blocked accounts + still gets
    data from the working ones."""
    (ds_id, platform, q_type, date_range_type, fields, num_days) = sig
    ok_count = 0
    err_count = 0
    newly_blocked: List[str] = []
    rows_total = 0

    def _one(item):
        (client_id, acc, _q) = item
        single_query = {
            "ds_id":           ds_id,
            "ds_accounts":     acc,
            "date_range_type": date_range_type,
            "fields":          fields,
            "max_rows":        50000,
        }
        if num_days:
            single_query["num_days"] = num_days
        qc = QueryConfig(name=f"single_{client_id}_{q_type}",
                        platform=platform, type=q_type, query=single_query)
        started = datetime.utcnow().isoformat() + "Z"
        try:
            payload = sm.run_query(qc)
            if q_type == "ad_daily":
                rs = sm.normalize_ad_daily(payload, platform,
                                          fallback_client_id=client_id)
                n = cache.upsert_ad_daily(rs)
            elif q_type == "ad_status":
                rs = sm.normalize_ad_status(payload, platform,
                                           fallback_client_id=client_id)
                n = cache.upsert_ads_status(rs)
            else:
                n = 0
            cache.record_sync(client_id, f"fallback_{q_type}", started,
                              datetime.utcnow().isoformat() + "Z", n, "ok")
            return ("ok", client_id, acc, n, None)
        except SupermetricsError as e:
            msg = str(e)
            cache.record_sync(client_id, f"fallback_{q_type}", started,
                              datetime.utcnow().isoformat() + "Z", 0, "error", msg)
            return ("err", client_id, acc, 0, msg, _is_block_signal(msg))

    # Parallel fallback — independent per-account queries, ~5-10s each.
    with ThreadPoolExecutor(max_workers=FALLBACK_PARALLELISM) as pool:
        futures = [pool.submit(_one, item) for item in cluster]
        for fut in as_completed(futures):
            result = fut.result()
            if result[0] == "ok":
                ok_count += 1
                rows_total += result[3]
            else:
                err_count += 1
                if result[5]:  # is_blocked
                    blocked.add(result[2])
                    newly_blocked.append(result[2])
                    log.info("Marked blocked: %s (%s)", result[1], result[2])

    return {
        "type":           q_type,
        "accounts":       len(cluster),
        "ok":             ok_count,
        "errors":         err_count,
        "newly_blocked":  newly_blocked,
        "rows":           rows_total,
        "status":         "partial",
    }


def _split_by_client(rows: List[Dict]) -> Dict[str, List[Dict]]:
    by_client: Dict[str, List[Dict]] = defaultdict(list)
    for r in rows:
        by_client[r["client_id"]].append(r)
    return by_client
