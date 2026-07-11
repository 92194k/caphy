"""Phase 8 - Vosk offline speech engine (microphone -> text) with optional
spoken replies via pyttsx3. Uses a small command grammar so it only listens
for CAPHY's commands - far fewer false transcriptions. All offline.
"""
import json
import queue

import sounddevice as sd
from vosk import Model, KaldiRecognizer

# Only these phrases are recognized (English model). When you add a Tagalog
# model later, add the Tagalog phrases here too.
GRAMMAR = json.dumps([
    "arm system", "disarm system", "stop siren", "system status",
    "yes", "no", "[unk]"
])


class VoskVoice:
    def __init__(self, model_path, sample_rate=16000):
        self.model = Model(model_path)
        self.rec = KaldiRecognizer(self.model, sample_rate, GRAMMAR)
        self.sample_rate = sample_rate
        self.q = queue.Queue()

        self._tts = None
        try:
            import pyttsx3
            self._tts = pyttsx3.init()
        except Exception:
            pass

    def say(self, text):
        print(f"CAPHY: {text}")
        if self._tts:
            try:
                self._tts.say(text)
                self._tts.runAndWait()
            except Exception:
                pass

    def _callback(self, indata, frames, time_info, status):
        self.q.put(bytes(indata))

    def listen(self):
        with sd.RawInputStream(samplerate=self.sample_rate, blocksize=8000,
                               dtype="int16", channels=1, callback=self._callback):
            while True:
                data = self.q.get()
                if self.rec.AcceptWaveform(data):
                    text = json.loads(self.rec.Result()).get("text", "").strip()
                    if text:
                        yield text