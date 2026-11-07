#!/usr/bin/env python3
"""Turns-per-session and per-request-length CDFs for the 4 official traces.

Mooncake traces carry no session field, so sessions are *inferred*: a request
whose hash_ids extend the previous request's hash_ids (exact prefix
continuation) is treated as the next turn of the same session. Qwen traces
have real chat_id/turn fields.
X axes are linear (no log), per request.
"""
import json
import sys
from collections import defaultdict
from pathlib import Path

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt

BASE = Path("/home/fzz27/buoy_prefix_cache_simulator_production_profile/buoy_prefix_cache_simulator")
TRACES = {
    "Qwen Trace A (to-C)": "qwen_traceA_sim.jsonl",
    "Qwen Trace B (to-B)": "qwen_traceB_sim.jsonl",
    "Mooncake Conv": "mooncake_conv_sim.jsonl",
    "Mooncake Tool&Agent": "mooncake_tool_sim.jsonl",
}
OUT = BASE / "results" / "trace_stats"
OUT.mkdir(parents=True, exist_ok=True)

turn_sessions = {}
len_rows = {}
for label, fname in TRACES.items():
    turns_per_sess = defaultdict(int)
    inferred = False
    prev = None
    prev_sid = None
    cur_sid = None
    lens = []
    n_multi = 0
    with (BASE / "trace" / fname).open(encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            lens.append(r["input_length"])
            if r.get("session_id", "-") != "-":          # Qwen: real chat_id/turn
                key = ("chat", r["session_id"])
                turns_per_sess[key] = max(turns_per_sess[key], r["turn"])
            else:                                        # Mooncake: infer session
                inferred = True
                ids = r["tokens"]
                if prev is not None and ids[: len(prev)] == prev:
                    key = prev_sid                       # continuation turn
                else:
                    cur_sid = len(turns_per_sess)        # new session
                    key = ("inf", cur_sid)
                turns_per_sess[key] += 1
                prev = ids
                prev_sid = key
    sess_turns = sorted(turns_per_sess.values())
    turn_sessions[label] = sess_turns
    len_rows[label] = sorted(lens)
    n_multi = sum(1 for t in sess_turns if t > 1)
    tag = " (inferred sessions)" if inferred else ""
    print(f"{label}: sessions={len(sess_turns)} turns/session "
          f"[min {sess_turns[0]}, median {sess_turns[len(sess_turns)//2]}, "
          f"max {sess_turns[-1]}] multi-turn sessions={n_multi}{tag} | "
          f"req len [min {lens and min(lens)}, median {sorted(lens)[len(lens)//2]}, "
          f"max {max(lens)}]")


def cdf_xy(values):
    n = len(values)
    xs, ys, last = [], [], -1.0
    for i, v in enumerate(values):
        c = (i + 1) / n
        if v != last:
            xs.append(v)
            ys.append(c)
            last = v
    return xs, ys


COLORS = ["tab:blue", "tab:orange", "tab:green", "tab:red"]

# ---- turns per session CDF (linear x) ----
fig, axes = plt.subplots(1, 4, figsize=(17, 4.0))
for ax, (label, _), c in zip(axes, TRACES.items(), COLORS):
    vals = turn_sessions[label]
    xs, ys = cdf_xy(vals)
    ax.plot(xs, ys, color=c, lw=1.8)
    med = vals[len(vals) // 2]
    ax.axvline(med, ls="--", c="gray", lw=1)
    ax.set_title(f"{label}\nsessions={len(vals)}, median={med}, max={vals[-1]}",
                 fontsize=10)
    ax.set_xlabel("turns per session")
    ax.set_ylabel("CDF")
    ax.set_ylim(0, 1.02)
    ax.grid(alpha=0.3)
fig.suptitle("Turns-per-session CDF (linear x-axis)", y=1.04)
fig.tight_layout()
fig.savefig(OUT / "turns_cdf.png", dpi=200, bbox_inches="tight")
plt.close(fig)
print("wrote", OUT / "turns_cdf.png")

# ---- per-request length CDF (linear x) ----
fig, axes = plt.subplots(1, 4, figsize=(17, 4.0))
for ax, (label, _), c in zip(axes, TRACES.items(), COLORS):
    vals = len_rows[label]
    xs, ys = cdf_xy(vals)
    ax.plot(xs, ys, color=c, lw=1.8)
    med = vals[len(vals) // 2]
    ax.axvline(med, ls="--", c="gray", lw=1)
    ax.set_title(f"{label}\nmedian={med}, max={vals[-1]:,}", fontsize=10)
    ax.set_xlabel("input length (tokens)")
    ax.set_ylabel("CDF")
    ax.set_ylim(0, 1.02)
    ax.grid(alpha=0.3)
fig.suptitle("Per-request input-length CDF (linear x-axis)", y=1.04)
fig.tight_layout()
fig.savefig(OUT / "length_cdf.png", dpi=200, bbox_inches="tight")
plt.close(fig)
print("wrote", OUT / "length_cdf.png")
