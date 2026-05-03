"""PII / secret redaction.

Every code path that ingests external data (telemetry, git commit messages,
docstrings going into skeletons that may end up over the network in MCP /
hosted mode) MUST run through ``redact()``. Idempotent — redacting an
already-redacted string yields the same string.

Patterns redacted:

  - Email addresses               → ``<EMAIL>``
  - JWTs (``eyJ...``)             → ``<JWT>``
  - Hex strings ≥ 32 chars        → ``<HEX>``
  - API keys (sk-, xoxb-, ghp_,
    AKIA, AIza)                   → ``<APIKEY>``
  - IPv4 / IPv6 addresses         → ``<IP>``
  - UK phones / +intl phones      → ``<PHONE>``

The patterns are intentionally aggressive — false positives on
docstrings are acceptable; false negatives that leak secrets are not.
"""

from __future__ import annotations

import re

_EMAIL = re.compile(r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b")
_JWT = re.compile(r"\beyJ[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\.[A-Za-z0-9_\-]+\b")
_APIKEY = re.compile(
    r"\b(?:sk-[A-Za-z0-9]{16,}|xoxb-[A-Za-z0-9\-]{16,}|ghp_[A-Za-z0-9]{16,}|"
    r"AKIA[A-Z0-9]{12,}|AIza[A-Za-z0-9_\-]{16,})\b"
)
_HEX = re.compile(r"\b[a-fA-F0-9]{32,}\b")
_IPV4 = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_IPV6 = re.compile(r"\b(?:[A-Fa-f0-9]{1,4}:){2,7}[A-Fa-f0-9]{0,4}\b")
_PHONE = re.compile(r"(?:\+\d{1,3}[\s\-]?)?(?:\(?\d{2,4}\)?[\s\-]?)?\d{3,4}[\s\-]?\d{3,4}")


def redact(text: str) -> str:
    if not text:
        return text
    out = text
    out = _APIKEY.sub("<APIKEY>", out)
    out = _JWT.sub("<JWT>", out)
    out = _EMAIL.sub("<EMAIL>", out)
    out = _HEX.sub("<HEX>", out)
    out = _IPV6.sub("<IP>", out)
    out = _IPV4.sub("<IP>", out)

    # Phone is the most ambiguous — only apply to substrings that look
    # phone-like enough (>= 9 digits including separators).
    def _phone_repl(m: re.Match) -> str:
        digits = re.sub(r"\D", "", m.group(0))
        return "<PHONE>" if 7 <= len(digits) <= 15 else m.group(0)

    out = _PHONE.sub(_phone_repl, out)
    return out


def redact_dict(d: dict) -> dict:
    """Recursively redact every string value. Keys are not redacted."""
    out: dict = {}
    for k, v in d.items():
        if isinstance(v, str):
            out[k] = redact(v)
        elif isinstance(v, dict):
            out[k] = redact_dict(v)
        elif isinstance(v, list):
            out[k] = [
                redact(x) if isinstance(x, str) else redact_dict(x) if isinstance(x, dict) else x
                for x in v
            ]
        else:
            out[k] = v
    return out


__all__ = ["redact", "redact_dict"]
