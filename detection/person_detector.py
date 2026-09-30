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
        # BUG FIX: this used to call resource_path(os.path.basename(model_path)),
        # which strips the "models/" directory from "models/caphy_person_best.pt"
        # before resolving - but CAPHY.spec bundles the weights AT
        # _MEIPASS/models/caphy_person_best.pt (datas=[('models/caphy_person_best.pt', 'models')]),
        # keeping the models/ subfolder. Stripping it made the fallback look in
        # the wrong place too, so BOTH the raw relative path (broken once the
        # frozen app changes its working directory to %LOCALAPPDATA%\CAPHY)
        # and the "fixed" fallback failed - which is exactly why Person
        # Detection silently stayed off with a "models\\caphy_person_best.pt"
        # not-found warning in System Logs. Passing the full relative path
        # (not just its basename) through resource_path() preserves the
        # models/ subfolder, matching how the .exe actually bundles it.
        import os as _os
        if not _os.path.exists(model_path):
            try:
                from resource_path import resource_path
                _b = resource_path(model_path)
                if _os.path.exists(_b):
                    model_path = _b
            except Exception:
                pass
        self.model = YOLO(model_path)
        self.person_class = person_class
        self.conf = conf
        self.imgsz = imgsz            # smaller = faster inference
        # Internal-only floor passed to the model call itself (see detect()) -
        # deliberately much lower than self.conf so a sub-threshold or
        # wrong-class detection still comes back from YOLO instead of being
        # silently dropped before this code can see and log it. This is NOT
        # a relaxation of what counts as an accepted person - self.conf is
        # still the only threshold that decides that.
        self._debug_conf_floor = 0.10

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
        """Return (persons, best_rejected).

        persons: [{'box': (x1,y1,x2,y2), 'conf': float}, ...] - ONLY boxes
                 YOLO itself classified as the trained "person" class AND
                 at/above self.conf. This is the sole piece of evidence
                 CAPHY ever treats as "this might be a person" - nothing
                 about color, brightness, or motion size feeds into it.

        best_rejected: debug info about the single highest-confidence
                 detection this frame that did NOT qualify as an accepted
                 person (wrong class, or right class but under threshold),
                 or None if YOLO found nothing at all. Never used as
                 detection evidence - purely so the pipeline can log WHY a
                 motion event was rejected (see detection/two_factor.py
                 and Worker.run()'s "DETECT" log line), e.g. "closest match
                 was class=chair conf=0.31" instead of just "no person".

        BUG FIX (evidence quality): this used to call the model with
        classes=[self.person_class] and conf=self.conf, which means any
        detection that didn't already qualify was silently discarded by
        ultralytics itself before this code ever saw it - there was no way
        to tell "YOLO saw nothing" apart from "YOLO saw a person-shaped
        thing at 0.40 confidence, just under our 0.65 floor" apart from
        "YOLO saw a chair". Running with NO class filter and a low internal
        floor, then doing the real accept/reject filtering here in Python,
        costs nothing extra (ultralytics' classes= argument only filters
        results AFTER inference, it does not skip work) and turns every
        rejection into a debuggable, loggable reason instead of a silent
        "nothing happened".
        """
        results = self.model(
            frame, verbose=False, conf=self._debug_conf_floor,
            imgsz=self.imgsz, device=self.device)

        persons = []
        best_rejected = None
        for r in results:
            names = getattr(r, "names", None) or getattr(self.model, "names", {})
            for b in r.boxes:
                cls_id = int(b.cls[0])
                conf = float(b.conf[0])
                x1, y1, x2, y2 = (int(v) for v in b.xyxy[0])
                if cls_id == self.person_class and conf >= self.conf:
                    persons.append({"box": (x1, y1, x2, y2), "conf": conf})
                    continue
                # Track the single best non-qualifying detection this frame,
                # purely for logging - never fed back into any accept/reject
                # decision.
                if best_rejected is None or conf > best_rejected["conf"]:
                    best_rejected = {
                        "box": (x1, y1, x2, y2),
                        "conf": conf,
                        "cls_id": cls_id,
                        "cls_name": names.get(cls_id, str(cls_id)) if isinstance(names, dict) else str(cls_id),
                        "is_person_class": cls_id == self.person_class,
                    }
        return persons, best_rejected