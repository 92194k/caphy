"""Speech-to-Text using OpenAI Whisper (runs entirely locally).

No cloud, no API calls. Transcribes audio after wake word fires.

Install: pip install openai-whisper pyaudio
"""
import whisper
import pyaudio
import numpy as np


class SpeechToText:
    """Convert speech to text locally using Whisper."""

    def __init__(self, model="base"):
        """
        Args:
            model: "tiny", "base" (140MB), "small", "medium" (1.5GB), "large" (3GB)
                   tiny/base run on CPU. Larger models faster but need more RAM.
        """
        print(f"📥 Loading Whisper model '{model}'... (this takes a moment)")
        self.model = whisper.load_model(model)
        print(f"✓ Whisper {model} ready")

    def record_audio(self, duration=5, sample_rate=16000):
        """
        Record audio from microphone.

        Args:
            duration: Seconds to record
            sample_rate: 16000 Hz for Whisper

        Returns:
            numpy array (float32, -1.0 to 1.0)
        """
        print(f"🎙️  Recording for {duration} seconds...")

        pa = pyaudio.PyAudio()

        stream = pa.open(
            rate=sample_rate,
            channels=1,
            format=pyaudio.paInt16,
            input=True,
            frames_per_buffer=1024
        )

        frames = []
        num_frames = int((sample_rate / 1024) * duration)

        for _ in range(num_frames):
            data = stream.read(1024)
            frames.append(data)

        stream.stop_stream()
        stream.close()
        pa.terminate()

        # Convert bytes to numpy array
        audio_data = np.frombuffer(b"".join(frames), dtype=np.int16)
        audio_float = audio_data.astype(np.float32) / 32768.0

        print(f"✓ Recorded {len(audio_float) / sample_rate:.1f}s of audio")
        return audio_float

    def transcribe(self, audio, language="en"):
        """
        Convert audio to text.

        Args:
            audio: numpy array (float32) or path to .wav file
            language: "en", "tl" (Tagalog), or auto-detect

        Returns:
            str - transcribed text
        """
        print("🧠 Transcribing with Whisper...")

        result = self.model.transcribe(audio, language=language)
        text = result["text"].strip()

        print(f"✓ Transcribed: '{text}'")
        return text

    def transcribe_file(self, filepath):
        """Transcribe a .wav or .mp3 file."""
        print(f"📂 Transcribing {filepath}...")
        result = self.model.transcribe(filepath)
        return result["text"].strip()


if __name__ == "__main__":
    # Test locally
    stt = SpeechToText(model="base")

    # Record 5 seconds
    audio = stt.record_audio(duration=5)

    # Transcribe
    text = stt.transcribe(audio, language="en")
    print(f"\nYou said: {text}")
