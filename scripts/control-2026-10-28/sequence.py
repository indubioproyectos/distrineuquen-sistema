#!/usr/bin/env python3
"""Order of the WhatsApp messages inside the same minute.

The lab keeps the chat with minute resolution and the import created the text
messages before the photos, so the ids do not tell whether a note came before
or after its photo. The raw exports do: this script walks them in order and
writes DATA/sequence.csv (message_id, seq) by matching each export line to the
lab message with the same minute, sender and text/filename.

  python3 -I sequence.py <export.txt> [<export2.txt> ...]
"""
import csv
import os
import re
import sys
import unicodedata
from collections import defaultdict
from datetime import datetime, timedelta

DATA = os.environ.get("CTL_DATA", "/tmp/claude-1000/-srv/297057ae-d6c4-4f4f-bef1-a067ba52a7c3/scratchpad/data")
LINE_RE = re.compile(r"^(\d{1,2}/\d{1,2}/\d{4}), (\d{1,2}:\d{2}) - (.*)$")
MEDIA_RE = re.compile(r"^‎?(.+?) \(archivo adjunto\)$")
AR_OFFSET = timedelta(hours=3)


def fold(text):
    text = unicodedata.normalize("NFKD", (text or "").casefold())
    return "".join(ch for ch in text if not unicodedata.combining(ch))


def norm(text):
    return " ".join(fold(text).replace("‎", "").split())[:80]


def parse(path):
    items = []
    with open(path, encoding="utf-8") as fh:
        for raw in fh:
            raw = raw.rstrip("\n")
            m = LINE_RE.match(raw)
            if not m:
                if items:
                    items[-1]["body"] += "\n" + raw
                continue
            d, hm, rest = m.groups()
            when = datetime.strptime("%s %s" % (d, hm), "%d/%m/%Y %H:%M") + AR_OFFSET
            if ": " not in rest:
                continue  # system line
            sender, body = rest.split(": ", 1)
            items.append({"t": when, "sender": sender.strip(), "body": body})
    for it in items:
        mm = MEDIA_RE.match(it["body"].split("\n", 1)[0])
        if mm:
            it["kind"] = "media"
            it["key"] = norm(mm.group(1))
            it["caption"] = it["body"].split("\n", 1)[1] if "\n" in it["body"] else ""
        else:
            it["kind"] = "text"
            it["key"] = norm(it["body"].split("\n", 1)[0])
    return items


def main(paths):
    msgs = []
    with open(os.path.join(DATA, "messages.csv"), newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if not r["timestamp"]:
                continue
            t = datetime.fromisoformat(r["timestamp"][:19])
            sender = r["distri_chat_sender"] or r["profile_name"] or ""
            if r["message_type"] in ("image", "document", "video", "audio") or r["media_filename"]:
                kind, key = "media", norm(r["media_filename"] or "")
            else:
                kind, key = "text", norm((r["body"] or "").split("\n", 1)[0])
            msgs.append({"id": int(r["id"]), "t": t, "sender": norm(sender), "kind": kind, "key": key})
    pool = defaultdict(list)
    for m in msgs:
        pool[(m["t"], m["sender"], m["kind"], m["key"])].append(m)
    seq_of = {}
    seq = 0
    unmatched = 0
    for path in paths:
        for it in parse(path):
            seq += 1
            key = (it["t"], norm(it["sender"]), it["kind"], it["key"])
            cands = [m for m in pool.get(key, []) if m["id"] not in seq_of]
            if not cands:
                unmatched += 1
                continue
            seq_of[cands[0]["id"]] = seq
    out = os.path.join(DATA, "sequence.csv")
    with open(out, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["message_id", "seq"])
        for mid, s in sorted(seq_of.items()):
            w.writerow([mid, s])
    print("messages=%d matched=%d unmatched_export_lines=%d" % (len(msgs), len(seq_of), unmatched))
    # how many receipts of the lab have no sequence
    missing = [m for m in msgs if m["id"] not in seq_of]
    by_month = defaultdict(int)
    for m in missing:
        by_month[m["t"].strftime("%Y-%m")] += 1
    print("lab messages without sequence by month:", dict(sorted(by_month.items())))


if __name__ == "__main__":
    main(sys.argv[1:])
