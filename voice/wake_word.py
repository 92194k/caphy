"""Local wake word detection using Porcupine.

Always listens for "Hey Sentinel" without uploading audio to cloud.
Minimal CPU cost - only fires callback when wake word detected.

Install: pip install porcupine-porcupine pyaudio
Get access key: https://console.picovoice.ai/
"""
import pvporcupine
import pyaudio
import struct


class WakeWordDetector:
    """Listen for wake word locally."""

    def __init__(self, access_key, keywords=None):
        """
        Args:
            access_key: Porcupine access key from console.picovoice.ai
            keywords: List of wake words, e.g., ["hey sentinel"]
        """
        if keywords is None:
            keywords = ["hey sentinel"]

        self.porcupine = pvporcupine.create(
            access_key=access_key,
            keywords=keywords
        )
        self.sample_rate = self.porcupine.sample_rate
        self.frame_length = self.porcupine.frame_length

        print(f"✓ Wake word detector ready (sample_rate={self.sample_rate})")

    def listen_forever(self, callback, callback_name="wake_detected"):
        """
        Listen forever for wake word.

        Args:
            callback: Function to call when wake word is detected
            callback_name: Name for logging purposes
        """
        pa = pyaudio.PyAudio()

        stream = pa.open(
            rate=self.sample_rate,
            channels=1,
            format=pyaudio.paInt16,
            input=True,
            frames_per_buffer=self.frame_length
        )

        print(f"🎤 Listening for wake word... Call {callback_name} when detected")

        try:
            while True:
                pcm = stream.read(self.frame_length)
                pcm = struct.unpack_from("h" * self.frame_length, pcm)

                keyword_index = self.porcupine.process(pcm)

                if keyword_index >= 0:
                    print(f"\n✓ {callback_name} fired!")
                    callback()

        except KeyboardInterrupt:
            print("\nShutting down wake word detector")

        finally:
            stream.stop_stream()
            stream.close()
            pa.terminate()


if __name__ == "__main__":
    # Test locally
    import os

    # Get key from environment or hardcode for testing
    access_key = os.getenv("PORCUPINE_ACCESS_KEY", "YOUR_ACCESS_KEY_HERE")

    if access_key == "YOUR_ACCESS_KEY_HERE":
        print("⚠️  Set PORCUPINE_ACCESS_KEY environment variable")
        print("Get free key at: https://console.picovoice.ai/")
        exit(1)

    detector = WakeWordDetector(access_key=access_key, keywords=["hey sentinel"])

    def on_wake():
        print("Wake word detected!")

    detector.listen_forever(on_wake, callback_name="on_wake")
