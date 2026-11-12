#!/usr/bin/env python3
import argparse, json
from pathlib import Path
from buoy_sim.trace import load_trace
from buoy_sim.policies import BuoyPolicy, LRUPolicy, OPTPolicy, S3FIFOPolicy
from buoy_sim.simulator import run_policy, write_csv

def main():
    p = argparse.ArgumentParser(description="Replay an LLM prefix-cache trace with Buoy, LRU, and/or S3-FIFO.")
    p.add_argument("--trace", required=True, help="Input .jsonl, .json, or .csv trace")
    p.add_argument("--policy", choices=["buoy", "lru", "s3fifo", "opt", "both", "all"], default="buoy",
                   help="'both' = buoy+lru, 'all' = buoy+lru+s3fifo+opt")
    p.add_argument("--hbm-capacity", type=int, default=1024, help="HBM capacity in chunks")
    p.add_argument("--dram-capacity", type=int, default=4096, help="DRAM capacity in chunks")
    p.add_argument("--ghost-capacity", type=int, default=4096, help="Ghost FIFO capacity in IDs")
    p.add_argument("--block-size", type=int, default=16, help="Tokens per KV chunk")
    p.add_argument("--initial-depth-threshold", type=int, default=1)
    p.add_argument("--move-to-main-threshold", type=int, default=1,
                   help="S3-FIFO: re-accesses inside HBM required before promotion to DRAM")
    p.add_argument("--request-constraint", action="store_true",
                   help="pin all chunks of the request being processed (they cannot be "
                        "evicted until the request completes; missing chunks force-admitted)")
    p.add_argument("--disable-admission", action="store_true")
    p.add_argument("--output-dir", default="results")
    args = p.parse_args()
    if args.block_size <= 0:
        p.error("--block-size must be positive")
    requests = load_trace(args.trace)
    out = Path(args.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    policies = []
    if args.policy in ("buoy", "both", "all"):
        policies.append(BuoyPolicy(args.hbm_capacity, args.dram_capacity, args.ghost_capacity,
                                   args.initial_depth_threshold, admission=not args.disable_admission,
                                   request_constraint=args.request_constraint))
    if args.policy in ("lru", "both", "all"):
        policies.append(LRUPolicy(args.hbm_capacity, args.dram_capacity,
                                  request_constraint=args.request_constraint))
    if args.policy in ("s3fifo", "all"):
        policies.append(S3FIFOPolicy(args.hbm_capacity, args.dram_capacity, args.ghost_capacity,
                                     move_to_main_threshold=args.move_to_main_threshold,
                                     request_constraint=args.request_constraint))
    if args.policy in ("opt", "all"):
        policies.append(OPTPolicy(args.hbm_capacity, args.dram_capacity,
                                  request_constraint=args.request_constraint))
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
