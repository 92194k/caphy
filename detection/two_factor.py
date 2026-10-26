"""The two-factor gate - the defensible core of CAPHY.

    Motion?  --NO-->  ignore
      |YES
    Run YOLO  (only every Nth frame for speed - boxes reused in between)
      |
    Person, big enough, AND at/above conf? --NO--> ignore (empty movement,
      |YES                                          shadow, tiny noise, or
      |                                              a non-person object)
    Seen for several evaluations IN A ROW?  --NO--> not yet confirmed
      |YES                                          (pending, no alert)
    CONFIRMED PERSON --> classify into Tier 1 / 2 / 3 by distance

Factor 1 (cheap, motion) gates Factor 2 (expensive, AI). Factor 2 itself is
now a small state machine, not a single yes/no per frame - see "Why a single
YOLO frame is not enough" below.

To cut lag, YOLO runs every Nth frame while a person is present and the last
ACCEPTED boxes are reused on the frames in between - this roughly doubles the
frame rate at N=2 with no loss of coverage.

WHY A SINGLE YOLO FRAME IS NOT ENOUGH
--------------------------------------
YOLO clearing its own confidence threshold on ONE frame used to be treated as
"a person is here" immediately. That is exactly how a one-off misfire - a
sudden color/lighting change, a shadow, a reflection, a moving piece of
furniture - could turn into a false "person detected" alert if YOLO happened
to misclassify that single frame. A real human does not flicker in and out of
existence; a lighting glitch does.

Two extra, still AI-only checks now sit between "YOLO said person" and
"CAPHY calls it a confirmed threat" (config.py has the tunable numbers):

  1. SIZE - the box must be big enough (height AND area, as a fraction of
     the frame) to plausibly be a real, nearby human, not a tiny fragment of
     noise. This does NOT use color, brightness, or motion-area - only the
     size of the box YOLO itself drew around what it thinks is a person.
  2. PERSISTENCE (temporal smoothing / hysteresis) - an accepted detection
     must keep showing up for several YOLO evaluations IN A ROW (with a
     little tolerance for one missed evaluation, so a person briefly turning
     side-on doesn't reset all progress) before it is "confirmed". Only a
     confirmed person ever sets result["threat"] = True, which is the single
     value every downstream feature (alerts, recording, siren, tier display)
     already keys off - so this fix required no changes anywhere else.

Nothing in this file ever looks at frame colors, pixel brightness, or the
raw motion-detector's area/magnitude as evidence that something is a person -
those numbers exist only for Factor 1's cheap "did anything move at all?"
gate and for the thesis evaluation log, never for this decision.

A STANDING-STILL PERSON IS STILL A PERSON
------------------------------------------
Motion (Factor 1) is only required to START a confirmation - it is not
required to KEEP one. A background-subtraction motion detector correctly
reports "no motion" once a person stops moving, but that person has not
vanished from the actual video frame, so YOLO can and still does see them.
Once a person is CONFIRMED, this file keeps evaluating YOLO on the raw frame
even while motion is absent, and only drops the confirmation if YOLO ALSO
stops finding them for PERSON_STATIONARY_GRACE evaluations in a row (config
.py). A scene that was never confirmed and currently has no motion is still
left alone immediately, exactly as before - this only affects someone who
has already cleared the full two-factor bar at least once.
"""


class TwoFactorDetector:
    def __init__(self, motion_detector, person_detector=None, tier_engine=None,
                 person_every_n=1, min_box_height_frac=0.06, min_box_area_frac=0.0025,
                 consecutive_required=3, consecutive_grace=1, stationary_grace=6):
        self.motion = motion_detector
        self.person = person_detector
        self.tier = tier_engine
        self.person_every_n = max(1, int(person_every_n))
        self._frame_i = 0
        self._last_persons = []          # last ACCEPTED (size-filtered) boxes, reused between YOLO evaluations

        # ---- size filter ----
        self.min_box_height_frac = float(min_box_height_frac)
        self.min_box_area_frac = float(min_box_area_frac)

        # ---- temporal persistence / hysteresis (INITIAL confirmation - motion-gated) ----
        self.consecutive_required = max(1, int(consecutive_required))
        self.consecutive_grace = max(0, int(consecutive_grace))
        self._consecutive_hits = 0
        self._consecutive_misses = 0
        self._confirmed = False

        # ---- stationary hold (keeps an ALREADY-confirmed person even once
        # they stop moving; see process() for why) ----
        self.stationary_grace = max(0, int(stationary_grace))
        self._stationary_misses = 0

    def reset(self):
        """Forget the current person entirely. Call when there is no motion
        and no one is currently confirmed, or when persistence/stationary
        hold has been lost for longer than its grace window."""
        self._last_persons = []
        self._consecutive_hits = 0
        self._consecutive_misses = 0
        self._stationary_misses = 0
        self._confirmed = False
        if self.tier is not None:
            self.tier.reset()

    def _passes_size_filter(self, box, frame_shape):
        """True if this box is big enough to plausibly be a real, nearby
        human - purely a size check against the box YOLO already drew
        around a "person" classification. Never uses color or brightness."""
        x1, y1, x2, y2 = box
        h = max(0, y2 - y1)
        w = max(0, x2 - x1)
        fh, fw = frame_shape[0], frame_shape[1]
        if fh <= 0 or fw <= 0:
            return True
        height_frac = h / fh
        area_frac = (h * w) / float(fh * fw)
        return height_frac >= self.min_box_height_frac and area_frac >= self.min_box_area_frac

    def process(self, frame):
        result = {"motion": False, "motion_area": 0.0, "ran_yolo": False,
                  "persons": [], "threat": False, "tier": 0, "mask": None,
                  "candidate_persons": 0, "confirmed": False,
                  "consecutive_hits": 0, "consecutive_required": self.consecutive_required,
                  "stationary_misses": 0, "stationary_grace": self.stationary_grace,
                  "reject_reason": None, "best_rejected": None}

        moved, area, mask = self.motion.detect(frame)
        result["motion"] = moved
        result["motion_area"] = area
        result["mask"] = mask

        if self.person is None:
            if not moved:
                self.reset()
            result["reject_reason"] = "no_person_model"
            return result

        # Factor 1 (motion) gates the INITIAL confirmation: a person can only
        # start being confirmed while real motion is present (hits only ever
        # accumulate below when moved is True). BUT a person who has ALREADY
        # been confirmed and then simply stops moving (stands still) is not
        # gone - a background-subtraction motion detector reporting "no
        # motion" for a static body is expected and correct, it is not
        # evidence the person left. So once confirmed, keep calling YOLO on
        # the raw frame (which still shows them, motion or not) instead of
        # instantly clearing them the moment motion stops. A scene that was
        # never confirmed and has no motion is left alone exactly as before.
        if not moved and not self._confirmed:
            self.reset()
            result["reject_reason"] = "no_motion"
            return result

        # frame-skip: run YOLO every Nth frame, reuse the last ACCEPTED boxes otherwise
        self._frame_i += 1
        ran = (self._frame_i % self.person_every_n == 0) or not self._last_persons
        if ran:
            raw_persons, best_rejected = self.person.detect(frame)
            accepted = [p for p in raw_persons if self._passes_size_filter(p["box"], frame.shape)]
            rejected_small = len(raw_persons) - len(accepted)

            if accepted:
                if moved:
                    self._consecutive_hits += 1
                    self._consecutive_misses = 0
                else:
                    # Already-confirmed, stationary person: YOLO still sees
                    # them, so just hold the confirmation. This does not
                    # advance _consecutive_hits - that count is reserved for
                    # the motion-gated INITIAL confirmation - it only clears
                    # the stationary-miss counter below.
                    self._stationary_misses = 0
                if self.tier is not None:
                    for p in accepted:
                        p.update(self.tier.classify(p["box"]))
                self._last_persons = accepted
            elif moved:
                self._consecutive_misses += 1
                if self._consecutive_misses > self.consecutive_grace:
                    # Genuinely gone (missed longer than the grace window) -
                    # lose confirmation progress and clear state so the NEXT
                    # detection has to earn confirmation from scratch again.
                    self._consecutive_hits = 0
                    self._confirmed = False
                    self._last_persons = []
                    self._stationary_misses = 0
                    if self.tier is not None:
                        self.tier.reset()
                # else: within the grace window - deliberately keep
                # self._last_persons and confirmation progress as-is, so one
                # missed evaluation (e.g. a person turned side-on for a
                # moment) doesn't wipe out an otherwise-real detection. This
                # is the "hysteresis" half of the persistence check.
            else:
                # Confirmed, no motion, AND YOLO also failed to find them
                # this evaluation - they may genuinely have left (or slipped
                # out of frame) while standing still. Tolerate a few misses
                # (PERSON_STATIONARY_GRACE) before giving up, same hysteresis
                # idea as the motion-gated path above.
                self._stationary_misses += 1
                if self._stationary_misses > self.stationary_grace:
                    self._consecutive_hits = 0
                    self._confirmed = False
                    self._last_persons = []
                    self._stationary_misses = 0
                    if self.tier is not None:
                        self.tier.reset()

            if self._consecutive_hits >= self.consecutive_required:
                self._confirmed = True

            result["candidate_persons"] = len(raw_persons)
            result["best_rejected"] = best_rejected
            if not raw_persons:
                result["reject_reason"] = "no_person_class_at_confidence"
            elif rejected_small and not accepted:
                result["reject_reason"] = f"too_small(rejected={rejected_small})"
            elif accepted and not self._confirmed:
                result["reject_reason"] = (
                    f"pending_confirmation({self._consecutive_hits}/{self.consecutive_required})")

        result["ran_yolo"] = ran
        result["consecutive_hits"] = self._consecutive_hits
        result["stationary_misses"] = self._stationary_misses
        result["confirmed"] = self._confirmed

        if self._confirmed and self._last_persons:
            result["persons"] = self._last_persons
            result["threat"] = True
            if self.tier is not None:
                result["tier"] = max(p["tier"] for p in self._last_persons)
        else:
            result["persons"] = []
            result["threat"] = False

        return result
