"""Escaped dotted configuration-key parsing."""


def _split_key_path(key: str) -> list[str]:
    """Split a dotted config-key path, honoring backslash-escaped dots (``a\\.b`` -> ``a.b``).
    Backslashes before any other character are preserved verbatim.

    ``hermes config set`` uses ``.`` as the nesting separator, so a key that itself contains a literal dot
    (e.g. provider names like ``qwen3.5-397b-wafer``) was silently split into bogus nested segments
    (#84064).
    """
    parts: list[str] = []
    current: list[str] = []
    i = 0
    while i < len(key):
        ch = key[i]
        if ch == "\\" and key[i + 1:i + 2] == ".":
            current.append(".")
            i += 2
            continue
        if ch == ".":
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
        i += 1
    parts.append("".join(current))
    return parts
