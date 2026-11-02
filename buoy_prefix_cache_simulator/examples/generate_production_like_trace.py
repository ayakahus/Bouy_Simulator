#!/usr/bin/env python3
"""Generate a deterministic synthetic agent-workload prefix-cache trace.

This is a synthetic workload generator, not captured production traffic and not
a tokenizer. It emits stable token-like IDs to exercise prefix reuse behavior.
"""
import argparse, json, random
from pathlib import Path

def main():
    p = argparse.ArgumentParser()
    p.add_argument("--output", default="examples/production_like_trace.jsonl")
    p.add_argument("--sessions", type=int, default=120)
    p.add_argument("--seed", type=int, default=20261027)
    args = p.parse_args()
    rng = random.Random(args.seed)
    common = [f"SYS_COMMON_{i:04d}" for i in range(128)]
    agents = {
        "research": [f"RESEARCH_TOOL_SPEC_{i:04d}" for i in range(96)],
        "coding": [f"CODING_TOOL_SPEC_{i:04d}" for i in range(96)],
        "browser": [f"BROWSER_TOOL_SPEC_{i:04d}" for i in range(96)],
        "data": [f"DATA_TOOL_SPEC_{i:04d}" for i in range(96)],
    }
    templates = {a: [[f"{a.upper()}_TEMPLATE_{t}_{i:04d}" for i in range(64)]
                     for t in range(8)] for a in agents}
    sessions = {}
    pending = []
    for sid in range(args.sessions):
        name = f"s{sid:04d}"
        sessions[name] = {
            "agent": rng.choice(list(agents)), "template": rng.randrange(8),
            "history": [], "turns": rng.randint(3, 7),
            "start": rng.uniform(0, 3600)
        }
        t = sessions[name]["start"]
        for turn in range(sessions[name]["turns"]):
            if turn:
                t += rng.expovariate(1 / 45.0)
            pending.append((t, name, turn))
    pending.sort()
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", encoding="utf-8") as f:
        for i, (ts, name, turn) in enumerate(pending):
            s = sessions[name]; agent = s["agent"]
            toks = common + agents[agent] + templates[agent][s["template"]]
            toks += [f"SESSION_{name}_META_{j:04d}" for j in range(32)]
            toks += s["history"]
            toks += [f"TURN_MARKER_{turn}", f"ROLE_USER_{agent}"]
            toks += [f"USER_{name}_T{turn}_{j:04d}" for j in range(rng.randint(80, 420))]
            if rng.random() < .38:
                toks += [f"TOOL_CALL_{agent}_{turn}_{j:04d}" for j in range(rng.randint(80, 600))]
                toks += [f"TOOL_OBS_{name}_{turn}_{j:04d}" for j in range(rng.randint(80, 500))]
            if rng.random() < .14:
                toks += [f"LONG_CONTEXT_{name}_{turn}_{j:04d}" for j in range(rng.randint(400, 1800))]
            f.write(json.dumps({
                "timestamp": round(ts, 3), "request_id": f"req_{i:05d}",
                "session_id": name, "turn_index": turn, "workload_class": agent,
                "prompt_tokens": len(toks), "tokens": toks
            }, separators=(",", ":")) + "\n")
            s["history"] += [f"HISTORY_{name}_T{turn}_{j:04d}" for j in range(rng.randint(80, 220))]
    print(f"Wrote {len(pending)} requests to {out}")

if __name__ == "__main__":
    main()
