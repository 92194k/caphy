"""Text-to-Speech using Piper (runs locally, sub-500ms latency).

Fast, natural-sounding voices. No cloud, no API costs.

Install: pip install piper-tts pyaudio
Models: https://github.com/rhasspy/piper#voices
"""
import subprocess
import os
import tempfile
import pyaudio
import wave
import platform


class TextToSpeech:
    """Generate and play speech locally using Piper TTS."""

    def __init__(self, voice="en_US-joey-medium", piper_path="piper"):
        """
        Args:
            voice: Voice model, e.g., "en_US-joey-medium"
                   Other options: en_US-libritts-high, en_GB-cori-high
                   See: https://github.com/rhasspy/piper#voices
            piper_path: Path to piper binary (or "piper" if in PATH)
        """
        self.voice = voice
        self.piper_path = piper_path
        self.cache_dir = tempfile.gettempdir()

        # Check if piper is installed
        result = subprocess.run([piper_path, "--help"], capture_output=True)
        if result.returncode != 0:
            raise RuntimeError(
                f"Piper not found. Install: pip install piper-tts\n"
                f"Or ensure '{piper_path}' is in PATH"
            )

        print(f"✓ Piper TTS ready (voice: {voice})")

    def speak_sync(self, text, output_file=None):
        """
        Generate speech file synchronously.

        Args:
            text: Text to speak
            output_file: Save to file (or temp file if None)

        Returns:
            Path to .wav file
        """
        if output_file is None:
            output_file = os.path.join(self.cache_dir, "piper_response.wav")

        print(f"🔊 Generating speech: '{text[:50]}...'")

        cmd = [
            self.piper_path,
            "--model", self.voice,
            "--output-file", output_file,
        ]

        process = subprocess.Popen(
            cmd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE
        )

        process.stdin.write(text.encode("utf-8"))
        process.stdin.close()
        stdout, stderr = process.communicate()

        if process.returncode != 0:
            raise RuntimeError(f"Piper error: {stderr.decode()}")

        print(f"✓ Speech generated: {output_file}")
        return output_file

    def play(self, text, delete_after=True):
        """
        Generate speech and play through speakers.

        Args:
            text: Text to speak
            delete_after: Delete temp file after playing
        """
        wav_file = self.speak_sync(text)

        print("🔈 Playing audio...")

        try:
            with wave.open(wav_file, "rb") as wf:
                pa = pyaudio.PyAudio()

                stream = pa.open(
                    format=pa.get_format_from_width(wf.getsampwidth()),
                    channels=wf.getnchannels(),
                    rate=wf.getframerate(),
                    output=True
                )

                chunk_size = 1024
                data = wf.readframes(chunk_size)

                while data:
                    stream.write(data)
                    data = wf.readframes(chunk_size)

                stream.stop_stream()
                stream.close()
                pa.terminate()

                print("✓ Playback complete")

        finally:
            if delete_after and os.path.exists(wav_file):
                os.remove(wav_file)

    def play_system(self, text):
        """
        Play audio using system command (macOS, Linux, Windows).
        Useful as fallback if pyaudio fails.
        """
        wav_file = self.speak_sync(text, delete_after=False)

        system = platform.system()
        try:
            if system == "Darwin":  # macOS
                subprocess.run(["afplay", wav_file], check=True)
            elif system == "Linux":
                subprocess.run(["aplay", wav_file], check=True)
            elif system == "Windows":
                import winsound
                winsound.PlaySound(wav_file, winsound.SND_FILENAME)
            print("✓ Playback complete")
        finally:
            if os.path.exists(wav_file):
                os.remove(wav_file)


if __name__ == "__main__":
    tts = TextToSpeech(voice="en_US-joey-medium")

    # Test
    tts.play("System armed. Monitoring active.")
    tts.play("Unknown person detected at front door. Threat level high.")
