"""Factor 1 of the two-factor pipeline: motion detection.

Uses MOG2 background subtraction. MOG2 can mark shadows separately from real
foreground, which is how we satisfy the "ignore shadows" success criterion:
shadows come back as gray (value 127) and we simply throw them away.
"""
import cv2


class MotionDetector:
    def __init__(self, min_area, history, var_threshold, blur):
        self.min_area = min_area
        self.blur = blur if blur % 2 == 1 else blur + 1  # blur size must be odd
        # detectShadows=True is the key line: it separates shadows from real motion
        self.bg = cv2.createBackgroundSubtractorMOG2(
            history=history, varThreshold=var_threshold, detectShadows=True)
        # small kernel reused for cleaning noise
        self.kernel = cv2.getStructuringElement(cv2.MORPH_ELLIPSE, (3, 3))

    def detect(self, frame):
        """Return (moved, motion_area, mask).

        moved       True if enough real motion was seen this frame.
        motion_area total area of moving pixels (useful for tuning / later tiers).
        mask        cleaned black/white motion mask (white = real motion).
        """
        gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
        gray = cv2.GaussianBlur(gray, (self.blur, self.blur), 0)

        mask = self.bg.apply(gray)

        # MOG2 output: 255 = foreground, 127 = shadow, 0 = background.
        # Keep only 255 (real motion); anything below 200 (incl. shadows) is dropped.
        _, mask = cv2.threshold(mask, 200, 255, cv2.THRESH_BINARY)

        # remove speckle noise, then grow the blobs a little so they connect
        mask = cv2.morphologyEx(mask, cv2.MORPH_OPEN, self.kernel)
        mask = cv2.dilate(mask, self.kernel, iterations=2)

        # measure how much real motion there is
        contours, _ = cv2.findContours(mask, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
        motion_area = 0.0
        for c in contours:
            a = cv2.contourArea(c)
            if a >= 200:              # ignore tiny leftover blobs
                motion_area += a

        moved = motion_area >= self.min_area
        return moved, motion_area, mask