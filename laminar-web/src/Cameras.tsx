import { useEffect, useRef, useState } from "react";
import { useQuery } from "convex/react";
import { api } from "../convex/_generated/api";

type Cam = "side" | "wrist";
const NAME: Record<Cam, string> = { side: "Side camera · tray", wrist: "Wrist camera · on the arm" };

/**
 * One bench camera, live. The frame's JPEG bytes arrive through a Convex live query (robot.liveFrame), pushed over
 * the websocket the page already has open: no polling, no storage URL, no second download.
 */
function LiveCam({ cam }: { cam: Cam }) {
  const f = useQuery(api.robot.liveFrame, { cam });
  const [src, setSrc] = useState<string | null>(null);
  const [size, setSize] = useState<{ w: number; h: number } | null>(null);
  const prev = useRef<string | null>(null);
  const seen = useRef<{ seq: number; at: number }[]>([]);
  const [, tick] = useState(0);
  useEffect(() => { const t = setInterval(() => tick((x) => x + 1), 500); return () => clearInterval(t); }, []);
  useEffect(() => {
    if (!f) return;
    const url = URL.createObjectURL(new Blob([f.jpeg], { type: "image/jpeg" }));
    setSrc(url);
    if (prev.current) URL.revokeObjectURL(prev.current);
    prev.current = url;
    seen.current = [...seen.current.filter((s) => Date.now() - s.at < 3000), { seq: f.seq, at: Date.now() }];
  }, [f?.seq]); // eslint-disable-line react-hooks/exhaustive-deps
  useEffect(() => () => { if (prev.current) URL.revokeObjectURL(prev.current); }, []);
  const age = f ? (Date.now() - f.t) / 1000 : null;
  const recent = seen.current.filter((s) => Date.now() - s.at < 3000);
  const fps = recent.length > 1 ? (recent.length - 1) / ((recent.at(-1)!.at - recent[0].at) / 1000) : 0;
  const live = age !== null && age < 3;
  // Each tile takes the exact shape of the frames the bench sends (the side camera sends a wide strip of the
  // tray); nothing is cropped. Wide frames get the full row.
  const wide = !!size && size.w / size.h > 2;
  return (
    <figure className={`cam${wide ? " wide" : ""}`}>
      {src ? (
        <img src={src} alt={NAME[cam]} style={size ? { aspectRatio: `${size.w} / ${size.h}` } : undefined}
          onLoad={(e) => { const i = e.currentTarget; if (!size || size.w !== i.naturalWidth || size.h !== i.naturalHeight) setSize({ w: i.naturalWidth, h: i.naturalHeight }); }} />
      ) : <div className="cam-empty">No frames yet</div>}
      <figcaption>
        <span className={`chip ${live ? "live" : ""}`}>{live ? `live · ${fps.toFixed(0)} fps` : age === null ? "off" : `${age < 120 ? Math.round(age) + " s" : Math.round(age / 60) + " min"} ago`}</span>
        <span>{NAME[cam]}</span>
        {size && <span className="mono" style={{ marginLeft: "auto" }}>{size.w}×{size.h}</span>}
      </figcaption>
    </figure>
  );
}

export default function Cameras() {
  return (
    <div className="cams">
      <LiveCam cam="side" />
      <LiveCam cam="wrist" />
    </div>
  );
}
