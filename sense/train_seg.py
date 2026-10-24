"""Step 3: train the tiny level model on the labelled frames, then export it to ONNX.

Every label becomes a mask (liquid below the clicked level). Training also shows the model random
brightness, contrast, colour, blur, noise and small box shifts, so it copes with lighting and
placement it never saw. 20 % of the frames are held back to measure the real accuracy.

    python train_seg.py                      # -> level_seg.onnx + level_seg.json (accuracy)
    python train_seg.py --epochs 80

Prints the level error on the held-back frames in px and as % of the column box.
"""
from __future__ import annotations

import argparse
import json
import random
import time
from pathlib import Path

import cv2
import numpy as np
import torch
import torch.nn as nn
import torch.nn.functional as F

from seg_level import IN_H, IN_W, level_from_probs

HERE = Path(__file__).resolve().parent


# ---------------------------------------------------------------- model

def block(cin, cout):
    return nn.Sequential(nn.Conv2d(cin, cout, 3, padding=1), nn.BatchNorm2d(cout), nn.ReLU(inplace=True),
                         nn.Conv2d(cout, cout, 3, padding=1), nn.BatchNorm2d(cout), nn.ReLU(inplace=True))


class TinyUNet(nn.Module):
    """~120k parameters. Input (N, 3, 128, 32), output (N, 1, 128, 32) logits: liquid or not."""

    def __init__(self, c=16):
        super().__init__()
        self.e1, self.e2, self.e3 = block(3, c), block(c, 2 * c), block(2 * c, 4 * c)
        self.d2, self.d1 = block(6 * c, 2 * c), block(3 * c, c)
        self.out = nn.Conv2d(c, 1, 1)

    def forward(self, x):
        e1 = self.e1(x)
        e2 = self.e2(F.max_pool2d(e1, 2))
        e3 = self.e3(F.max_pool2d(e2, 2))
        d2 = self.d2(torch.cat([F.interpolate(e3, scale_factor=2.0), e2], 1))
        d1 = self.d1(torch.cat([F.interpolate(d2, scale_factor=2.0), e1], 1))
        return self.out(d1)


# ---------------------------------------------------------------- data

def load_samples(data: Path, calib: dict | None) -> list[tuple[str, list, float]]:
    """(frame, roi, level px) for every usable label: label_levels.py ones use the calibration boxes,
    label_scene.py ones carry their own box."""
    labels = json.loads((data / "labels.json").read_text())
    rois = (calib or {}).get("columns", {})
    out = []
    for key, value in labels.items():
        fname, col = key.rsplit("|", 1)
        if value is None or not (data / fname).exists():
            continue
        if isinstance(value, dict):
            if value.get("level") is not None:
                out.append((fname, value["roi"], float(value["level"])))
        elif col in rois:
            out.append((fname, rois[col]["roi"], float(value)))
    return out


def make_example(img: np.ndarray, roi, level: float, augment: bool, rng: random.Random):
    """Crop (with a small random shift when augmenting), photometric jitter, resize; mask from level."""
    x, y, w, h = roi
    dx = dy = dh = dw = 0
    if augment:                                # hand-drawn boxes: never exactly where they were in training
        dx, dy = rng.randint(-3, 3), rng.randint(-8, 8)
        dh, dw = int(round(h * rng.uniform(-0.08, 0.08))), rng.randint(-3, 2)
    w, h0 = max(4, w + dw), h
    h = max(8, h + dh)
    x0 = int(np.clip(x + dx - dw // 2, 0, img.shape[1] - w))
    y1 = int(np.clip(y + h0 + dy, h, img.shape[0]))              # bottom edge of the jittered box
    y0 = y1 - h
    crop = img[y0:y1, x0:x0 + w].astype(np.float32)
    lev = float(np.clip(level + (y1 - (y + h0)), 0, h))          # box bottom moved down by d: level + d
    if augment:
        crop = crop * rng.uniform(0.5, 1.5) + rng.uniform(-40, 40)                  # brightness
        crop = (crop - crop.mean()) * rng.uniform(0.6, 1.4) + crop.mean()            # contrast
        crop = crop * np.array([rng.uniform(0.85, 1.15) for _ in range(3)])          # white balance
        crop = 255 * np.clip(crop / 255, 0, 1) ** rng.uniform(0.6, 1.6)              # gamma
        if rng.random() < 0.3:
            crop = cv2.GaussianBlur(crop, (3, 3), rng.uniform(0.3, 1.2))
        crop = crop + np.random.normal(0, rng.uniform(0, 8), crop.shape)            # sensor noise
    crop = np.clip(crop, 0, 255).astype(np.uint8)
    xin = cv2.resize(crop, (IN_W, IN_H), interpolation=cv2.INTER_AREA).astype(np.float32) / 255.0
    rows = lev * IN_H / h
    mask = np.zeros((IN_H, IN_W), np.float32)
    full = int(np.floor(rows))
    if full > 0:
        mask[IN_H - full:] = 1.0
    if full < IN_H:
        mask[IN_H - full - 1] = rows - full     # partial row at the surface
    return xin.transpose(2, 0, 1), mask[None], rows


class Frames(torch.utils.data.Dataset):
    def __init__(self, samples, data: Path, augment: bool, repeat: int = 1, seed: int = 0):
        self.samples, self.data, self.augment = samples, data, augment
        self.repeat = repeat
        self.rng = random.Random(seed)
        self.cache = {}

    def __len__(self):
        return len(self.samples) * self.repeat

    def __getitem__(self, i):
        for k in range(len(self.samples)):                      # an unreadable image: use the next sample
            fname, roi, level = self.samples[(i + k) % len(self.samples)]
            if fname not in self.cache:
                self.cache[fname] = cv2.imread(str(self.data / fname))
            if self.cache[fname] is not None:
                break
        x, m, rows = make_example(self.cache[fname], roi, level, self.augment, self.rng)
        return torch.from_numpy(x), torch.from_numpy(m), torch.tensor(rows, dtype=torch.float32)


def test_report(model, samples, data: Path, box_h: float, capacity_ml: float) -> dict:
    """Error on recordings the model never saw, split by what the column holds."""
    loader = torch.utils.data.DataLoader(Frames(samples, data, False), batch_size=64)
    pred, true = [], []
    model.eval()
    with torch.no_grad():
        for x, _, rows in loader:
            for pmap, r in zip(torch.sigmoid(model(x))[:, 0].numpy(), rows.numpy()):
                pred.append(level_from_probs(pmap)[0] / IN_H)
                true.append(r / IN_H)
    pred, true = np.array(pred), np.array(true)
    err_ml = np.abs(pred - true) * capacity_ml
    groups = {"filled (>25 %)": true > 0.25, "spill / little (2-25 %)": (true > 0.02) & (true <= 0.25),
              "empty": true <= 0.02}
    print("\nTEST on recordings it never saw:")
    out = {}
    for name, m in groups.items():
        if m.any():
            e = err_ml[m]
            print(f"  {name:24s} {m.sum():5d} crops   mean error {e.mean():.2f} mL ({e.mean() / capacity_ml:.1%}),"
                  f" 95% within {np.percentile(e, 95):.2f} mL")
            out[name] = {"n": int(m.sum()), "mean_ml": round(float(e.mean()), 3), "p95_ml": round(float(np.percentile(e, 95)), 3)}
    rise = 3.0 / box_h                                              # the spill alarm's 3 px
    empty, spill = true <= 0.02, (true > 0.02) & (true <= 0.25)
    if empty.any():
        print(f"  empty columns read as >3 px (false spill): {np.mean(pred[empty] > rise):.1%}")
    if spill.any():
        print(f"  spills read as >3 px (caught):             {np.mean(pred[spill] > rise):.1%}")
    return out


# ---------------------------------------------------------------- train

def dice_loss(logits, target):
    p = torch.sigmoid(logits)
    inter = (p * target).sum((1, 2, 3))
    return (1 - (2 * inter + 1) / (p.sum((1, 2, 3)) + target.sum((1, 2, 3)) + 1)).mean()


def evaluate(model, loader, device, box_h_px):
    model.eval()
    errs = []
    with torch.no_grad():
        for x, _, rows in loader:
            probs = torch.sigmoid(model(x.to(device)))[:, 0].cpu().numpy()
            for pmap, r in zip(probs, rows.numpy()):
                pred_rows, _ = level_from_probs(pmap)
                errs.append(abs(pred_rows - r))
    errs = np.array(errs)
    mae_rows = float(errs.mean()) if len(errs) else float("nan")
    return mae_rows * box_h_px / IN_H, mae_rows / IN_H, float(np.percentile(errs, 95)) / IN_H if len(errs) else float("nan")


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--data", default=str(HERE / "seg_data"))
    p.add_argument("--calib", default=str(HERE / "level_calib.json"))
    p.add_argument("--epochs", type=int, default=60)
    p.add_argument("--batch", type=int, default=32)
    p.add_argument("--lr", type=float, default=2e-3)
    p.add_argument("--out", default=str(HERE / "level_seg.onnx"))
    p.add_argument("--capacity-ml", type=float, default=4.5, help="mL in a full column (for the test report)")
    p.add_argument("--seed", type=int, default=0)
    p.add_argument("--test", default="fill4,setup3", help="recordings kept out completely, to test on at the end")
    p.add_argument("--val", default="setup1b", help="recordings used only to pick the best epoch")
    a = p.parse_args()

    random.seed(a.seed)
    np.random.seed(a.seed)
    torch.manual_seed(a.seed)
    data = Path(a.data)
    calib = json.loads(Path(a.calib).read_text()) if Path(a.calib).exists() else None
    samples = load_samples(data, calib)
    if len(samples) < 20:
        raise SystemExit(f"only {len(samples)} labelled crops: record and label more (aim for 300+)")
    group = lambda f: Path(f).parent.name.split("_syn")[0]          # synthetic copies go with their source
    test_s, val_s = set(a.test.split(",")) if a.test else set(), set(a.val.split(",")) if a.val else set()
    sessions = sorted({group(s[0]) for s in samples})
    if not val_s:                                                  # pick check recordings at random
        rest = [x for x in sessions if x not in test_s]
        random.Random(a.seed).shuffle(rest)
        val_s = set(rest[:max(1, len(rest) // 5)])
    train = [s for s in samples if group(s[0]) not in val_s | test_s]
    val = [s for s in samples if group(s[0]) in val_s and "_syn" not in s[0]]
    test = [s for s in samples if group(s[0]) in test_s and "_syn" not in s[0]]
    print(f"train: {', '.join(x for x in sessions if x not in val_s | test_s)} (+ synthetic copies)")
    print(f"check (picks the best epoch): {', '.join(sorted(val_s))}   TEST (never seen): {', '.join(sorted(test_s)) or '-'}")
    box_h = float(np.mean([s[1][3] for s in samples]))
    print(f"{len(train)} training crops, {len(val)} check, {len(test)} test, box ~{box_h:.0f} px tall")

    device = "mps" if torch.backends.mps.is_available() else "cpu"
    repeat = max(1, 2000 // max(1, len(train)))                    # ~2000 augmented crops per epoch
    tl = torch.utils.data.DataLoader(Frames(train, data, True, repeat, a.seed), batch_size=a.batch, shuffle=True)
    vl = torch.utils.data.DataLoader(Frames(val, data, False), batch_size=a.batch)
    model = TinyUNet().to(device)
    print(f"model: {sum(p.numel() for p in model.parameters()):,} parameters, training on {device}")
    opt = torch.optim.AdamW(model.parameters(), lr=a.lr, weight_decay=1e-4)
    sched = torch.optim.lr_scheduler.OneCycleLR(opt, max_lr=a.lr, total_steps=a.epochs * len(tl))

    best, best_state, t0 = float("inf"), None, time.time()
    for ep in range(1, a.epochs + 1):
        model.train()
        total = 0.0
        for x, m, _ in tl:
            x, m = x.to(device), m.to(device)
            logits = model(x)
            loss = F.binary_cross_entropy_with_logits(logits, m) + dice_loss(logits, m)
            opt.zero_grad()
            loss.backward()
            opt.step()
            sched.step()
            total += loss.item() * len(x)
        mae_px, mae_frac, p95 = evaluate(model, vl, device, box_h)
        if mae_px < best:
            best, best_state = mae_px, {k: v.detach().cpu().clone() for k, v in model.state_dict().items()}
        if ep == 1 or ep % 5 == 0 or ep == a.epochs:
            print(f"  epoch {ep:3d}  loss {total / len(tl.dataset):.3f}  held-back error {mae_px:.2f} px "
                  f"({mae_frac:.1%} of box, 95% within {p95:.1%})  {time.time() - t0:.0f}s")

    model.load_state_dict(best_state)
    model.eval().cpu()
    mae_px, mae_frac, p95 = evaluate(model, vl, "cpu", box_h)
    out = Path(a.out)
    torch.onnx.export(model, torch.zeros(1, 3, IN_H, IN_W), str(out), input_names=["image"], output_names=["logits"],
                      dynamic_axes={"image": {0: "n"}, "logits": {0: "n"}}, opset_version=17, dynamo=False)
    meta = {"in_h": IN_H, "in_w": IN_W, "held_back_error_px": round(mae_px, 2), "held_back_error_of_box": round(mae_frac, 4),
            "p95_error_of_box": round(p95, 4), "train_crops": len(train), "held_back_crops": len(val),
            "trained": time.strftime("%Y-%m-%d %H:%M:%S")}
    if test:
        meta["test"] = test_report(model, test, data, box_h, a.capacity_ml)
    out.with_suffix(".json").write_text(json.dumps(meta, indent=2))
    print(f"\nbest held-back error {mae_px:.2f} px = {mae_frac:.1%} of the box (95% of frames within {p95:.1%})")
    print(f"saved {out} and {out.with_suffix('.json').name}. Check it live:  python seg_level.py --cam 0")


if __name__ == "__main__":
    main()
