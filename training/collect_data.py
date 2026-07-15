"""
Capture frames from your webcam to build a training dataset.

    python training/collect_data.py
    python training/collect_data.py --source 1

Keys:
    SPACE = save current frame
    q = quit
"""

import argparse
import os
import cv2

OUT = os.path.join("training", "dataset", "images", "train")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--source", default="0")
    args = ap.parse_args()

    os.makedirs(OUT, exist_ok=True)

    source = int(args.source) if args.source.isdigit() else args.source

    # Gumamit ng DirectShow backend sa Windows
    cap = cv2.VideoCapture(source, cv2.CAP_DSHOW)

    if not cap.isOpened():
        print("ERROR: Cannot open camera.")
        return

    i = len([f for f in os.listdir(OUT) if f.lower().endswith(".jpg")])

    print("SPACE = save frame, q = quit")

    while True:
        ok, frame = cap.read()

        if not ok:
            print("ERROR: Cannot read frame from camera.")
            break

        cv2.putText(
            frame,
            f"saved: {i}   SPACE=save   q=quit",
            (10, 30),
            cv2.FONT_HERSHEY_SIMPLEX,
            0.6,
            (80, 200, 120),
            2,
        )

        cv2.imshow("CAPHY - collect dataset", frame)

        key = cv2.waitKey(1) & 0xFF

        if key == ord("q"):
            break

        if key == ord(" "):
            filename = os.path.join(OUT, f"img_{i:04d}.jpg")
            cv2.imwrite(filename, frame)
            print("Saved:", filename)
            i += 1

    cap.release()
    cv2.destroyAllWindows()

    print(f"\nDone. {i} images in {OUT}")


if __name__ == "__main__":
    main()