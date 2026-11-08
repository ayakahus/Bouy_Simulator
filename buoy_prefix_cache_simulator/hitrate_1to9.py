#!/usr/bin/env python3
"""HBM:DRAM = 1:9 tier-hit comparison: Buoy vs LRU vs S3-FIFO on the four
official hash-block traces (Qwen TraceA/TraceB, Mooncake Conv/Tool).

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

from buoy_sim.policies import BuoyPolicy, LRUPolicy, S3FIFOPolicy

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
POLICIES = ("buoy", "lru", "s3fifo")
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


def build_policy(name: str, hbm: int, dram: int, ghost: int):
    if name == "buoy":
        return FastGhostBuoy(hbm, dram, ghost)
    if name == "lru":
        return LRUPolicy(hbm, dram)
    if name == "s3fifo":
        return FastGhostS3FIFO(hbm, dram, ghost)
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
        policy = build_policy(pname, hbm, dram, ghost)
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
            "policy": pname,
            "chunk_hit_rate": round(hit_chunks / n_blocks, 6) if n_blocks else 0.0,
            "hbm_hits": s["hbm_hits"], "dram_hits": s["dram_hits"], "misses": s["misses"],
            "hbm_hit_rate": round(s["hbm_hits"] / n_blocks, 6) if n_blocks else 0.0,
            "dram_hit_rate": round(s["dram_hits"] / n_blocks, 6) if n_blocks else 0.0,
            "hbm_share_of_hits": round(s["hbm_hits"] / hit_chunks, 6) if hit_chunks else 0.0,
            "dram_share_of_hits": round(s["dram_hits"] / hit_chunks, 6) if hit_chunks else 0.0,
            "admitted_chunks": s["admitted_chunks"], "rejected_chunks": s["rejected_chunks"],
            "demotions": s["demotions"], "evictions": s["evictions"],
            "ghost_hits": s["ghost_hits"], "second_chances": s["second_chances"],
            "unique_chunks": W, "elapsed_s": round(elapsed, 1),
        })
        print(f"[{trace_name}/{cap_label}] {pname:6s} hit={rows[-1]['chunk_hit_rate']:.4f} "
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

    POLICY_COLORS = {"buoy": "tab:blue", "lru": "tab:orange", "s3fifo": "tab:green"}
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
        width = 0.26
        for i, p in enumerate(POLICIES):
            vals = [float(by_tc[(t, c)][p]["chunk_hit_rate"]) * 100 for c in caps]
            pos = [j + (i - 1) * width for j in range(len(caps))]
            ax.bar(pos, vals, width=width, label=p, color=POLICY_COLORS[p])
        ax.set_xticks(range(len(caps)))
        ax.set_xticklabels([f"{c}\n(total {int(by_tc[(t, c)]['buoy']['total_capacity']):,})"
                            for c in caps], fontsize=9)
        ax.set_title(t)
        ax.set_ylabel("chunk hit rate (%)")
        ax.set_ylim(0, ymax)
        ax.grid(axis="y", alpha=0.3)
    axes.flat[0].legend(loc="upper left", fontsize=9)
    fig.suptitle("Chunk hit rate by capacity (HBM:DRAM = 1:9)")
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
        width = 0.26
        for i, p in enumerate(POLICIES):
            vals = [int(by_tc[(t, c)][p]["demotions"]) for c in caps]
            pos = [j + (i - 1) * width for j in range(len(caps))]
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

    plot(rows)

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
    L.append("| 轨迹 | 容量点 | 总容量 | Buoy | LRU | S3-FIFO |")
    L.append("|---|---|---|---|---|---|")
    by_tc = {}
    for r in rows:
        by_tc.setdefault((r["trace"], r["cap_label"]), {})[r["policy"]] = r
    for t in TRACES:
        for c, _ in CAP_LABELS:
            g = by_tc[(t, c)]
            L.append(f"| {t} | {c} | {_int(g['buoy']['total_capacity'])} "
                     f"| {_pct(g['buoy']['chunk_hit_rate'])} | {_pct(g['lru']['chunk_hit_rate'])} "
                     f"| {_pct(g['s3fifo']['chunk_hit_rate'])} |")
    L.append("")

    L.append("## 3. HBM / DRAM 命中分布")
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

    L.append("## 4. 缓存机制统计(medium 容量点)")
    L.append("")
    L.append("| 轨迹 | 策略 | 准入 | 拒绝 | HBM→DRAM 降配 | 淘汰 | ghost 命中 | 二次机会 |")
    L.append("|---|---|---|---|---|---|---|---|")
    for t in TRACES:
        for p in POLICIES:
            r = by_tc[(t, "medium")][p]
            L.append(f"| {t} | {p} | {_int(r['admitted_chunks'])} | {_int(r['rejected_chunks'])} "
                     f"| {_int(r['demotions'])} | {_int(r['evictions'])} "
                     f"| {_int(r['ghost_hits'])} | {_int(r['second_chances'])} |")
    L.append("")

    L.append("## 5. 对比图")
    L.append("")
    L.append("![各容量点总命中率](fig1_chunk_hit_rate.png)")
    L.append("")
    L.append("![命中来自哪一层(100% 堆叠)](fig2_tier_share.png)")
    L.append("")
    L.append("![HBM 承担的命中份额](fig3_hbm_share.png)")
    L.append("")
    L.append("![HBM→DRAM 降配写流量(symlog)](fig4_demotions.png)")
    L.append("")

    L.append("## 6. 观察与结论")
    L.append("")
    for t in TRACES:
        for c, _ in CAP_LABELS:
            g = by_tc[(t, c)]
            best = max(POLICIES, key=lambda p: float(g[p]["chunk_hit_rate"]))
            L.append(f"- **{t}/{c}**:最佳命中率 {_pct(g[best]['chunk_hit_rate'])}(by {best});"
                     f"S3-FIFO 命中 {_pct(g['s3fifo']['chunk_hit_rate'])},"
                     f"其中 {_pct(g['s3fifo']['dram_share_of_hits'])} 来自 DRAM")
    L.append("")
    lru_dem = sum(int(by_tc[(t, "medium")]["lru"]["demotions"]) for t in TRACES)
    s3_dem = sum(int(by_tc[(t, "medium")]["s3fifo"]["demotions"]) for t in TRACES)
    buoy_dem = sum(int(by_tc[(t, "medium")]["buoy"]["demotions"]) for t in TRACES)
    L.append(f"- **HBM→DRAM 降配流量**(medium,4 轨迹求和):LRU {_int(lru_dem)},"
             f"Buoy {_int(buoy_dem)},S3-FIFO {_int(s3_dem)}"
             + (f";S3-FIFO 仅为 LRU 的 {s3_dem / lru_dem:.1%}" if lru_dem else ""))
    s3_hbm = sum(float(by_tc[(t, "medium")]["s3fifo"]["hbm_share_of_hits"]) for t in TRACES) / len(TRACES)
    lru_hbm = sum(float(by_tc[(t, "medium")]["lru"]["hbm_share_of_hits"]) for t in TRACES) / len(TRACES)
    buoy_hbm = sum(float(by_tc[(t, "medium")]["buoy"]["hbm_share_of_hits"]) for t in TRACES) / len(TRACES)
    L.append(f"- **命中热度的分层分布**(medium 平均):Buoy 命中的 {_pct(buoy_hbm)} 落在 HBM,"
             f"LRU {_pct(lru_hbm)},S3-FIFO 仅 {_pct(s3_hbm)} —— S3-FIFO 把 HBM 当作短试用期,"
             f"热点迅速沉淀到 DRAM,HBM 写流量随之最小化")
    buoy_rej = sum(int(by_tc[(t, "medium")]["buoy"]["rejected_chunks"]) for t in TRACES)
    L.append(f"- **Buoy 的深度准入**在 medium 点共拒绝 {_int(buoy_rej)} 个 chunk,"
             f"显著减少进入缓存的一次性访问(对照 S3-FIFO/LRU 全量准入)")

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



