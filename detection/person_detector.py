"""Factor 2 of the two-factor pipeline: person verification with YOLOv8.

This only runs AFTER Factor 1 sees motion, so YOLO is not wasted on empty frames.
It reads the human silhouette (the 'person' class), not the face - that is the
part of CAPHY that works regardless of lighting or whether the face is visible.
"""


class PersonDetector:
    def __init__(self, model_path, person_class, conf):
        # imported here (not at top) so the motion half of the system can still
        # run for testing even if ultralytics is not installed yet.
        from ultralytics import YOLO
        self.model = YOLO(model_path)
        self.person_class = person_class
        self.conf = conf

    def detect(self, frame):
        """Return a list of persons: [{'box': (x1,y1,x2,y2), 'conf': float}, ...]."""
        results = self.model(
            frame, verbose=False, classes=[self.person_class], conf=self.conf)

        persons = []
        for r in results:
            for b in r.boxes:
                if int(b.cls[0]) != self.person_class:
                    continue
                x1, y1, x2, y2 = (int(v) for v in b.xyxy[0])
                persons.append({"box": (x1, y1, x2, y2), "conf": float(b.conf[0])})
        return persons