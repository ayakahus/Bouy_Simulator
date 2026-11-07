#!/usr/bin/env python3
"""Convert hash-based official traces (Qwen Bailian / Mooncake) into
simulator-format JSONL.

Each hash_id becomes ONE pseudo-token; replay with block_size=1 so that one
simulated chunk == one hash block (16 real tokens for Qwen, ~500 for
Mooncake). Chunk identity chains through the parent block, so two requests
share chunk j iff their first j+1 hash blocks are identical — exactly the
real prefix-cache semantics.
"""
from __future__ import annotations

import argparse
import json
import statistics
from pathlib import Path


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--input", required=True)
    p.add_argument("--output", required=True)
    args = p.parse_args()

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)

    n_req = n_blocks = n_tokens = 0
    lens = []
    with open(args.input, encoding="utf-8") as fin, \
            out.open("w", encoding="utf-8") as fout:
        for line in fin:
            if not line.strip():
                continue
            r = json.loads(line)
            ids = r["hash_ids"]
            tokens = [str(h) for h in ids]
            fout.write(json.dumps({
                "timestamp": r["timestamp"],
                "request_id": f"r{n_req}",
                "session_id": r.get("chat_id", "-"),
                "turn": r.get("turn", 1),
                "input_length": r["input_length"],
                "tokens": tokens,
            }, separators=(",", ":")) + "\n")
            n_req += 1
            n_blocks += len(ids)
            n_tokens += r["input_length"]
            lens.append(r["input_length"])
    print(f"{out.name}: requests={n_req} blocks={n_blocks} "
          f"input_tokens={n_tokens} median_len={int(statistics.median(lens))}")


if __name__ == "__main__":
    main()
