"""Train the scene model (finds the columns by itself) from label_scene.py labels, export to ONNX.

Each labelled frame becomes a class map: background / column (empty part) / liquid, from the
column boxes marked per setup and the liquid level clicked per frame. Training moves, scales,
rotates and flips the frames and changes the lighting, so it copes with new setups. Whole
setups are held back to measure how well it does on a setup it has never seen.

    python train_scene.py                    # -> scene_seg.onnx + scene_seg.json
    python train_scene.py --epochs 40
"""
from __future__ import annotations

import argparse
import json
import random
import time
from collections import defaultdict
from pathlib import Path

import cv2
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from scene_level import SCENE_H, SCENE_W, find_columns
from train_seg import block, dice_loss

HERE = Path(__file__).resolve().parent
IGNORE = 255


class SceneUNet(nn.Module):
    """4-level U-Net, ~470k parameters. (N, 3, 240, 320) -> (N, 3, 240, 320) class logits."""

    def __init__(self, c=16):
        super().__init__()
        self.e1, self.e2, self.e3, self.e4 = block(3, c), block(c, 2 * c), block(2 * c, 4 * c), block(4 * c, 8 * c)
        self.d3, self.d2, self.d1 = block(12 * c, 4 * c), block(6 * c, 2 * c), block(3 * c, c)
        self.out = nn.Conv2d(c, 3, 1)

    def forward(self, x):
        e1 = self.e1(x)
        e2 = self.e2(F.max_pool2d(e1, 2))
        e3 = self.e3(F.max_pool2d(e2, 2))
        e4 = self.e4(F.max_pool2d(e3, 2))
        d3 = self.d3(torch.cat([F.interpolate(e4, scale_factor=2.0), e3], 1))
        d2 = self.d2(torch.cat([F.interpolate(d3, scale_factor=2.0), e2], 1))
        d1 = self.d1(torch.cat([F.interpolate(d2, scale_factor=2.0), e1], 1))
        return self.out(d1)


def load_frames(data: Path) -> dict[str, list[tuple[list, float | None]]]:
    """frame -> [(roi, level or None)] for every column of its session (label_scene.py labels)."""
    labels = json.loads((data / "labels.json").read_text())
    frames: dict[str, list] = defaultdict(list)
    for key, v in labels.items():
        if isinstance(v, dict) and "#" in key.rsplit("|", 1)[1] and (data / key.rsplit("|", 1)[0]).exists():
            frames[key.rsplit("|", 1)[0]].append((v["roi"], v["level"]))
    return dict(frames)


def target_map(shape, cols) -> np.ndarray:
    """Full-resolution class map. Boxes shrink by 1 px left/right so walls stay background."""
    t = np.zeros(shape[:2], np.uint8)
    for (x, y, w, h), level in cols:
        x0, x1 = x + 1, x + w - 1
        if level is None:
            t[y:y + h, x0:x1] = IGNORE
            continue
        top = int(round(y + h - level))
        t[y:top, x0:x1] = 1
        t[top:y + h, x0:x1] = 2
    return t


def make_example(img, cols, augment: bool, rng: random.Random):
    t = target_map(img.shape, cols)
    if augment:
        h, w = img.shape[:2]
        # virtual camera: rotate / zoom / shift, plus a perspective tilt as if the camera were higher,
        # lower or more to one side than in the recording (keystone of up to ~12 % of the frame)
        m = cv2.getRotationMatrix2D((w / 2 + rng.uniform(-0.15, 0.15) * w, h / 2 + rng.uniform(-0.15, 0.15) * h),
                                    rng.uniform(-8, 8), rng.uniform(0.7, 1.3))
        m[:, 2] += [rng.uniform(-0.15, 0.15) * w, rng.uniform(-0.15, 0.15) * h]
        tilt_v, tilt_h = rng.uniform(-0.12, 0.12), rng.uniform(-0.12, 0.12)
        corners = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
        moved = corners + np.float32([[tilt_v * w, tilt_h * h], [-tilt_v * w, -tilt_h * h],
                                      [tilt_v * w * -0.2, tilt_h * h], [-tilt_v * w * -0.2, -tilt_h * h]]) \
            + np.float32([[rng.uniform(-0.03, 0.03) * w, rng.uniform(-0.03, 0.03) * h] for _ in range(4)])
        persp = cv2.getPerspectiveTransform(corners, moved) @ np.vstack([m, [0, 0, 1]])
        border = tuple(int(v) for v in np.median(img.reshape(-1, 3), 0))
        img = cv2.warpPerspective(img, persp, (w, h), flags=cv2.INTER_LINEAR, borderValue=border)
        t = cv2.warpPerspective(t, persp, (w, h), flags=cv2.INTER_NEAREST, borderValue=0)
        if rng.random() < 0.5:
            img, t = img[:, ::-1], t[:, ::-1]
        f = img.astype(np.float32)
        f = f * rng.uniform(0.5, 1.5) + rng.uniform(-40, 40)
        f = (f - f.mean()) * rng.uniform(0.6, 1.4) + f.mean()
        f = f * np.array([rng.uniform(0.85, 1.15) for _ in range(3)])
        f = 255 * np.clip(f / 255, 0, 1) ** rng.uniform(0.6, 1.6)
        if rng.random() < 0.3:
            f = cv2.GaussianBlur(f, (3, 3), rng.uniform(0.3, 1.2))
        img = np.clip(f + np.random.normal(0, rng.uniform(0, 8), f.shape), 0, 255).astype(np.uint8)
    x = cv2.resize(np.ascontiguousarray(img), (SCENE_W, SCENE_H), interpolation=cv2.INTER_AREA).astype(np.float32) / 255
    t = cv2.resize(np.ascontiguousarray(t), (SCENE_W, SCENE_H), interpolation=cv2.INTER_NEAREST)
    return x.transpose(2, 0, 1), t.astype(np.int64)


class SceneFrames(torch.utils.data.Dataset):
    def __init__(self, items, data: Path, augment: bool, repeat: int = 1, seed: int = 0):
        self.items, self.data, self.augment, self.repeat = items, data, augment, repeat
        self.rng, self.cache = random.Random(seed), {}

    def __len__(self):
        return len(self.items) * self.repeat

    def __getitem__(self, i):
        for k in range(len(self.items)):                        # an unreadable image: use the next frame
            fname, cols = self.items[(i + k) % len(self.items)]
            if fname not in self.cache:
                self.cache[fname] = cv2.imread(str(self.data / fname))
            if self.cache[fname] is not None:
                break
        x, t = make_example(self.cache[fname], cols, self.augment, self.rng)
        return torch.from_numpy(x), torch.from_numpy(t)


def evaluate(model, items, data: Path, device) -> dict:
    """On held-back setups: how many columns found, box edge error, fill error."""
    model.eval()
    found = missed = extra = 0
    edge_err, fill_err = [], []
    with torch.no_grad():
        for fname, cols in items:
            img = cv2.imread(str(data / fname))
            if img is None:
                continue
            x, _ = make_example(img, cols, False, random.Random(0))
            cls = model(torch.from_numpy(x)[None].to(device))[0].argmax(0).cpu().numpy()
            sx, sy = img.shape[1] / SCENE_W, img.shape[0] / SCENE_H
            pred = [(px * sx, py * sy, pw * sx, ph * sy, c["fill"]) for c in find_columns(cls)
                    for (px, py, pw, ph) in [c["roi"]]]
            used = set()
            for (gx, gy, gw, gh), level in cols:
                match = [i for i, p in enumerate(pred) if gx <= p[0] + p[2] / 2 <= gx + gw and i not in used]
                if not match:
                    missed += 1
                    continue
                i = match[0]
                used.add(i)
                found += 1
                px, py, pw, ph, fill = pred[i]
                edge_err += [abs(py - gy), abs(py + ph - gy - gh)]
                if level is not None:
                    fill_err.append(abs(fill - level / gh))
            extra += len(pred) - len(used)
    total = found + missed
    return {"columns_found": f"{found}/{total}", "extra_columns": extra,
            "rim_floor_error_px": round(float(np.mean(edge_err)), 2) if edge_err else None,
            "fill_error": round(float(np.mean(fill_err)), 4) if fill_err else None,
            "fill_error_p95": round(float(np.percentile(fill_err, 95)), 4) if fill_err else None}


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data", default=str(HERE / "seg_data"))
    p.add_argument("--epochs", type=int, default=40)
    p.add_argument("--batch", type=int, default=8)
    p.add_argument("--lr", type=float, default=2e-3)
    p.add_argument("--out", default=str(HERE / "scene_seg.onnx"))
    p.add_argument("--seed", type=int, default=0)
    a = p.parse_args()
    random.seed(a.seed)
    np.random.seed(a.seed)
    torch.manual_seed(a.seed)

    data = Path(a.data)
    frames = load_frames(data)
    group = lambda f: Path(f).parent.name.split("_syn")[0]          # synthetic copies go with their source
    sessions = sorted({group(f) for f in frames})
    if len(frames) < 20 or len(sessions) < 2:
        raise SystemExit(f"{len(frames)} labelled frames in {len(sessions)} setups: record and label at least "
                         "3 different setups (python label_scene.py)")
    random.Random(a.seed).shuffle(sessions)
    val_sessions = set(sessions[:max(1, len(sessions) // 4)])     # whole setups held back
    items = sorted(frames.items())
    train = [it for it in items if group(it[0]) not in val_sessions]
    val = [it for it in items if group(it[0]) in val_sessions and "_syn" not in it[0]]   # test on real frames only
    print(f"{len(train)} training frames, {len(val)} held back (setups never trained on: {', '.join(sorted(val_sessions))})")

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    repeat = max(1, 600 // max(1, len(train)))
    tl = torch.utils.data.DataLoader(SceneFrames(train, data, True, repeat, a.seed), batch_size=a.batch, shuffle=True)
    model = SceneUNet().to(device)
    print(f"model: {sum(p.numel() for p in model.parameters()):,} parameters, training on {device}")
    opt = torch.optim.AdamW(model.parameters(), lr=a.lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=a.lr, total_steps=a.epochs * len(tl))
    weights = torch.tensor([0.5, 1.0, 1.0], device=device)
    best, best_state, t0 = None, None, time.time()
    for ep in range(1, a.epochs + 1):
        model.train()
        total = 0.0
        for x, t in tl:
            x, t = x.to(device), t.to(device)
            logits = model(x)
            loss = F.cross_entropy(logits, t, weight=weights, ignore_index=IGNORE)
            valid = (t != IGNORE).unsqueeze(1).float()
            for k in (1, 2):
                loss = loss + 0.5 * dice_loss(logits[:, k:k + 1] - 1e4 * (1 - valid), ((t == k).unsqueeze(1).float()) * valid)
            opt.zero_grad()
            loss.backward()
            opt.step()
            sched.step()
            total += loss.item() * len(x)
        if ep == 1 or ep % 5 == 0 or ep == a.epochs:
            m = evaluate(model, val, data, device)
            score = (int(m["columns_found"].split("/")[0]) - m["extra_columns"], -(m["fill_error"] or 1))
            if best is None or score > best:
                best, best_state = score, {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
            print(f"  epoch {ep:3d}  loss {total / len(tl.dataset):.3f}  held-back setups: columns {m['columns_found']}, "
                  f"extra {m['extra_columns']}, rim/floor {m['rim_floor_error_px']} px, fill error {m['fill_error']}  "
                  f"{time.time() - t0:.0f}s")

    model.load_state_dict(best_state)
    model.eval().cpu()
    m = evaluate(model, val, data, "cpu")
    out = Path(a.out)
    torch.onnx.export(model, torch.zeros(1, 3, SCENE_H, SCENE_W), str(out), input_names=["image"],
                      output_names=["logits"], opset_version=17, dynamo=False)
    out.with_suffix(".json").write_text(json.dumps({**m, "train_frames": len(train), "held_back_frames": len(val),
                                                    "held_back_setups": sorted(val_sessions),
                                                    "trained": time.strftime("%Y-%m-%d %H:%M:%S")}, indent=2))
    print(f"\nheld-back setups: {m}")
    print(f"saved {out}. Check it live:  python scene_level.py --cam 0")


if __name__ == "__main__":
    main()
