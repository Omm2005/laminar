"""Label recordings automatically: find the reservoir's channels and measure the liquid in every frame.

For the 12-channel reservoir with dark liquid. Per steady stretch of a recording it finds the rim,
the floor and the 12 channels (walls fitted as a regular grid with a little perspective), then in
every frame measures each channel's level by comparing it, row by row, with the empty channels of
the same frame (so lighting changes cancel out). Writes label_scene.py-format labels for ALL 12
channels, and names the target columns A-D in columns.json. Then CHECK the overlays it draws.

    python autolabel.py setup1a --targets 2,5,8,11      # channel numbers of A, B, C, D (left to right)
    python autolabel.py setup1a --targets auto          # A-D = the 4 channels that end up fullest
    python autolabel.py setup1a --check                 # only redraw the overlays (no relabel)

Overlays: seg_data/check/<session>_*.jpg (boxes, measured level, % full; A-D in magenta).
"""
from __future__ import annotations

import argparse
import json
from pathlib import Path

import cv2
import numpy as np

from label_scene import placements

HERE = Path(__file__).resolve().parent
N_CH = 12


def _median(frames: list[Path], idx: list[int], n: int = 30) -> np.ndarray:
    pick = [idx[int(k)] for k in np.linspace(0, len(idx) - 1, min(n, len(idx)))]
    return np.median(np.stack([cv2.imread(str(frames[i])).astype(np.float32) for i in pick]), 0)


TARGET_CHANNELS = (1, 4, 7, 10)      # A, B, C, D: every third channel, starting at the first


def dark_columns(gray: np.ndarray, y0: int, y1: int) -> list[tuple[int, int]]:
    """x-ranges of liquid-filled columns: runs clearly darker than the plastic around them."""
    prof = gray[y0:y1].mean(0)
    base = np.percentile(prof, 75)
    dark = prof < base - 22
    runs, start = [], None
    for x, d in enumerate(np.append(dark, False)):
        if d and start is None:
            start = x
        elif not d and start is not None:
            if x - start >= 14:
                runs.append((start, x))
            start = None
    return runs


def fit_grid(runs: list[tuple[int, int]]) -> tuple[float, float]:
    """(x of channel 1's left wall, pitch) from liquid columns that sit 3 channels apart (A, B, C, D)."""
    centers = np.array([(a + b) / 2 for a, b in runs])
    best = None
    for pitch in np.arange(30, 56, 0.1):
        # number the columns by their spacing, A = channel 1
        ch = np.round((centers - centers[0]) / pitch).astype(int) + 1
        if len(set(ch)) != len(ch) or any((c - 1) % 3 for c in ch):
            continue
        origin = np.mean(centers - (ch - 0.5) * pitch)
        err = np.abs(origin + (ch - 0.5) * pitch - centers).mean()
        if best is None or err < best[0]:
            best = (err, origin, pitch)
    if best is None:
        raise ValueError("liquid columns are not 3 channels apart")
    return best[1], best[2]


def find_rim(gray: np.ndarray, x0: int, x1: int) -> int:
    """Top edge of the reservoir: the row where it gets brighter going down (plastic below, scene above)."""
    col = np.median(gray[:, x0:x1], axis=1)
    best, rim = -1e9, 0
    for y in range(80, gray.shape[0] - 120):
        d = col[y + 3:y + 18].mean() - col[y - 18:y - 3].mean() + np.abs(col[y + 1] - col[y - 1])
        if d > best:
            best, rim = d, y
    return rim


def find_floor(gray: np.ndarray, runs, rim: int) -> int:
    """Channel floor: the lowest row the liquid columns still reach (the bottom of the U)."""
    bottoms = []
    for a, b in runs:
        prof = gray[:, a + 3:b - 3].mean(1)
        ref = np.median(prof[rim + 5:rim + 120])
        rows = np.flatnonzero(prof[rim + 20:] < ref + 25) + rim + 20
        if len(rows):
            run_end = rows[0]
            for r in rows:
                if r - run_end > 2:
                    break
                run_end = r
            bottoms.append(run_end)
    return int(np.median(bottoms))


def row_liquid(color: np.ndarray, bg: np.ndarray) -> np.ndarray:
    """Per row of one channel crop: how much darker than the empty-channel background (0..1)."""
    g = color.mean(axis=2).mean(axis=1)
    b = bg.mean(axis=1)
    return np.clip((b - g) / np.maximum(b, 1) * 3, 0, 1)


def measure(img: np.ndarray, boxes: list[list[int]], wet: set[int] | None = None,
            dark: float = 0.12, dark_wet: float = 0.06) -> list[float]:
    """Liquid height (px from the floor) per channel. Each row of a channel (middle half of its width,
    away from the walls) is compared with the same row of its neighbours (+-2 channels): liquid is
    clearly darker than the brighter neighbours. Local, so uneven backlight and the glow that the
    frosted plastic spreads into empty neighbours do not count as liquid.
    wet = channel numbers (1-based) known to hold liquid in this recording: the others are 0, and
    these get a more sensitive threshold (faint dye still counts)."""
    hmin = min(h for x, y, w, h in boxes)
    rows = np.stack([img[y:y + hmin, x + w // 4:x + 3 * w // 4].astype(np.float32).mean(axis=(1, 2))
                     for x, y, w, h in boxes])                                  # (channels, rows)
    levels = []
    for i, (x, y, w, h) in enumerate(boxes):
        if wet is not None and i + 1 not in wet:
            levels.append(0.0)
            continue
        # A-D get the sensitive threshold (faint dye); bad fills next to them would pick up their glow
        thr = dark_wet if wet is not None and i + 1 in TARGET_CHANNELS else dark
        near = [j for j in range(max(0, i - 2), min(len(boxes), i + 3)) if j != i]
        nb = np.sort(rows[near], axis=0)
        bg = nb[-2] if len(near) >= 4 else nb[-1]           # 2nd brightest neighbour; edge channels: brightest
        is_wet = (bg - rows[i]) / np.maximum(bg, 1) > thr
        level, gap = 0, 0
        for j in range(hmin - 1, -1, -1):                                       # contiguous from the floor up
            if is_wet[j]:
                level, gap = hmin - j, 0
            else:
                gap += 1
                if gap > 3:
                    break
        levels.append(float(min(level, h)))
    return levels


def run(session: str, data: Path, targets_arg: str, check_only: bool) -> None:
    sdir = data / "frames" / session
    frames = sorted(sdir.glob("*.jpg"))
    labels_path = data / "labels.json"
    labels = json.loads(labels_path.read_text()) if labels_path.exists() else {}
    if check_only:
        placed = json.loads((sdir / "placements.json").read_text())
    else:
        placed = []
        for idx in placements(frames):
            idx = sorted(idx)
            med = _median(frames[:], idx[-max(5, len(idx) // 3):])      # end of the stretch: most liquid
            gray = med.mean(axis=2)
            h = gray.shape[0]
            runs = dark_columns(gray, int(h * 0.42), int(h * 0.58))
            if len(runs) < 2:
                print(f"  stretch frames {idx[0] + 1}-{idx[-1] + 1}: fewer than 2 liquid columns, skipped")
                continue
            try:
                origin, pitch = fit_grid(runs)
            except ValueError as e:
                print(f"  stretch frames {idx[0] + 1}-{idx[-1] + 1}: {e}, skipped")
                continue
            rim = find_rim(gray, int(origin), int(origin + 12 * pitch))
            floor = find_floor(gray, runs, rim)
            boxes = [[int(round(origin + j * pitch)) + 3, rim + 3, max(4, int(round(pitch)) - 6), floor - rim - 3]
                     for j in range(N_CH)]
            placed.append({"frames": idx, "boxes": boxes, "rim": rim, "floor": floor})
        (sdir / "placements.json").write_text(json.dumps(placed))

    # levels for every frame of every stretch
    keys_done = 0
    end_levels = np.zeros(N_CH)
    for place in placed:
        boxes = place["boxes"]
        for i in place["frames"]:
            img = cv2.imread(str(frames[i]))
            levels = measure(img, boxes)
            rel = str(frames[i].relative_to(data))
            for c, (lev, roi) in enumerate(zip(levels, boxes)):
                labels[f"{rel}|{session}#{c + 1}"] = {"level": lev, "roi": roi}
                keys_done += 1
        end_levels = np.array(levels) / np.array([b[3] for b in boxes])
    labels_path.write_text(json.dumps(labels, indent=1))

    if targets_arg == "auto":
        targets = list(TARGET_CHANNELS)
    else:
        targets = [int(t) for t in targets_arg.split(",")]
    (sdir / "columns.json").write_text(json.dumps({"targets": targets, "names": dict(zip("ABCD", targets)),
                                                   "auto": True}))
    print(f"{session}: {len(placed)} stretch(es), {keys_done} channel labels, A-D = channels {targets}")
    draw_check(session, data, placed, frames, labels, targets)


def draw_check(session, data, placed, frames, labels, targets):
    out = data / "check"
    out.mkdir(exist_ok=True)
    names = {t: n for n, t in zip("ABCD", targets)}
    for k, place in enumerate(placed):
        idx = place["frames"]
        picks = [idx[int(j)] for j in np.linspace(0, len(idx) - 1, 4)]
        tiles = []
        for i in picks:
            img = cv2.imread(str(frames[i]))
            rel = str(frames[i].relative_to(data))
            for c, (x, y, w, h) in enumerate(place["boxes"]):
                v = labels[f"{rel}|{session}#{c + 1}"]
                tgt = (c + 1) in names
                color = (255, 0, 255) if tgt else (0, 200, 0)
                cv2.rectangle(img, (x, y), (x + w, y + h), color, 1)
                ly = int(round(y + h - v["level"]))
                cv2.line(img, (x - 2, ly), (x + w + 2, ly), (0, 0, 255), 2)
                tag = names.get(c + 1, str(c + 1))
                cv2.putText(img, tag, (x + 2, y - 14), cv2.FONT_HERSHEY_SIMPLEX, 0.4, color, 1, cv2.LINE_AA)
                cv2.putText(img, f"{v['level'] / h:.0%}", (x, y - 3), cv2.FONT_HERSHEY_SIMPLEX, 0.3, (0, 255, 255), 1)
            cv2.putText(img, f"{session} stretch {k + 1} frame {i + 1}", (8, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.5,
                        (255, 255, 255), 2, cv2.LINE_AA)
            tiles.append(cv2.resize(img, (480, 360)))
        sheet = np.vstack([np.hstack(tiles[:2]), np.hstack(tiles[2:])])
        cv2.imwrite(str(out / f"{session}_stretch{k + 1}.jpg"), sheet)
    print(f"  overlays: {out}/{session}_stretch*.jpg")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("session")
    p.add_argument("--data", default=str(HERE / "seg_data"))
    p.add_argument("--targets", default="auto", help="channel numbers of A,B,C,D (default 1,4,7,10)")
    p.add_argument("--check", action="store_true", help="only redraw overlays from the saved labels")
    a = p.parse_args()
    run(a.session, Path(a.data), a.targets, a.check)


if __name__ == "__main__":
    main()


# ---------------------------------------------------------------- geometry from marked target boxes

def boxes_from_targets(target_boxes: list[list[int]], channels=TARGET_CHANNELS) -> tuple[list[list[int]], dict]:
    """All 12 channel boxes from a few marked target columns (A, B, C, D = channels 1, 4, 7, 10):
    pitch from their spacing, one shared rim line (top) and floor line (bottom)."""
    centers = np.array([x + w / 2 for x, y, w, h in target_boxes])
    ch = np.array(channels[:len(target_boxes)])
    pitch, c0 = np.polyfit(ch, centers, 1)                 # center of channel k = c0 + pitch k
    rim = int(np.median([y for x, y, w, h in target_boxes]))
    floor = int(np.median([y + h for x, y, w, h in target_boxes]))
    w = max(4, int(round(pitch)) - 6)
    boxes = [[int(round(c0 + pitch * k - w / 2)), rim, w, floor - rim] for k in range(1, N_CH + 1)]
    return boxes, {"pitch": round(float(pitch), 2), "rim": rim, "floor": floor}


def run_from_marks(session: str, data: Path, marked: list[list[int]], marked_frames: list[int],
                   wet: set[int] | None = None) -> None:
    sdir = data / "frames" / session
    frames = sorted(sdir.glob("*.jpg"))
    boxes, geo = boxes_from_targets(marked)
    stretch = next(sorted(s) for s in placements(frames) if set(marked_frames) & set(s))
    placed = [{"frames": stretch, "boxes": boxes, **geo}]
    (sdir / "placements.json").write_text(json.dumps(placed))
    labels_path = data / "labels.json"
    labels = json.loads(labels_path.read_text()) if labels_path.exists() else {}
    labels = {k: v for k, v in labels.items() if not k.startswith(f"frames/{session}/")}
    for i in stretch:
        img = cv2.imread(str(frames[i]))
        rel = str(frames[i].relative_to(data))
        for c, (lev, roi) in enumerate(zip(measure(img, boxes, wet), boxes)):
            labels[f"{rel}|{session}#{c + 1}"] = {"level": lev, "roi": roi}
    labels_path.write_text(json.dumps(labels, indent=1))
    targets = list(TARGET_CHANNELS)
    (sdir / "columns.json").write_text(json.dumps({"targets": targets, "names": dict(zip("ABCD", targets)), "auto": True,
                                                   "wet": sorted(wet) if wet else None}))
    print(f"{session}: frames {stretch[0] + 1}-{stretch[-1] + 1} ({len(stretch)}), pitch {geo['pitch']} px, "
          f"rim {geo['rim']}, floor {geo['floor']}, {len(stretch) * N_CH} channel labels")
    for old in (data / "check").glob(f"{session}_stretch*.jpg"):
        old.unlink()
    draw_check(session, data, placed, frames, labels, targets)
