"""Phase 10 - software siren (loud, continuous). A seamless wailing tone played
through the speakers via a continuous sounddevice output stream. No hardware.
It keeps wailing until stop() is called (no gaps or clicks)."""
import threading
import numpy as np


class Siren:
    def __init__(self, sample_rate=44100, volume=0.9, wail_hz=1.0):
        self.sr = sample_rate
        self.volume = volume
        self.wail_hz = wail_hz      # how fast it wails up/down
        self._on = False
        self._stream = None
        self._phase = 0.0           # audio phase (kept continuous across blocks)
        self._lfo_t = 0.0           # slow sweep position

    def _callback(self, outdata, frames, time_info, status):
        sr = self.sr
        # slow triangle sweep of the pitch between 600 and 1200 Hz
        lt = self._lfo_t + np.arange(frames) / sr
        sweep = np.abs(2 * ((lt * self.wail_hz) % 1.0) - 1.0)   # 0..1..0
        freq = 600.0 + 600.0 * sweep
        # integrate frequency into a continuous phase (seamless, no clicks)
        dphase = 2 * np.pi * freq / sr
        phase = self._phase + np.cumsum(dphase)
        wave = self.volume * np.sin(phase)
        self._phase = float(phase[-1] % (2 * np.pi))
        self._lfo_t = float(lt[-1] + 1.0 / sr)
        outdata[:, 0] = wave.astype(np.float32)

    def start(self):
        if self._on:
            return
        try:
            import sounddevice as sd
            self._stream = sd.OutputStream(samplerate=self.sr, channels=1,
                                           blocksize=1024, callback=self._callback)
            self._stream.start()
            self._on = True
        except Exception as e:
            print(f"[CAPHY] Siren unavailable: {e}")

    def stop(self):
        self._on = False
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    @property
    def active(self):
        return self._on