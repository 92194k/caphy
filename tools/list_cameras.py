"""CAPHY camera finder.

Prints every camera the laptop can see, so you know which NUMBER is your
built-in cam and which is your phone (DroidCam). Start the DroidCam PC client
FIRST (so the phone shows up as a camera), then run:

    python list_cameras.py
"""
import os
import cv2

print("Scanning camera indices 0-9 ...\n")
found = []
for i in range(10):
    cap = cv2.VideoCapture(i, cv2.CAP_DSHOW) if os.name == "nt" else cv2.VideoCapture(i)
    opened = cap.isOpened()
    got_frame, w, h = False, 0, 0
    if opened:
        got_frame, frame = cap.read()
        if got_frame:
            h, w = frame.shape[:2]
    cap.release()
    if opened and got_frame:
        found.append(i)
        print(f"  [ WORKS ]  index {i}   {w}x{h}")
    else:
        print(f"  [   -   ]  index {i}")

print()
if found:
    print(f"Working cameras: {found}\n")
    print(f"  Run the FIRST one alone:   python app.py {found[0]}")
    if len(found) > 1:
        print(f"  Run the SECOND one alone:  python app.py {found[1]}")
        print(f"  Run BOTH together:         python app.py {' '.join(map(str, found))}")
else:
    print("No cameras found.")
    print("If testing the phone: open the DroidCam PC client and press Start first,")
    print("then run this again.")