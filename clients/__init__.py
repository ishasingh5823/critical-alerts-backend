"""Client registry for the Critical Alert Dashboard.

Loads per-client JSON configs from this directory. Each config maps a Joveo
end-client (Kenvue, Trimac, etc.) to one or more Supermetrics saved queries
that pull that client's campaign data.
"""

import json
from pathlib import Path
from typing import Dict, List

_HERE = Path(__file__).resolve().parent
_cache: Dict[str, Dict] = {}


def _load_all() -> Dict[str, Dict]:
    global _cache
    if _cache:
        return _cache
    for path in _HERE.glob("*.json"):
        with open(path, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        cid = cfg.get("id") or path.stem
        _cache[cid] = cfg
    return _cache


def get_client_config(client_id: str) -> Dict:
    all_cfgs = _load_all()
    if client_id not in all_cfgs:
        raise KeyError(
            f"Unknown client '{client_id}'. Known: {sorted(all_cfgs.keys())}"
        )
    return all_cfgs[client_id]


def list_clients() -> List[Dict]:
    """Return all real clients (skip _-prefixed templates like _example.json)."""
    return [
        {
            "id": c["id"],
            "display_name": c.get("display_name", c["id"]),
            "platforms": c.get("platforms", []),
            "queries_count": len(c.get("queries", [])),
        }
        for c in sorted(_load_all().values(), key=lambda x: x.get("display_name", x["id"]))
        if not c["id"].startswith("_")
    ]


def reload() -> None:
    global _cache
    _cache = {}
    _load_all()
