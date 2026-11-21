"""Clean raw proxy logs into an SFT-ready dataset.

Pipeline: raw JSONL logs -> keep successful chat calls -> extract the
full conversation (request messages + final assistant reply) -> dedupe
by conversation hash -> drop too-short / error / truncated items ->
export ShareGPT-style JSONL: {"conversations": [{"from": ..., "value": ...}]}

Usage:
  python3 scripts/clean_dataset.py --out data/dataset/train.jsonl
"""
from __future__ import annotations

import argparse
import hashlib
import json
import pathlib
import re

ROOT = pathlib.Path(__file__).resolve().parent.parent
ROLE_MAP = {"system": "system", "user": "human", "assistant": "gpt", "tool": "observation"}


def iter_records(log_dir: pathlib.Path):
    for path in sorted(log_dir.glob("*.jsonl")):
        with path.open(encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if line:
                    try:
                        yield json.loads(line)
                    except json.JSONDecodeError:
                        continue


def extract_conversation(rec: dict) -> list[dict] | None:
    req = rec.get("request") or {}
    messages = req.get("messages")
    if not isinstance(messages, list) or not messages:
        return None
    conv = []
    for m in messages:
        role = ROLE_MAP.get(m.get("role", ""), m.get("role", ""))
        content = m.get("content")
        if isinstance(content, list):  # multimodal / tool blocks -> keep text parts
            content = "\n".join(p.get("text", "") for p in content
                                if isinstance(p, dict) and p.get("type") in ("text", None))
        if content:
            conv.append({"from": role, "value": str(content)})
    reply = None
    resp = rec.get("response") or {}
    choices = resp.get("choices") or []
    if choices:
        reply = (choices[0].get("message") or {}).get("content")
    if not reply:
        reply = rec.get("response_text")
    if reply:
        conv.append({"from": "gpt", "value": reply})
    return conv or None


def conv_key(conv: list[dict]) -> str:
    h = hashlib.sha256()
    for turn in conv:
        h.update(turn["from"].encode())
        h.update(turn["value"].encode())
        h.update(b"\x00")
    return h.hexdigest()


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--log-dir", default=str(ROOT / "data" / "raw_logs"))
    ap.add_argument("--out", default=str(ROOT / "data" / "dataset" / "train.jsonl"))
    ap.add_argument("--min-chars", type=int, default=200, help="drop conversations shorter than this (total chars)")
    ap.add_argument("--min-turns", type=int, default=2)
    args = ap.parse_args()

    seen: set[str] = set()
    kept, dropped = 0, {"status": 0, "no_conv": 0, "dup": 0, "short": 0}
    out_path = pathlib.Path(args.out)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    with out_path.open("w", encoding="utf-8") as out:
        for rec in iter_records(pathlib.Path(args.log_dir)):
            if rec.get("status") != 200:
                dropped["status"] += 1
                continue
            conv = extract_conversation(rec)
            if not conv:
                dropped["no_conv"] += 1
                continue
            key = conv_key(conv)
            if key in seen:
                dropped["dup"] += 1
                continue
            seen.add(key)
            total_chars = sum(len(t["value"]) for t in conv)
            human_turns = sum(1 for t in conv if t["from"] == "human")
            if total_chars < args.min_chars or human_turns < 1 or len(conv) < args.min_turns:
                dropped["short"] += 1
                continue
            out.write(json.dumps({"conversations": conv, "model": rec.get("model"),
                                  "source_log": rec.get("id")}, ensure_ascii=False) + "\n")
            kept += 1
    print(f"kept: {kept}  dropped: {dropped}  -> {out_path}")


if __name__ == "__main__":
    main()
