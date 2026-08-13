"""Factor 2 of the two-factor pipeline: person verification with YOLOv8.

This only runs AFTER Factor 1 sees motion, so YOLO is not wasted on empty frames.
It reads the human silhouette (the 'person' class), not the face - that is the
part of CAPHY that works regardless of lighting or whether the face is visible.
"""


class PersonDetector:
    def __init__(self, model_path, person_class, conf, imgsz=640):
        # imported here (not at top) so the motion half of the system can still
        # run for testing even if ultralytics is not installed yet.
        from ultralytics import YOLO
        # Resolve the weights path so it works when packaged into the .exe
        # (the .pt is bundled and unpacked to a temp dir, not the cwd).
        import os as _os
        if not _os.path.exists(model_path):
            try:
                from resource_path import resource_path
                _b = resource_path(_os.path.basename(model_path))
                if _os.path.exists(_b):
                    model_path = _b
            except Exception:
                pass
        self.model = YOLO(model_path)
        self.person_class = person_class
        self.conf = conf
        self.imgsz = imgsz            # smaller = faster inference

        # Ultralytics defaults to CPU unless a device is explicitly given -
        # it does NOT auto-detect and use an available NVIDIA GPU on its
        # own. On hardware with a real GPU (e.g. this project's RTX 3060
        # laptop GPU), running inference on the CPU instead leaves the
        # single biggest available speedup completely unused, and is very
        # likely why detection-heavy frames felt slow/laggy even on
        # otherwise capable hardware - every 8th frame (PERSON_EVERY_N)
        # was taking far longer than it needed to on the CPU path. This
        # detects CUDA availability once at startup and pins inference to
        # the GPU when present, silently falling back to CPU (unchanged
        # behavior) on machines without one - so this is a pure win where
        # available and a no-op everywhere else.
        self.device = "cpu"
        try:
            import torch
            if torch.cuda.is_available():
                self.device = "cuda:0"
                print(f"[CAPHY] YOLO person detector using GPU: {torch.cuda.get_device_name(0)}")
            else:
                print("[CAPHY] YOLO person detector using CPU (no CUDA GPU detected)")
        except Exception as e:
            print(f"[CAPHY] Could not check for GPU, using CPU ({e})")

    def detect(self, frame):
        """Return a list of persons: [{'box': (x1,y1,x2,y2), 'conf': float}, ...]."""
        results = self.model(
            frame, verbose=False, classes=[self.person_class], conf=self.conf,
            imgsz=self.imgsz, device=self.device)

        persons = []
        for r in results:
            for b in r.boxes:
                if int(b.cls[0]) != self.person_class:
                    continue
                x1, y1, x2, y2 = (int(v) for v in b.xyxy[0])
                persons.append({"box": (x1, y1, x2, y2), "conf": float(b.conf[0])})
        return persons