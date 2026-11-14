"""A deterministic, local, offline stand-in for backend/llm_client.py's
real `embed_text` (HuggingFace bge-small-en-v1.5, 384-dim). NOT a real
embedding model -- it's a hashed bag-of-words vector, so two texts sharing
vocabulary land closer together (by L2 distance) than texts that don't.
That's just barely enough signal to make recall@k/MRR over the fixture
corpus meaningful and fully reproducible without any network call. See
eval/results.md's Limitations section: this stub's retrieval quality is a
floor, not a prediction of the real HuggingFace model's quality.
"""

import hashlib
import math
import re
from typing import List

DIM = 64
_TOKEN_RE = re.compile(r"[a-z0-9]+")


def _tokenize(text: str) -> List[str]:
    return _TOKEN_RE.findall((text or "").lower())


def embed(text: str) -> List[float]:
    vec = [0.0] * DIM
    for token in _tokenize(text):
        h = hashlib.md5(token.encode("utf-8")).hexdigest()
        bucket = int(h[:8], 16) % DIM
        sign = 1.0 if int(h[8:10], 16) % 2 == 0 else -1.0
        vec[bucket] += sign

    norm = math.sqrt(sum(v * v for v in vec))
    if norm > 0:
        vec = [v / norm for v in vec]
    return vec


def l2_distance(a: List[float], b: List[float]) -> float:
    return math.sqrt(sum((x - y) ** 2 for x, y in zip(a, b)))
