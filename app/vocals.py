"""Separate the singing from the music before the AI listens (optional, AI sync → "Separate the
vocals first").

Plain English: busy, loud songs bury the voice under synths and drums, so the speech
recognition misses words. This uses Spleeter (by Deezer, MIT licence) through the sherpa-onnx
library (Apache-2.0) to keep only the voice. It runs on your PC; the model (about 40 MB) is
downloaded once like the speech model, and the sound is never saved.
"""

from __future__ import annotations

import logging

log = logging.getLogger(__name__)

SPLEETER_RATE = 44100
CHUNK_SECONDS = 30.0


def available() -> str | None:
    try:
        import sherpa_onnx  # noqa: F401
    except Exception as e:      # pragma: no cover - depends on the build
        return f"vocal separation isn't included in this build ({e})"
    return None


def _resample(x, src: int, dst: int):
    import numpy as np
    if src == dst or len(x) == 0:
        return x.astype(np.float32)
    n = int(round(len(x) * dst / src))
    pos = np.linspace(0, len(x) - 1, n)
    return np.interp(pos, np.arange(len(x)), x).astype(np.float32)


class VocalSeparator:
    def __init__(self, model_dir, threads: int = 2):
        import sherpa_onnx
        from pathlib import Path
        d = Path(model_dir)
        voc = next(iter(sorted(d.glob("vocals*.onnx"))), None)
        acc = next(iter(sorted(d.glob("accompaniment*.onnx"))), None)
        if voc is None or acc is None:
            raise RuntimeError("the vocal separation model isn't downloaded")
        config = sherpa_onnx.OfflineSourceSeparationConfig(
            model=sherpa_onnx.OfflineSourceSeparationModelConfig(
                spleeter=sherpa_onnx.OfflineSourceSeparationSpleeterModelConfig(
                    vocals=str(voc), accompaniment=str(acc)),
                num_threads=max(1, int(threads)), debug=False, provider="cpu"))
        if not config.validate():
            raise RuntimeError("the vocal separation model couldn't be loaded")
        self.sp = sherpa_onnx.OfflineSourceSeparation(config)

    def isolate(self, audio16k, rate: int = 16000, progress=None):
        """16 kHz mono in, 16 kHz mono out - with (mostly) only the voice left."""
        import numpy as np
        x = _resample(np.asarray(audio16k, dtype=np.float32), rate, SPLEETER_RATE)
        step = int(CHUNK_SECONDS * SPLEETER_RATE)
        out = []
        for k in range(0, len(x), step):
            piece = x[k:k + step]
            if len(piece) < 2048:
                out.append(np.zeros_like(piece))
                continue
            stereo = np.ascontiguousarray(np.stack([piece, piece]))
            res = self.sp.process(sample_rate=SPLEETER_RATE, samples=stereo)
            voc = np.asarray(res.stems[0].data, dtype=np.float32)
            if k == 0:
                log.info("vocal separation: in %s @%d Hz -> stems %d, vocals %s @%s Hz, other %s",
                         stereo.shape, SPLEETER_RATE, len(res.stems), voc.shape,
                         getattr(res, "sample_rate", "?"),
                         np.asarray(res.stems[1].data).shape if len(res.stems) > 1 else None)
            voc = voc.mean(axis=0) if voc.ndim == 2 else voc
            sr = int(getattr(res, "sample_rate", SPLEETER_RATE) or SPLEETER_RATE)
            voc = _resample(voc, sr, SPLEETER_RATE)
            out.append(voc[:len(piece)] if len(voc) >= len(piece)
                       else np.pad(voc, (0, len(piece) - len(voc))))
            if progress:
                progress(min(1.0, (k + len(piece)) / len(x)))
        vocals = np.concatenate(out) if out else x
        y = _resample(vocals, SPLEETER_RATE, rate)
        n = len(audio16k)
        return y[:n] if len(y) >= n else np.pad(y, (0, n - len(y)))
