"""Label a new recording in one step: drag ONE box over the whole tray, every channel gets labelled.

The box goes from the LEFT wall of channel 1 to the RIGHT wall of channel 12, from the rim to the
floor; it is split into the 12 channels. Every frame of every steady stretch is then measured, all
12 channels (targets, already-filled columns and spills alike). Each channel row is compared with
the brightest of two references:
  - its neighbours in the same frame (like autolabel.py), and
  - itself when it was emptiest during the recording (so a column next to filled ones still works).
Frames where the picture jumps (hand in front, tray moving) are skipped automatically.

    python label_new.py fill1                 # label seg_data/frames/fill1
    python label_new.py fill1 --check         # only redraw the check images

Then LOOK at seg_data/check/fill1_*.jpg (green line = labelled level). The tray must not move
during a recording.
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np

from autolabel import N_CH, draw_check
from label_scene import placements
from live_view import draw_boxes

HERE = Path(__file__).resolve().parent
WALL = 6                     # px of each channel's share that is wall (left out of its box)
DARK = 0.12                  # a row is liquid when this much darker than its neighbours ...
DARK_SELF = 0.22             # ... or this much darker than itself when empty (stricter: a full
                             # neighbour's glow darkens an empty channel a little over time)
UNSURE_PX = 15               # a clear frame whose reading is this far from the smoothed one is skipped
SAT_MIN_GAP = 40
GLOW = 0.75                  # a channel this much less coloured than a neighbour is only that neighbour's glow             # dyed liquid: also liquid where much more coloured than empty plastic


def split_tray(tray: list[int], n: int = N_CH) -> list[list[int]]:
    x, y, w, h = tray
    pitch = w / n
    bw = max(4, int(round(pitch)) - WALL)
    return [[int(round(x + pitch * (k + 0.5) - bw / 2)), y, bw, h] for k in range(n)]


def tray_shift(ref: np.ndarray, img: np.ndarray, tray: list[int], max_dx: int = 60) -> int:
    """How far (px, x) the tray moved from ref to img: the right end of the tray's base (just below
    the floor) is a sharp bright-plastic-to-table step that liquid never changes. (Matching all the
    channel walls instead locks onto the wrong wall: they all look alike.)"""
    x, y, w, h = tray

    def right_end(im):
        g = cv2.cvtColor(im, cv2.COLOR_BGR2GRAY).astype(np.float32)
        prof = g[min(g.shape[0] - 2, y + h - 5):min(g.shape[0], y + h + 25)].mean(axis=0)
        prof = np.convolve(prof, np.ones(5) / 5, mode="same")
        lo, hi = max(3, x + w - max_dx), min(len(prof) - 3, x + w + max_dx)
        step = prof[lo - 3:hi - 3] - prof[lo + 3:hi + 3]                # bright -> dark, left to right
        return lo + int(np.argmax(step)), float(step.max())

    (r0, s0), (r1, s1) = right_end(ref), right_end(img)
    return r1 - r0 if min(s0, s1) > 15 else 0                         # no clear edge: assume it stayed


def profiles(img: np.ndarray, boxes: list[list[int]], channel: int | None = None) -> np.ndarray:
    """(channels, rows) mean brightness (or one HSV channel) of the middle half of each channel, top to bottom."""
    h = min(b[3] for b in boxes)
    if channel is not None:
        img = cv2.cvtColor(img, cv2.COLOR_BGR2HSV)[..., channel:channel + 1]
    return np.stack([img[y:y + h, x + w // 4:x + 3 * w // 4].astype(np.float32).mean(axis=(1, 2))
                     for x, y, w, h_ in boxes])


def level_from_wet(is_wet: np.ndarray, h: int) -> float:
    """Height of the wet run that starts at the floor. The box bottom may sit a few rows below the
    floor (dry), so the run may start within the bottom 8 % of the box."""
    n = len(is_wet)
    start = next((j for j in range(n - 1, n - 1 - max(2, int(0.08 * n)), -1) if is_wet[j]), None)
    if start is None:
        return 0.0
    level, gap = n - start, 0
    for j in range(start, -1, -1):                     # contiguous up from there
        if is_wet[j]:
            level, gap = n - j, 0
        else:
            gap += 1
            if gap > 3:
                break
    return float(min(level, h))


def rising_only(levels: np.ndarray, k: int = 9) -> np.ndarray:
    """Liquid is only added during a recording, so a reading above most LATER readings is a hand /
    syringe in front: median over k frames (drops short blips), then capped by the 30th percentile
    of the next 15 (a few bad later frames cannot wipe out a whole column)."""
    n = len(levels)
    half = [min(k // 2, t, n - 1 - t) for t in range(n)]               # centred window, also at the ends
    med = np.stack([np.median(levels[t - h:t + h + 1], axis=0) for t, h in zip(range(n), half)])
    cap = np.stack([np.percentile(med[t:t + 15], 30, axis=0) for t in range(n)])
    out = np.minimum(med, cap)
    out[max(0, n - k // 2 - 3):] = np.nan                              # the last frames have no later
    return out                                                         # frames to check them: not labelled


def hand_frames(imgs: list[np.ndarray], tray: list[int], thr: float = 0.05) -> np.ndarray:
    """True where something is above the tray (hand, syringe, arm): the band above the rim differs
    from its usual look. Liquid going into the channels never changes that band."""
    x, y, w, h = tray
    bands = np.stack([im[max(0, y - int(0.6 * h)):max(1, y - 3), x:x + w].astype(np.float32) for im in imgs])
    dif = np.abs(bands - np.median(bands, axis=0)).mean(axis=(1, 2, 3)) / 255
    calm = np.argsort(dif)[:max(3, len(imgs) // 3)]                  # usual look = the calmest third
    dif = np.abs(bands - np.median(bands[calm], axis=0)).mean(axis=(1, 2, 3)) / 255
    return dif > thr


def measure_stretch(imgs: list[np.ndarray], boxes: list[list[int]], tray: list[int]) -> tuple[list[list[float]], int]:
    """Levels (px) per frame and channel, and how many frames had a hand above the tray."""
    P = np.stack([profiles(im, boxes) for im in imgs])                 # (frames, channels, rows)
    scale = np.percentile(P.reshape(len(P), -1), 90, axis=1)          # lighting of each frame
    Pn = P / scale[:, None, None]
    hand = hand_frames(imgs, tray)
    clear = ~hand if (~hand).sum() >= 3 else np.ones(len(P), bool)
    empty_ref = np.percentile(Pn[clear], 90, axis=0)                   # each channel at its emptiest
    S = np.stack([profiles(im, boxes, channel=1) for im in imgs])      # saturation: dye is coloured,
    s_empty, s_dye = np.percentile(S[clear], 5), np.percentile(S[clear], 97)   # plastic is grey
    use_sat = s_dye - s_empty > SAT_MIN_GAP
    s_thr = s_empty + 0.35 * (s_dye - s_empty)                         # clearly coloured (faint dye too)
    raw = []
    for f in range(len(P)):
        levels = []
        for i in range(len(boxes)):
            near = [j for j in range(max(0, i - 2), min(len(boxes), i + 3)) if j != i]
            nb = np.sort(Pn[f, near], axis=0)
            bg = nb[-2] if len(near) >= 4 else nb[-1]
            is_wet = ((bg - Pn[f, i]) / np.maximum(bg, 1e-3) > DARK) | \
                     ((empty_ref[i] - Pn[f, i]) / np.maximum(empty_ref[i], 1e-3) > DARK_SELF)
            if use_sat:                                                # works however many are filled;
                s_nb = S[f, [j for j in (i - 1, i + 1) if 0 <= j < len(boxes)]].max(axis=0)
                is_wet |= (S[f, i] > s_thr) & (S[f, i] > GLOW * s_nb)  # glow is weaker than its source
            levels.append(level_from_wet(is_wet, boxes[i][3]))
        raw.append(levels)
    raw = np.array(raw)
    out = raw.copy()
    ci = np.flatnonzero(clear)
    out[ci] = rising_only(raw[ci])
    unsure = (np.abs(out[ci] - raw[ci]) > UNSURE_PX).any(axis=1)        # reading disagrees with its
    out[ci[unsure]] = np.nan                                           # neighbours in time: not labelled
    ci = ci[~np.isnan(out[ci]).any(axis=1)]
    for f in np.flatnonzero(~clear):                                   # hand frames: between the clear
        before, after = ci[ci < f], ci[ci > f]                         # frames around them
        lo = out[before[-1]] if len(before) else np.zeros(raw.shape[1])
        if not len(after) or not len(before):
            out[f] = np.nan                                            # not between clear frames: not labelled
            continue
        out[f] = np.clip(raw[f], lo, out[after[0]])
    return out.tolist(), int(hand.sum())


def main():
    p = argparse.ArgumentParser()
    p.add_argument("session")
    p.add_argument("--data", default=str(HERE / "seg_data"))
    p.add_argument("--check", action="store_true", help="only redraw the check images")
    p.add_argument("--same-box", action="store_true", help="reuse the tray box drawn last time for this recording")
    a = p.parse_args()
    data = Path(a.data)
    sdir = data / "frames" / a.session
    frames = sorted(sdir.glob("*.jpg"))
    if not frames:
        raise SystemExit(f"no frames in {sdir}: record first (record_frames.py --session {a.session})")
    labels_path = data / "labels.json"
    labels = json.loads(labels_path.read_text()) if labels_path.exists() else {}
    if a.check:
        placed = json.loads((sdir / "placements.json").read_text())
        draw_check(a.session, data, placed, frames, labels, [])
        return

    stretches = [sorted(s) for s in placements(frames)]
    if not stretches:
        raise SystemExit("no steady stretch found (did the camera or tray keep moving?)")
    print(f"{len(frames)} frames, {len(stretches)} steady stretch(es), {sum(map(len, stretches))} usable")
    if a.same_box:
        old = json.loads((sdir / "placements.json").read_text())[0]
        b0, b1 = old["boxes"][0], old["boxes"][-1]
        tray = old.get("tray") or [b0[0] - WALL // 2, b0[1], b1[0] + b1[2] + WALL // 2 - (b0[0] - WALL // 2), b0[3]]
    else:
        show = cv2.imread(str(frames[stretches[0][-1]]))
        print("drag ONE box over the whole tray: left wall of channel 1 to right wall of channel 12, rim to floor. ENTER.")
        drawn = draw_boxes(show)
        if not drawn:
            return
        tray = max(drawn, key=lambda b: b[2] * b[3])
    boxes = split_tray(tray)

    labels = {k: v for k, v in labels.items() if not k.startswith(f"frames/{a.session}/")}
    placed = []
    ref = cv2.imread(str(frames[stretches[0][-1]]))                   # the frame the tray box fits
    for idx in stretches:
        imgs = [cv2.imread(str(frames[i])) for i in idx]
        dx = tray_shift(ref, imgs[len(imgs) // 2], tray)               # the tray may be nudged between stretches
        tray_k = [tray[0] + dx, tray[1], tray[2], tray[3]]
        boxes = split_tray(tray_k)
        levels_all, n_hand = measure_stretch(imgs, boxes, tray_k)
        print(f"  stretch of {len(idx)} frames: tray moved {dx:+d} px, {n_hand} with a hand above the tray")
        kept = []
        for i, levels in zip(idx, levels_all):
            rel = str(frames[i].relative_to(data))
            if any(np.isnan(levels)):
                continue
            kept.append(i)
            for c, (lev, roi) in enumerate(zip(levels, boxes)):
                labels[f"{rel}|{a.session}#{c + 1}"] = {"level": lev, "roi": roi}
        placed.append({"frames": kept, "boxes": boxes, "pitch": round(tray[2] / N_CH, 2),
                       "rim": tray[1], "floor": tray[1] + tray[3], "tray": tray_k})
    labels_path.write_text(json.dumps(labels, indent=1))
    (sdir / "placements.json").write_text(json.dumps(placed))
    print(f"{a.session}: {sum(len(p['frames']) for p in placed) * N_CH} channel labels, pitch {tray[2] / N_CH:.1f} px")
    for old in (data / "check").glob(f"{a.session}_stretch*.jpg"):
        old.unlink()
    draw_check(a.session, data, placed, frames, labels, [])


if __name__ == "__main__":
    main()
