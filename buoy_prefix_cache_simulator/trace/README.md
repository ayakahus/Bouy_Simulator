# Traces

Simulator-format JSONL traces (one request per line:
`timestamp, request_id, session_id, turn_index, tokens`). All real workloads
are tokenized with the Llama-3.1-8B-Instruct tokenizer (chat template applied,
`date_string` fixed to "13 Sep 2025" for deterministic shared prefixes), and
inter-arrival times are Poisson (lambda = 1 req/s, seed = 42) unless noted.

| file | workload | requests | sessions | prompt tokens | unique chunks (block=16) |
|---|---|---|---|---|---|
| `agentbank_apps_llama31_real.jsonl` | AgentBank "apps" agent sessions (single-turn) | 4,408 | 4,408 | 1,688,421 | 98,894 |
| `sharegpt5k_llama31_real.jsonl` | ShareGPT (full pipeline, 5,000-session sample) | 17,191 | 5,000 | 12,048,260 | 300,179 |
| `sharegpt20_llama31_real.jsonl` | ShareGPT (UniCache demo head-20 sample) | 82 | 20 | 60,326 | 1,375 |
| `quac_llama31_real.jsonl` | QuAC v0.2 multi-turn QA (2,000-session sample) | 14,451 | 2,000 | 10,003,776 | 103,078 |
| `mooncake_conversation_trace.jsonl` | Mooncake FAST'25 conversation workload (Kimi, 1h) | 12,031 | — | 144,793,823 | hash-based (≈500 tok/block) |
| `mooncake_toolagent_trace.jsonl` | Mooncake FAST'25 tool & agent workload (Kimi, 1h) | 23,608 | — | 202,940,084 | hash-based (≈500 tok/block) |
| `qwen_traceA_blksz_16.jsonl` | Qwen Bailian to-C chat services (2h sample) | 43,058 | 23,101 chats | 100,382,715 | hash-based (16 tok/block) |
| `qwen_traceB_blksz_16.jsonl` | Qwen Bailian to-B API calling (2h sample) | 172,800 | single-turn | 158,029,113 | hash-based (16 tok/block) |

## Qwen Bailian traces (official release)

`qwen_traceA_blksz_16.jsonl` (to-C chat) and `qwen_traceB_blksz_16.jsonl`
(to-B API calling) come from the **Qwen-Bailian Anonymous Dataset**:
`github.com/alibaba-edu/qwen-bailian-usagetraces-anon` — anonymized
production KVCache traces of a Qwen serving cluster on Aliyun Bailian
(two-hour samples; sha256 verified against the Git-LFS pointers; provenance:
Wang et al., "KVCache Cache in the Wild", USENIX ATC'25, arXiv:2506.02634).
The repo also ships `qwen_thinking_blksz_16.jsonl` and
`qwen_coder_blksz_16.jsonl` (reasoning / code-gen scenarios) if needed.

Format per line: `{"chat_id", "parent_chat_id", "timestamp", "input_length",
"output_length", "type", "turn", "hash_ids"}`. `hash_ids` are salted
SipHash-2-4 block IDs of **16-token blocks** (already chat-templated by the
serving engine — do NOT re-apply a chat template). TraceA is multi-turn
(19,957 continuation requests over 23,101 chats; turn prefix identity is
implied by repeated hash-id sequences); TraceB is single-turn API traffic.
To replay in this simulator: map each hash_id to one pseudo-token and use
`block_size=1` (1 simulated chunk == 1 hash block == 16 real tokens), or
expand ids to real-token granularity if a tokenizer round-trip is required.

## Mooncake traces (official release)

`mooncake_conversation_trace.jsonl` and `mooncake_toolagent_trace.jsonl` are
downloaded from the official Mooncake FAST'25 artifact:
`github.com/kvcache-ai/Mooncake`, path `FAST25-release/traces/`
(conversation_trace.jsonl = 12,031 reqs; toolagent_trace.jsonl = 23,608 reqs;
both sampled from one hour of online Kimi traffic; Qin et al., FAST'25,
arXiv:2407.00079).

Format per line: `{"timestamp": int, "input_length": int, "output_length": int,
"hash_ids": [int, ...]}`. **These traces contain prefix-hash block IDs, not
token IDs**: each `hash_id` identifies one ≈500-token block, and a repeated
hash-id sequence across requests means an identical cached prefix. To replay
them in this simulator, map each hash_id to one pseudo-token and run with
`block_size=1` (so one simulated chunk == one Mooncake hash block, matching
the paper's 512-token chunk granularity), or keep `block_size=16` after
expanding each hash_id into 32 pseudo-tokens. Note `input_length` is the true
token count and may exceed `32 * len(hash_ids)` for the last partial block.

## Provenance

- **AgentBank**: `Solaris99/AgentBank` (HF), "apps" split; tokenized session
  data shipped with UniCache at
  `TaskData/AgentBankData/Llama-3.1-8B-Instruct/`. Real agent prompts;
  single-turn, reuse is shallow (shared chat-template head).
- **ShareGPT**: `anon8231489123/ShareGPT_Vicuna_unfiltered`
  (ShareGPT_V3_unfiltered_cleaned_split.json, downloaded via hf-mirror.com),
  processed with the UniCache pipeline in
  `UniCache-main/TaskData/SharedGPT-multi-turn/`
  (`clean_ShareGPT_V3.py` -> 92,542 valid; `deduplicate.py` -> 57,519 unique;
  stride-11 sample of 5,000 sessions tokenized with
  `examples/build_sharegpt_session_data.py`). Real multi-turn chat; reuse is
  deep (multi-turn history), 71% of sessions have >1 turn; turn prefix
  extension holds 12,191/12,191.
- **QuAC**: `Choi et al., 2018` v0.2 train split (`/data/datasets/quac`),
  stride-5 sample of 2,000 paragraph-sessions tokenized with
  `examples/build_quac_session_data.py` (document context as system message).
  Real multi-turn QA; deep reuse plus cross-session context sharing; turn
  prefix extension holds 12,451/12,451.

The demo trace (`examples/demo.jsonl`) and the synthetic agent-workload trace
(`examples/production_like_trace.jsonl`, regenerate with
`examples/generate_production_like_trace.py`) remain in `examples/` because
the README quick-start commands reference them there.

**Note**: sampled real traces (ShareGPT 5k, QuAC 2k) are deterministic stride
samples chosen for tractable simulation; the full ShareGPT session file
(57,519 sessions) is kept at
`UniCache-main/TaskData/SharedGPT-multi-turn/Llama-3.1-8B-Instruct/`.
