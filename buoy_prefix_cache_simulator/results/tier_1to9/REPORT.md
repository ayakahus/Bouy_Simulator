# HBM:DRAM = 1:9 分层前缀缓存命中率报告

- **算法**:Buoy / LRU / S3-FIFO(`buoy_sim/policies.py`)
- **分层比例**:HBM:DRAM = 1:9(S3-FIFO 中 HBM 充当 small FIFO,DRAM 充当 main FIFO)
- **ghost 容量**:4×HBM(buoy 与 s3fifo 使用;LRU 无 ghost)
- **容量点**:small / medium / large = 5% / 25% / 100% × 各轨迹唯一块工作集 W
- **重放语义**:1 个模拟 chunk = 1 个 hash 块(Qwen 16 真实 token/块,Mooncake ≈500 token/块),`block_size=1`
- **到达顺序**:文件序(已验证 4 条轨迹时间戳单调)
- **原始数据**:`tier_1to9_summary.csv`(含全部机制统计:demotions / evictions / ghost_hits / second_chances 等)

## 1. 轨迹概况

| 轨迹 | 请求数 | 块访问数 | 唯一块 W |
|---|---|---|---|
| QwenTraceA | 43,058 | 6,293,976 | 2,656,400 |
| QwenTraceB | 172,800 | 9,956,137 | 4,638,378 |
| MooncakeConv | 12,031 | 288,500 | 182,790 |
| MooncakeTool | 23,608 | 409,616 | 183,300 |

## 2. 总块命中率(chunk hit rate)

| 轨迹 | 容量点 | 总容量 | Buoy | LRU | S3-FIFO |
|---|---|---|---|---|---|
| QwenTraceA | small | 132,820 | 33.90% | 41.28% | 31.08% |
| QwenTraceA | medium | 664,100 | 44.29% | 56.23% | 49.21% |
| QwenTraceA | large | 2,656,400 | 45.36% | 57.79% | 55.36% |
| QwenTraceB | small | 231,919 | 52.23% | 51.29% | 51.95% |
| QwenTraceB | medium | 1,159,594 | 52.77% | 53.01% | 52.60% |
| QwenTraceB | large | 4,638,378 | 52.85% | 53.41% | 53.02% |
| MooncakeConv | small | 9,140 | 17.98% | 19.54% | 12.28% |
| MooncakeConv | medium | 45,698 | 28.77% | 35.35% | 25.15% |
| MooncakeConv | large | 182,790 | 29.45% | 36.64% | 32.77% |
| MooncakeTool | small | 9,165 | 42.37% | 44.94% | 39.81% |
| MooncakeTool | medium | 45,825 | 48.89% | 55.09% | 48.68% |
| MooncakeTool | large | 183,300 | 49.15% | 55.25% | 53.71% |

## 3. HBM / DRAM 命中分布

命中率 = 该层命中数 / 总块访问数;命中份额 = 该层命中数 / 全部命中(两者之和为 100%)。

### QwenTraceA

| 容量点 | 策略 | HBM 命中率 | DRAM 命中率 | 命中来自 HBM | 命中来自 DRAM |
|---|---|---|---|---|---|
| small | buoy | 18.84% | 15.06% | 55.57% | 44.43% |
| small | lru | 14.89% | 26.39% | 36.06% | 63.94% |
| small | s3fifo | 1.07% | 30.02% | 3.43% | 96.57% |
| medium | buoy | 27.42% | 16.87% | 61.91% | 38.09% |
| medium | lru | 29.36% | 26.87% | 52.22% | 47.78% |
| medium | s3fifo | 14.17% | 35.04% | 28.79% | 71.21% |
| large | buoy | 32.23% | 13.13% | 71.06% | 28.94% |
| large | lru | 50.21% | 7.59% | 86.87% | 13.13% |
| large | s3fifo | 30.80% | 24.56% | 55.64% | 44.36% |

### QwenTraceB

| 容量点 | 策略 | HBM 命中率 | DRAM 命中率 | 命中来自 HBM | 命中来自 DRAM |
|---|---|---|---|---|---|
| small | buoy | 50.40% | 1.84% | 96.49% | 3.51% |
| small | lru | 42.73% | 8.56% | 83.32% | 16.68% |
| small | s3fifo | 1.23% | 50.72% | 2.36% | 97.64% |
| medium | buoy | 51.79% | 0.98% | 98.14% | 1.86% |
| medium | lru | 49.78% | 3.23% | 93.91% | 6.09% |
| medium | s3fifo | 2.71% | 49.89% | 5.15% | 94.85% |
| large | buoy | 51.82% | 1.03% | 98.06% | 1.94% |
| large | lru | 52.28% | 1.14% | 97.87% | 2.13% |
| large | s3fifo | 6.04% | 46.98% | 11.39% | 88.61% |

### MooncakeConv

| 容量点 | 策略 | HBM 命中率 | DRAM 命中率 | 命中来自 HBM | 命中来自 DRAM |
|---|---|---|---|---|---|
| small | buoy | 7.30% | 10.68% | 40.60% | 59.40% |
| small | lru | 4.35% | 15.19% | 22.28% | 77.72% |
| small | s3fifo | 0.13% | 12.15% | 1.08% | 98.92% |
| medium | buoy | 15.13% | 13.64% | 52.58% | 47.42% |
| medium | lru | 9.86% | 25.48% | 27.90% | 72.10% |
| medium | s3fifo | 5.16% | 19.99% | 20.53% | 79.47% |
| large | buoy | 18.03% | 11.42% | 61.24% | 38.76% |
| large | lru | 27.84% | 8.80% | 75.98% | 24.02% |
| large | s3fifo | 16.55% | 16.21% | 50.52% | 49.48% |

### MooncakeTool

| 容量点 | 策略 | HBM 命中率 | DRAM 命中率 | 命中来自 HBM | 命中来自 DRAM |
|---|---|---|---|---|---|
| small | buoy | 36.29% | 6.08% | 85.64% | 14.36% |
| small | lru | 33.98% | 10.96% | 75.61% | 24.39% |
| small | s3fifo | 0.18% | 39.63% | 0.45% | 99.55% |
| medium | buoy | 41.57% | 7.32% | 85.02% | 14.98% |
| medium | lru | 38.11% | 16.97% | 69.19% | 30.81% |
| medium | s3fifo | 4.20% | 44.48% | 8.63% | 91.37% |
| large | buoy | 42.72% | 6.43% | 86.92% | 13.08% |
| large | lru | 50.77% | 4.48% | 91.88% | 8.12% |
| large | s3fifo | 13.92% | 39.79% | 25.92% | 74.08% |

## 4. 缓存机制统计(medium 容量点)

| 轨迹 | 策略 | 准入 | 拒绝 | HBM→DRAM 降配 | 淘汰 | ghost 命中 | 二次机会 |
|---|---|---|---|---|---|---|---|
| QwenTraceA | buoy | 1,658,220 | 1,847,932 | 995,349 | 994,120 | 734,494 | 847,568 |
| QwenTraceA | lru | 2,754,841 | 0 | 4,379,601 | 2,090,741 | 0 | 0 |
| QwenTraceA | s3fifo | 3,196,546 | 0 | 686,115 | 2,532,446 | 461,369 | 474,329 |
| QwenTraceB | buoy | 1,562,914 | 3,139,513 | 0 | 421,428 | 48,143 | 8,625 |
| QwenTraceB | lru | 4,678,155 | 0 | 4,883,744 | 3,518,561 | 0 | 0 |
| QwenTraceB | s3fifo | 4,719,691 | 0 | 69,176 | 4,496,494 | 38,062 | 0 |
| MooncakeConv | buoy | 108,860 | 96,641 | 34,791 | 63,162 | 18,029 | 24,290 |
| MooncakeConv | lru | 186,522 | 0 | 255,474 | 140,824 | 0 | 0 |
| MooncakeConv | s3fifo | 215,940 | 0 | 13,582 | 173,119 | 24,669 | 0 |
| MooncakeTool | buoy | 78,753 | 130,594 | 25,422 | 32,928 | 23,476 | 20,856 |
| MooncakeTool | lru | 183,970 | 0 | 248,915 | 138,145 | 0 | 0 |
| MooncakeTool | s3fifo | 210,234 | 0 | 14,093 | 168,091 | 23,468 | 0 |

## 5. 对比图

![各容量点总命中率](fig1_chunk_hit_rate.png)

![命中来自哪一层(100% 堆叠)](fig2_tier_share.png)

![HBM 承担的命中份额](fig3_hbm_share.png)

![HBM→DRAM 降配写流量(symlog)](fig4_demotions.png)

## 6. 观察与结论

- **QwenTraceA/small**:最佳命中率 41.28%(by lru);S3-FIFO 命中 31.08%,其中 96.57% 来自 DRAM
- **QwenTraceA/medium**:最佳命中率 56.23%(by lru);S3-FIFO 命中 49.21%,其中 71.21% 来自 DRAM
- **QwenTraceA/large**:最佳命中率 57.79%(by lru);S3-FIFO 命中 55.36%,其中 44.36% 来自 DRAM
- **QwenTraceB/small**:最佳命中率 52.23%(by buoy);S3-FIFO 命中 51.95%,其中 97.64% 来自 DRAM
- **QwenTraceB/medium**:最佳命中率 53.01%(by lru);S3-FIFO 命中 52.60%,其中 94.85% 来自 DRAM
- **QwenTraceB/large**:最佳命中率 53.41%(by lru);S3-FIFO 命中 53.02%,其中 88.61% 来自 DRAM
- **MooncakeConv/small**:最佳命中率 19.54%(by lru);S3-FIFO 命中 12.28%,其中 98.92% 来自 DRAM
- **MooncakeConv/medium**:最佳命中率 35.35%(by lru);S3-FIFO 命中 25.15%,其中 79.47% 来自 DRAM
- **MooncakeConv/large**:最佳命中率 36.64%(by lru);S3-FIFO 命中 32.77%,其中 49.48% 来自 DRAM
- **MooncakeTool/small**:最佳命中率 44.94%(by lru);S3-FIFO 命中 39.81%,其中 99.55% 来自 DRAM
- **MooncakeTool/medium**:最佳命中率 55.09%(by lru);S3-FIFO 命中 48.68%,其中 91.37% 来自 DRAM
- **MooncakeTool/large**:最佳命中率 55.25%(by lru);S3-FIFO 命中 53.71%,其中 74.08% 来自 DRAM

- **HBM→DRAM 降配流量**(medium,4 轨迹求和):LRU 9,767,734,Buoy 1,055,562,S3-FIFO 782,966;S3-FIFO 仅为 LRU 的 8.0%
- **命中热度的分层分布**(medium 平均):Buoy 命中的 74.41% 落在 HBM,LRU 60.80%,S3-FIFO 仅 15.78% —— S3-FIFO 把 HBM 当作短试用期,热点迅速沉淀到 DRAM,HBM 写流量随之最小化
- **Buoy 的深度准入**在 medium 点共拒绝 5,214,680 个 chunk,显著减少进入缓存的一次性访问(对照 S3-FIFO/LRU 全量准入)
