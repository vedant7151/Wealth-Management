"""Input normalization/validation helpers (Phase 10).

User-supplied free text (client names, portfolio themes, review reasons,
edited briefing content) is:
  1. Unicode-normalized to NFKC, so visually-identical but differently-encoded
     strings (fullwidth letters, ligatures, composed vs decomposed accents)
     collapse to one canonical form -- which makes duplicate detection and
     allow/deny matching meaningful;
  2. stripped of control characters and invisible/bidirectional formatting
     characters (category Cc/Cf: zero-width spaces, RLO/LRO overrides, BOM)
     that can be used to spoof what a reviewer sees or to smuggle text past a
     filter. Newlines/tabs are kept only for multi-line fields;
  3. length-capped by the caller (caps come from settings.py).

This is hygiene + abuse limiting, NOT prompt-injection defense: theme text
still flows into LLM prompts; the injection guard/compliance engine are the
controls for that (see docs/SECURITY.md)."""

import re
import unicodedata

_WS_RE = re.compile(r"[ \t\r\n\f\v]+")
_ALLOWED_MULTILINE_CONTROLS = {"\n", "\t"}


def normalize_text(value: str, *, multiline: bool = False) -> str:
    value = unicodedata.normalize("NFKC", value)
    cleaned = []
    for ch in value:
        cat = unicodedata.category(ch)
        if cat in ("Cc", "Cf", "Cs", "Co", "Cn"):
            if multiline and ch in _ALLOWED_MULTILINE_CONTROLS:
                cleaned.append(ch)
            elif ch in ("\r",) and multiline:
                cleaned.append("\n")
            continue
        cleaned.append(ch)
    out = "".join(cleaned)
    if not multiline:
        out = _WS_RE.sub(" ", out)
    return out.strip()


def normalize_themes(themes, *, max_count: int, max_chars: int):
    """Normalizes each theme, drops empties/duplicates (case-insensitive,
    order-preserving), enforces the count and per-theme length caps."""
    if len(themes) > max_count * 5:
        # Refuse absurd lists before doing any per-item work.
        raise ValueError(f"at most {max_count} themes are allowed")
    seen = set()
    out = []
    for raw in themes:
        t = normalize_text(raw)
        if not t:
            continue
        if len(t) > max_chars:
            raise ValueError(f"each theme must be at most {max_chars} characters")
        key = t.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(t)
    if len(out) > max_count:
        raise ValueError(f"at most {max_count} themes are allowed")
    return out
