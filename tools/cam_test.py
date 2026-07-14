"""Quick two-camera test (plain OpenCV windows, no detection).

Confirms your laptop cam + USB phone cam both open before running the full app.
Start the DroidCam/Iriun PC client FIRST, then:

    python cam_test.py            # laptop=0, phone=1 (defaults)
    python cam_test.py 0 2        # if your phone is index 2

Press  q  to quit.
"""
import os
import sys
import cv2

a = sys.argv[1:]
i_laptop = int(a[0]) if len(a) > 0 else 0
i_phone  = int(a[1]) if len(a) > 1 else 1


def open_cam(idx):
    # DirectShow on Windows = reliable + low latency for USB webcams
    cap = cv2.VideoCapture(idx, cv2.CAP_DSHOW) if os.name == "nt" else cv2.VideoCapture(idx)
    cap.set(cv2.CAP_PROP_BUFFERSIZE, 1)          # keep only newest frame -> no lag build-up
    return cap


laptop_cam = open_cam(i_laptop)
phone_cam  = open_cam(i_phone)

if not laptop_cam.isOpened():
    print(f"Laptop cam (index {i_laptop}) did not open.")
if not phone_cam.isOpened():
    print(f"Phone cam (index {i_phone}) did not open. Is the DroidCam/Iriun PC client started?")

print("Showing both feeds. Press q in a window to quit.")
while True:
    ret1, frame_laptop = laptop_cam.read()
    ret2, frame_phone = phone_cam.read()

    if ret1:
        cv2.imshow("Laptop Camera (index %d)" % i_laptop, frame_laptop)
    if ret2:
        cv2.imshow("Phone Camera (index %d)" % i_phone, frame_phone)
    if not ret1 and not ret2:
        print("Neither camera returned a frame yet...")

    if cv2.waitKey(1) & 0xFF == ord("q"):
        break

laptop_cam.release()
phone_cam.release()
cv2.destroyAllWindows()