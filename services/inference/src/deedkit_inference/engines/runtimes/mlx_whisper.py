"""Whisper through mlx-whisper 0.4.3, loaded from a verified local directory: `langid` (a small multilingual model,
the first 30 s) and `transcribe` (large-v3-turbo). Only runs on the Mac mini; CI uses the fake runtime.

Calls (mlx_whisper/load_models.py:11, decoding.py:20, transcribe.py:62 at v0.4.3): `load_model(dir, dtype)`,
`detect_language(mel)`, `transcribe(wav, path_or_hf_repo=dir, language=..., ...)`. Audio comes as 16 kHz mono WAV
windows made by ffmpeg, one window decoded at a time."""

import gc
import os

from ... import media
from .common import require_dir


class Runtime:
    def __init__(self, options, directory):
        self.options, self.directory = options, directory

    def windows(self, job):
        return media.iter_chunks(self.options["ffmpeg"], job["files"][0], job["duration"], self.options["scratch"])

    def run(self, job):
        if self.options["mode"] == "langid":
            return self.detect(job)
        import mlx_whisper

        language = job.get("language") or None
        segments, found = [], language
        for offset, _length, wav in self.windows(job):
            result = mlx_whisper.transcribe(wav, path_or_hf_repo=self.directory, language=language, fp16=True,
                                            condition_on_previous_text=False, verbose=None)
            found = found or result.get("language")
            segments += [{"start": offset + s["start"], "end": offset + s["end"], "text": s["text"]}
                         for s in result["segments"]]
        return {"segments": segments, "language": found, "duration": job["duration"]}

    def detect(self, job):
        import mlx.core as mx
        from mlx_whisper.audio import N_SAMPLES, load_audio, log_mel_spectrogram, pad_or_trim

        wav = os.path.join(self.options["scratch"], "langid.wav")
        if not media.chunk_wav(self.options["ffmpeg"], job["files"][0], 0.0, 30.0, wav, 120.0,
                              os.path.join(self.options["scratch"], media.MARKER_NAME)):
            raise RuntimeError("decode_failed")
        try:
            audio = pad_or_trim(load_audio(wav), N_SAMPLES)
        finally:
            os.unlink(wav)
        mel = log_mel_spectrogram(audio, n_mels=self.model.dims.n_mels).astype(mx.float16)
        _, probabilities = self.model.detect_language(mel)
        return {"language": max(probabilities, key=probabilities.get)}

    def unload(self):
        self.model = None
        gc.collect()
        import mlx.core as mx

        mx.clear_cache()


def load(paths, options):
    import mlx.core as mx

    directory = require_dir(paths["model"])
    runtime = Runtime(options, directory)
    if options["mode"] == "langid":
        from mlx_whisper.load_models import load_model

        runtime.model = load_model(directory, dtype=mx.float16)
        if not runtime.model.is_multilingual:
            raise RuntimeError("language identification needs a multilingual model")
    else:
        from mlx_whisper.transcribe import ModelHolder

        runtime.model = ModelHolder.get_model(directory, mx.float16)
    return runtime
