import { useState } from "react";
import { useAction, useMutation, useQuery } from "convex/react";
import { api } from "../../convex/_generated/api";

const EXAMPLES = [
  "pour 2 ml into channel 3 from vial 1",
  "channels 1 to 4, 1.5 mL each from vial 2",
  "set tolerance to 0.2 mL",
  "why did the last job stop?",
];

type Step = { vial: number; channel: number; targetMl: number };
type Msg = {
  _id: string;
  role: "user" | "assistant" | "tool";
  text: string;
  createdAt: number;
  tool?: { name: string; args: unknown } | null;
};

export default function Work() {
  const messages = useQuery(api.messages.list, {}) as Msg[] | undefined;
  const tolerance = useQuery(api.settings.get, { key: "tolerance_ml" }) as number | null | undefined;
  const send = useMutation(api.messages.send);
  const approve = useAction(api.agent.approvePlan);
  const [draft, setDraft] = useState("");

  async function onSend(text: string) {
    const t = text.trim();
    if (!t) return;
    setDraft("");
    await send({ text: t });
  }

  return (
    <div className="work-grid">
      <section className="card chat-card">
        <div className="card-title">Chat</div>
        <ul className="chat-list">
          {(messages ?? []).map((m) => (
            <li key={m._id} className={`chat-msg chat-${m.role}`}>
              <div className="chat-bubble">
                {m.role === "assistant" && m.tool?.name === "propose_plan"
                  ? <ProposedWorklist text={m.text}
                                      steps={(m.tool.args as { steps: Step[] }).steps}
                                      onApprove={(name) =>
                                        approve({ steps: (m.tool!.args as { steps: Step[] }).steps, approvedBy: name })} />
                  : <div>{m.text}</div>}
                {m.tool?.name === "set_constraint" && (
                  <div className="small dim" style={{ marginTop: 4 }}>
                    (saved to settings)
                  </div>
                )}
              </div>
            </li>
          ))}
          {messages && messages.length === 0 && (
            <li className="empty-text">
              Ask for a pour, a batch, or why something happened. Example: "pour 2 ml into channel 3".
            </li>
          )}
        </ul>
        <div className="chip-row">
          {EXAMPLES.map((ex) => (
            <button key={ex} className="chip" onClick={() => setDraft(ex)}>{ex}</button>
          ))}
        </div>
        <div className="row">
          <input
            value={draft}
            onChange={(e) => setDraft(e.target.value)}
            placeholder="Describe the pours you want…"
            onKeyDown={(e) => { if (e.key === "Enter" && !e.shiftKey) onSend(draft); }}
          />
          <button className="btn-primary" onClick={() => onSend(draft)}>Send</button>
        </div>
      </section>
      <aside className="work-side">
        <section className="card">
          <div className="card-title">Current settings</div>
          <div className="setting-row">
            <span>Pour tolerance</span>
            <span className="num">{tolerance != null ? `${Number(tolerance).toFixed(2)} mL` : "—"}</span>
          </div>
          <div className="small dim">
            Ask the agent "set tolerance to 0.2 mL" to change it.
          </div>
        </section>
      </aside>
    </div>
  );
}

function ProposedWorklist({
  text, steps, onApprove,
}: {
  text: string;
  steps: Step[];
  onApprove: (name: string) => void;
}) {
  const [approvedBy, setApprovedBy] = useState("hema");
  const [done, setDone] = useState(false);
  return (
    <>
      <div>{text}</div>
      <table className="worklist">
        <thead>
          <tr><th>#</th><th>From</th><th>Into</th><th>Volume</th><th>Status</th></tr>
        </thead>
        <tbody>
          {steps.map((s, i) => (
            <tr key={i}>
              <td className="num">{i + 1}</td>
              <td>Vial {String.fromCharCode(64 + s.vial)}</td>
              <td>Channel {s.channel}</td>
              <td className="num">{s.targetMl.toFixed(1)} mL</td>
              <td>{done ? "Submitted" : "Awaiting approval"}</td>
            </tr>
          ))}
        </tbody>
      </table>
      {!done && (
        <div className="row" style={{ marginTop: 8 }}>
          <input style={{ maxWidth: 140 }}
                 value={approvedBy} onChange={(e) => setApprovedBy(e.target.value)} />
          <button className="btn-primary"
                  onClick={async () => { await onApprove(approvedBy); setDone(true); }}>
            Approve all {steps.length}
          </button>
        </div>
      )}
    </>
  );
}
