"""Trace replay and metrics."""
from __future__ import annotations
import csv, json
from pathlib import Path
from .trace import Request, prefix_chunks
from .policies import BuoyPolicy, LRUPolicy

def run_policy(requests: list[Request], policy, block_size: int) -> tuple[dict, list[dict]]:
    rows = []
    total_prompt_tokens = total_hit_tokens = total_chunks = total_hits = 0
    total_hbm = total_dram = total_misses = 0
    all_chunks = [prefix_chunks(req.tokens, block_size) for req in requests]
    if hasattr(policy, "prepare"):
        # Offline policies (Belady OPT) need the full access sequence up front.
        policy.prepare(all_chunks)
    for req, chunks in zip(requests, all_chunks):
        result = policy.access_request(chunks)
        n = len(chunks)
        total_prompt_tokens += len(req.tokens)
        total_hit_tokens += result["hit_tokens"]
        total_chunks += n
        total_hits += result["hit_chunks"]
        total_hbm += result["hbm_hits"]
        total_dram += result["dram_hits"]
        total_misses += result["misses"]
        rows.append({
            "request_index": req.index, "timestamp": req.timestamp, "request_id": req.request_id,
            "prompt_tokens": len(req.tokens), "chunks": n,
            "hbm_hits": result["hbm_hits"], "dram_hits": result["dram_hits"],
            "hit_chunks": result["hit_chunks"], "miss_chunks": result["misses"],
            "hit_tokens": result["hit_tokens"],
            "chunk_hit_rate": result["hit_chunks"] / n if n else 0.0,
            "token_hit_rate": result["hit_tokens"] / len(req.tokens) if req.tokens else 0.0,
        })
    stats = policy.stats
    summary = {
        "policy": policy.name,
        "requests": len(requests),
        "prompt_tokens": total_prompt_tokens,
        "chunk_accesses": total_chunks,
        "hbm_hits": total_hbm,
        "dram_hits": total_dram,
        "hit_chunks": total_hits,
        "miss_chunks": total_misses,
        "chunk_hit_rate": total_hits / total_chunks if total_chunks else 0.0,
        "token_hit_rate": total_hit_tokens / total_prompt_tokens if total_prompt_tokens else 0.0,
        "estimated_prefill_tokens_saved": total_hit_tokens,
        "hbm_hit_rate": total_hbm / total_chunks if total_chunks else 0.0,
        "dram_hit_rate": total_dram / total_chunks if total_chunks else 0.0,
        **{k: v for k, v in stats.items() if k not in {"total_chunks", "hbm_hits", "dram_hits", "misses"}},
    }
    if isinstance(policy, BuoyPolicy):
        summary["final_depth_threshold"] = policy.depth_threshold
        summary["reuse_events"] = policy.reuse_events
    return summary, rows

def write_csv(path: Path, rows: list[dict]):
    path.parent.mkdir(parents=True, exist_ok=True)
    if not rows:
        path.write_text("", encoding="utf-8")
        return
    with path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        writer.writeheader()
        writer.writerows(rows)
