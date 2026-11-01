"""The switches that keep an engine library offline and silent. Plain data, so the worker can apply it before it
imports anything of an engine, and the service can put the same values in the environment it spawns the worker with.
The telemetry subset (`switches.TELEMETRY`) is also applied by the package itself, for every process."""

from ..switches import TELEMETRY

OFFLINE = {
    **TELEMETRY,
    "HF_HUB_OFFLINE": "1", "TRANSFORMERS_OFFLINE": "1", "HF_DATASETS_OFFLINE": "1",
    "HF_HUB_DISABLE_IMPLICIT_TOKEN": "1",
    "TOKENIZERS_PARALLELISM": "false", "PYTHONDONTWRITEBYTECODE": "1", "PYTHONNOUSERSITE": "1",
}
