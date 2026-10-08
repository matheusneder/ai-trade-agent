"""Tagging of the assets mentioned in news (code → names dictionary).

Codes are recognized in uppercase, standing alone, with a ``$`` in front (``$SOL``) or in
parentheses (``Solana (SOL)``). Two-character codes (e.g. ``OP``) only in the last two
forms, to avoid false positives. Names are recognized case-insensitively.
"""

import re
from collections.abc import Iterable, Mapping, Sequence


class AssetTagger:
    def __init__(self, assets: Iterable[str], names: Mapping[str, Sequence[str]]) -> None:
        codes = sorted({a.upper() for a in assets} | {a.upper() for a in names}, key=len)
        self._codes = set(codes)
        alternatives = "|".join(re.escape(c) for c in reversed(codes)) or "(?!)"
        self._marked = re.compile(rf"(?:\$|\()({alternatives})(?![A-Za-z0-9])")
        self._bare = re.compile(rf"(?<![A-Za-z0-9$])({alternatives})(?![A-Za-z0-9])")
        self._names = [
            (code.upper(), re.compile(rf"(?<![a-z0-9]){re.escape(n.lower())}(?![a-z0-9])"))
            for code, aliases in names.items()
            for n in aliases
        ]

    def tag(self, text: str) -> tuple[str, ...]:
        found = [m.group(1) for m in self._marked.finditer(text)]
        found += [m.group(1) for m in self._bare.finditer(text) if len(m.group(1)) >= 3]
        lowered = text.lower()
        found += [code for code, pattern in self._names if pattern.search(lowered)]
        return tuple(sorted(dict.fromkeys(c for c in found if c in self._codes)))
