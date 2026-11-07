#!/usr/bin/env python3
"""Small/Medium/Large capacity sweep: Buoy vs LRU on the 4 official traces.

Chunk IDs are pre-computed once per trace (blake2b-16 chain, identical to
buoy_sim.trace.prefix_chunks semantics), then replayed for every
(capacity, policy) pair. Capacities scale with each trace's unique-chunk
working set W: small=5%W, medium=25%W, large=100%W (HBM:DRAM = 1:4,
ghost = 4xHBM).
"""
from __future__ import annotations

import csv
import hashlib
import json
import time
from pathlib import Path

from buoy_sim.policies import BuoyPolicy, LRUPolicy

from collections import OrderedDict


class FastGhostBuoy(BuoyPolicy):
    """BuoyPolicy with an O(1) ghost-FIFO backing store.

    Semantics are identical to the library implementation (FIFO refresh on
    re-add, membership set, capacity trim from the oldest end); only the
    container changes from list+set to OrderedDict, which matters for traces
    where the ghost table grows to ~10^5-10^6 ids.
    """

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ghost = OrderedDict()  # key = chunk id; insertion order = FIFO age

    def _ghost_contains(self, cid):
        return cid in self.ghost

    def _ghost_add(self, cid):
        if self.gcap <= 0:
            return
        if cid in self.ghost:          # refresh: newest end of the FIFO
            self.ghost.move_to_end(cid)
        else:
            self.ghost[cid] = None
        while len(self.ghost) > self.gcap:
            old, _ = self.ghost.popitem(last=False)
            self.ghost_set.discard(old)

    def _ghost_remove(self, cid):
        if cid in self.ghost:
            del self.ghost[cid]
            self.ghost_set.discard(cid)


BASE = Path("/home/fzz27/buoy_prefix_cache_simulator_production_profile/buoy_prefix_cache_simulator")
TRACES = {
    "QwenTraceA": "qwen_traceA_sim.jsonl",
    "QwenTraceB": "qwen_traceB_sim.jsonl",
    "MooncakeConv": "mooncake_conv_sim.jsonl",
    "MooncakeTool": "mooncake_tool_sim.jsonl",
}
FRACTIONS = [("small", 0.05), ("medium", 0.25), ("large", 1.00)]
OUT = BASE / "results" / "hitrate_sml"
OUT.mkdir(parents=True, exist_ok=True)


def precompute_chunks(path: Path):
    """Return per-request lists of 16-byte chunk ids (block_size=1 semantics)."""
    chunk_reqs = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            ids = r["tokens"]
            parent = "ROOT"
            cids = []
            ap = cids.append
            for hs in ids:
                payload = b'["' + parent.encode() + b'","' + hs.encode() + b'"]'
                cid = hashlib.blake2b(payload, digest_size=16).digest()
                ap(cid)
                parent = cid.hex()
            chunk_reqs.append(cids)
    return chunk_reqs


def replay(policy, chunk_reqs):
    hits = 0
    for cids in chunk_reqs:
        if not cids:
            continue
        n = len(cids)
        res = policy.access_request([(c, i + 1, 1) for i, c in enumerate(cids)])
        hits += res["hit_chunks"]
    total = sum(len(c) for c in chunk_reqs)
    return hits / total if total else 0.0


def main() -> None:
    rows = []
    for label, fname in TRACES.items():
        t0 = time.time()
        chunk_reqs = precompute_chunks(BASE / "trace" / fname)
        allc = [c for req in chunk_reqs for c in req]
        W = len(set(allc))
        del allc
        print(f"[{label}] {len(chunk_reqs)} reqs, {sum(len(c) for c in chunk_reqs):,} "
              f"block accesses, unique chunks W={W:,} (precompute {time.time()-t0:.0f}s)")

        for cap_label, frac in FRACTIONS:
            total = max(4, round(W * frac))
            hbm = max(1, round(total * 0.2))
            dram = max(1, total - hbm)
            ghost = max(1, hbm * 4)
            for pname, policy in (
                ("buoy", FastGhostBuoy(hbm, dram, ghost)),
                ("lru", LRUPolicy(hbm, dram)),
            ):
                hr = replay(policy, chunk_reqs)
                s = policy.stats
                rows.append({
                    "trace": label, "cap_label": cap_label,
                    "total_capacity": total, "hbm_capacity": hbm,
                    "dram_capacity": dram, "ghost_capacity": ghost,
                    "policy": pname, "chunk_hit_rate": round(hr, 6),
                    "hbm_hits": s["hbm_hits"], "dram_hits": s["dram_hits"],
                    "misses": s["misses"], "admitted_chunks": s["admitted_chunks"],
                    "rejected_chunks": s["rejected_chunks"], "demotions": s["demotions"],
                    "evictions": s["evictions"], "ghost_hits": s["ghost_hits"],
                    "second_chances": s["second_chances"], "unique_chunks": W,
                })
                print(f"   {cap_label:6s} T={total:8,} [{pname:4s}] "
                      f"hit_rate={hr:.4f} (hbm={s['hbm_hits']:,} dram={s['dram_hits']:,})")
        del chunk_reqs

    with (OUT / "hitrate_summary.csv").open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print("wrote", OUT / "hitrate_summary.csv")


if __name__ == "__main__":
    main()
