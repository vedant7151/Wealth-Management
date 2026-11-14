"""CI guard: fail the build if the repository contains secrets or files that
must never be committed. Scans TRACKED files only (what would ship).

Checks: (1) no tracked .env* except *.example; (2) no tracked key/credential
files; (3) no key-shaped strings (Google, Groq, Tavily, HuggingFace, LangSmith,
Clerk, OpenAI-style, private-key blocks, JWTs, bearer tokens, Postgres URLs
with a non-placeholder password); (4) no tracked file over 5 MB.
Prints paths, line numbers and a redacted fingerprint -- never the secret."""

import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
os.chdir(ROOT)

NAME_RX = re.compile(r"(^|/)\.env($|\.)|\.(pem|key|p12|pfx|jks)$|(^|/)(credentials|service-account)[^/]*\.json$")
PATTERNS = {
    "google_api_key": re.compile(r"AIza[0-9A-Za-z_\-]{30,}"),
    "groq_key": re.compile(r"gsk_[0-9A-Za-z]{20,}"),
    "tavily_key": re.compile(r"tvly-[0-9A-Za-z_\-]{10,}"),
    "huggingface_token": re.compile(r"hf_[0-9A-Za-z]{30,}"),
    "langsmith_key": re.compile(r"lsv2_[0-9a-z_]{20,}"),
    "clerk_secret_key": re.compile(r"sk_(live|test)_[0-9A-Za-z]{20,}"),
    "openai_style_key": re.compile(r"\bsk-[0-9A-Za-z_\-]{30,}"),
    "private_key_block": re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    "jwt": re.compile(r"\beyJ[A-Za-z0-9_\-]{15,}\.[A-Za-z0-9_\-]{15,}\.[A-Za-z0-9_\-]{15,}"),
    "bearer_token": re.compile(r"Bearer [A-Za-z0-9._~+/\-]{25,}"),
    "db_url_password": re.compile(r"postgres(?:ql)?://[^:/\s\"'@<>]+:([^@\s\"'<>/]{3,})@"),
}
PLACEHOLDER_PW = {"pass", "p", "ci", "build", "postgres", "wealth", "password", "user", "secret", "changeme"}
MAX_BYTES = 5_000_000


def main() -> int:
    files = subprocess.run(["git", "ls-files"], capture_output=True, text=True, check=True).stdout.splitlines()
    problems = []
    for f in files:
        if NAME_RX.search(f) and not f.endswith(".example"):
            problems.append(f"{f}: forbidden file name tracked")
        if not os.path.isfile(f):
            continue
        if os.path.getsize(f) > MAX_BYTES:
            problems.append(f"{f}: larger than {MAX_BYTES} bytes")
            continue
        try:
            text = open(f, encoding="utf-8", errors="ignore").read()
        except OSError:
            continue
        for lineno, line in enumerate(text.splitlines(), 1):
            for name, rx in PATTERNS.items():
                m = rx.search(line)
                if not m:
                    continue
                if name == "db_url_password" and m.group(1) in PLACEHOLDER_PW:
                    continue
                s = m.group(0)
                problems.append(f"{f}:{lineno}: {name} ({s[:4]}..., len={len(s)})")
    if problems:
        print("REPO HYGIENE FAILED:")
        for p in problems:
            print("  -", p)
        return 1
    print(f"repo hygiene OK ({len(files)} tracked files scanned)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
