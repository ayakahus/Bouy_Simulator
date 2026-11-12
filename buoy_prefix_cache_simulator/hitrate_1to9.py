#!/usr/bin/env python3
"""HBM:DRAM = 1:9 tier-hit comparison: Buoy vs LRU vs S3-FIFO vs Belady OPT
on the four official hash-block traces (Qwen TraceA/TraceB, Mooncake
Conv/Tool).

Methodology mirrors hitrate_sml.py: chunk ids are pre-computed once per trace
(parent-chained blake2b-16, block_size=1 semantics: one simulated chunk == one
hash block == 16 real tokens for Qwen / ~500 for Mooncake), then replayed in
file order (= timestamp order, verified monotonic) for every
(capacity, policy) pair. Capacities scale with each trace's unique-chunk
working set W: small=5%W, medium=25%W, large=100%W; HBM:DRAM = 1:9 and
ghost = 4xHBM.

Usage:
    python hitrate_1to9.py worker TRACE_NAME TRACE_FILE CAP_LABEL FRAC
    python hitrate_1to9.py report    # merges partials, draws figs, writes REPORT.md

Outputs (results/tier_1to9/): tier_1to9_summary.csv, REPORT.md, fig1..fig4 PNGs,
partials/*.csv
"""
from __future__ import annotations

import csv
import hashlib
import json
import time
from collections import OrderedDict
from pathlib import Path

from buoy_sim.policies import BuoyPolicy, LRUPolicy, OPTPolicy, S3FIFOPolicy

BASE = Path(__file__).resolve().parent
TRACE_DIR = BASE / "trace"
OUT = BASE / "results" / "tier_1to9"
PARTIALS = OUT / "partials"
TRACES = {
    "QwenTraceA": "qwen_traceA_sim.jsonl",
    "QwenTraceB": "qwen_traceB_sim.jsonl",
    "MooncakeConv": "mooncake_conv_sim.jsonl",
    "MooncakeTool": "mooncake_tool_sim.jsonl",
}
CAP_LABELS = (("small", 0.05), ("medium", 0.25), ("large", 1.00))
POLICIES = ("buoy", "lru", "s3fifo", "opt")
HBM_FRAC = 0.1  # 1:9 HBM:DRAM


class FastGhostBuoy(BuoyPolicy):
    """BuoyPolicy with an O(1) ghost-FIFO backing store (see hitrate_sml.py)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ghost = OrderedDict()  # key = chunk id; insertion order = FIFO age

    def _ghost_contains(self, cid):
        return cid in self.ghost

    def _ghost_add(self, cid):
        if self.gcap <= 0:
            return
        if cid in self.ghost:          # refresh: newest end of the FIFO
            self.ghost.move_to_end(cid)
        else:
            self.ghost[cid] = None
        while len(self.ghost) > self.gcap:
            old, _ = self.ghost.popitem(last=False)
            self.ghost_set.discard(old)

    def _ghost_remove(self, cid):
        if cid in self.ghost:
            del self.ghost[cid]
            self.ghost_set.discard(cid)


class FastGhostS3FIFO(S3FIFOPolicy):
    """S3FIFOPolicy with an OrderedDict ghost (same rationale as FastGhostBuoy)."""

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.ghost = OrderedDict()

    def _ghost_contains(self, cid):
        return cid in self.ghost

    def _ghost_add(self, cid):
        if self.gcap <= 0:
            return
        if cid in self.ghost:
            self.ghost.move_to_end(cid)
        else:
            self.ghost[cid] = None
        while len(self.ghost) > self.gcap:
            old, _ = self.ghost.popitem(last=False)
            self.ghost_set.discard(old)

    def _ghost_remove(self, cid):
        if cid in self.ghost:
            del self.ghost[cid]
            self.ghost_set.discard(cid)

def precompute_chunks(path: Path):
    """Return per-request lists of 16-byte chunk ids (block_size=1 semantics)."""
    chunk_reqs = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            r = json.loads(line)
            ids = r["tokens"]
            parent = "ROOT"
            cids = []
            ap = cids.append
            for hs in ids:
                payload = b'["' + parent.encode() + b'","' + hs.encode() + b'"]'
                cid = hashlib.blake2b(payload, digest_size=16).digest()
                ap(cid)
                parent = cid.hex()
            chunk_reqs.append(cids)
    return chunk_reqs


def build_policy(name: str, hbm: int, dram: int, ghost: int, constraint: bool):
    if name == "buoy":
        return FastGhostBuoy(hbm, dram, ghost, request_constraint=constraint)
    if name == "lru":
        return LRUPolicy(hbm, dram, request_constraint=constraint)
    if name == "s3fifo":
        return FastGhostS3FIFO(hbm, dram, ghost, request_constraint=constraint)
    if name == "opt":
        return OPTPolicy(hbm, dram, request_constraint=constraint)
    raise ValueError(f"unknown policy {name!r}")


def worker(trace_name: str, fname: str, cap_label: str, frac: float) -> None:
    path = TRACE_DIR / fname
    chunk_reqs = precompute_chunks(path)
    n_reqs = len(chunk_reqs)
    n_blocks = sum(len(c) for c in chunk_reqs)
    allc = [c for req in chunk_reqs for c in req]
    W = len(set(allc))
    del allc
    total = max(4, round(W * frac))
    hbm = max(1, round(total * HBM_FRAC))
    dram = max(1, total - hbm)
    ghost = max(1, hbm * 4)

    rows = []
    for pname in POLICIES:
        for constraint in (False, True):
            policy = build_policy(pname, hbm, dram, ghost, constraint)
            if hasattr(policy, "prepare"):
                # Offline policies (Belady OPT) need the full access sequence.
                policy.prepare(chunk_reqs)
            t0 = time.time()
            for cids in chunk_reqs:
                if not cids:
                    continue
                policy.access_request([(c, i + 1, 1) for i, c in enumerate(cids)])
            elapsed = time.time() - t0
            s = policy.stats
            hit_chunks = s["hbm_hits"] + s["dram_hits"]
            rows.append({
                "trace": trace_name, "requests": n_reqs, "block_accesses": n_blocks,
                "cap_label": cap_label, "total_capacity": total,
                "hbm_capacity": hbm, "dram_capacity": dram, "ghost_capacity": ghost,
                "policy": pname, "constraint": "on" if constraint else "off",
                "chunk_hit_rate": round(hit_chunks / n_blocks, 6) if n_blocks else 0.0,
                "hbm_hits": s["hbm_hits"], "dram_hits": s["dram_hits"], "misses": s["misses"],
                "hbm_hit_rate": round(s["hbm_hits"] / n_blocks, 6) if n_blocks else 0.0,
                "dram_hit_rate": round(s["dram_hits"] / n_blocks, 6) if n_blocks else 0.0,
                "hbm_share_of_hits": round(s["hbm_hits"] / hit_chunks, 6) if hit_chunks else 0.0,
                "dram_share_of_hits": round(s["dram_hits"] / hit_chunks, 6) if hit_chunks else 0.0,
                "admitted_chunks": s["admitted_chunks"], "rejected_chunks": s["rejected_chunks"],
                "demotions": s["demotions"], "evictions": s["evictions"],
                "ghost_hits": s["ghost_hits"], "second_chances": s["second_chances"],
                "constraint_waived": s.get("constraint_waived", 0),
                "unique_chunks": W, "elapsed_s": round(elapsed, 1),
            })
            print(f"[{trace_name}/{cap_label}] {pname:6s} c={'on ' if constraint else 'off'} "
                  f"hit={rows[-1]['chunk_hit_rate']:.4f} "
                  f"hbm_share={rows[-1]['hbm_share_of_hits']:.3f} "
                  f"dram_share={rows[-1]['dram_share_of_hits']:.3f} ({elapsed:.0f}s)", flush=True)

    PARTIALS.mkdir(parents=True, exist_ok=True)
    part = PARTIALS / f"partial_{trace_name}_{cap_label}.csv"
    with part.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)
    print(f"wrote {part}", flush=True)

def _pct(x) -> str:
    return f"{float(x) * 100:.2f}%"


def _int(x) -> str:
    return f"{int(x):,}"


def plot(rows) -> None:
    """Draw the four comparison figures next to the merged CSV."""
    import matplotlib
    matplotlib.use("Agg")  # headless-safe
    import matplotlib.pyplot as plt

    POLICY_COLORS = {"buoy": "tab:blue", "lru": "tab:orange", "s3fifo": "tab:green", "opt": "tab:red"}
    HBM_C, DRAM_C = "#d73027", "#4575b4"  # tier colors: HBM warm, DRAM cool
    caps = [c for c, _ in CAP_LABELS]
    traces = list(TRACES)
    by_tc = {}
    for r in rows:
        by_tc.setdefault((r["trace"], r["cap_label"]), {})[r["policy"]] = r
    ymax = max(float(r["chunk_hit_rate"]) for r in rows) * 100 * 1.15

    # fig1: chunk hit rate, grouped bars per capacity point
    fig, axes = plt.subplots(2, 2, figsize=(12, 8), squeeze=False)
    for ax, t in zip(axes.flat, traces):
        width = 0.8 / len(POLICIES)
        for i, p in enumerate(POLICIES):
            vals = [float(by_tc[(t, c)][p]["chunk_hit_rate"]) * 100 for c in caps]
            pos = [j + (i - (len(POLICIES) - 1) / 2) * width for j in range(len(caps))]
            ax.bar(pos, vals, width=width, label=p, color=POLICY_COLORS[p])
        ax.set_xticks(range(len(caps)))
        ax.set_xticklabels([f"{c}\n(total {int(by_tc[(t, c)]['buoy']['total_capacity']):,})"
                            for c in caps], fontsize=9)
        ax.set_title(t)
        ax.set_ylabel("chunk hit rate (%)")
        ax.set_ylim(0, ymax)
        ax.grid(axis="y", alpha=0.3)
    axes.flat[0].legend(loc="upper left", fontsize=9)
    fig.suptitle("Chunk hit rate by capacity (1:9, request-level constraint)")
    fig.tight_layout()
    fig.savefig(OUT / "fig1_chunk_hit_rate.png", dpi=200, bbox_inches="tight")
    plt.close(fig)

    # fig2: which tier serves the hits, 100% stacked horizontal bars
    fig, axes = plt.subplots(2, 2, figsize=(12, 9), squeeze=False)
    for ax, t in zip(axes.flat, traces):
        ylabels, hbm, dram = [], [], []
        for c in caps:
            for p in POLICIES:
                r = by_tc[(t, c)][p]
                ylabels.append(f"{c} / {p}")
                hbm.append(float(r["hbm_share_of_hits"]) * 100)
                dram.append(float(r["dram_share_of_hits"]) * 100)
        y = list(range(len(ylabels)))
        ax.barh(y, hbm, color=HBM_C, label="hits from HBM")
        ax.barh(y, dram, left=hbm, color=DRAM_C, label="hits from DRAM")
        ax.set_yticks(y)
        ax.set_yticklabels(ylabels, fontsize=8)
        ax.invert_yaxis()
        ax.set_xlim(0, 100)
        ax.set_xlabel("share of hits (%)")
        ax.set_title(t)
        ax.grid(axis="x", alpha=0.3)
    axes.flat[0].legend(loc="lower right", fontsize=8)
    fig.suptitle("Which tier serves the hits (100% stacked)")
    fig.tight_layout()
    fig.savefig(OUT / "fig2_tier_share.png", dpi=200, bbox_inches="tight")
    plt.close(fig)

    # fig3: share of hits served from HBM, per policy across capacities
    fig, axes = plt.subplots(2, 2, figsize=(12, 8), squeeze=False)
    for ax, t in zip(axes.flat, traces):
        for p in POLICIES:
            vals = [float(by_tc[(t, c)][p]["hbm_share_of_hits"]) * 100 for c in caps]
            ax.plot(caps, vals, marker="o", label=p, color=POLICY_COLORS[p])
        ax.set_title(t)
        ax.set_ylabel("share of hits from HBM (%)")
        ax.set_ylim(-3, 105)
        ax.grid(alpha=0.3)
    axes.flat[0].legend(fontsize=9)
    fig.suptitle("Share of hits served from HBM (lower = hotter data sinks to DRAM)")
    fig.tight_layout()
    fig.savefig(OUT / "fig3_hbm_share.png", dpi=200, bbox_inches="tight")
    plt.close(fig)

    # fig4: HBM->DRAM demotion write traffic, symlog scale
    fig, axes = plt.subplots(2, 2, figsize=(12, 8), squeeze=False)
    for ax, t in zip(axes.flat, traces):
        width = 0.8 / len(POLICIES)
        for i, p in enumerate(POLICIES):
            vals = [int(by_tc[(t, c)][p]["demotions"]) for c in caps]
            pos = [j + (i - (len(POLICIES) - 1) / 2) * width for j in range(len(caps))]
            ax.bar(pos, vals, width=width, label=p, color=POLICY_COLORS[p])
        ax.set_xticks(range(len(caps)))
        ax.set_xticklabels(caps)
        ax.set_yscale("symlog")
        ax.set_title(t)
        ax.set_ylabel("HBM->DRAM demotions (symlog)")
        ax.grid(axis="y", alpha=0.3)
    axes.flat[0].legend(fontsize=9)
    fig.suptitle("HBM->DRAM demotion write traffic (lower = fewer tier writes)")
    fig.tight_layout()
    fig.savefig(OUT / "fig4_demotions.png", dpi=200, bbox_inches="tight")
    plt.close(fig)
    print("wrote fig1..fig4 PNGs", flush=True)


def plot_delta(rows_on, rows_off) -> None:
    """fig5: per-policy hit-rate delta (constrained - unconstrained)."""
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    POLICY_COLORS = {"buoy": "tab:blue", "lru": "tab:orange", "s3fifo": "tab:green", "opt": "tab:red"}
    caps = [c for c, _ in CAP_LABELS]
    traces = list(TRACES)
    on, off = {}, {}
    for r in rows_on:
        on[(r["trace"], r["cap_label"], r["policy"])] = float(r["chunk_hit_rate"]) * 100
    for r in rows_off:
        off[(r["trace"], r["cap_label"], r["policy"])] = float(r["chunk_hit_rate"]) * 100

    fig, axes = plt.subplots(2, 2, figsize=(12, 8), squeeze=False)
    for ax, t in zip(axes.flat, traces):
        width = 0.8 / len(POLICIES)
        for i, p in enumerate(POLICIES):
            vals = [on[(t, c, p)] - off[(t, c, p)] for c in caps]
            pos = [j + (i - (len(POLICIES) - 1) / 2) * width for j in range(len(caps))]
            ax.bar(pos, vals, width=width, label=p, color=POLICY_COLORS[p])
        ax.axhline(0, color="black", linewidth=0.8)
        ax.set_xticks(range(len(caps)))
        ax.set_xticklabels(caps)
        ax.set_title(t)
        ax.set_ylabel("hit rate: constrained - unconstrained (pp)")
        ax.grid(axis="y", alpha=0.3)
    axes.flat[0].legend(fontsize=9)
    fig.suptitle("Request-level constraint impact on chunk hit rate")
    fig.tight_layout()
    fig.savefig(OUT / "fig5_constraint_delta.png", dpi=200, bbox_inches="tight")
    plt.close(fig)
    print("wrote fig5 PNG", flush=True)


def report() -> None:
    parts = sorted(PARTIALS.glob("partial_*.csv"))
    expected = {f"partial_{t}_{c}.csv" for t in TRACES for c, _ in CAP_LABELS}
    missing = expected - {p.name for p in parts}
    if missing:
        raise SystemExit(f"missing partial results: {sorted(missing)}; run workers first")

    rows = []
    for p in parts:
        with p.open(newline="", encoding="utf-8") as f:
            rows.extend(csv.DictReader(f))
    t_order = {t: i for i, t in enumerate(TRACES)}
    c_order = {c: i for i, (c, _) in enumerate(CAP_LABELS)}
    p_order = {p: i for i, p in enumerate(POLICIES)}
    rows.sort(key=lambda r: (t_order[r["trace"]], c_order[r["cap_label"]], p_order[r["policy"]]))

    csv_path = OUT / "tier_1to9_summary.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader()
        w.writerows(rows)

    rows_on = [r for r in rows if r["constraint"] == "on"]
    rows_off = [r for r in rows if r["constraint"] == "off"]
    plot(rows_on)
    plot_delta(rows_on, rows_off)

    meta = {}
    for r in rows:
        meta.setdefault(r["trace"], r)

    L = []
    L.append("# HBM:DRAM = 1:9 分层前缀缓存命中率报告")
    L.append("")
    L.append("- **算法**:Buoy / LRU / S3-FIFO(`buoy_sim/policies.py`)")
    L.append("- **分层比例**:HBM:DRAM = 1:9(S3-FIFO 中 HBM 充当 small FIFO,DRAM 充当 main FIFO)")
    L.append("- **ghost 容量**:4×HBM(buoy 与 s3fifo 使用;LRU 无 ghost)")
    L.append("- **容量点**:small / medium / large = 5% / 25% / 100% × 各轨迹唯一块工作集 W")
    L.append("- **重放语义**:1 个模拟 chunk = 1 个 hash 块(Qwen 16 真实 token/块,Mooncake ≈500 token/块),`block_size=1`")
    L.append("- **请求级约束(主结果)**:请求处理期间其全部 chunk 免于驱逐(层间迁移允许),"
             "缺失 chunk 强制准入(Buoy 深度准入失效);N > 总容量的请求豁免并计入 constraint_waived。"
             "报告同时给出无约束基线对比(见 §3)")
    L.append("- **OPT(Belady MIN)**:离线最优基线——对总容量(hbm+dram)做严格 MIN 驱逐,"
             "oracle 贪心分层(HBM 优先持有下次访问最早的 chunk,只拉不推近似);"
             "命中率为同容量所有策略的理论上界")
    L.append("- **到达顺序**:文件序(已验证 4 条轨迹时间戳单调)")
    L.append(f"- **原始数据**:`{csv_path.name}`(含全部机制统计:demotions / evictions / ghost_hits / second_chances 等)")
    L.append("")
    L.append("## 1. 轨迹概况")
    L.append("")
    L.append("| 轨迹 | 请求数 | 块访问数 | 唯一块 W |")
    L.append("|---|---|---|---|")
    for t in TRACES:
        m = meta[t]
        L.append(f"| {t} | {_int(m['requests'])} | {_int(m['block_accesses'])} | {_int(m['unique_chunks'])} |")
    L.append("")

    L.append("## 2. 总块命中率(chunk hit rate)")
    L.append("")
    L.append("| 轨迹 | 容量点 | 总容量 | Buoy | LRU | S3-FIFO | OPT |")
    L.append("|---|---|---|---|---|---|---|")
    by_tc = {}
    for r in rows_on:
        by_tc.setdefault((r["trace"], r["cap_label"]), {})[r["policy"]] = r
    by_tc_off = {}
    for r in rows_off:
        by_tc_off.setdefault((r["trace"], r["cap_label"]), {})[r["policy"]] = r
    for t in TRACES:
        for c, _ in CAP_LABELS:
            g = by_tc[(t, c)]
            L.append(f"| {t} | {c} | {_int(g['buoy']['total_capacity'])} "
                     f"| {_pct(g['buoy']['chunk_hit_rate'])} | {_pct(g['lru']['chunk_hit_rate'])} "
                     f"| {_pct(g['s3fifo']['chunk_hit_rate'])} | {_pct(g['opt']['chunk_hit_rate'])} |")
    L.append("")

    L.append("## 3. 请求级约束 vs 无约束(总块命中率)")
    L.append("")
    L.append("约束语义:请求处理期间其全部 chunk 免于驱逐(层间迁移允许),缺失 chunk 强制准入;N > 总容量的请求豁免。")
    L.append("")
    L.append("| 轨迹 | 容量点 | 策略 | 无约束 | 有约束 | Δ (pp) |")
    L.append("|---|---|---|---|---|---|")
    for t in TRACES:
        for c, _ in CAP_LABELS:
            for p in POLICIES:
                off_r = float(by_tc_off[(t, c)][p]["chunk_hit_rate"]) * 100
                on_r = float(by_tc[(t, c)][p]["chunk_hit_rate"]) * 100
                L.append(f"| {t} | {c} | {p} | {off_r:.2f}% | {on_r:.2f}% | {on_r - off_r:+.2f} |")
    L.append("")

    L.append("## 4. HBM / DRAM 命中分布(有约束)")
    L.append("")
    L.append("命中率 = 该层命中数 / 总块访问数;命中份额 = 该层命中数 / 全部命中(两者之和为 100%)。")
    L.append("")
    for t in TRACES:
        L.append(f"### {t}")
        L.append("")
        L.append("| 容量点 | 策略 | HBM 命中率 | DRAM 命中率 | 命中来自 HBM | 命中来自 DRAM |")
        L.append("|---|---|---|---|---|---|")
        for c, _ in CAP_LABELS:
            for p in POLICIES:
                r = by_tc[(t, c)][p]
                L.append(f"| {c} | {p} | {_pct(r['hbm_hit_rate'])} | {_pct(r['dram_hit_rate'])} "
                         f"| {_pct(r['hbm_share_of_hits'])} | {_pct(r['dram_share_of_hits'])} |")
        L.append("")

    L.append("## 5. 缓存机制统计(medium 容量点,有约束)")
    L.append("")
    L.append("| 轨迹 | 策略 | 准入 | 拒绝 | HBM→DRAM 降配 | 淘汰 | ghost 命中 | 二次机会 | 约束豁免 |")
    L.append("|---|---|---|---|---|---|---|---|---|")
    for t in TRACES:
        for p in POLICIES:
            r = by_tc[(t, "medium")][p]
            L.append(f"| {t} | {p} | {_int(r['admitted_chunks'])} | {_int(r['rejected_chunks'])} "
                     f"| {_int(r['demotions'])} | {_int(r['evictions'])} "
                     f"| {_int(r['ghost_hits'])} | {_int(r['second_chances'])} | {_int(r['constraint_waived'])} |")
    L.append("")

    L.append("## 6. 对比图")
    L.append("")
    L.append("![各容量点总命中率(有约束)](fig1_chunk_hit_rate.png)")
    L.append("")
    L.append("![命中来自哪一层(100% 堆叠,有约束)](fig2_tier_share.png)")
    L.append("")
    L.append("![HBM 承担的命中份额(有约束)](fig3_hbm_share.png)")
    L.append("")
    L.append("![HBM→DRAM 降配写流量(symlog,有约束)](fig4_demotions.png)")
    L.append("")
    L.append("![请求级约束对命中率的影响](fig5_constraint_delta.png)")
    L.append("")

    L.append("## 7. 观察与结论")
    L.append("")
    for t in TRACES:
        for c, _ in CAP_LABELS:
            g = by_tc[(t, c)]
            opt_r = float(g["opt"]["chunk_hit_rate"])
            online = max((p for p in POLICIES if p != "opt"),
                         key=lambda p: float(g[p]["chunk_hit_rate"]))
            on_r = float(g[online]["chunk_hit_rate"])
            L.append(f"- **{t}/{c}**:OPT(约束下){_pct(opt_r)};在线最佳 {_pct(on_r)}(by {online}),"
                     f"距最优 {(opt_r - on_r) * 100:.2f} pp;S3-FIFO 为 OPT(约束下)的 "
                     f"{float(g['s3fifo']['chunk_hit_rate']) / opt_r * 100:.1f}%,"
                     f"其命中 {_pct(g['s3fifo']['dram_share_of_hits'])} 来自 DRAM")
    L.append("")
    lru_dem = sum(int(by_tc[(t, "medium")]["lru"]["demotions"]) for t in TRACES)
    s3_dem = sum(int(by_tc[(t, "medium")]["s3fifo"]["demotions"]) for t in TRACES)
    buoy_dem = sum(int(by_tc[(t, "medium")]["buoy"]["demotions"]) for t in TRACES)
    opt_dem = sum(int(by_tc[(t, "medium")]["opt"]["demotions"]) for t in TRACES)
    L.append(f"- **HBM→DRAM 降配流量**(medium,4 轨迹求和):LRU {_int(lru_dem)},"
             f"Buoy {_int(buoy_dem)},S3-FIFO {_int(s3_dem)},OPT(oracle) {_int(opt_dem)}"
             + (f";S3-FIFO 仅为 LRU 的 {s3_dem / lru_dem:.1%}" if lru_dem else ""))
    s3_hbm = sum(float(by_tc[(t, "medium")]["s3fifo"]["hbm_share_of_hits"]) for t in TRACES) / len(TRACES)
    lru_hbm = sum(float(by_tc[(t, "medium")]["lru"]["hbm_share_of_hits"]) for t in TRACES) / len(TRACES)
    buoy_hbm = sum(float(by_tc[(t, "medium")]["buoy"]["hbm_share_of_hits"]) for t in TRACES) / len(TRACES)
    opt_hbm = sum(float(by_tc[(t, "medium")]["opt"]["hbm_share_of_hits"]) for t in TRACES) / len(TRACES)
    L.append(f"- **命中热度的分层分布**(medium 平均):Buoy 命中的 {_pct(buoy_hbm)} 落在 HBM,"
             f"LRU {_pct(lru_hbm)},S3-FIFO 仅 {_pct(s3_hbm)},"
             f"OPT 的 oracle 放置为 {_pct(opt_hbm)}(HBM 命中份额的上界)—— "
             "S3-FIFO 把 HBM 当作短试用期,热点迅速沉淀到 DRAM,HBM 写流量随之最小化")
    buoy_rej = sum(int(by_tc_off[(t, "medium")]["buoy"]["rejected_chunks"]) for t in TRACES)
    L.append(f"- **Buoy 的深度准入**在无约束 medium 点共拒绝 {_int(buoy_rej)} 个 chunk;"
             f"请求级约束下强制准入使其失效(有约束时拒绝数为 0),一次性访问随之进入缓存")
    deltas = {p: sum(float(by_tc[(t, "medium")][p]["chunk_hit_rate"]) -
                     float(by_tc_off[(t, "medium")][p]["chunk_hit_rate"]) for t in TRACES) / len(TRACES) * 100
              for p in POLICIES}
    L.append("- **约束的平均命中率变化**(medium,4 轨迹平均):"
             + ", ".join(f"{p} {deltas[p]:+.2f} pp" for p in POLICIES)
             + "(负值 = 约束限制了受害者选择并引入强制准入的代价)")
    L.append("")
    L.append("注 1:OPT 的 demotion 为 oracle 换入换出流量(每次 DRAM 命中至多触发一次),"
             "与在线策略“溢出时降配”的口径不同,算法间比较以命中率与分层命中份额为主。")
    L.append("注 2:约束模式下 OPT 不再是严格 MIN 上界(pin 限制受害者选择 + 强制准入偏离纯 MIN),"
             "无约束 OPT(§3)仍为全局命中率上界。")

    (OUT / "REPORT.md").write_text("\n".join(L) + "\n", encoding="utf-8")
    print(f"wrote {csv_path}\nwrote {OUT / 'REPORT.md'}")


def main():
    import sys
    if len(sys.argv) >= 2 and sys.argv[1] == "report":
        report()
        return
    if len(sys.argv) >= 2 and sys.argv[1] == "worker":
        worker(sys.argv[2], sys.argv[3], sys.argv[4], float(sys.argv[5]))
        return
    raise SystemExit(__doc__)


if __name__ == "__main__":
    main()



