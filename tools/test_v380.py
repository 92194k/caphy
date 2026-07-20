"""Quick V380 Pro / IP-camera connection tester.

Usage:
    python tools/test_v380.py YOUR_RTSP_PASSWORD
    python tools/test_v380.py YOUR_RTSP_PASSWORD 192.168.1.14 admin

It first checks the camera is reachable, then tries the common V380 RTSP
paths and prints the FIRST one that returns a real frame. Paste that winner
into EXTRA_CAMERAS in config.py.
"""
import os
import sys
import socket

# ffmpeg: use TCP + a 5s socket timeout so a bad URL fails fast instead of hanging
# (newer ffmpeg uses "timeout", older uses "stimeout" - set both keys just in case)
os.environ["OPENCV_FFMPEG_CAPTURE_OPTIONS"] = "rtsp_transport;tcp|timeout;5000000|stimeout;5000000"
import cv2

PW   = sys.argv[1] if len(sys.argv) > 1 else "PASSWORD"
IP   = sys.argv[2] if len(sys.argv) > 2 else "192.168.1.14"
USER = sys.argv[3] if len(sys.argv) > 3 else "admin"

PATHS = [
    "/live/ch00_1", "/live/ch00_0",
    "/Onvif/live/1/1", "/Onvif/live/1/2",
    "/11", "/12",
    "/h264/ch1/main/av_stream", "/h264/ch1/sub/av_stream",
    "/stream1", "/stream2", "/cam/realmonitor?channel=1&subtype=0",
]


def reachable(ip, port, timeout=2.0):
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(timeout)
    try:
        s.connect((ip, port))
        return True
    except Exception:
        return False
    finally:
        s.close()


def try_url(url):
    cap = cv2.VideoCapture(url, cv2.CAP_FFMPEG)
    ok = cap.isOpened()
    frame_ok = False
    if ok:
        for _ in range(5):          # give it a few reads to warm up
            r, f = cap.read()
            if r and f is not None:
                frame_ok = True
                break
    cap.release()
    return frame_ok


print(f"\nTesting camera {IP}  (user={USER})\n" + "-" * 46)

# scan every port a cheap IP cam might expose a stream on
SCAN = {554: "RTSP", 8554: "RTSP-alt", 555: "RTSP-alt", 10554: "RTSP-alt",
        8899: "ONVIF", 8000: "control", 8080: "HTTP", 34567: "XMeye/Dahua",
        8800: "V380-proprietary", 80: "HTTP"}
open_ports = {}
for prt, label in sorted(SCAN.items()):
    up = reachable(IP, prt, 1.5)
    open_ports[prt] = up
    print(f"Port {prt:<6} ({label:<16}):", "OPEN" if up else "closed")
print("-" * 46)

rtsp_ports = [p for p in (554, 8554, 555, 10554) if open_ports.get(p)]
if not rtsp_ports:
    print("\nNo RTSP port is open. This camera is not serving a stream we can grab.")
    if open_ports.get(8800):
        print("Only the V380 proprietary port (8800) is open -> it is cloud/app-locked;")
        print("there is no open standard to connect to. Direct RTSP is not possible.")
    print("\nBest bypass: bridge it through a virtual camera (OBS) - ask Claude to set this up.")
    sys.exit(0)

port = rtsp_ports[0]
print(f"RTSP port {port} is open - trying stream paths...\n" + "-" * 46)

winner = None
for p in PATHS:
    url = f"rtsp://{USER}:{PW}@{IP}:{port}{p}"
    print(f"trying {p:<40} ", end="", flush=True)
    if try_url(url):
        print("OK  <-- WORKS")
        winner = url
        break
    print("no")

print("-" * 46)
if winner:
    print("\nSUCCESS. Put this exact line in config.py EXTRA_CAMERAS:\n")
    print(f'    "{winner}",\n')
else:
    print("\nNo path returned a frame. Most likely causes:")
    print("  1. RTSP/ONVIF still OFF in the V380 Pro app (turn it on, set a password).")
    print("  2. Wrong password (the ONVIF password, not your app login).")
    print("  3. PC not on the same WiFi as the camera.")
    print("  If port 554 shows CLOSED above, it's almost certainly #1 or #3.")
