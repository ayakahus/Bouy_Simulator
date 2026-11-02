"""Trace loading and deterministic prefix-chunk identities."""
from __future__ import annotations
import csv
import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

@dataclass
class Request:
    index: int
    timestamp: float
    request_id: str
    tokens: list[str]

def _normalise_tokens(value: Any) -> list[str] | None:
    if value is None:
        return None
    if isinstance(value, list):
        return [str(x) for x in value]
    if isinstance(value, (int, float)):
        return None
    if isinstance(value, str):
        value = value.strip()
        if not value:
            return []
        try:
            parsed = json.loads(value)
            if isinstance(parsed, list):
                return [str(x) for x in parsed]
        except (json.JSONDecodeError, TypeError):
            pass
        return value.split()
    return None

def _request_from_obj(obj: dict[str, Any], index: int) -> Request:
    timestamp = float(obj.get("timestamp", obj.get("time", index)))
    request_id = str(obj.get("request_id", obj.get("id", index)))
    tokens = _normalise_tokens(obj.get("tokens"))
    if tokens is None:
        tokens = _normalise_tokens(obj.get("prompt"))
    if tokens is None:
        tokens = _normalise_tokens(obj.get("prefix"))
    if tokens is None:
        count = int(obj.get("prompt_tokens", obj.get("input_tokens", 0)) or 0)
        # A unique synthetic sequence is intentional: token counts alone cannot identify shared prefixes.
        tokens = [f"__synthetic_req{index}_tok{i}" for i in range(count)]
    return Request(index=index, timestamp=timestamp, request_id=request_id, tokens=tokens)

def load_trace(path: str | Path) -> list[Request]:
    path = Path(path)
    requests: list[Request] = []
    if path.suffix.lower() in {".jsonl", ".ndjson"}:
        with path.open("r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if line.strip():
                    requests.append(_request_from_obj(json.loads(line), len(requests)))
    elif path.suffix.lower() == ".json":
        with path.open("r", encoding="utf-8") as f:
            obj = json.load(f)
        rows = obj if isinstance(obj, list) else obj.get("requests", [])
        for row in rows:
            requests.append(_request_from_obj(row, len(requests)))
    elif path.suffix.lower() == ".csv":
        with path.open("r", encoding="utf-8-sig", newline="") as f:
            for row in csv.DictReader(f):
                requests.append(_request_from_obj(row, len(requests)))
    else:
        raise ValueError(f"Unsupported trace format: {path.suffix}. Use .jsonl, .json, or .csv")
    # Stable ordering: timestamp first, original index as tie breaker.
    requests.sort(key=lambda r: (r.timestamp, r.index))
    return requests

def prefix_chunks(tokens: list[str], block_size: int) -> list[tuple[str, int, int]]:
    """Return (chunk_id, depth, token_count) for each fixed-size prefix chunk."""
    out = []
    parent = "ROOT"
    for start in range(0, len(tokens), block_size):
        chunk = tokens[start:start + block_size]
        # Include parent identity so identical local blocks under different prefixes do not collide.
        payload = json.dumps([parent, chunk], ensure_ascii=False, separators=(",", ":")).encode("utf-8")
        cid = hashlib.blake2b(payload, digest_size=16).hexdigest()
        depth = len(out) + 1
        out.append((cid, depth, len(chunk)))
        parent = cid
    return out
