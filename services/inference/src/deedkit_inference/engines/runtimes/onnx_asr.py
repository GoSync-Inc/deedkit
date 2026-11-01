"""GigaAM v3 e2e-RNNT through onnx-asr 0.12.0 on the CPU, with the package's own Silero VAD for audio over 60 s.
Loaded from verified local directories (`load_model(name, path=...)`, `load_vad("silero", path=...)`, onnx_asr/loader.py
at v0.12.0). It reads PCM WAV only, which the ffmpeg windows are. Only runs on the Mac mini; CI uses the fake runtime."""

import gc

from ... import media
from .common import require_dir

VAD_ABOVE_SECONDS = 60.0
PROVIDERS = ["CPUExecutionProvider"]


class Runtime:
    def __init__(self, options, model, with_vad):
        self.options, self.model, self.with_vad = options, model, with_vad

    def run(self, job):
        segments = []
        for offset, length, wav in media.iter_chunks(self.options["ffmpeg"], job["files"][0], job["duration"],
                                                     self.options["scratch"]):
            if length <= VAD_ABOVE_SECONDS:
                segments.append({"start": offset, "end": offset + length, "text": str(self.model.recognize(wav))})
            else:
                segments += [{"start": offset + s.start, "end": offset + s.end, "text": s.text}
                             for s in self.with_vad.recognize(wav)]
        return {"segments": segments, "language": "ru", "duration": job["duration"]}

    def unload(self):
        self.model = self.with_vad = None
        gc.collect()


def load(paths, options):
    import onnx_asr

    model = onnx_asr.load_model("gigaam-v3-e2e-rnnt", path=require_dir(paths["model"]), providers=PROVIDERS)
    vad = onnx_asr.load_vad("silero", path=require_dir(paths["vad"]), providers=PROVIDERS)
    return Runtime(options, model, model.with_vad(vad))
