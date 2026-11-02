#!/usr/bin/env python3
"""Sweep cache capacities with the AgentBank workload and measure DRAM writes.

A DRAM *data write* is counted whenever chunk data is physically placed into
the DRAM tier:
  - an admitted miss inserted into DRAM            (stats: admitted_chunks)
  - an HBM tail chunk demoted into DRAM            (stats: demotions)
Queue re-ordering inside DRAM (second-chance head reinsert) is metadata-only
and is NOT counted as a write (tracked separately as second_chances).

Edit the CONFIG block, then run from anywhere:
    python dram_write_sweep.py
"""
from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    HAS_MPL = True
except ImportError:
    HAS_MPL = False

from buoy_sim.policies import BuoyPolicy, LRUPolicy
from buoy_sim.simulator import run_policy, write_csv
from buoy_sim.trace import load_trace, prefix_chunks

# ================================ CONFIG (edit me) ================================
CONFIG: dict = {
    # Real tokenized ShareGPT workload (Llama-3.1 tokenizer; full UniCache
    # ShareGPT pipeline, 5,000-session sample). Other traces in trace/:
    #   sharegpt20_llama31_real.jsonl   (ShareGPT demo head-20, 82 requests)
    #   quac_llama31_real.jsonl         (QuAC multi-turn QA, 14,451 requests)
    #   agentbank_apps_llama31_real.jsonl (AgentBank agent prompts, 4,408 requests)
    "trace": PROJECT_ROOT / "trace" / "sharegpt5k_llama31_real.jsonl",
    "block_size": 16,
    "initial_depth_threshold": 1,
    # Total resident capacity sweep (hbm + dram chunks per policy instance).
    # Scaled to this workload's working set (~1.4k unique chunks).
    "total_capacities": [128, 256, 512, 1024, 2048, 4096],
    # hbm = round(total * hbm_fraction); dram = total - hbm  (1:4, the
    # production-profile ratio 16384:65536)
    "hbm_fraction": 0.2,
    # ghost capacity = round(hbm * ghost_to_hbm)  (profile: 65536/16384 = 4)
    "ghost_to_hbm": 4.0,
    # Series to sweep: "buoy" (depth admission on), "buoy_noadmission",
    # and "lru" reference. Buoy is the focus; the other two isolate the
    # contribution of admission and serve as baselines.
    "policies": ["buoy", "buoy_noadmission", "lru"],
    "output_dir": PROJECT_ROOT / "results" / "dram_write_sweep_sharegpt20",
    "plot": True,
    "plot_dpi": 200,
}
# ===================================================================================

WATCH = ("admitted_chunks", "demotions", "second_chances",
         "rejected_chunks", "ghost_hits", "evictions")


class InstrumentedBuoy(BuoyPolicy):
    """BuoyPolicy + per-request DRAM-write accounting (no core-code changes)."""

    def __init__(self, hbm, dram, ghost, threshold=1, admission=True, name="buoy"):
        super().__init__(hbm, dram, ghost, threshold, admission=admission)
        self.name = name
        self.per_request: list[dict] = []

    def access_request(self, chunks):
        before = {k: self.stats[k] for k in WATCH}
        result = super().access_request(chunks)
        rec = {k: self.stats[k] - before[k] for k in WATCH}
        # hcap>0 assumed (the sweep guarantees it): every admitted miss and
        # every demotion places data into DRAM exactly once.
        rec["dram_writes"] = rec["admitted_chunks"] + rec["demotions"]
        self.per_request.append(rec)
        return result


class InstrumentedLRU(LRUPolicy):
    """LRUPolicy + per-request accounting; with hcap>0 a DRAM write == demotion."""

    def __init__(self, hbm, dram, name="lru"):
        super().__init__(hbm, dram)
        self.name = name
        self.per_request: list[dict] = []

    def access_request(self, chunks):
        before = {k: self.stats[k] for k in WATCH}
        result = super().access_request(chunks)
        rec = {k: self.stats[k] - before[k] for k in WATCH}
        rec["admitted_chunks"] = 0  # LRU admits into HBM, not DRAM
        rec["dram_writes"] = rec["demotions"]
        self.per_request.append(rec)
        return result


def build_policy(kind: str, hbm: int, dram: int, ghost: int, cfg: dict):
    threshold = cfg["initial_depth_threshold"]
    if kind == "buoy":
        return InstrumentedBuoy(hbm, dram, ghost, threshold, admission=True, name="buoy")
    if kind == "buoy_noadmission":
        return InstrumentedBuoy(hbm, dram, ghost, threshold, admission=False,
                                name="buoy_noadmission")
    if kind == "lru":
        return InstrumentedLRU(hbm, dram)
    raise ValueError(f"unknown policy kind {kind!r}")

def main() -> None:
    cfg = CONFIG
    out_dir = Path(cfg["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    requests = load_trace(cfg["trace"])
    n_req = len(requests)
    unique_chunks = len({cid for r in requests
                         for cid, _, _ in prefix_chunks(r.tokens, cfg["block_size"])})
    print(f"Loaded {n_req} requests | unique chunks at block_size="
          f"{cfg['block_size']}: {unique_chunks} (compulsory-write floor)\n")

    sweep_rows: list[dict] = []
    for total in cfg["total_capacities"]:
        hbm = max(1, round(total * cfg["hbm_fraction"]))
        dram = total - hbm
        ghost = max(1, round(hbm * cfg["ghost_to_hbm"]))
        for kind in cfg["policies"]:
            policy = build_policy(kind, hbm, dram, ghost, cfg)
            summary, rows = run_policy(requests, policy, cfg["block_size"])
            recs = policy.per_request
            assert len(recs) == n_req
            total_writes = sum(r["dram_writes"] for r in recs)
            per_req_avg = total_writes / n_req if n_req else 0.0
            row = {
                "policy": kind, "total_capacity": total, "hbm_capacity": hbm,
                "dram_capacity": dram, "ghost_capacity": ghost,
                "chunk_hit_rate": round(summary["chunk_hit_rate"], 6),
                "token_hit_rate": round(summary["token_hit_rate"], 6),
                "dram_writes_total": total_writes,
                "dram_writes_admitted": sum(r["admitted_chunks"] for r in recs),
                "dram_writes_demoted": sum(r["demotions"] for r in recs),
                "avg_dram_writes_per_request": round(per_req_avg, 4),
                "median_dram_writes_per_request": statistics.median(
                    r["dram_writes"] for r in recs),
                "max_dram_writes_per_request": max(r["dram_writes"] for r in recs),
                "requests_with_zero_dram_write": sum(r["dram_writes"] == 0 for r in recs),
                "rejected_chunks": summary.get("rejected_chunks", 0),
                "ghost_hits": summary.get("ghost_hits", 0),
                "second_chances": summary.get("second_chances", 0),
                "unique_chunks": unique_chunks,
            }
            sweep_rows.append(row)
            detail = []
            for r, rec in zip(rows, recs):
                base = {k: r[k] for k in ("request_index", "timestamp", "request_id",
                                          "prompt_tokens", "chunks", "hit_chunks",
                                          "miss_chunks")}
                base.update(admitted_to_dram=rec["admitted_chunks"],
                            demoted_to_dram=rec["demotions"],
                            dram_writes=rec["dram_writes"],
                            second_chances=rec["second_chances"],
                            ghost_hits=rec["ghost_hits"])
                detail.append(base)
            write_csv(out_dir / f"per_request_{kind}_T{total}.csv", detail)
            print(f"[{kind:17s}] total={total:6d} (hbm={hbm:5d} dram={dram:6d}) "
                  f"hit={summary['chunk_hit_rate']:.4f} "
                  f"dram_writes={total_writes:6d} "
                  f"(admitted={row['dram_writes_admitted']}, "
                  f"demoted={row['dram_writes_demoted']}) avg/req={per_req_avg:.3f}")

    write_csv(out_dir / "sweep_summary.csv", sweep_rows)
    (out_dir / "sweep_config.json").write_text(
        json.dumps({k: (str(v) if isinstance(v, Path) else v) for k, v in cfg.items()},
                   indent=2, ensure_ascii=False), encoding="utf-8")

    if cfg["plot"] and HAS_MPL:
        fig, axes = plt.subplots(1, 2, figsize=(12.5, 4.8))
        for i, kind in enumerate(cfg["policies"]):
            pts = [r for r in sweep_rows if r["policy"] == kind]
            xs = [r["total_capacity"] for r in pts]
            color = "tab:gray" if kind == "lru" else f"C{i}"
            axes[0].plot(xs, [r["dram_writes_total"] for r in pts],
                         marker="o", label=kind, color=color)
            axes[1].plot(xs, [r["avg_dram_writes_per_request"] for r in pts],
                         marker="o", label=kind, color=color)
        axes[0].axhline(unique_chunks, ls="--", c="k", lw=1,
                        label=f"unique chunks ({unique_chunks})")
        for ax in axes:
            ax.set_xscale("log", base=2)
            ax.set_xlabel("total capacity (HBM+DRAM, chunks)")
        axes[0].set_ylabel("DRAM data writes (total)")
        axes[0].set_title("Total DRAM writes vs cache size")
        axes[0].legend()
        axes[1].set_ylabel("avg DRAM writes per request")
        axes[1].set_title("Average DRAM writes per request")
        fig.tight_layout()
        path = out_dir / "dram_writes_vs_capacity.png"
        fig.savefig(path, dpi=cfg["plot_dpi"], bbox_inches="tight")
        plt.close(fig)
        print(f"\nplot: {path.name}")
    elif cfg["plot"]:
        print("\nmatplotlib not installed; skipped plot.")

    print(f"\nDone. Results in: {out_dir.resolve()}")


if __name__ == "__main__":
    main()
