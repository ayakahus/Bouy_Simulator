#!/usr/bin/env python3
"""Annotated length CDF (P50/P90/P99) + Buoy-vs-LRU hit-rate bars (S/M/L)."""
import csv
import json
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
PCTS = [(0.50, "P50"), (0.90, "P90"), (0.99, "P99")]
STATS_OUT = BASE / "results" / "trace_stats"
HITR_OUT = BASE / "results" / "hitrate_sml"

# ---------------- 1. annotated length CDF ----------------
lengths = {}
for label, fname in TRACES.items():
    lens = []
    with (BASE / "trace" / fname).open(encoding="utf-8") as f:
        for line in f:
            lens.append(json.loads(line)["input_length"])
    lengths[label] = sorted(lens)

fig, axes = plt.subplots(1, 4, figsize=(17.5, 4.2))
for ax, (label, _), c in zip(axes, TRACES.items(), ["tab:blue", "tab:orange", "tab:green", "tab:red"]):
    vals = lengths[label]
    n = len(vals)
    xs, ys, last = [], [], -1.0
    for i, v in enumerate(vals):
        cf = (i + 1) / n
        if v != last:
            xs.append(v)
            ys.append(cf)
            last = v
    ax.plot(xs, ys, color=c, lw=1.8, label="CDF")
    for pfrac, pname in PCTS:
        v = vals[min(n - 1, int(round(pfrac * n)) - 1)]
        ax.plot([v], [pfrac], "o", ms=4, color="black", zorder=5)
        ax.vlines(v, 0, pfrac, colors="gray", linestyles=":", lw=1)
        ax.annotate(f"{pname}\n{v:,}", xy=(v, pfrac), xytext=(v + xs[-1] * 0.03, pfrac - 0.13),
                    fontsize=8, ha="left",
                    bbox=dict(boxstyle="round,pad=0.2", fc="white", ec="gray", alpha=0.85))
    med = vals[n // 2]
    ax.set_title(f"{label}\nrequests={n:,}, median={med:,}, max={vals[-1]:,}", fontsize=10)
    ax.set_xlabel("input length (tokens)")
    ax.set_ylabel("CDF")
    ax.set_ylim(0, 1.03)
    ax.set_xlim(0, xs[-1] * 1.02)
    ax.grid(alpha=0.3)
fig.suptitle("Per-request input-length CDF with key percentiles (linear x-axis)", y=1.04)
fig.tight_layout()
p1 = STATS_OUT / "length_cdf_percentiles.png"
fig.savefig(p1, dpi=200, bbox_inches="tight")
plt.close(fig)
print("wrote", p1)

# ---------------- 2. hit-rate bars (S/M/L, Buoy vs LRU) ----------------
rows = list(csv.DictReader(open(HITR_OUT / "hitrate_summary.csv")))
by_trace = defaultdict(dict)
caps_order = ["small", "medium", "large"]
cap_tot = {}
cap_hbm = {}
cap_dram = {}
for r in rows:
    key = (r["trace"], r["cap_label"])
    by_trace[key][r["policy"]] = float(r["chunk_hit_rate"])
    cap_tot[key] = int(r["total_capacity"])
    cap_hbm[key] = int(r["hbm_capacity"])
    cap_dram[key] = int(r["dram_capacity"])
traces = sorted({t for (t, _) in by_trace})

fig, axes = plt.subplots(1, 4, figsize=(17.5, 5.0))
width = 0.35
for ax, (label, fname), c in zip(axes, TRACES.items(), ["tab:blue", "tab:orange", "tab:green", "tab:red"]):
    csv_name = {"Qwen Trace A (to-C)": "QwenTraceA", "Qwen Trace B (to-B)": "QwenTraceB",
                "Mooncake Conv": "MooncakeConv", "Mooncake Tool&Agent": "MooncakeTool"}[label]
    xs = range(len(caps_order))
    bv, lv, tick = [], [], []
    for cap in caps_order:
        key = (csv_name, cap)
        bv.append(by_trace[key]["buoy"])
        lv.append(by_trace[key]["lru"])
        tick.append(f"{cap} (T={cap_tot[key]:,})\nHBM {cap_hbm[key]:,}\nDRAM {cap_dram[key]:,}")
    b1 = ax.bar([x - width / 2 for x in xs], bv, width, label="Buoy", color="tab:blue")
    b2 = ax.bar([x + width / 2 for x in xs], lv, width, label="LRU", color="tab:orange")
    ax.bar_label(b1, fmt="%.3f", fontsize=7, padding=1)
    ax.bar_label(b2, fmt="%.3f", fontsize=7, padding=1)
    ax.set_xticks(list(xs))
    ax.set_xticklabels(tick, fontsize=7.5)
    ax.set_ylim(0, 0.72)
    n_unique = int([r for r in rows if r["trace"] == csv_name][0]["unique_chunks"])
    ax.set_title(f"{label}\nW={n_unique:,} unique chunks", fontsize=10)
    ax.set_ylabel("chunk hit rate")
    ax.grid(axis="y", alpha=0.3)
    ax.legend(fontsize=8, loc="upper left")
fig.suptitle("Buoy vs LRU chunk hit rate under small / medium / large capacities (5% / 25% / 100% of working set)", y=1.04)
fig.tight_layout()
p2 = HITR_OUT / "hitrate_bars.png"
fig.savefig(p2, dpi=200, bbox_inches="tight")
plt.close(fig)
print("wrote", p2)
