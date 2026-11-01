"""The alias table the package ships (ANYM-683): `audio-default` and `vision-default`.

A config with an empty `aliases` table serves these. `resident_gb` is the measured peak of the research run
(local-inference-11-01, section 2) plus a stated headroom, never below what the canary saw: Qwen3.5-4B 3.90 GB
peak measured on the mini 11-02 (the worker holds 3.84 GB, the research run said 4.85) -> 4.5 (15 percent over the
peak: with it the pair vision + GigaAM, 6.1 GB, stays over the 6.0 GB cap until a warm worker's growth is reserved), GigaAM v3 1.55 GB
resident -> 1.6, Whisper large-v3-turbo 2.64 GB -> 2.7, Whisper base about 0.2 -> 0.3.
`deedkit-inference canary` records the real figures on the mini; a model upgrade changes `models.json` and these
numbers in the same reviewed change."""

import copy

SHIPPED = {
    "audio-default": {
        "kind": "audio",
        "scope": "audio",
        "engine": "speech",
        "timeout_seconds": 1800,
        "phases": [
            # Language identification on the first 30 s; skipped when the request names `language`.
            {"name": "langid", "slot": "gpu", "resident_gb": 0.3, "share": 0.05},
            {"name": "gigaam", "slot": "cpu", "resident_gb": 1.6, "share": 0.95, "route": "ru"},
            {"name": "whisper", "slot": "gpu", "resident_gb": 2.7, "share": 0.95, "route": "other"},
        ],
    },
    "vision-default": {
        "kind": "chat",
        "scope": "image",
        "extra_scopes": ["document"],
        "engine": "vision",
        "timeout_seconds": 600,
        "default_max_tokens": 2000,
        "phases": [{"name": "vlm", "slot": "gpu", "resident_gb": 4.5, "share": 1.0}],
    },
}


def shipped_aliases():
    return copy.deepcopy(SHIPPED)
