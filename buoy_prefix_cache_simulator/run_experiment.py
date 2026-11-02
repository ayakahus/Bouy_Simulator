#!/usr/bin/env python3
"""Run Buoy and/or LRU on a prefix-cache trace and generate comparison plots.

Edit the CONFIG block below, then run from anywhere:

    python run_experiment.py

Outputs (written to CONFIG["output_dir"]):
  - requests_<policy>.csv / final_state_<policy>.json / summary.csv
    (same formats as run_simulation.py)
  - experiment_config.json  (the exact parameters used, for reproducibility)
  - one .png per enabled entry in CONFIG["plots"]
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

try:
    import matplotlib
    matplotlib.use("Agg")  # headless-safe: write PNG files, no window
    import matplotlib.pyplot as plt
    HAS_MPL = True
except ImportError:
    HAS_MPL = False

from buoy_sim.policies import BuoyPolicy, LRUPolicy
from buoy_sim.simulator import run_policy, write_csv
from buoy_sim.trace import load_trace

# ================================ CONFIG (edit me) ================================
CONFIG: dict = {
    # ---- input ----------------------------------------------------------------
    # Synthetic demo trace (see README). Real tokenized workloads live in trace/:
    #   trace/sharegpt5k_llama31_real.jsonl     (ShareGPT 5k sessions, 17,191 reqs)
    #   trace/sharegpt20_llama31_real.jsonl     (ShareGPT head-20 demo, 82 reqs)
    #   trace/quac_llama31_real.jsonl           (QuAC multi-turn QA, 14,451 reqs)
    #   trace/agentbank_apps_llama31_real.jsonl (AgentBank agents, 4,408 reqs)
    "trace": PROJECT_ROOT / "examples" / "production_like_trace.jsonl",
    # Policies to run and compare: any subset of ["buoy", "lru"]
    "policies": ["buoy", "lru"],
    # ---- cache parameters (capacities are in chunks, not bytes) ----------------
    # Production-like profile from configs/llama31_8b_bf16_example.json.
    # Scaled smoke test: hbm=2048, dram=8192, ghost=8192.
    "hbm_capacity": 2048,
    "dram_capacity": 8192,
    "ghost_capacity": 8192,
    "block_size": 16,
    "initial_depth_threshold": 1,
    "disable_admission": False,
    # ---- output ----------------------------------------------------------------
    "output_dir": PROJECT_ROOT / "results" / "H2048_D8192",
    # Enabled plots, any subset of:
    #   "summary_bars"    grouped bars of the summary hit rates
    #   "per_request"     token hit rate of every request
    #   "cumulative"      running token hit rate over the trace
    #   "tier_breakdown"  HBM / DRAM / miss share of chunk accesses
    #   "length_buckets"  token hit rate vs prompt-length bucket
    #   "time_buckets"    token hit rate vs arrival-time bucket
    "plots": ["summary_bars", "per_request", "cumulative",
              "tier_breakdown", "length_buckets", "time_buckets"],
    "plot_dpi": 200,
    # Prompt-length bucket edges for "length_buckets" (None = +infinity)
    "length_bucket_edges": [1000, 2000, 4000, None],
    # Equal-width arrival-time buckets for "time_buckets"
    "time_bucket_count": 10,
}
# ===================================================================================

PALETTE = ["tab:blue", "tab:orange", "tab:green", "tab:red", "tab:purple"]


def build_policy(name: str, cfg: dict):
    if name == "buoy":
        return BuoyPolicy(cfg["hbm_capacity"], cfg["dram_capacity"], cfg["ghost_capacity"],
                          cfg["initial_depth_threshold"],
                          admission=not cfg["disable_admission"])
    if name == "lru":
        return LRUPolicy(cfg["hbm_capacity"], cfg["dram_capacity"])
    raise ValueError(f"unknown policy {name!r}; choose from 'buoy', 'lru'")


def _color(i: int) -> str:
    return PALETTE[i % len(PALETTE)]


def _fmt(x: float) -> str:
    return f"{x / 1000:g}k" if x >= 1000 else f"{x:g}"


def _length_edges_labels(raw_edges: list) -> tuple[list[float], list[str]]:
    edges = [float(e) if e is not None else float("inf") for e in raw_edges]
    if not edges or edges[-1] != float("inf"):
        edges.append(float("inf"))
    labels = []
    for i, edge in enumerate(edges):
        prev = edges[i - 1] if i else 0.0
        if edge == float("inf"):
            labels.append(f">{_fmt(prev)}")
        elif i == 0:
            labels.append(f"<= {_fmt(edge)}")
        else:
            labels.append(f"{_fmt(prev)}-{_fmt(edge)}")
    return edges, labels


def _weighted_hit_rate(rows: list[dict]) -> float:
    hit = sum(r["hit_tokens"] for r in rows)
    tok = sum(r["prompt_tokens"] for r in rows)
    return hit / tok if tok else float("nan")


def _save(fig, out_dir: Path, name: str, dpi: int) -> None:
    fig.tight_layout()
    path = out_dir / f"{name}.png"
    fig.savefig(path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)
    print(f"  plot: {path.name}")

def plot_summary_bars(summaries: list[dict], out_dir: Path, dpi: int) -> None:
    metrics = ["chunk_hit_rate", "token_hit_rate", "hbm_hit_rate", "dram_hit_rate"]
    names = [s["policy"] for s in summaries]
    fig, ax = plt.subplots(figsize=(8, 5))
    width = 0.8 / len(summaries)
    for i, summary in enumerate(summaries):
        xs = [j + (i - (len(summaries) - 1) / 2) * width for j in range(len(metrics))]
        bars = ax.bar(xs, [summary[m] for m in metrics], width=width,
                      label=names[i], color=_color(i))
        ax.bar_label(bars, fmt="%.3f", fontsize=8)
    ax.set_xticks(range(len(metrics)))
    ax.set_xticklabels([m.replace("_", " ") for m in metrics])
    ax.set_ylabel("rate")
    ax.set_ylim(0, 1.08)
    ax.set_title("Summary hit rates")
    ax.legend()
    _save(fig, out_dir, "summary_bars", dpi)


def plot_per_request(rows_by_policy: dict[str, list[dict]], out_dir: Path, dpi: int) -> None:
    fig, ax = plt.subplots(figsize=(10, 4.5))
    for i, (name, rows) in enumerate(rows_by_policy.items()):
        xs = [r["request_index"] for r in rows]
        ys = [r["token_hit_rate"] for r in rows]
        ax.plot(xs, ys, drawstyle="steps-post", lw=1.1, alpha=0.8,
                color=_color(i), label=name)
    ax.set_xlabel("request index (timestamp order)")
    ax.set_ylabel("token hit rate")
    ax.set_ylim(-0.02, 1.02)
    ax.set_title("Per-request token hit rate")
    ax.legend(loc="upper right")
    _save(fig, out_dir, "per_request", dpi)


def plot_cumulative(rows_by_policy: dict[str, list[dict]], out_dir: Path, dpi: int) -> None:
    fig, ax = plt.subplots(figsize=(10, 4.5))
    for i, (name, rows) in enumerate(rows_by_policy.items()):
        hit = tok = 0
        xs: list[int] = []
        ys: list[float] = []
        for r in rows:
            hit += r["hit_tokens"]
            tok += r["prompt_tokens"]
            xs.append(r["request_index"])
            ys.append(hit / tok if tok else 0.0)
        ax.plot(xs, ys, lw=1.6, color=_color(i), label=name)
    ax.set_xlabel("request index (timestamp order)")
    ax.set_ylabel("cumulative token hit rate")
    ax.set_ylim(0, 1.0)
    ax.set_title("Running token hit rate over the trace")
    ax.legend(loc="lower right")
    _save(fig, out_dir, "cumulative", dpi)

def plot_tier_breakdown(summaries: list[dict], out_dir: Path, dpi: int) -> None:
    names = [s["policy"] for s in summaries]
    components = [("hbm_hits", "HBM hits", "tab:green"),
                  ("dram_hits", "DRAM hits", "tab:orange"),
                  ("miss_chunks", "misses", "tab:red")]
    xs = list(range(len(names)))
    bottoms = [0.0] * len(names)
    fig, ax = plt.subplots(figsize=(6, 5))
    for key, label, color in components:
        vals = [s[key] / (s["chunk_accesses"] or 1) for s in summaries]
        ax.bar(xs, vals, bottom=bottoms, label=label, color=color, width=0.55)
        bottoms = [b + v for b, v in zip(bottoms, vals)]
    ax.set_xticks(xs)
    ax.set_xticklabels(names)
    ax.set_ylabel("share of chunk accesses")
    ax.set_ylim(0, 1.0)
    ax.set_title("HBM / DRAM / miss composition")
    ax.legend(loc="center left", bbox_to_anchor=(1.02, 0.5))
    _save(fig, out_dir, "tier_breakdown", dpi)


def plot_length_buckets(rows_by_policy: dict[str, list[dict]], cfg: dict,
                        out_dir: Path, dpi: int) -> None:
    edges, labels = _length_edges_labels(cfg["length_bucket_edges"])
    n_pol = len(rows_by_policy)
    fig, ax = plt.subplots(figsize=(8, 5))
    width = 0.8 / n_pol
    for i, (name, rows) in enumerate(rows_by_policy.items()):
        rates = []
        for j, edge in enumerate(edges):
            lower = edges[j - 1] if j else 0.0
            bucket = [r for r in rows if lower < r["prompt_tokens"] <= edge]
            rates.append(_weighted_hit_rate(bucket))
        xs = [j + (i - (n_pol - 1) / 2) * width for j in range(len(edges))]
        bars = ax.bar(xs, rates, width=width, label=name, color=_color(i))
        ax.bar_label(bars, fmt="%.2f", fontsize=8)
    ax.set_xticks(range(len(edges)))
    ax.set_xticklabels(labels)
    ax.set_xlabel("prompt tokens")
    ax.set_ylabel("token hit rate")
    ax.set_ylim(0, 1.05)
    ax.set_title("Token hit rate by prompt-length bucket")
    ax.legend()
    _save(fig, out_dir, "length_buckets", dpi)


def plot_time_buckets(rows_by_policy: dict[str, list[dict]], cfg: dict,
                      out_dir: Path, dpi: int) -> None:
    first_rows = next(iter(rows_by_policy.values()), [])
    if not first_rows:
        return
    tmin = min(r["timestamp"] for r in first_rows)
    tmax = max(r["timestamp"] for r in first_rows)
    if tmax <= tmin:
        print("  time_buckets skipped: all timestamps are identical")
        return
    n = max(1, int(cfg["time_bucket_count"]))
    width_t = (tmax - tmin) / n
    fig, ax = plt.subplots(figsize=(9, 5))
    bar_width = 0.8 / len(rows_by_policy)
    for i, (name, rows) in enumerate(rows_by_policy.items()):
        buckets: list[list[dict]] = [[] for _ in range(n)]
        for r in rows:
            k = min(int((r["timestamp"] - tmin) / width_t), n - 1)
            buckets[k].append(r)
        rates = [_weighted_hit_rate(b) for b in buckets]
        xs = [j + (i - (len(rows_by_policy) - 1) / 2) * bar_width for j in range(n)]
        bars = ax.bar(xs, rates, width=bar_width, label=name, color=_color(i))
        ax.bar_label(bars, fmt="%.2f", fontsize=7)
    ax.set_xticks(range(n))
    ax.set_xticklabels([f"{tmin + (j + 0.5) * width_t:.0f}s" for j in range(n)], fontsize=8)
    ax.set_xlabel("arrival time (bucket midpoints)")
    ax.set_ylabel("token hit rate")
    ax.set_ylim(0, 1.05)
    ax.set_title("Token hit rate over arrival time")
    ax.legend()
    _save(fig, out_dir, "time_buckets", dpi)


def main() -> None:
    cfg = CONFIG
    if not cfg["policies"]:
        raise SystemExit("CONFIG['policies'] is empty; add 'buoy' and/or 'lru'.")
    out_dir = Path(cfg["output_dir"])
    out_dir.mkdir(parents=True, exist_ok=True)

    requests = load_trace(cfg["trace"])
    print(f"Loaded {len(requests)} requests from {cfg['trace']}\n")

    summaries: list[dict] = []
    rows_by_policy: dict[str, list[dict]] = {}
    for name in cfg["policies"]:
        policy = build_policy(name, cfg)
        summary, rows = run_policy(requests, policy, cfg["block_size"])
        summaries.append(summary)
        rows_by_policy[name] = rows
        write_csv(out_dir / f"requests_{name}.csv", rows)
        (out_dir / f"final_state_{name}.json").write_text(
            json.dumps(policy.snapshot(), indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"[{name}] chunk_hit_rate={summary['chunk_hit_rate']:.4f} "
              f"token_hit_rate={summary['token_hit_rate']:.4f} "
              f"(hbm={summary['hbm_hits']}, dram={summary['dram_hits']}, "
              f"miss={summary['miss_chunks']})")
    write_csv(out_dir / "summary.csv", summaries)

    serialisable = {k: (str(v) if isinstance(v, Path) else v) for k, v in cfg.items()}
    (out_dir / "experiment_config.json").write_text(
        json.dumps(serialisable, indent=2, ensure_ascii=False), encoding="utf-8")

    if cfg["plots"] and not HAS_MPL:
        print("\nmatplotlib is not installed; skipping plots.")
    elif cfg["plots"]:
        print("\nGenerating plots:")
        if "summary_bars" in cfg["plots"]:
            plot_summary_bars(summaries, out_dir, cfg["plot_dpi"])
        if "per_request" in cfg["plots"]:
            plot_per_request(rows_by_policy, out_dir, cfg["plot_dpi"])
        if "cumulative" in cfg["plots"]:
            plot_cumulative(rows_by_policy, out_dir, cfg["plot_dpi"])
        if "tier_breakdown" in cfg["plots"]:
            plot_tier_breakdown(summaries, out_dir, cfg["plot_dpi"])
        if "length_buckets" in cfg["plots"]:
            plot_length_buckets(rows_by_policy, cfg, out_dir, cfg["plot_dpi"])
        if "time_buckets" in cfg["plots"]:
            plot_time_buckets(rows_by_policy, cfg, out_dir, cfg["plot_dpi"])

    print(f"\nDone. Results and plots in: {out_dir.resolve()}")


if __name__ == "__main__":
    main()