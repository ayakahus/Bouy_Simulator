#!/usr/bin/env python3
"""Build UniCache session-format jsonl from QuAC v0.2 (real multi-turn dialogues).

Run with the simulator conda env (needs transformers):
    /home/fzz27/anaconda3/envs/simulator/bin/python build_quac_session_data.py

Semantics follow UniCache vllm/simulator/data_parser.py exactly:
  - the shared document context becomes a *system* message, so all sessions on
    the same context share their deepest prefix (realistic RAG-style reuse);
  - turn i's prompt = chat template over [system, u1, a1, ..., u_i] with
    add_generation_prompt=True and date_string="13 Sep 2025";
  - input_ids = tokenizer.encode(prompt, add_special_tokens=True).
"""
from __future__ import annotations

import argparse
import json
import random
from pathlib import Path

from transformers import AutoTokenizer

QUAC_TRAIN = "/data/datasets/quac/raw/v0.2/train_v0.2.json"
QUAC_VAL = "/data/datasets/quac/raw/v0.2/val_v0.2.json"
TOKENIZER = "/home/fzz27/UniCache-main/models/Llama-3.1-8B-Instruct"


def get_prompt_text(tokenizer, conversations, add_generation_prompt=True):
    chat = [{"role": t["from"], "content": t["value"].strip()} for t in conversations]
    if chat[-1]["role"] != "user" and add_generation_prompt:
        raise ValueError("the last turn must be from 'user' when add_generation_prompt=True")
    return tokenizer.apply_chat_template(
        chat, tokenize=False, add_generation_prompt=add_generation_prompt,
        date_string="13 Sep 2025")


def build_session(sid: str, context: str, qa_pairs: list, tokenizer) -> dict:
    conversations = [{"from": "system", "value": context}]
    result = {"id": sid, "num_turns": len(qa_pairs)}
    for i, qa in enumerate(qa_pairs, 1):
        conversations.append({"from": "user", "value": qa["question"]})
        input_text = get_prompt_text(tokenizer, conversations, add_generation_prompt=True)
        input_ids = tokenizer.encode(input_text, add_special_tokens=True)
        answers = qa.get("answers") or []
        ans = answers[0]["text"].strip() if answers and answers[0]["text"].strip() \
            else "CANNOT ANSWER"
        resp_text = ans + tokenizer.decode([tokenizer.eos_token_id])
        resp_ids = tokenizer.encode(resp_text, add_special_tokens=False)
        conversations.append({"from": "assistant", "value": ans})
        result[f"turn_{i}"] = {"input_text": input_text, "input_ids": input_ids,
                               "response_text": resp_text, "response_ids": resp_ids}
    return result


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--output", default="TaskData/QuAC-multi-turn/QuAC_session_data.jsonl")
    p.add_argument("--sessions", type=int, default=2000, help="number of sessions to sample")
    p.add_argument("--seed", type=int, default=42)
    args = p.parse_args()

    tokenizer = AutoTokenizer.from_pretrained(TOKENIZER)
    rng = random.Random(args.seed)
    data = json.load(open(QUAC_TRAIN))["data"]
    entries = [(e["paragraphs"][0]["context"], e["paragraphs"][0]["qas"])
               for e in data if e.get("paragraphs")]
    # deterministic stride sampling keeps context-sharing groups intact
    stride = max(1, len(entries) // args.sessions)
    picked = entries[::stride][:args.sessions]
    print(f"QuAC train: {len(entries)} paragraph-sessions, stride={stride}, using {len(picked)}")

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    n_turns = n_tokens = 0
    with out.open("w", encoding="utf-8") as f:
        for j, (context, qas) in enumerate(picked):
            sid = f"quac_{j:05d}_{rng.randrange(1 << 26):06x}"
            sess = build_session(sid, context, qas, tokenizer)
            f.write(json.dumps({sid: sess}, ensure_ascii=False, separators=(",", ":")) + "\n")
            n_turns += sess["num_turns"]
            n_tokens += sum(len(sess[f"turn_{i}"]["input_ids"])
                            for i in range(1, sess["num_turns"] + 1))
    print(f"Wrote {len(picked)} sessions / {n_turns} turns / {n_tokens} prompt tokens to {out}")


if __name__ == "__main__":
    main()
