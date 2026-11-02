#!/usr/bin/env python3
"""Convert UniCache session-format data (tokenized ShareGPT / AgentBank) into
JSONL traces for the buoy prefix-cache simulator.

UniCache session format (one session per line):
  {"<session_key>": {"id": ..., "num_turns": N,
                     "turn_1": {"input_ids": [...], ...}, ...}}

Output format (one request per line, consumed by buoy_sim.trace.load_trace):
  {"timestamp": <poisson arrival>, "request_id": "<session>_t<i>",
   "session_id": "<session>", "turn_index": i, "tokens": ["128000", ...]}

Tokens are the real Llama-3.1 tokenizer IDs (as strings), so chunk identities
are exact. Inter-arrival times are exponential (Poisson process), matching
UniCache's tasks_config lambda_rate.
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path


def main() -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--session-data", required=True, help="UniCache session .jsonl")
    p.add_argument("--output", required=True, help="output trace .jsonl for the buoy simulator")
    p.add_argument("--lambda-rate", type=float, default=1.0, help="Poisson arrival rate (req/s)")
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()

    rng = random.Random(args.seed)
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    n_sessions = n_requests = n_tokens = 0
    t = 0.0
    with Path(args.session_data).open(encoding="utf-8") as fin, \
            out.open("w", encoding="utf-8") as fout:
        for line in fin:
            if not line.strip():
                continue
            obj = json.loads(line)
            sid = next(iter(obj))
            inner = obj[sid]
            n_sessions += 1
            for i in range(1, inner.get("num_turns", 0) + 1):
                ids = inner[f"turn_{i}"]["input_ids"]
                t += rng.expovariate(args.lambda_rate) if args.lambda_rate > 0 else 0.0
                fout.write(json.dumps({
                    "timestamp": round(t, 3),
                    "request_id": f"{sid}_t{i}",
                    "session_id": sid,
                    "turn_index": i,
                    "tokens": [str(x) for x in ids],
                }, separators=(",", ":")) + "\n")
                n_requests += 1
                n_tokens += len(ids)
    print(f"Wrote {n_requests} requests ({n_sessions} sessions, {n_tokens} prompt tokens) to {out}")


if __name__ == "__main__":
    main()
