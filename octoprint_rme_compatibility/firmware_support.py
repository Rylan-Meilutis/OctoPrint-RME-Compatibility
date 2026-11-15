# Verified against Prusa's published release assets through 2026-11-05.
# Historical release availability, not an upgrade major-version ceiling.
SUPPORTED = {
    "8.0.0": {"coreone", "coreonel", "coreone_indx", "mk4", "mk3.5"},
    "8.0.1": {"xl", "mini", *("mini-en-" + x for x in ("cs", "de", "es", "fr", "it", "pl", "ja", "uk"))},
    "6.4.2": {"xl", "mini", *("mini-en-" + x for x in ("cs", "de", "es", "fr", "it", "pl", "ja", "uk"))},
    "6.5.3": {"coreone", "coreonel", "mk4", "mk3.5"},
    "6.5.7": {"coreone", "coreonel", "mk4", "mk3.5"},
    **{v: {"coreone_indx"} for v in ("6.6.0", "6.6.1", "6.6.2", "6.6.3", "6.9.0", "6.9.1", "6.9.2")},
    "6.8.1": {"coreone", "coreonel"},
    **{v: {"xl"} for v in ("6.10.1", "6.10.2", "6.10.3")},
}

VARIANTS = frozenset().union(*SUPPORTED.values())


def image_supported(version, variant):
    """Keep known exclusions without stranding a future same-machine BBF."""
    if variant not in VARIANTS:
        return False
    parts = str(version).split(".")
    if len(parts) != 3 or any(not p.isascii() or not p.isdigit() or not 0 <= int(p) <= 255 for p in parts):
        return False
    return version not in SUPPORTED or variant in SUPPORTED[version]

