"""Phase 10 - software siren, smooth and glitch-free.

Primary mode: plays a real alarm sound file (config.SIREN_SOUND_PATH) on
loop through pygame.mixer.music, fading in on start() and fading out on
stop() instead of snapping on/off - no click, no jarring instant-on. Rapid
Tier flip-flopping (Tier 3 confirmed -> gone -> confirmed again within a
couple of frames is normal, detection is frame-by-frame) just re-triggers
the fade instead of glitching, because start()/stop() are idempotent (a
start() while already playing does nothing; a stop() while already silent
does nothing).

Fallback mode: if the sound file is missing or pygame isn't available, a
synthesized wailing tone is used instead (smooth cosine pitch sweep + soft
saturation via sounddevice, with its own per-block gain fade), so the
system always has SOME siren even before you've added a sound file.
"""
import os
import threading

import numpy as np

try:
    import config
except Exception:
    config = None


class _FileSiren:
    """Loops a real alarm sound file via pygame.mixer.music, with a smooth
    fade in/out instead of snapping on or clicking off."""

    def __init__(self, path, fade_ms=300, volume=0.9):
        import pygame
        self._pygame = pygame
        if not pygame.mixer.get_init():
            pygame.mixer.init()
        pygame.mixer.music.load(path)
        pygame.mixer.music.set_volume(max(0.0, min(1.0, volume)))
        self.fade_ms = fade_ms
        self._playing = False

    def start(self):
        if self._playing:
            return   # already playing/fading in - don't restart, avoids a pop
        self._playing = True
        self._pygame.mixer.music.play(loops=-1, fade_ms=self.fade_ms)

    def stop(self):
        if not self._playing:
            return
        self._playing = False
        self._pygame.mixer.music.fadeout(self.fade_ms)

    def shutdown(self):
        self.stop()

    @property
    def active(self):
        return self._playing


class _ToneSiren:
    """Synthesized fallback wail - a smooth cosine pitch sweep (not a sharp
    triangle, which "chirps" at each reversal) run through gentle
    soft-saturation for a fuller tone, with its own continuous per-block
    gain ramp so start()/stop() fade instead of click - the audio stream is
    never torn down on an ordinary toggle, only on shutdown()."""

    def __init__(self, sample_rate=44100, volume=0.75, wail_hz=0.6):
        self.sr = sample_rate
        self.volume = volume
        self.wail_hz = wail_hz
        self._stream = None
        self._phase = 0.0
        self._lfo_t = 0.0
        self._gain = 0.0
        self._target = 0.0
        self._lock = threading.Lock()

    def _callback(self, outdata, frames, time_info, status):
        sr = self.sr
        lt = self._lfo_t + np.arange(frames) / sr
        sweep = 0.5 - 0.5 * np.cos(2 * np.pi * self.wail_hz * lt)
        freq = 650.0 + 450.0 * sweep
        dphase = 2 * np.pi * freq / sr
        phase = self._phase + np.cumsum(dphase)
        tone = np.sin(phase)
        tone = np.tanh(1.6 * tone) / np.tanh(1.6)
        self._phase = float(phase[-1] % (2 * np.pi))
        self._lfo_t = float(lt[-1] + 1.0 / sr)

        ramp_sec = 0.18
        step = frames / sr / ramp_sec
        with self._lock:
            target = self._target
        if self._gain < target:
            self._gain = min(target, self._gain + step)
        elif self._gain > target:
            self._gain = max(target, self._gain - step)

        outdata[:, 0] = (tone * self.volume * self._gain).astype(np.float32)

    def start(self):
        with self._lock:
            self._target = 1.0
        if self._stream is None:
            try:
                import sounddevice as sd
                self._stream = sd.OutputStream(samplerate=self.sr, channels=1,
                                               blocksize=1024, callback=self._callback)
                self._stream.start()
            except Exception as e:
                print(f"[CAPHY] Siren unavailable: {e}")
                self._stream = None

    def stop(self):
        with self._lock:
            self._target = 0.0

    def shutdown(self):
        with self._lock:
            self._target = 0.0
        if self._stream is not None:
            try:
                self._stream.stop()
                self._stream.close()
            except Exception:
                pass
            self._stream = None

    @property
    def active(self):
        return self._target > 0.0 or self._gain > 0.001


class Siren:
    """Uses a real alarm sound file if one is configured and playable,
    otherwise falls back to the synthesized tone. Same interface either
    way, so callers (web/server.py, main.py) never need to know which."""

    def __init__(self, sample_rate=44100, volume=None, wail_hz=0.6):
        path = getattr(config, "SIREN_SOUND_PATH", None) if config else None
        fade_ms = getattr(config, "SIREN_FADE_MS", 300) if config else 300
        vol = volume
        if vol is None:
            vol = getattr(config, "SIREN_VOLUME", 0.9) if config else 0.9

        self._impl = None
        if path and os.path.exists(path):
            try:
                self._impl = _FileSiren(path, fade_ms=fade_ms, volume=vol)
                print(f"[CAPHY] Siren: using sound file {path}")
            except Exception as e:
                print(f"[CAPHY] Siren: could not use sound file ({e}) - "
                      f"falling back to synthesized tone")
        if self._impl is None:
            if path:
                print(f"[CAPHY] Siren: no sound file at {path} - "
                      f"using synthesized tone (drop an .mp3 there to change this)")
            self._impl = _ToneSiren(sample_rate=sample_rate,
                                    volume=min(vol, 0.75), wail_hz=wail_hz)

    def start(self):
        self._impl.start()

    def stop(self):
        self._impl.stop()

    def shutdown(self):
        self._impl.shutdown()

    @property
    def active(self):
        return self._impl.active
