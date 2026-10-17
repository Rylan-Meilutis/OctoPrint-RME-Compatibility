# Verified against Prusa's published release assets on 2026-10-17.
# Unknown versions fail closed until their supported machines are verified.
SUPPORTED = {
    "6.4.2": {"xl", "mini", *("mini-en-" + x for x in ("cs", "de", "es", "fr", "it", "pl", "ja", "uk"))},
    "6.5.3": {"coreone", "coreonel", "mk4", "mk3.5"},
    "6.5.7": {"coreone", "coreonel", "mk4", "mk3.5"},
    **{v: {"coreone_indx"} for v in ("6.6.0", "6.6.1", "6.6.2", "6.6.3", "6.9.0", "6.9.1", "6.9.2")},
    "6.8.1": {"coreone", "coreonel"},
    **{v: {"xl"} for v in ("6.10.1", "6.10.2", "6.10.3")},
}

