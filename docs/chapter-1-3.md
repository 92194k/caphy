# CAPHY — Chapters 1–3 (Full Text, as of 2026-08-31)

> Saved verbatim from user-provided draft, then corrected against the actual running codebase (see "Key corrections" below). Supersedes the older project description (which incorrectly mentioned an "Adaptive Telegram Protocol" — not part of the current design).

## Key corrections vs. old project description
- **No Telegram anywhere.** Remote alerts are delivered via **Firebase Cloud Messaging (FCM)** to the **CAPHY App** (Flutter-based), not Telegram.
- **Live video viewing** = **MJPEG stream** (local Wi-Fi) + **WebRTC** (cross-network/mobile data) served by the **Flask** backend.
- Two-Factor Motion Validation = **OpenCV** (motion/frame differencing + CLAHE low-light enhancement) → **YOLOv8-nano** (human verification + distance/tier classification).
- **3-Tier Threat Level** (via bounding-box height distance estimation, with smoothing/hysteresis):
  - Tier 1: >4.5m → snapshot + notification
  - Tier 2: 2.5–4.5m → adds short video clip
  - Tier 3: <2.5m → adds siren
  - Armed Mode: any confirmed detection escalates to full Tier 3 response
- **Voice control (corrected 2026-08-31 — see full rewrite below): NOT Vosk.** The codebase has no working Vosk integration anywhere — a `voice/` folder using Vosk/Whisper/Porcupine existed but was legacy/abandoned code, confirmed dead (never imported by the running server or app) and has since been removed. The actual, currently-working voice assistant ("Ask CAPHY") lives entirely in the Flutter app (`ask_caphy_screen.dart`) plus a backend router (`assistant_ai/`): phone-native speech-to-text and text-to-speech, classified by a cloud LLM (**Groq**) into talk/know/do/clarify intents, with a **local keyword-matching fallback** (`assistant_ai/offline_fallback.py`) that keeps direct security commands (arm, disarm, stop siren, status) working with zero internet. Free-form conversational replies ("talk"/"know" answers) do require the cloud AI and are not available offline. Ollama was considered as a fully-local LLM fallback but was never implemented — it appears only in code comments as a future idea.
- Storage: **SQLite locally**, syncs to **Firebase (Firestore + Firebase Storage)** when internet is available. Hard deletes are used throughout (no soft-delete/trash); a small tombstone record on both sides (SQLite `deleted_alerts` table + Firestore `deleted_alerts` collection) lets every client — web dashboard, phone on LAN, phone on mobile data — reconcile and drop a deleted alert even if it missed the live broadcast.
- Hardware: repurposed laptop (Intel i3/Ryzen 3+, 4-8GB RAM, 256GB SSD+) as local server; laptop battery = free UPS; 2 independently controlled camera sources (webcam/Bluetooth/smartphone cam). **Detection resolution is 1920×1080 (native 1080p)** as of 2026-08-31 (previously 960×720; raised for sharper snapshots/recordings once testing confirmed no FPS/lag regression on target hardware). The live-view *streaming* copy shown in the app/dashboard is separately downscaled to 640×360 for bandwidth — this does not affect detection accuracy, recordings, or snapshot sharpness, which all use the full 1080p frame.
- Safety timers: 8-second arming grace period; camera warm-up period after startup; starts disarmed by default.
- Evaluation: ISO/IEC 25010 (Functional Suitability, Performance Efficiency, Compatibility, Usability, Reliability, Security, Maintainability, Portability) via 4-point Likert survey, 50 respondents (45 homeowners + 5 lab personnel, purposive sampling).
- Methodology: Applied Research + Agile Scrum Prototyping SDLC.
- Team: name "CAPHY" = "Capture" + "Alphy" (member Mark Alphy R. Miasis). Developed at Taguig City University.

---

## CHAPTER I — INTRODUCTION

### Background of the Study
Smart home security is now a standard household need because of the growth of Internet of Things (IoT) technology. However, most modern security systems rely heavily on cloud servers. This cloud-first design works well in countries with fast internet but causes problems in the Philippines. Issues like slow internet, frequent power outages, high hardware costs, and data privacy risks make these systems hard to use for average Filipino homes. Additionally, standard security cameras often cause "alert fatigue." This happens when users get too many notifications for harmless movements, causing them to eventually ignore real security threats. To solve this, a security system that processes data locally on a repurposed laptop offers a better solution. This allows continuous threat detection without needing expensive hardware or a constant internet connection.

### Rationale
The name CAPHY started during early planning. It combines "Capture" to represent the system capturing motion, and "Alphy" to represent team member Mark Alphy R. Miasis. The project is formally titled as an AI-Based Intelligent Security System with Two-Factor Motion Validation.

This study shows that repurposing an older laptop into a local security server gives homes reliable security at a low cost. Local AI processing is useful because motion detection and object verification still work even if the internet goes down. Furthermore, a smart connectivity feature ensures the system uses resources wisely. When the internet is active, the system syncs data to the cloud and sends remote alerts. When offline, it safely saves compressed data to local storage.

### Project Context
CAPHY is designed to handle the specific internet and power limits of Philippine households. To avoid the problems of slow internet and power cuts, CAPHY runs its core AI tasks locally on a repurposed laptop.

The system uses hybrid storage. When connected to the internet, verified alerts and videos are sent to the user through the CAPHY App and saved to cloud storage. When the network is offline or weak, the same data is compressed and kept locally until the connection returns. To save internet bandwidth and protect privacy, live camera viewing over the public internet is handled through WebRTC when the phone and laptop are on different networks, while same-Wi-Fi viewing uses a lightweight local MJPEG stream. Internet alerts are only sent to the owner when a real threat is verified and the internet is available.

### Purpose and Description
The main goal of this study is to build an affordable and strong home security system using a repurposed laptop. CAPHY uses Two-Factor Motion Validation to prevent false alarms and reduce alert fatigue. This process uses OpenCV to detect the first sign of movement. It then uses a YOLOv8-nano AI model to verify if that movement is a person.

CAPHY uses a 3-Tier Threat Level based on distance. The system estimates this distance using the height of the detected person's bounding box. Distance smoothing and hysteresis are used to keep the tier stable and prevent flickering alerts. The system integrates the CAPHY App to send alerts using Firebase Cloud Messaging. The system also includes a voice assistant ("Ask CAPHY") that lets users issue security commands and ask questions by voice from the app; direct commands (arm, disarm, stop siren, status) keep working even without internet through a local keyword-matching fallback, while natural conversational replies use a cloud AI model when online. To help the cameras see better in the dark, CAPHY applies CLAHE (Contrast Limited Adaptive Histogram Equalization) software enhancement. For safety, the system starts in a disarmed state. It applies a short eight-second arming grace period to stop immediate false alarms and uses a brief camera warm-up period after startup.

### Objectives of the Study
This study aims to design a local AI-based security system that uses two-factor motion validation and smart alert management to handle changing power and internet conditions.

Specifically, the study seeks to:
1. Design a security system that uses a repurposed laptop as a local server for AI processing, using its battery for backup power and SQLite for local storage.
2. Develop a Two-Factor Motion Validation process using OpenCV and YOLOv8-nano to help reduce false alerts and save hardware power.
3. Develop an alert management system via the CAPHY App that adjusts responses like snapshots, videos, and sirens based on a 3-Tier Threat Level and network availability.
4. Integrate a voice assistant feature into the CAPHY App that keeps core security commands (arm, disarm, stop siren) working without internet, while offering richer natural-language interaction when a connection is available.
5. Evaluate CAPHY using the ISO/IEC 25010 software quality model in terms of Functional Suitability, Performance Efficiency, Compatibility, Usability, Reliability, Security, Maintainability, and Portability.

### Conceptual Framework
Figure 1 presents the conceptual paradigm of the study using the Input–Process–Output (IPO) model. It illustrates how the study inputs are transformed through the development and operation of the CAPHY system into the expected outputs. A feedback loop is also included to show how testing and evaluation results will be used to identify areas for improvement and refine the system.

**Input phase:** hardware (repurposed laptop as local security server, two independently controlled camera sources — USB webcams, Bluetooth-connected cameras, or networked smartphone cameras), software (Python and Flask for the local server, YOLOv8-nano and OpenCV for motion detection and object verification, a cloud-AI-plus-local-fallback voice assistant integrated into the Flutter-based CAPHY App, SQLite for local event logging, Firebase for remote alerts and cloud storage), and live system data (camera feeds, voice commands, battery status, network status).

**Process phase:** planning, coding, testing, and improving the prototype. CAPHY performs Two-Factor Motion Validation (OpenCV detects movement, YOLOv8-nano verifies the object). CLAHE-based image enhancement for low-light conditions. 3-Tier Threat Level determines the appropriate security response. Voice commands are classified by a cloud AI model when online, falling back to a local keyword matcher for direct commands when offline. Monitoring Protocols manage notifications and stored security data based on available network connection — synced to cloud when online, retained locally when offline. Evaluated using ISO/IEC 25010.

**Output:** the completed CAPHY system — AI-based security monitoring, two-factor threat validation, tiered alerts, a voice assistant with an internet-independent command path, local and remote monitoring, resilient to short power/internet interruptions via laptop battery and local storage.

**Feedback** connects evaluation results back to development for improvements.

### Scope
This study covers the design, development, and evaluation of the CAPHY security system. The prototype supports two independently controlled camera feeds. It uses a repurposed laptop for local AI processing. The scope includes Two-Factor Motion Validation, the 3-Tier Threat Level logic, tiered alert routing via the CAPHY App, hybrid local and cloud storage, and a voice assistant with an offline-capable command path. The evaluation is limited to testing the system using the ISO/IEC 25010 standard.

### Limitations
The system is designed for controlled testing environments like a university laboratory and a residential house. The hardware is limited to two camera feeds. Same-Wi-Fi live viewing uses a local MJPEG stream; cross-network viewing uses WebRTC and depends on internet/TURN relay availability. The CLAHE software improves low-light visibility but does not provide real infrared night vision in total darkness. Distance estimation for the 3-Tier Threat Level relies on bounding-box heights instead of real depth sensors and needs manual calibration (recalibrated whenever the detection capture resolution changes). Voice control's natural-language conversation ability depends on a cloud AI service and is unavailable offline — only a fixed set of direct security commands (arm, disarm, stop siren, status) continue to work without internet, via local keyword matching. Finally, while local detection, recording, and those direct voice commands work offline, remote push notifications, cloud syncing, and conversational voice replies require an active internet connection.

### Significance of the Study
This study focuses on CAPHY, an AI-based intelligent security system that uses two-factor motion validation and monitoring protocols, developed as a prototype at Taguig City University. The study aims to provide a more accessible, reliable, and privacy-conscious approach to residential security despite limitations in internet connectivity and power availability.

Beneficiaries: Filipino Homeowners (budget-friendly alternative, reliable during outages/slow internet), Cybersecurity and Data Privacy Sector (demonstrates hybrid local/cloud privacy protection), Future Researchers (architecture guide for edge computing and smart home security), Academic Institution (practical test of computer vision and software engineering).

### Operational and Technical Definition of Terms
(Full glossary — see source draft for all entries; key ones below)

- **Alert Fatigue** – reduced through Two-Factor Motion Validation so only verified threats trigger notifications.
- **Armed Mode** – any confirmed detection escalates to full Tier 3 response (siren, video recording, high-priority alert).
- **Arming Grace Period** – 8-second default interval after arming during which alerts/siren are suppressed.
- **CAPHY App** – Flutter mobile app; receives alerts, views captured media, accesses camera feeds over local network or WebRTC, hosts the "Ask CAPHY" voice assistant.
- **Camera Warm-Up Period** – short interval after camera/system startup where detection is held back.
- **CLAHE** – image-processing technique for low-light contrast enhancement.
- **Edge (Local) AI Processing** – OpenCV + YOLOv8-nano run on the repurposed laptop, no internet required.
- **Firebase Cloud Messaging (FCM)** – delivers verified alerts from host laptop to CAPHY App when internet is available.
- **Hybrid Local–Cloud Architecture** – local AI processing; local or cloud (Firebase) storage depending on connectivity.
- **MJPEG Live Stream** – sequential JPEG images over HTTP; used for live camera viewing on local Wi-Fi.
- **WebRTC Live Stream** – peer-to-peer video used for live camera viewing when phone and laptop are on different networks.
- **Monitoring Protocols** – determines notification/media response based on threat tier + network availability.
- **Ask CAPHY (Voice/Chat Assistant)** – app-side assistant combining phone-native speech-to-text/text-to-speech with a cloud AI (Groq) for intent classification and conversation; falls back to a local keyword matcher for direct commands (arm, disarm, stop siren, status) when offline.
- **Night Vision (Low-Light Enhancement)** – software-only via CLAHE; not true IR night vision.
- **OpenCV** – first factor of motion validation (frame differencing).
- **Repurposed Hardware Architecture** – older laptop as local server; built-in battery as backup power.
- **Tombstone Record** – a small persisted marker (local SQLite + Firestore) recording that an alert was deleted, so every connected client can reconcile and remove it even after a hard delete.
- **Two-Factor Motion Validation** – OpenCV motion detection → YOLOv8-nano human verification.
- **3-Tier Threat Level** – Tier 1 (>4.5m, snapshot+notification), Tier 2 (2.5–4.5m, +video clip), Tier 3 (<2.5m, +siren); Armed Mode escalates all to Tier 3.
- **YOLO** – real-time object detection model; YOLOv8-nano used as second validation factor.

---

## CHAPTER II — REVIEW OF RELATED LITERATURE AND STUDIES

Themes covered: localized processing and data privacy, hardware sustainability, lightweight motion validation, reliable alert delivery, voice interaction with a resilient offline path, alert fatigue, and repurposed consumer hardware.

### Localized Processing and Data Privacy
- IBM (2024): edge AI reduces latency, limits transmitted data, protects sensitive info — supports CAPHY's local motion analysis/verification.
- Hall et al. (2020): smart-home privacy risks of continuous transmission to third-party servers — supports local processing + dedicated app for alerts.
- RA 10173 (Data Privacy Act of 2012, Official Gazette 2012) — supports hybrid local-cloud design minimizing exposure.
- Redmon et al. (2016): introduced YOLO. Ultralytics (2023): documents YOLOv8 family incl. YOLOv8-nano, runs on CPU/GPU — supports CAPHY's local object verification.

### Hardware Sustainability and Software-Based Motion Validation
- Benoit-Cattin, Velasco-Montero, Fernández-Berni (2020): sustained visual inference on CPU edge devices → thermal throttling — supports staged/activated-when-needed AI architecture.
- Singla (2014): frame differencing is lightweight vs. deep learning — supports OpenCV as first-stage motion detection.
- Philippine Institute for Development Studies (2024): recurring power reliability issues in PH — supports laptop-battery-as-UPS design.

### Reliable Alert Delivery under Varying Network Conditions
- PIDS (2024) & World Bank (2025): broadband availability/affordability gaps in PH.
- Pushwoosh (2024): app-based push notifications deliver fast when device is online — supports CAPHY App + FCM.
- Hojjat, Haberer, Landsiedel (2024): adaptive compression for IoT under bandwidth constraints — supports prioritizing lightweight alerts before larger media, plus local storage fallback + sync on reconnect.

### Voice Interaction with a Resilient Offline Command Path
- FTC (2023): privacy concerns with cloud-based voice assistants (false activations, recorded data handling) — motivates keeping direct security commands (arm/disarm/siren) executable without depending on a cloud round-trip.
- General literature on hybrid cloud/edge NLU pipelines: routing free-form language to a cloud model while handling a small closed set of critical commands with a local matcher is a common resilience pattern for assistants that must keep working during connectivity loss — this is the pattern CAPHY's voice assistant follows (cloud AI classification via Groq, local keyword fallback via `offline_fallback.py`).
- Multi-turn/keyword confirmation reduces accidental activation from background noise or misrecognized speech.

### User-Centered Design and Alert-Fatigue Mitigation
- Proofpoint (2024); IBM (2024): frequent false/unnecessary alerts desensitize users — supports Two-Factor Motion Validation.
- Liang, Ma, Zhang (2022): monocular camera distance estimation feasibility — supports bounding-box-height-based proximity estimation and the 3-Tier Threat Level (with smoothing/hysteresis at boundaries).

### Economic Viability of Repurposed Consumer Electronics
- PIDS (2024); World Bank (2025): PH digital divide (broadband access/affordability).
- World Economic Forum (2023): frugal innovation — creating value with fewer resources, incl. repurposing existing tools.
- Environmental Management Bureau (2025): e-waste concern in PH — repurposing old laptops/cameras extends useful life.
- Consumer laptops combine processing + built-in battery, ideal for short power-interruption resilience.

### Synthesis
Existing literature supports the individual technologies (local AI, motion detection, object verification, resilient voice interaction, proximity estimation, reliable alert delivery, repurposed hardware) but treats them as separate solutions. CAPHY's contribution is integrating all of them — Repurposed Hardware Architecture, Two-Factor Motion Validation, 3-Tier Threat Level, CAPHY App, local+cloud storage, a voice assistant with a resilient offline command path — into one affordable system suited to Filipino households.

---

## CHAPTER III — RESEARCH METHODOLOGY

### Research Methodology
Applied Research Method + Agile Prototyping SDLC (Scrum). Applied research fits because CAPHY addresses real-world concerns (alert fatigue, network dependency, reliability). Agile Scrum supports iterative refinement of motion detection, threat-tier classification, low-light enhancement, remote monitoring, and the CAPHY App, followed by final system evaluation.

### Population, Sample Size, and Sampling Technique
50 participants total: 45 residential homeowners (90%) + 5 computer laboratory personnel (10%). Lab personnel evaluate technical aspects; homeowners assess usability of the voice assistant and CAPHY App alerts. Purposive sampling.

**Table 1 — Distribution of Respondents**
| Respondents | Sample (n) | Percentage |
|---|---|---|
| Residential Homeowners | 45 | 90% |
| Computer Laboratory Personnel | 5 | 10% |
| **Total** | **50** | **100%** |

### Research Instrument
Structured survey questionnaire based on ISO/IEC 25010 (Functional Suitability, Performance Efficiency, Compatibility, Usability, Reliability, Security, Maintainability, Portability). 4-point Likert scale: 4 = Strongly Agree, 3 = Agree, 2 = Disagree, 1 = Strongly Disagree.

### Data Gathering Procedure
Purposive sampling, no formal interviews — data via structured questionnaire only. Procedure: deploy CAPHY in testing environments → brief participants on cameras, CAPHY App, and the voice assistant → simulate motion events at different distances to test Two-Factor Motion Validation, 3-Tier Threat Level, Armed Mode → test under daytime/low-light for CLAHE → test CAPHY App, local dashboard, voice commands (both online and with internet disabled) → distribute ISO/IEC 25010 questionnaire (face-to-face or online) → collect, tally, analyze.

### Statistical Treatment of the Data
1. **Frequency and Percentage Distribution** — P = (f / N) × 100
2. **Weighted Mean** — WM = Σ(f × w) / N
3. **Standard Deviation** — SD = √[Σ(x − x̄)² / N]

**Table 2 — Four-Point Likert Scale**
| Mean Range | Scale | Interpretation |
|---|---|---|
| 3.26–4.00 | 4 | Strongly Agree (SA) |
| 2.51–3.25 | 3 | Agree (A) |
| 1.75–2.50 | 2 | Disagree (D) |
| 1.00–1.74 | 1 | Strongly Disagree (SD) |

### Technical Requirements

**A. Hardware Requirements**
- Host machine: Intel Core i3 / Ryzen 3 or higher, 256GB+ SSD, 4–8GB RAM. Laptop battery = backup power during short outages.
- Camera Sources: 2 independently controlled sources (laptop built-in cam + USB webcam / Bluetooth cam / smartphone cam via companion app). Each opened/closed independently. Detection capture resolution: 1920×1080 (native 1080p) as of the 2026-08-31 revision, verified to run without lag on target hardware; separately, the live-view stream shown in the app/dashboard is downscaled to 640×360 purely for bandwidth and does not affect detection, recordings, or snapshots.

**B. Software Requirements**
- Python 3.10+ (main environment, supports Flask backend).
- Visual Studio Code (primary IDE).
- OpenCV (motion detection + CLAHE) and YOLOv8-nano (human verification + threat classification).

**C. Network Requirements**
- Local Wi-Fi network for MJPEG live camera feeds; WebRTC (with TURN relay) for cross-network live viewing.
- Active internet required for Firebase Cloud Messaging, cloud sync, and conversational (non-command) voice assistant replies.
- Offline: continues via local processing + SQLite storage; direct voice security commands (arm/disarm/stop siren/status) still work via local keyword matching.

**D. API Specifications**
- CAPHY App (Flutter) + Firebase: FCM for push notifications, Firestore + Firebase Storage for event/media sync.
- Groq API: cloud LLM used by the "Ask CAPHY" assistant to classify spoken/typed input and hold natural conversation when internet is available.
- Phone-native speech-to-text and text-to-speech (on-device via the OS, through Flutter's `speech_to_text` and `flutter_tts` packages) — not a locally-hosted server-side voice engine.

**E. Project Design**
- Local dashboard: HTML/CSS served via Flask backend.
- Mobile app: Flutter.
- Consistent dark-navy security theme across both interfaces.

### Diagrams (planned)
- System Architecture (Figure 10): 2 camera sources → Flask-coordinated host laptop → OpenCV (motion + CLAHE) → YOLOv8-nano (verification + tier) → local response + CAPHY App alerts (voice assistant lives in the app, calling Groq when online / local keyword matcher when offline) → local storage synced to Firebase when online.
- Data Flow Diagram Context Level 0 (Figure 11) and Level 1 (Figure 12).
- Flowchart (Figure 13): camera warm-up → Two-Factor Motion Validation → threat-tier classification → alert generation → Armed Mode response → voice command handling (cloud AI or offline keyword fallback) → return to monitoring.
- UML Use Case Diagram (Figure 14): Homeowner, Intruder, CAPHY App actors; functions = monitor environment, control system state, detect threats, receive alerts.
- Database Structure (Figure 15): local SQLite tables for security events, deletion tombstones, and system status (timestamps, threat tiers, media references, arming status, battery info).
- System Development (Figure 16): Agile Scrum-based development/testing process — iterative integration/testing of OpenCV, YOLOv8-nano, the voice assistant, Flask, CAPHY App, dashboard, mobile interface, voice commands, motion detection, and threat-tier thresholds.

### Algorithm Discussion

**Table 3 — Comparison of Algorithms**
| Algorithm | Strengths | Weaknesses |
|---|---|---|
| OpenCV (Frame Differencing) | Low CPU usage; real-time motion detection; efficient motion trigger | Sensitive to lighting/environment changes; can't identify object type |
| YOLOv8-nano | Detects human objects; improves motion validation; reduces false detections | More CPU-intensive than basic motion detection; continuous use raises system load |
| Groq-based Intent Classification + Local Keyword Fallback | Fast, capable natural-language understanding when online; direct security commands still work fully offline via the keyword fallback | Free-form conversational ability is unavailable without internet; fallback only covers a fixed set of predefined commands |

1. **OpenCV** — frame differencing + CLAHE low-light enhancement. Initial motion detector; triggers further detection only when motion found (reduces unnecessary processing).
2. **YOLOv8-nano** — lightweight real-time object detection. Validates OpenCV-detected motion by identifying human figures; supports distance-based threat-tier classification.
3. **Voice Assistant Router (Groq + Local Fallback)** — spoken/typed input is transcribed on-device (phone-native speech-to-text) and sent to a cloud AI (Groq) that classifies it into talk/know/do/clarify and executes recognized commands (arm, disarm, silence siren, etc.). If the cloud AI is unreachable (no internet, provider down), a local keyword matcher takes over for the same fixed set of direct commands, so core voice control never goes fully silent offline — only open-ended conversation requires connectivity.
</content>
