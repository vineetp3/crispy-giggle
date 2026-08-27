"""Normalise two KNOWN pre-existing nondeterminisms before diffing. Both are cosmetic.

1. matching.detect_foreign_product_ids iterates a set() of id strings, so which
   contaminated id the diagnostic names varies with PYTHONHASHSEED. Verdict unaffected.
2. search.assertions_for orders by (trust_class, field) only, so assertions tying on
   field come back in physical row order -- which shifts whenever merge re-inserts
   them. The `facts` attribute preview prints the first two, so for an attribute whose
   rows all carry the SAME label it shows an arbitrary 2 of N. Collapsed to a count.
   Attributes with distinct labels are left fully intact.
"""
import re
import sys

ID_RE = re.compile(r"product id \d+")
ATTR_RE = re.compile(r"^(\s+(?:YES|no )\s+\S+\s+)(.*)$")


def collapse_repeated_label(preview: str) -> str:
    parts = preview.split("; ")
    if len(parts) < 2:
        return preview
    prefixes = [p.split(": ", 1)[0] if ": " in p else None for p in parts]
    if prefixes[0] and len(set(prefixes)) == 1:
        return f"{prefixes[0]}: <{len(parts)} values>"
    return preview


for line in open(sys.argv[1], encoding="utf-8", errors="replace"):
    line = ID_RE.sub("product id <ID>", line.rstrip("\n"))
    m = ATTR_RE.match(line)
    if m:
        line = m.group(1) + collapse_repeated_label(m.group(2))
    print(line)
