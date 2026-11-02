#!/usr/bin/env python3
import argparse, json
from pathlib import Path
from buoy_sim.trace import load_trace
from buoy_sim.policies import BuoyPolicy, LRUPolicy
from buoy_sim.simulator import run_policy, write_csv

def main():
    p = argparse.ArgumentParser(description="Replay an LLM prefix-cache trace with Buoy and/or LRU.")
    p.add_argument("--trace", required=True, help="Input .jsonl, .json, or .csv trace")
    p.add_argument("--policy", choices=["buoy", "lru", "both"], default="buoy")
    p.add_argument("--hbm-capacity", type=int, default=1024, help="HBM capacity in chunks")
    p.add_argument("--dram-capacity", type=int, default=4096, help="DRAM capacity in chunks")
    p.add_argument("--ghost-capacity", type=int, default=4096, help="Ghost FIFO capacity in IDs")
    p.add_argument("--block-size", type=int, default=16, help="Tokens per KV chunk")
    p.add_argument("--initial-depth-threshold", type=int, default=1)
    p.add_argument("--disable-admission", action="store_true")
    p.add_argument("--output-dir", default="results")
    args = p.parse_args()
    if args.block_size <= 0:
        p.error("--block-size must be positive")
    requests = load_trace(args.trace)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    policies = []
    if args.policy in ("buoy", "both"):
        policies.append(BuoyPolicy(args.hbm_capacity, args.dram_capacity, args.ghost_capacity,
                                   args.initial_depth_threshold, admission=not args.disable_admission))
    if args.policy in ("lru", "both"):
        policies.append(LRUPolicy(args.hbm_capacity, args.dram_capacity))
    summaries = []
    for policy in policies:
        summary, rows = run_policy(requests, policy, args.block_size)
        summaries.append(summary)
        write_csv(out / f"requests_{policy.name}.csv", rows)
        (out / f"final_state_{policy.name}.json").write_text(
            json.dumps(policy.snapshot(), indent=2, ensure_ascii=False), encoding="utf-8")
        print(json.dumps(summary, indent=2))
    write_csv(out / "summary.csv", summaries)
    print(f"\nWrote results to: {out.resolve()}")

if __name__ == "__main__":
    main()
