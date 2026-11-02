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
