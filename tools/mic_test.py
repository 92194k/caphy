# --- path bootstrap: run from tools/ but import project modules at repo root ---
import sys, os as _bootos
sys.path.insert(0, _bootos.path.dirname(_bootos.path.dirname(_bootos.path.abspath(__file__))))
# --- end bootstrap ---

import queue, json
import sounddevice as sd
from vosk import Model, KaldiRecognizer
import config

q = queue.Queue()
model = Model(config.VOSK_MODEL_PATH)
rec = KaldiRecognizer(model, 16000)

def cb(indata, frames, t, status):
    if status:
        print("status:", status)
    q.put(bytes(indata))

print("Talk now (say 'arm system'). Press Ctrl+C to stop.")
with sd.RawInputStream(samplerate=16000, blocksize=8000, dtype="int16",
                       channels=1, callback=cb):
    while True:
        data = q.get()
        if rec.AcceptWaveform(data):
            print("FINAL:", json.loads(rec.Result()).get("text"))
        else:
            p = json.loads(rec.PartialResult()).get("partial")
            if p:
                print("partial:", p)