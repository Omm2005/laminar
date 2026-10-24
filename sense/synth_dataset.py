"""More training data from labelled recordings: lower fill levels and other liquid colours.

For every labelled frame it makes variants:
  - DRAIN: lower some columns to a random level (as if the recording had stopped earlier). The rows
    above the new level are replaced by the nearest EMPTY channel's pixels from the same frame (same
    lighting, so it blends in), so the new level is exact (no measuring).
  - TINT: recolour only the liquid (from the labels) to red, yellow, green or blue, keeping its
    brightness, so the models learn levels of any dye colour.

Writes new sessions <session>_syn with labels; trainers group them with their source recording, so a
recording held back for testing never has its synthetic copies in training.

    python synth_dataset.py                       # every labelled recording, 4 variants per frame
    python synth_dataset.py --sessions setup2 --variants 6
    python synth_dataset.py --preview             # also writes seg_data/check/synth_*.jpg
"""
from __future__ import annotations

import argparse
import json
import random
import shutil
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np

HERE = Path(__file__).resolve().parent
HUES = {"red": 0, "yellow": 26, "green": 62, "blue": 112}       # OpenCV hue (0-179)
SIDE = 3                                                         # px between box edge and channel wall


def base_session(name: str) -> str:
    return name.split("_syn")[0]


def load(data: Path):
    labels = json.loads((data / "labels.json").read_text())
    frames = defaultdict(dict)                                  # frame -> {channel: (roi, level)}
    for k, v in labels.items():
        f, rest = k.split("|")
        if "#" not in rest or not isinstance(v, dict) or v.get("level") is None:
            continue
        session = f.split("/")[1]
        if "_syn" in session:
            continue
        frames[f][int(rest.split("#")[1])] = (v["roi"], float(v["level"]))
    return labels, frames


def emptiest(data: Path, session: str, roi, frame_names: list[str], cache) -> np.ndarray | None:
    """The channel crop (with side margins) from the frame of this stretch where it is brightest."""
    x, y, w, h = roi
    best, crop = -1, None
    for f in frame_names:
        if f not in cache:
            cache[f] = cv2.imread(str(data / f))
        c = cache[f][y:y + h, max(0, x - SIDE):x + w + SIDE]
        m = float(c.mean())
        if m > best:
            best, crop = m, c
    return crop


def tint(img: np.ndarray, mask: np.ndarray, hue: int) -> np.ndarray:
    hsv = cv2.cvtColor(img, cv2.COLOR_BGR2HSV).astype(np.float32)
    h, s, v = hsv[..., 0], hsv[..., 1], hsv[..., 2]
    h[mask] = hue
    s[mask] = np.clip(s[mask] * 1.6 + 70, 0, 255)
    if hue == HUES["yellow"]:
        v[mask] = np.clip(v[mask] * 1.35 + 15, 0, 255)          # yellow dye is a bright colour
    return cv2.cvtColor(hsv.astype(np.uint8), cv2.COLOR_HSV2BGR)


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data", default=str(HERE / "seg_data"))
    p.add_argument("--sessions", default=None, help="comma list (default: every labelled recording)")
    p.add_argument("--variants", type=int, default=4, help="new frames per labelled frame")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--preview", action="store_true")
    a = p.parse_args()
    rng = random.Random(a.seed)
    data = Path(a.data)
    labels, frames = load(data)
    by_session = defaultdict(list)
    for f in frames:
        by_session[f.split("/")[1]].append(f)
    sessions = a.sessions.split(",") if a.sessions else sorted(by_session)

    preview = []
    for s in sessions:
        out_name = f"{s}_syn"
        out_dir = data / "frames" / out_name
        if out_dir.exists():
            shutil.rmtree(out_dir)
        out_dir.mkdir(parents=True)
        labels = {k: v for k, v in labels.items() if not k.startswith(f"frames/{out_name}/")}
        # every frame of the recording (labelled or not) can be the "empty" source, within its stretch
        place = json.loads((data / "frames" / s / "placements.json").read_text())
        all_frames = sorted((data / "frames" / s).glob("*.jpg"))
        stretch_of = {}
        for pl in place:
            names = [str(all_frames[i].relative_to(data)) for i in pl["frames"]]
            for n in names:
                stretch_of[n] = names[::2]
        cache, n_out = {}, 0
        for f in sorted(by_session[s]):
            img = cv2.imread(str(data / f))
            chans = frames[f]
            for v in range(a.variants):
                out = img.copy()
                new = {}
                for c, (roi, lev) in chans.items():
                    x, y, w, h = roi
                    new_lev = lev
                    if lev > 4 and rng.random() < 0.75:                  # drain this column
                        new_lev = rng.choice([0.0, rng.uniform(0, lev * 0.9)])
                        empties = [d for d, (_, dl) in chans.items() if d != c and dl <= 2]
                        top, cut = y + h - int(round(lev)), y + h - int(round(new_lev))
                        if empties and cut > top:
                            d = min(empties, key=lambda d: abs(d - c))      # nearest empty channel, same frame
                            xd, yd, wd, hd = chans[d][0]
                            src_px = img[top:cut, max(0, xd - SIDE):xd + wd + SIDE]
                            x0 = max(0, x - SIDE)
                            width = min(src_px.shape[1], w + 2 * SIDE, out.shape[1] - x0)
                            out[top:cut, x0:x0 + width] = src_px[:, :width]
                            if new_lev > 0:                               # a soft surface line
                                out[max(cut - 1, 0):cut + 1, x0:x0 + width] = \
                                    (out[max(cut - 1, 0):cut + 1, x0:x0 + width] * 0.85).astype(np.uint8)
                        else:
                            new_lev = lev
                    new[c] = (roi, new_lev)
                colour = rng.choice(["original"] + list(HUES))
                if colour != "original":
                    mask = np.zeros(out.shape[:2], bool)
                    for c, (roi, lev) in new.items():
                        x, y, w, h = roi
                        if lev > 0:
                            mask[y + h - int(round(lev)):y + h, max(0, x - SIDE):x + w + SIDE] = True
                    out = tint(out, mask, HUES[colour])
                name = f"frame_{n_out:05d}.jpg"
                cv2.imwrite(str(out_dir / name), out, [cv2.IMWRITE_JPEG_QUALITY, 95])
                rel = f"frames/{out_name}/{name}"
                for c, (roi, lev) in new.items():
                    labels[f"{rel}|{out_name}#{c}"] = {"level": float(lev), "roi": roi}
                if len(preview) < 8 and v == 0 or (len(preview) < 8 and rng.random() < 0.05):
                    preview.append((out, new, colour))
                n_out += 1
        (out_dir / "source.json").write_text(json.dumps({"from": s}))
        print(f"{s}: {len(by_session[s])} labelled frames -> {n_out} synthetic frames in {out_name}")
    (data / "labels.json").write_text(json.dumps(labels, indent=1))

    if a.preview and preview:
        tiles = []
        for img, new, colour in preview[:8]:
            v = img.copy()
            for c, ((x, y, w, h), lev) in new.items():
                ly = int(round(y + h - lev))
                cv2.line(v, (x - 2, ly), (x + w + 2, ly), (0, 255, 0), 1)
            cv2.putText(v, colour, (8, 22), cv2.FONT_HERSHEY_SIMPLEX, 0.7, (255, 255, 255), 2, cv2.LINE_AA)
            tiles.append(cv2.resize(v, (400, 300)))
        while len(tiles) % 4:
            tiles.append(np.zeros_like(tiles[0]))
        sheet = np.vstack([np.hstack(tiles[i:i + 4]) for i in range(0, len(tiles), 4)])
        (data / "check").mkdir(exist_ok=True)
        cv2.imwrite(str(data / "check" / "synth_preview.jpg"), sheet)
        print(f"preview: {data / 'check' / 'synth_preview.jpg'}")


if __name__ == "__main__":
    main()
