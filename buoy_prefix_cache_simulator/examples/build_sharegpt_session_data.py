#!/usr/bin/env python3
"""Build UniCache session-format jsonl from cleaned ShareGPT conversations.

Run with the simulator conda env (needs transformers):
    /home/fzz27/anaconda3/envs/simulator/bin/python build_sharegpt_session_data.py

Semantics follow UniCache vllm/simulator/data_parser.py (prepare_session_data,
no-system branch, exactly as its data.sh pipeline would):
  - turn i's prompt = chat template over conversations[:2i-1] (ends with user)
    with add_generation_prompt=True and date_string="13 Sep 2025";
  - input_ids = tokenizer.encode(prompt, add_special_tokens=True);
  - response_i = conversations[2i-1] + eos, encoded without special tokens.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

from transformers import AutoTokenizer

TOKENIZER = "/home/fzz27/UniCache-main/models/Llama-3.1-8B-Instruct"


def get_prompt_text(tokenizer, conversations, add_generation_prompt=True):
    chat = [{"role": t["from"], "content": t["value"].strip()} for t in conversations]
    if chat[-1]["role"] != "user" and add_generation_prompt:
        raise ValueError("the last turn must be from 'user' when add_generation_prompt=True")
    return tokenizer.apply_chat_template(
        chat, tokenize=False, add_generation_prompt=add_generation_prompt,
        date_string="13 Sep 2025")


def build_session(sid: str, conversations: list, tokenizer) -> dict:
    turn = len(conversations) // 2
    result = {"id": sid, "num_turns": turn}
    for i in range(1, turn + 1):
        input_text = get_prompt_text(tokenizer, conversations[:2 * i - 1],
                                     add_generation_prompt=True)
        input_ids = tokenizer.encode(input_text, add_special_tokens=True)
        resp = conversations[2 * i - 1]["value"]
        resp_text = resp + tokenizer.decode([tokenizer.eos_token_id])
        resp_ids = tokenizer.encode(resp_text, add_special_tokens=False)
        result[f"turn_{i}"] = {"input_text": input_text, "input_ids": input_ids,
                               "response_text": resp_text, "response_ids": resp_ids}
    return result


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--input", default="ShareGPT_cleaned_valid_deduplicated.json")
    p.add_argument("--output", default="ShareGPT_session_data_llama31.jsonl")
    p.add_argument("--sessions", type=int, default=5000)
    args = p.parse_args()

    tokenizer = AutoTokenizer.from_pretrained(TOKENIZER)
    data = json.load(open(args.input))
    stride = max(1, len(data) // args.sessions)
    picked = data[::stride][:args.sessions]
    print(f"ShareGPT dedup: {len(data)} sessions, stride={stride}, using {len(picked)}")

    out = Path(args.output)
    n_turns = 0
    with out.open("w", encoding="utf-8") as f:
        for j, item in enumerate(picked):
            convs = item["conversations"]
            if not convs or convs[-1]["from"] != "user" and len(convs) % 2 == 1:
                pass  # prepare_session_data keeps only full user/assistant pairs
            convs = convs[:2 * (len(convs) // 2)]  # drop trailing dangling user turn
            if not convs:
                continue
            sid = f"sharegpt_{j:05d}_{str(item.get('id', j))[-12:]}"
            sess = build_session(sid, convs, tokenizer)
            if sess["num_turns"] == 0:
                continue
            f.write(json.dumps({sid: sess}, ensure_ascii=False, separators=(",", ":")) + "\n")
            n_turns += sess["num_turns"]
    print(f"Wrote sessions to {out} (turns total: {n_turns})")


if __name__ == "__main__":
    main()
