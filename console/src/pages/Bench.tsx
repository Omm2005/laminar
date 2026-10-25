import { useState } from "react";
import { useMutation, useQuery } from "convex/react";
import { api } from "../../convex/_generated/api";

export default function Bench() {
  const vials = useQuery(api.vials.list, {});
  const channels = useQuery(api.channels.list, {});
  const upsertVial = useMutation(api.vials.upsert);
  const upsertChannel = useMutation(api.channels.upsert);

  const CHANNEL_COUNT = 12;
  const SLOT_COUNT = 4;
  const vialBySlot = new Map((vials ?? []).map((v) => [v.slot, v]));
  const channelByNumber = new Map((channels ?? []).map((c) => [c.number, c]));

  return (
    <>
      <section className="card">
        <div className="card-title">Vials</div>
        <div className="bench-vials">
          {Array.from({ length: SLOT_COUNT }, (_, i) => {
            const slot = i + 1;
            const v = vialBySlot.get(slot);
            return (
              <VialCard
                key={slot}
                slot={slot}
                vial={v}
                onSave={(contents, volumeMl) =>
                  upsertVial({
                    slot,
                    letter: v?.letter ?? String.fromCharCode(64 + slot),
                    contents, volumeMl,
                  })
                }
              />
            );
          })}
        </div>
      </section>

      <section className="card">
        <div className="card-title">Reservoir</div>
        <ReservoirSvg
          channels={Array.from({ length: CHANNEL_COUNT }, (_, i) => channelByNumber.get(i + 1) ?? null)}
        />
        <div className="channel-list">
          {Array.from({ length: CHANNEL_COUNT }, (_, i) => {
            const n = i + 1;
            const c = channelByNumber.get(n);
            return (
              <ChannelRow
                key={n}
                number={n}
                channel={c}
                onSave={(contents, targetMl, capacityMl, currentMl) =>
                  upsertChannel({ number: n, contents, targetMl, capacityMl, currentMl })
                }
              />
            );
          })}
        </div>
      </section>
    </>
  );
}

function VialCard({
  slot, vial, onSave,
}: {
  slot: number;
  vial?: { letter: string; contents: string; volumeMl: number };
  onSave: (contents: string, volumeMl: number) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [contents, setContents] = useState(vial?.contents ?? "");
  const [volume, setVolume] = useState(vial?.volumeMl ?? 0);
  const letter = vial?.letter ?? String.fromCharCode(64 + slot);

  if (!editing) {
    return (
      <div className="vial">
        <div className="vial-id">{letter}</div>
        <div className="vial-contents">{vial?.contents || "Empty slot"}</div>
        <div className="vial-vol num">{vial ? `${vial.volumeMl.toFixed(1)} mL` : "—"}</div>
        <button className="chip" onClick={() => setEditing(true)}>Edit</button>
      </div>
    );
  }
  return (
    <div className="vial editing">
      <div className="vial-id">{letter}</div>
      <input value={contents} onChange={(e) => setContents(e.target.value)} placeholder="Contents" />
      <input type="number" step="0.1" value={volume}
             onChange={(e) => setVolume(parseFloat(e.target.value) || 0)} placeholder="mL" />
      <div className="row">
        <button className="btn-primary" onClick={() => { onSave(contents, volume); setEditing(false); }}>Save</button>
        <button className="chip" onClick={() => setEditing(false)}>Cancel</button>
      </div>
    </div>
  );
}

type ChannelRow = { number: number; contents: string; currentMl: number; targetMl: number; capacityMl: number } | null;

function ReservoirSvg({ channels }: { channels: ChannelRow[] }) {
  const W = 520;
  const H = 110;
  const pad = 20;
  const n = channels.length;
  const w = (W - pad * 2) / n;
  return (
    <svg viewBox={`0 0 ${W} ${H}`} className="reservoir" preserveAspectRatio="xMidYMid meet">
      {channels.map((c, i) => {
        const x = pad + i * w + 2;
        const cw = w - 4;
        const bodyY = 24;
        const bodyH = 70;
        const cap = c?.capacityMl ?? 10;
        const curr = c?.currentMl ?? 0;
        const tgt = c?.targetMl ?? 0;
        const fillH = cap > 0 ? Math.max(0, Math.min(1, curr / cap)) * bodyH : 0;
        const targetY = cap > 0 ? bodyY + bodyH - Math.min(1, tgt / cap) * bodyH : bodyY + bodyH;
        return (
          <g key={i}>
            <rect x={x} y={bodyY} width={cw} height={bodyH} rx={3}
                  fill="#1b2130" stroke="#262c3b" />
            {fillH > 0 && (
              <rect x={x} y={bodyY + bodyH - fillH} width={cw} height={fillH} rx={3}
                    fill="var(--accent)" opacity={0.75} />
            )}
            {c && c.targetMl > 0 && (
              <line x1={x - 1} x2={x + cw + 1} y1={targetY} y2={targetY}
                    stroke="#e8c257" strokeWidth={1.5} strokeDasharray="3 2" />
            )}
            <text x={x + cw / 2} y={16} textAnchor="middle" fontSize="10" fill="#7d869c">{i + 1}</text>
            <text x={x + cw / 2} y={bodyY + bodyH + 12} textAnchor="middle" fontSize="9" fill="#7d869c">
              {c?.contents?.slice(0, 6) ?? ""}
            </text>
          </g>
        );
      })}
    </svg>
  );
}

function ChannelRow({
  number, channel, onSave,
}: {
  number: number;
  channel?: ChannelRow extends infer T ? T : never;
  onSave: (contents: string, targetMl: number, capacityMl: number, currentMl: number) => void;
}) {
  const [editing, setEditing] = useState(false);
  const [contents, setContents] = useState(channel?.contents ?? "");
  const [target, setTarget] = useState(channel?.targetMl ?? 0);
  const [capacity, setCapacity] = useState(channel?.capacityMl ?? 10);
  const [current, setCurrent] = useState(channel?.currentMl ?? 0);

  return (
    <div className="channel-row">
      <div className="ch-num num">{number}</div>
      {!editing ? (
        <>
          <div className="ch-contents">{channel?.contents || <span className="dim">Empty</span>}</div>
          <div className="ch-nums small dim num">
            {channel ? `${channel.currentMl.toFixed(1)} / ${channel.targetMl.toFixed(1)} mL` : "—"}
          </div>
          <button className="chip" onClick={() => setEditing(true)}>Edit</button>
        </>
      ) : (
        <div className="ch-edit">
          <input value={contents} onChange={(e) => setContents(e.target.value)} placeholder="Contents" />
          <input type="number" step="0.1" value={current}
                 onChange={(e) => setCurrent(parseFloat(e.target.value) || 0)} placeholder="Current mL" />
          <input type="number" step="0.1" value={target}
                 onChange={(e) => setTarget(parseFloat(e.target.value) || 0)} placeholder="Target mL" />
          <input type="number" step="0.1" value={capacity}
                 onChange={(e) => setCapacity(parseFloat(e.target.value) || 0)} placeholder="Capacity mL" />
          <div className="row">
            <button className="btn-primary"
                    onClick={() => { onSave(contents, target, capacity, current); setEditing(false); }}>Save</button>
            <button className="chip" onClick={() => setEditing(false)}>Cancel</button>
          </div>
        </div>
      )}
    </div>
  );
}
