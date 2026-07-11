"""Phase 9 - Night Vision (software, CLAHE-based).

Brightens and boosts contrast in low light so YOLO can still find a person -
no infrared hardware, just image processing:
  - measure average brightness -> decide if the frame is "low light"
  - CLAHE on the L (lightness) channel -> local contrast without wrecking color
  - a gamma lift -> extra overall brightness
"""
import cv2
import numpy as np


class NightVision:
    def __init__(self, clip_limit=2.5, tile_grid=8, low_light_threshold=70,
                 gamma=1.4, auto=True):
        self.clahe = cv2.createCLAHE(clipLimit=clip_limit,
                                     tileGridSize=(tile_grid, tile_grid))
        self.low_light_threshold = low_light_threshold
        self.gamma = gamma
        self.auto = auto           # True = only enhance when it's dark
        self.enabled = True        # master on/off (toggled with the 'n' key)
        # precompute a gamma lookup table (fast per-pixel brightening)
        inv = 1.0 / gamma
        self._lut = np.array([((i / 255.0) ** inv) * 255 for i in range(256)]).astype("uint8")

    def brightness(self, frame):
        """Average brightness of the frame, 0 (black) to 255 (white)."""
        return float(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY).mean())

    def is_low_light(self, frame):
        return self.brightness(frame) < self.low_light_threshold

    def enhance(self, frame):
        """Apply CLAHE to lightness, then a gamma lift."""
        lab = cv2.cvtColor(frame, cv2.COLOR_BGR2LAB)
        l, a, b = cv2.split(lab)
        l = self.clahe.apply(l)
        out = cv2.cvtColor(cv2.merge((l, a, b)), cv2.COLOR_LAB2BGR)
        if self.gamma != 1.0:
            out = cv2.LUT(out, self._lut)
        return out

    def process(self, frame):
        """Return (out_frame, applied). Enhances the frame when appropriate."""
        if not self.enabled:
            return frame, False
        if self.auto and not self.is_low_light(frame):
            return frame, False        # bright enough - leave it alone
        return self.enhance(frame), True