# Buoy Prefix Cache Simulator

A standalone Python simulator for replaying LLM prefix-cache traces with:
- Buoy's two-tier HBM/DRAM queue policy
- ghost FIFO
- visited-bit / second-chance eviction
- depth-aware admission using the online mean depth of reused chunks
- a two-tier LRU baseline (HBM overflow is demoted to DRAM, not discarded)
- CSV/JSONL input and CSV/JSON output

This is a research simulator, not a GPU/vLLM serving implementation. It models chunk residency and estimated prefill reuse; it does not model real transfer timings or kernel execution.

## Requirements
Python 3.10+; no third-party packages required.

## Quick start

```bash
python run_simulation.py --trace examples/demo.jsonl --policy buoy \
  --hbm-capacity 4 --dram-capacity 8 --block-size 4 --ghost-capacity 64 \
  --output-dir results
```

Compare Buoy and LRU:
```bash
python run_simulation.py --trace examples/demo.jsonl --policy both \
  --hbm-capacity 4 --dram-capacity 8 --block-size 4 --output-dir results
```

Output files:
- `results/summary.csv`: one row per policy
- `results/requests_<policy>.csv`: per-request hit/miss breakdown
- `results/final_state_<policy>.json`: final HBM/DRAM/ghost state and counters

## Input trace formats

### JSONL (recommended)
One request per line. `tokens` can be a list of token IDs/strings, or `prompt` can be a string.
```json
{"timestamp": 1.0, "request_id": "r1", "tokens": ["system", "hello", "world", "!"]}
{"timestamp": 2.0, "request_id": "r2", "tokens": ["system", "hello", "there", "!"]}
```

### CSV
Supported columns include:
- `timestamp` (optional; input order is used if absent)
- `request_id` (optional)
- `tokens` (JSON list string, e.g. `["a","b","c"]`)
- `prompt` (string; split on whitespace)
- `prompt_tokens` (integer only; synthetic tokens are generated for repeatable tests)
- `prefix` (string; split on whitespace)

If a row has `tokens` as a JSON list, it is used directly. If it has `prompt` or `prefix`, the text is split on whitespace. If only `prompt_tokens` is available, the simulator generates a unique synthetic token sequence for that request; such rows will not share prefixes unless a shared `prefix`/`tokens` representation is provided.

**Important:** Real LLM traces should provide token IDs or a stable tokenized prompt. Whitespace splitting is only a fallback for demos and simple traces, not equivalent to a model tokenizer.

## Model

Each request is split into fixed-size chunks. A chunk identity is a hash of the *entire token prefix through that chunk*, so two chunks are considered identical only when both the chunk contents and all preceding prefix contents match.

The simulator counts a chunk as a hit if its chunk ID is resident in HBM or DRAM. Buoy then:
1. moves hits to HBM and sets `visited=True`;
2. inserts misses into DRAM if depth admission permits;
3. uses the ghost FIFO to recognize recently evicted/rejected chunks;
4. demotes HBM tail chunks to DRAM;
5. uses a second chance in DRAM: visited tail chunks are reset and moved to the head, otherwise evicted to the ghost FIFO.

Depth-aware admission: the initial threshold is configurable. Each cache hit is a reuse event and updates the online cumulative mean reuse depth. By default, the admission threshold is `floor(mean reuse depth)` with a minimum of 1. A miss deeper than the threshold is rejected unless its chunk ID is in the ghost cache, in which case it is admitted with a second-chance bit. The paper describes a reuse-depth average but does not specify in the searchable algorithm text a rounding convention; this simulator makes that convention explicit/configurable.

The LRU baseline is a two-tier LRU with the same total capacity (`hbm + dram` chunks). Hits move to the HBM MRU position (promoting from DRAM when the hit lands in DRAM), misses enter HBM, HBM overflow is demoted to DRAM rather than discarded, and DRAM overflow is evicted. Because of promotion-on-hit and demotion-on-overflow, the two queues stay in strict global-recency order, so hit/miss decisions match a single-tier LRU of capacity `hbm + dram`; only the tier accounting (`hbm_hits`/`dram_hits`, `demotions`, `evictions`) differs.

## Metrics
- `chunk_hit_rate`: cached chunk accesses / all chunk accesses
- `token_hit_rate`: reused tokens / all prompt tokens (chunk-granular approximation; final partial chunk uses its actual length)
- `hbm_hit_rate`, `dram_hit_rate`: tier hit counts / all chunk accesses
- `estimated_prefill_tokens_saved`: token count in hit chunks
- `admitted_chunks`, `rejected_chunks`, `evictions`, `demotions`, `ghost_hits`
- request-level statistics

These are cache-simulation metrics, not measured throughput/TTFT. Comparing prefill throughput requires an explicit performance model or real serving experiment.

## Notes on algorithm fidelity
The implementation follows the paper's Algorithm 1 queue behavior and its depth-aware admission description. The paper's serving implementation may contain engineering details not fully represented by a simulator, and the cache-capacity inputs here are in chunks, not bytes. Set `--hbm-capacity` and `--dram-capacity` to chunk counts. For byte-based capacity, convert using model-specific KV bytes per chunk before running.


## Production-like capacity profile and agent trace

The repository includes:
- `configs/llama31_8b_bf16_example.json`: an **illustrative** capacity profile for Llama 3.1 8B with BF16 KV and 16-token blocks.
- `examples/production_like_trace.jsonl`: deterministic synthetic multi-agent/session traffic with shared system/tool prefixes, multi-turn histories, tool observations, variable prompt lengths, and arrival timestamps.
- `examples/generate_production_like_trace.py`: regenerate the synthetic trace with a chosen number of sessions.

For this profile, the estimated KV size is:
- 32 layers × 2 (K/V) × 8 KV heads × 128 head dimension × 2 bytes = 131,072 bytes/token (128 KiB/token)
- 16 tokens/block × 128 KiB/token = 2 MiB/block
- 32 GiB HBM budget = 16,384 blocks
- 128 GiB DRAM budget = 65,536 blocks

Run with the illustrative production-capacity budget:
```bash
python run_simulation.py --trace examples/production_like_trace.jsonl --policy both \\
  --hbm-capacity 16384 --dram-capacity 65536 --ghost-capacity 65536 \\
  --block-size 16 --output-dir results_production_capacity
```

For faster algorithm validation and more visible cache pressure on the included finite trace, use a scaled-down capacity:
```bash
python run_simulation.py --trace examples/production_like_trace.jsonl --policy both \\
  --hbm-capacity 2048 --dram-capacity 8192 --ghost-capacity 8192 \\
  --block-size 16 --output-dir results_scaled
```

**Important limitations:** this trace is synthetic, not a captured production trace, and its token-like strings are not output from a real tokenizer. It is useful for reproducible simulator testing and controlled experiments, but not sufficient evidence for thesis claims about real-world hit rates or throughput. For evaluation-quality results, replace it with actual tokenized request traces (with timestamps, session/request IDs, and full prompt token IDs) or a trace format that preserves exact token-prefix identity. The budgets are cache budgets, not total physical HBM/DRAM capacity. Real deployments may have different KV dtype, model architecture, tensor parallelism, memory reservation, chunk size, and host-memory constraints.
