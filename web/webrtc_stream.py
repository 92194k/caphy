"""
webrtc_stream.py — true live video streaming for CAPHY, over WebRTC.

Why this exists: the existing /video_feed MJPEG stream only ever worked on
the same LAN as the laptop (it's a plain HTTP endpoint at Store.baseUrl).
Getting REAL live video (not periodic snapshots) to a phone that's on a
different network needs an actual peer-to-peer video connection - that's
what WebRTC is for, and it's the same technology Ring/Nest/Arlo use for
this exact problem.

How it fits together:
  - Video itself flows PHONE <-> LAPTOP directly (peer-to-peer), or through
    a free TURN relay when direct P2P isn't possible (symmetric NAT,
    restrictive networks). No CAPHY-owned server ever sees the video.
  - Setting UP that connection (the "signaling" handshake - exchanging an
    SDP offer/answer and ICE candidates) rides on Firestore, exactly like
    the remote command queue in storage/device_registry.py - small JSON
    documents, not media.
  - On the SAME LAN, the phone should still just use /video_feed directly
    (see api.dart) - it's simpler and has zero setup latency. WebRTC is
    specifically the "I'm not on the same WiFi as my laptop" path.

Firestore layout (webrtc_calls/{call_id}):
    device_id       str
    caller_uid      str   — must equal devices/{device_id}.owner_uid
    cam             int   — which camera to stream
    offer           {sdp, type}
    answer          {sdp, type} | None
    caller_ice      [ {candidate, sdpMid, sdpMLineIndex}, ... ]
    callee_ice      [ ... ]
    status          "pending" | "answered" | "closed" | "error"
    turn_available  bool  — false if Metered.ca's TURN relay couldn't be
                            reached for this call (fell back to STUN-only);
                            lets the phone show a heads-up instead of just
                            hanging on "connecting..." on restrictive networks
    created_at      ts
"""

import asyncio
import fractions
import threading
import time
import urllib.request
import urllib.error
import json

import cv2
import numpy as np

import config
import secrets_config

try:
    from aiortc import RTCPeerConnection, RTCConfiguration, RTCIceServer, VideoStreamTrack
    from av import VideoFrame
    AIORTC_AVAILABLE = True
    AIORTC_IMPORT_ERROR = None
except Exception as _imp_err:   # ImportError, or a broken av/aiortc build
    AIORTC_AVAILABLE = False
    AIORTC_IMPORT_ERROR = repr(_imp_err)


def _fetch_metered_ice_servers(timeout=6):
    """
    Fetches fresh TURN+STUN credentials from Metered.ca's REST API - no
    username/password is ever stored anywhere in this repo or in
    config.py; only the API key does (via secrets_config.py, itself
    loaded from an env var or the git-ignored caphy_keys.json). Metered
    mints short-lived credentials scoped to the request, and hands back
    servers nearest the caller for lowest latency.

    Called fresh every time a call starts (not cached at process startup)
    so credentials never go stale mid-lifetime of the desktop process.

    Returns a list of dicts like {"urls": ..., "username": ..., "credential": ...}
    on success, or None on any failure (network down, bad/missing key,
    Metered outage) - callers must fall back to STUN-only in that case.
    """
    domain = secrets_config.get_metered_domain()
    api_key = secrets_config.get_metered_api_key()
    if not domain or not api_key:
        return None

    url = f"https://{domain}/api/v1/turn/credentials?apiKey={api_key}"
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            data = json.loads(resp.read().decode())
        if isinstance(data, list) and data:
            return data
        return None
    except Exception as e:
        print(f"[CAPHY WebRTC] Metered TURN fetch failed, falling back to STUN-only: {e}")
        return None


def _ice_servers_raw():
    """
    Returns (raw_ice_server_dicts, turn_available) - plain JSON-safe dicts
    (not aiortc objects), so this same result can be:
      1. Used to build aiortc's RTCIceServer list for THIS laptop's own
         RTCPeerConnection, and
      2. Written straight into the call doc's "ice_servers" field so the
         PHONE can also use the same real TURN credentials via
         RTCPeerConnection.setConfiguration() - the phone never talks to
         Metered or holds the API key itself (an APK can be decompiled,
         which would leak it); only this server-side code ever does.

    Tries Metered.ca first (real TURN relay coverage); on any failure,
    falls back to the public STUN-only list in config.FALLBACK_STUN_URLS -
    direct P2P still works for most network pairs on STUN alone, it just
    can't relay through the harder NAT cases without TURN.
    """
    fetched = _fetch_metered_ice_servers()
    if fetched:
        cleaned = []
        for entry in fetched:
            urls = entry.get("urls") or entry.get("url")
            if not urls:
                continue
            cleaned.append({
                "urls": urls if isinstance(urls, list) else [urls],
                "username": entry.get("username"),
                "credential": entry.get("credential"),
            })
        if cleaned:
            return cleaned, True

    print("[CAPHY WebRTC] Using fallback STUN-only servers - TURN relay is "
          "temporarily unavailable, calls on restrictive networks may fail.")
    fallback_urls = getattr(config, "FALLBACK_STUN_URLS", None) or [
        "stun:stun.l.google.com:19302",
    ]
    return [{"urls": [u], "username": None, "credential": None} for u in fallback_urls], False


def _ice_servers():
    """Builds the aiortc RTCIceServer list for THIS laptop's own
    RTCPeerConnection (used by _answer_call below). See _ice_servers_raw()
    for the JSON-safe version sent to the phone."""
    raw, turn_available = _ice_servers_raw()
    servers = [RTCIceServer(urls=e["urls"], username=e["username"], credential=e["credential"])
               for e in raw]
    return servers, turn_available


# TURN credentials the laptop publishes to its device doc so the PHONE can
# gather relay candidates from the very start of a call (essential on mobile
# data / carrier-grade NAT, where a direct connection is impossible and the
# relay is the ONLY path). Cached because Metered credentials are valid for
# hours - re-fetching on every heartbeat would be wasteful and could hit
# rate limits.
_turn_cache = {"servers": None, "turn_ok": False, "ts": 0.0}
_TURN_CACHE_TTL_SEC = 30 * 60


def get_ice_servers_cached():
    """Returns (raw_ice_server_dicts, turn_available), refreshing from
    Metered at most every ~30 min. Safe to call frequently (heartbeat)."""
    now = time.time()
    if _turn_cache["servers"] and (now - _turn_cache["ts"] < _TURN_CACHE_TTL_SEC):
        return _turn_cache["servers"], _turn_cache["turn_ok"]
    raw, turn_ok = _ice_servers_raw()
    _turn_cache["servers"] = raw
    _turn_cache["turn_ok"] = turn_ok
    _turn_cache["ts"] = now
    return raw, turn_ok


class WorkerVideoTrack(VideoStreamTrack):
    """Wraps a Worker's latest annotated frame (web/server.py's
    Worker.get_frame()) as a live aiortc video track - the SAME frames
    /video_feed serves as MJPEG, just handed to WebRTC as raw video
    instead of re-encoded JPEG-over-HTTP. One camera read, two possible
    viewers (LAN MJPEG, remote WebRTC)."""

    kind = "video"

    def __init__(self, worker, fps=15):
        super().__init__()
        self.worker = worker
        self._interval = 1.0 / fps
        self._timestamp = 0

    async def recv(self):
        frame = self.worker.get_frame()
        if frame is None:
            frame = np.full((config.FRAME_HEIGHT, config.FRAME_WIDTH, 3), 18, np.uint8)

        # BGR (OpenCV) -> RGB (what av/VideoFrame expects)
        rgb = cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)
        video_frame = VideoFrame.from_ndarray(rgb, format="rgb24")

        self._timestamp += int(90000 * self._interval)   # 90kHz clock, WebRTC convention
        video_frame.pts = self._timestamp
        video_frame.time_base = fractions.Fraction(1, 90000)

        await asyncio.sleep(self._interval)
        return video_frame


def _get_firestore():
    from storage.device_registry import _get_firestore as gf
    return gf()


class CallListener:
    """
    Runs on the desktop. Polls webrtc_calls for pending calls addressed to
    THIS device_id, answers them with aiortc, and streams the requested
    camera until the phone hangs up or the connection drops.

    One CallListener per desktop process - started from desktop_launcher.py
    alongside the existing remote-command listener (storage/device_registry.py
    pending_commands loop), same "poll Firestore every few seconds" pattern,
    same fail-silent-if-offline behavior so this never affects local
    detection/alerts if there's no internet.
    """

    def __init__(self, device_id, workers_getter):
        self.device_id = device_id
        self.workers_getter = workers_getter   # callable -> list of Worker
        self._loop = None
        self._active_pcs = {}   # call_id -> RTCPeerConnection

    def start(self):
        if not AIORTC_AVAILABLE:
            print("[CAPHY WebRTC] aiortc not installed - live streaming from "
                  "outside the LAN will not be available (pip install aiortc)")
            return
        t = threading.Thread(target=self._run, daemon=True)
        t.start()

    def _run(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        print(f"[CAPHY WebRTC] Call listener running for device {self.device_id} "
              f"- waiting for live-video requests from the phone.")
        self._loop.run_until_complete(self._poll_forever())

    async def _poll_forever(self):
        db = _get_firestore()
        while True:
            try:
                calls = list(db.collection("webrtc_calls")
                             .where("device_id", "==", self.device_id)
                             .where("status", "==", "pending")
                             .stream())
                if calls:
                    print(f"[CAPHY WebRTC] {len(calls)} incoming live-video "
                          f"request(s) - answering.")
                for doc in calls:
                    data = doc.to_dict()
                    asyncio.ensure_future(self._answer_call(doc.id, data))
            except Exception as e:
                print(f"[CAPHY WebRTC] Call poll skipped (offline/needs index?): {e}")
            await asyncio.sleep(3)

    async def _answer_call(self, call_id, data):
        from aiortc import RTCSessionDescription

        try:
            db = _get_firestore()
            call_ref = db.collection("webrtc_calls").document(call_id)

            # Claim it first (avoid double-answering if two poll ticks
            # both saw it "pending" - Firestore doesn't need a full
            # transaction here since worst case is a harmless duplicate
            # answer, which the phone just ignores after its first one).
            call_ref.update({"status": "answering"})

            cam = int(data.get("cam", 0))
            workers = self.workers_getter()
            worker = workers[cam] if 0 <= cam < len(workers) else (workers[0] if workers else None)
            if worker is None:
                call_ref.update({"status": "error", "error": "No camera available"})
                return

            ice_raw, turn_available = _ice_servers_raw()
            ice_servers = [RTCIceServer(urls=e["urls"], username=e["username"], credential=e["credential"])
                           for e in ice_raw]
            config_rtc = RTCConfiguration(iceServers=ice_servers)
            pc = RTCPeerConnection(configuration=config_rtc)
            self._active_pcs[call_id] = pc

            pc.addTrack(WorkerVideoTrack(worker))

            @pc.on("connectionstatechange")
            async def on_state_change():
                if pc.connectionState in ("failed", "closed", "disconnected"):
                    self._active_pcs.pop(call_id, None)
                    try:
                        call_ref.update({"status": "closed"})
                    except Exception:
                        pass

            offer = data.get("offer") or {}
            await pc.setRemoteDescription(RTCSessionDescription(sdp=offer.get("sdp", ""),
                                                                  type=offer.get("type", "offer")))
            answer = await pc.createAnswer()
            await pc.setLocalDescription(answer)

            print(f"[CAPHY WebRTC] Answered call {call_id} (cam {cam}, "
                  f"turn={'yes' if turn_available else 'STUN-only'}).")
            call_ref.update({
                "status": "answered",
                "answer": {"sdp": pc.localDescription.sdp, "type": pc.localDescription.type},
                # The SAME real TURN credentials this laptop is using for
                # its own side of the connection, so the phone's peer
                # connection can upgrade from STUN-only defaults to the
                # real relay via setConfiguration() the moment this
                # answer arrives (see caphy_app/lib/webrtc_call.dart).
                "ice_servers": ice_raw,
                # Lets the phone show "relay service temporarily
                # unavailable" instead of just hanging on "connecting..."
                # if TURN couldn't be fetched for this call.
                "turn_available": turn_available,
            })

            # Relay caller's ICE candidates already queued, and keep
            # watching for late-arriving ones for a short window (WebRTC
            # candidates can trickle in after the offer/answer exchange).
            await self._relay_ice(pc, call_ref, duration_sec=20)

        except Exception as e:
            print(f"[CAPHY WebRTC] Failed to answer call {call_id}: {e}")
            try:
                _get_firestore().collection("webrtc_calls").document(call_id).update(
                    {"status": "error", "error": str(e)})
            except Exception:
                pass

    async def _relay_ice(self, pc, call_ref, duration_sec=20):
        """
        Applies the caller's (phone's) trickled ICE candidates as they
        show up in the call doc, for up to duration_sec - WebRTC
        candidates commonly arrive AFTER the initial offer/answer
        exchange, so this keeps checking rather than reading the doc once.

        aiortc's RTCIceCandidate does not parse a raw "candidate:..."
        SDP line itself - candidate_from_sdp() (in aiortc.sdp) does that
        parsing and returns a ready-to-use RTCIceCandidate, so that is the
        only construction path used here (no manual RTCIceCandidate(...)
        with placeholder fields, which would silently describe the wrong
        candidate).
        """
        from aiortc.sdp import candidate_from_sdp

        seen_indices = set()
        deadline = time.time() + duration_sec
        while time.time() < deadline:
            try:
                snap = call_ref.get()
                data = snap.to_dict() or {}
                for i, cand in enumerate(data.get("caller_ice", []) or []):
                    if i in seen_indices or not cand.get("candidate"):
                        continue
                    seen_indices.add(i)
                    try:
                        # A full ICE candidate line looks like:
                        # "candidate:842163049 1 udp 1677729535 ... typ host"
                        # candidate_from_sdp wants everything AFTER the
                        # "candidate:" prefix.
                        raw = cand["candidate"]
                        sdp_part = raw.split("candidate:", 1)[-1] if "candidate:" in raw else raw
                        ice = candidate_from_sdp(sdp_part)
                        ice.sdpMid = cand.get("sdpMid")
                        ice.sdpMLineIndex = cand.get("sdpMLineIndex")
                        await pc.addIceCandidate(ice)
                    except Exception as e:
                        print(f"[CAPHY WebRTC] Skipped one bad ICE candidate: {e}")
            except Exception:
                pass
            await asyncio.sleep(1)
