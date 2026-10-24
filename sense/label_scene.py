"""Label recordings for the no-calibration models (scene model + crop model).

Record each SETUP as its own session (python record_frames.py --session setup1), moving the
reservoir / camera / lights between setups. Then, per session:

  1. The recording is split into steady stretches automatically (frames where the setup was
     moving are dropped). For each stretch you see a typical frame with the last boxes on it:
     ENTER = same boxes, n = mark new ones, s = skip the stretch. To mark: click TOP-LEFT (at
     the rim, inside the walls) then BOTTOM-RIGHT (on the floor) per column, left to right,
     right-click undoes, ENTER when done.
  2. Type which columns are FILLED DURING the recording, and which ALREADY HAVE liquid that
     does not change. The rest are labelled empty automatically.
  3. Already-filled columns: click their level once. Columns being filled: click the liquid top
     one frame per second (same keys as label_levels.py).

    python label_scene.py                       # every session that still needs labels
    python label_scene.py --session setup3
    python label_scene.py --every 8             # label fewer frames (every 8th)
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np

from label_levels import run_labeler

HERE = Path(__file__).resolve().parent


def pick_frame(frames: list[Path], idx: list[int]) -> int | None:
    """Grid of these frames (e.g. the last 15): click the clearest one to mark the columns on."""
    tw, th, cols = 256, 192, 5
    rows = (len(idx) + cols - 1) // cols
    sheet = np.zeros((rows * th + 36, cols * tw, 3), np.uint8)
    for k, i in enumerate(idx):
        r, c = divmod(k, cols)
        sheet[36 + r * th:36 + (r + 1) * th, c * tw:(c + 1) * tw] = cv2.resize(cv2.imread(str(frames[i])), (tw, th))
        cv2.putText(sheet, f"frame {i + 1}", (c * tw + 6, 36 + r * th + 18), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                    (0, 255, 255), 1, cv2.LINE_AA)
    cv2.putText(sheet, "CLICK the clearest frame (no hand in front) to mark the columns on.  q = quit", (8, 24),
                cv2.FONT_HERSHEY_SIMPLEX, 0.55, (240, 240, 240), 1, cv2.LINE_AA)
    picked = {"i": None}

    def on_mouse(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN and y >= 36:
            k = ((y - 36) // th) * cols + x // tw
            if k < len(idx):
                picked["i"] = idx[k]

    win = "pick a frame"
    cv2.namedWindow(win)
    cv2.setMouseCallback(win, on_mouse)
    while picked["i"] is None:
        cv2.imshow(win, sheet)
        if cv2.waitKey(30) & 0xFF == ord("q"):
            break
    cv2.destroyAllWindows()
    cv2.waitKey(1)
    return picked["i"]


def mark_columns(img: np.ndarray) -> list[list[int]] | None:
    """Two clicks per column on this frame. Returns boxes [x, y, w, h] left to right, or None if q."""
    clicks: list[tuple[int, int]] = []

    def on_mouse(event, x, y, flags, param):
        if event == cv2.EVENT_LBUTTONDOWN:
            clicks.append((x, y))
        elif event == cv2.EVENT_RBUTTONDOWN and clicks:
            clicks.pop()

    win = "mark columns: top-left (rim) then bottom-right (floor), left to right | right-click undo | ENTER done | q quit"
    cv2.namedWindow(win)
    cv2.setMouseCallback(win, on_mouse)
    while True:
        view = img.copy()
        boxes = []
        for i in range(0, len(clicks) - 1, 2):
            (x0, y0), (x1, y1) = clicks[i], clicks[i + 1]
            boxes.append([min(x0, x1), min(y0, y1), abs(x1 - x0), abs(y1 - y0)])
        for i, (x, y, w, h) in enumerate(sorted(boxes)):
            cv2.rectangle(view, (x, y), (x + w, y + h), (0, 200, 0), 1)
            cv2.putText(view, chr(ord("A") + i), (x + 2, y - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (0, 200, 0), 1)
        if len(clicks) % 2:
            cv2.circle(view, clicks[-1], 3, (0, 0, 255), -1)
        cv2.rectangle(view, (0, 0), (view.shape[1], 30), (30, 30, 30), -1)
        cv2.putText(view, f"{len(boxes)} columns marked. Rim top-left, then floor bottom-right. ENTER when done.",
                    (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 220, 0), 1, cv2.LINE_AA)
        cv2.imshow(win, view)
        key = cv2.waitKey(20) & 0xFF
        if key in (13, 10) and boxes and len(clicks) % 2 == 0:
            cv2.destroyAllWindows()
            cv2.waitKey(1)
            return sorted(boxes)
        if key == ord("q"):
            cv2.destroyAllWindows()
            return None


def _signature(img: np.ndarray) -> np.ndarray:
    """Edge structure of the lower 3/4 of the frame (people walk past at the top), blurred so 1-3 px
    of jitter does not count, normalised so lighting changes do not count."""
    h = img.shape[0]
    g = cv2.cvtColor(img[h // 4:], cv2.COLOR_BGR2GRAY).astype(np.float32)
    e = cv2.magnitude(cv2.Sobel(g, cv2.CV_32F, 1, 0), cv2.Sobel(g, cv2.CV_32F, 0, 1))
    e = cv2.GaussianBlur(cv2.resize(e, (128, 72)), (0, 0), 1.5)
    return (e - e.mean()) / (e.std() + 1e-6)


def placements(frames: list[Path], step_thr: float = 0.6, anchor_thr: float = 0.6, min_len: int = 15) -> list[list[int]]:
    """Split a recording into steady stretches: a cut where the picture jumps (setup moved) or drifts
    too far from the start of the stretch. Stretches shorter than min_len frames (moving) are dropped."""
    S = [_signature(cv2.imread(str(f))) for f in frames]
    segs, cur = [], [0]
    for i in range(1, len(S)):
        anchor = S[cur[min(len(cur) - 1, 3)]]
        if float((S[i] * S[i - 1]).mean()) < step_thr or float((S[i] * anchor).mean()) < anchor_thr:
            segs.append(cur)
            cur = [i]
        else:
            cur.append(i)
    segs.append(cur)
    out = [s for s in segs if len(s) >= min_len]
    # most typical frame first in each stretch (least likely to have a hand in it)
    for k, s in enumerate(out):
        mean = np.mean([S[i] for i in s], axis=0)
        out[k] = sorted(s, key=lambda i: -float((S[i] * mean).mean()))
    return out


def review_placement(img: np.ndarray, prev: list[list[int]] | None, title: str) -> str:
    """Show a placement's typical frame with the previous boxes on it. Returns 'same', 'new', 'skip' or 'quit'."""
    win = "placement: ENTER = same boxes as before | n = mark new | s = skip | q = quit"
    view = img.copy()
    for i, (x, y, w, h) in enumerate(prev or []):
        cv2.rectangle(view, (x, y), (x + w, y + h), (255, 255, 0), 1)
        cv2.putText(view, chr(ord("A") + i), (x + 2, y - 4), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (255, 255, 0), 1)
    cv2.rectangle(view, (0, 0), (view.shape[1], 30), (30, 30, 30), -1)
    hint = "boxes from the last placement: ENTER if they fit, n to mark new, s to skip" if prev else \
        "n = mark the columns on this frame, s = skip (moving / unusable)"
    cv2.putText(view, f"{title}  {hint}", (10, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.45, (240, 240, 240), 1, cv2.LINE_AA)
    while True:
        cv2.imshow(win, view)
        key = cv2.waitKey(30) & 0xFF
        if key in (13, 10) and prev:
            choice = "same"
        elif key in (ord("n"), ord("s"), ord("q")):
            choice = {ord("n"): "new", ord("s"): "skip", ord("q"): "quit"}[key]
        else:
            continue
        cv2.destroyAllWindows()
        cv2.waitKey(1)
        return choice


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data", default=str(HERE / "seg_data"))
    p.add_argument("--session", default=None, help="one session (folder in seg_data/frames)")
    p.add_argument("--every", type=int, default=None,
                   help="label every Nth frame (default 4 = one per second at 4 fps; 1 with --last)")
    p.add_argument("--last", type=int, default=15, help="label only the last N usable frames of each session (default 15)")
    p.add_argument("--all", action="store_true", help="label the whole recording instead of only the last frames")
    a = p.parse_args()
    if a.all:
        a.last = None

    data = Path(a.data)
    labels_path = data / "labels.json"
    labels = json.loads(labels_path.read_text()) if labels_path.exists() else {}
    sessions = [data / "frames" / a.session] if a.session else sorted(d for d in (data / "frames").iterdir() if d.is_dir())
    for s in sessions:
        all_frames = sorted(s.glob("*.jpg"))
        if not all_frames:
            continue
        place_path = s / "placements.json"
        if not place_path.exists():
            print(f"session {s.name}: finding the steady stretches of the recording ...")
            found = placements(all_frames)
            if a.last:
                # only the end of the recording: keep the last N usable frames, ask only about their stretches
                end = set(sorted(i for idx in found for i in idx)[-a.last:])
                found = [[i for i in idx if i in end] for idx in found]       # keeps each stretch's typical-first order
                found = [idx for idx in found if idx]
            print(f"  {len(found)} steady stretch(es), {sum(map(len, found))}/{len(all_frames)} frames"
                  + (f" (the last {a.last})" if a.last else ""))
            saved, prev, quit_ = [], None, False
            for k, idx in enumerate(found):
                first_i, last_i = min(idx), max(idx)
                choice = review_placement(cv2.imread(str(all_frames[idx[0]])), prev,
                                          f"stretch {k + 1}/{len(found)} (frames {first_i + 1}-{last_i + 1})")
                if choice == "quit":
                    quit_ = True
                    break
                if choice == "skip":
                    continue
                if choice == "same":
                    boxes = prev
                else:
                    chosen = pick_frame(all_frames, sorted(idx)[-15:])        # choose from the last 15 frames
                    boxes = None if chosen is None else mark_columns(cv2.imread(str(all_frames[chosen])))
                if boxes is None:
                    quit_ = True
                    break
                saved.append({"frames": sorted(idx), "boxes": boxes})
                prev = boxes
            if quit_:
                break
            if not saved:
                print(f"  session {s.name}: every stretch was skipped, nothing saved. Run again to mark columns.")
                continue
            place_path.write_text(json.dumps(saved))
        placed = json.loads(place_path.read_text())
        if not placed:
            place_path.unlink()          # an empty file from an earlier run: ask again next time
            print(f"  session {s.name}: no columns marked yet. Run again.")
            continue
        n_cols = max(len(p["boxes"]) for p in placed)
        kinds_path = s / "columns.json"
        if not kinds_path.exists():
            def ask(q):
                ans = input(f"session {s.name}: {q} letters A-{chr(ord('A') + n_cols - 1)} (or numbers), "
                            "space separated, ENTER for none: ")
                picks = {int(t) if t.isdigit() else ord(t.upper()) - ord("A") + 1 for t in ans.split() if t.isalnum()}
                return sorted(c for c in picks if 1 <= c <= n_cols)
            filling = ask("which columns are FILLED DURING this recording (level changes)?")
            static = [c for c in ask("which columns ALREADY HAVE liquid that does not change?") if c not in filling]
            kinds_path.write_text(json.dumps({"filling": filling, "static": static}))
        kinds = json.loads(kinds_path.read_text())
        filling, static = set(kinds["filling"]), set(kinds["static"])
        make = lambda level, roi: {"level": level, "roi": roi}
        usable = sorted(i for place in placed for i in place["frames"])
        wanted = set(usable[-a.last:]) if a.last else set(usable)       # --last N: only the end of the recording
        every = a.every or (1 if a.last else 4)
        quit_ = False
        for k, place in enumerate(placed):
            boxes = place["boxes"]
            frames = [all_frames[i] for i in sorted(place["frames"]) if i in wanted][::every]
            if not frames:
                continue
            # already-filled columns: one click on this stretch's first frame, copied to its other frames
            first = str(frames[0].relative_to(data))
            static_todo = [(first, f"{first}|{s.name}#{i + 1}", roi) for i, roi in enumerate(boxes)
                           if i + 1 in static and f"{first}|{s.name}#{i + 1}" not in labels]
            if static_todo:
                print(f"stretch {k + 1}: click the level of the {len(static_todo)} already-filled column(s) once")
                if not run_labeler(data, static_todo, labels, labels_path, make_value=make):
                    quit_ = True
                    break
            static_level = {i + 1: (labels.get(f"{first}|{s.name}#{i + 1}") or {}).get("level")
                            for i in range(len(boxes)) if i + 1 in static}
            todo = []
            for f in frames:
                rel = str(f.relative_to(data))
                for i, roi in enumerate(boxes):
                    key = f"{rel}|{s.name}#{i + 1}"
                    if key in labels:
                        continue
                    if i + 1 in filling:
                        todo.append((rel, key, roi))
                    elif i + 1 in static:
                        labels[key] = {"level": static_level[i + 1], "roi": roi}   # same level all stretch
                    else:
                        labels[key] = {"level": 0.0, "roi": roi}                    # stays empty
            labels_path.write_text(json.dumps(labels, indent=1))
            print(f"session {s.name}, stretch {k + 1}/{len(placed)}: {len(boxes)} columns, {len(todo)} crops to label")
            if todo and not run_labeler(data, todo, labels, labels_path, make_value=make):
                quit_ = True
                break
        if quit_:
            break
    usable = sum(1 for v in labels.values() if v is not None and (not isinstance(v, dict) or v["level"] is not None))
    print(f"{usable} usable labels in {labels_path}. Next:  python train_scene.py  and  python train_seg.py")


if __name__ == "__main__":
    main()
