import { useCallback, useEffect, useRef, useState } from "react";
import { useAction } from "convex/react";
import { api } from "../convex/_generated/api";
import type { Doc } from "../convex/_generated/dataModel";

/**
 * Cartesia voice on the Run tab. Hold the mic, speak, release: Ink transcribes it in Convex.
 * Sonic reads the checked plan back and calls out progress. Rules, as on the bench:
 *   - the read-back is built from the checked plan, never from model text
 *   - "approve" only works while a plan is waiting, and is logged as "<name> (voice)"
 *   - "stop", "abort", "halt", "freeze" or "emergency" anywhere in a sentence stops at once
 */

export const STOP = /\b(stop|abort|halt|freeze|emergency)\b/i;
export const CANCEL = /\b(don'?t|do not) approve\b|\bcancel\b/i;
export const APPROVE = /^\W*(yes,? )?(approve|approved|go ahead)\W*$/i;

const n = (x: number) => (x === 1 ? "1 milliliter" : `${x} milliliters`);
const list = (xs: number[]) => (xs.length < 2 ? `${xs[0]}` : `${xs.slice(0, -1).join(", ")} and ${xs.at(-1)}`);

/** Spoken read-back, built only from the checked tasks. */
export function readBack(run: Doc<"runs">) {
  const parts = (run.tasks ?? []).map((t) => {
    const same = t.pours.every((p) => p.ml === t.pours[0].ml);
    return same
      ? `Tube ${t.tube}: ${n(t.pours[0].ml)} into well${t.pours.length > 1 ? "s" : ""} ${list(t.pours.map((p) => p.well))}.`
      : `Tube ${t.tube}: ${t.pours.map((p) => `${n(p.ml)} into well ${p.well}`).join(", ")}.`;
  });
  return `${parts.join(" ")} ${run.moves?.length ?? 0} moves. Say approve or cancel.`;
}

export function useSpeaker(enabled: boolean) {
  const speak = useAction(api.voice.speak);
  const audio = useRef<HTMLAudioElement | null>(null);
  const queue = useRef<{ text: string; urgent: boolean }[]>([]);
  const busy = useRef(false);
  const [speaking, setSpeaking] = useState(false);
  const pump = useCallback(async () => {
    if (busy.current || !queue.current.length) return;
    busy.current = true; setSpeaking(true);
    const item = queue.current.shift()!;
    try {
      const { url } = await speak({ text: item.text, urgent: item.urgent });
      await new Promise<void>((resolve) => {
        const a = new Audio(url);
        audio.current = a;
        a.onended = a.onerror = () => resolve();
        a.play().catch(() => resolve());
      });
    } catch { /* voice can fail without stopping anything */ }
    audio.current = null; busy.current = false; setSpeaking(queue.current.length > 0);
    void pump();
  }, [speak]);
  const say = useCallback((text: string, urgent = false) => {
    if (!enabled) return;
    if (urgent) { queue.current = []; audio.current?.pause(); busy.current = false; }
    queue.current.push({ text, urgent });
    void pump();
  }, [enabled, pump]);
  return { say, speaking };
}

/** Push-to-talk with Cartesia Ink: hold, speak, release; the transcript goes to onText. Shared by the Run tab's
 *  voice panel and the assistant chat. */
export function useHoldToTalk(name: string, onText: (text: string) => Promise<unknown> | void) {
  const transcribe = useAction(api.voice.transcribe);
  const [recording, setRec] = useState<MediaRecorder | null>(null);
  const [error, setError] = useState("");
  const [working, setWorking] = useState(false);
  const start = async () => {
    setError("");
    if (!name.trim()) { setError("Enter your name at the top first, so the log records who spoke."); return; }
    try {
      const stream = await navigator.mediaDevices.getUserMedia({ audio: { channelCount: 1, echoCancellation: true, noiseSuppression: true } });
      const r = new MediaRecorder(stream);
      const chunks: Blob[] = [];
      r.ondataavailable = (e) => chunks.push(e.data);
      r.onstop = async () => {
        stream.getTracks().forEach((t) => t.stop());
        const blob = new Blob(chunks, { type: r.mimeType || "audio/webm" });
        if (blob.size < 2000) { setError("Too short. Hold the button while you speak."); return; }
        setWorking(true);
        try {
          const { text } = await transcribe({ audio: await blob.arrayBuffer(), mime: blob.type, by: name });
          if (!text) setError("I didn't catch that. Hold the button while you speak.");
          else await onText(text);
        } catch (e) { setError(String((e as Error).message).replace(/^.*Uncaught Error: /s, "").split("\n")[0]); }
        setWorking(false);
      };
      r.start();
      setRec(r);
    } catch {
      setError("Microphone access was refused. Allow it in the browser, or type the request.");
    }
  };
  const stop = () => { if (recording && recording.state !== "inactive") recording.stop(); setRec(null); };
  return { start, stop, recording, working, error };
}

export default function Voice({ name, run, onRequest, onApprove, onCancel }: {
  name: string;
  run: Doc<"runs"> | undefined;
  onRequest: (text: string) => Promise<unknown>;
  onApprove: () => Promise<unknown>;
  onCancel: (reason: string) => Promise<unknown>;
}) {
  const [enabled, setEnabled] = useState(() => { try { return localStorage.getItem("laminar.voice") !== "off"; } catch { return true; } });
  useEffect(() => { try { localStorage.setItem("laminar.voice", enabled ? "on" : "off"); } catch { /* ignore */ } }, [enabled]);
  const { say, speaking } = useSpeaker(enabled);
  const [heard, setHeard] = useState("");
  const [handleError, setError] = useState("");

  // Announce what happens to the run on screen.
  const seen = useRef<string>("");
  useEffect(() => {
    if (!run) return;
    const key = `${run._id}:${run.status}:${run.summary ? 1 : 0}`;
    if (seen.current === key) return;
    const first = seen.current === "";
    seen.current = key;
    if (first) return; // don't read out whatever was already on screen when the page opened
    if (run.status === "awaiting_approval") say(readBack(run));
    else if (run.status === "rejected") say(run.question ?? `I can't run that. ${run.errors?.[0] ?? ""}`);
    else if (run.status === "running") say("Approved. Starting now.");
    else if (run.status === "done") say(`Done. ${run.result ?? ""}`);
    else if (run.status === "aborted") say("Stopped. Nothing else will move.", true);
  }, [run, say]);
  // Call out each pour as it starts.
  const lastStep = useRef(-1);
  useEffect(() => {
    if (run?.status !== "running" || !run.moves) { lastStep.current = -1; return; }
    const i = (run.step ?? 0) - 1;
    if (i <= lastStep.current) return;
    lastStep.current = i;
    const m = run.moves[i]?.match(/^pour\(well(\d)/);
    if (m) say(`Pouring well ${m[1]}.`);
  }, [run?.status, run?.step, run?.moves, say]);

  async function handle(text: string) {
    setHeard(text);
    setError("");
    if (!text) { setError("I didn't catch that. Hold the button while you speak."); return; }
    const status = run?.status;
    if (STOP.test(text)) {
      say("Stopping now.", true);
      if (run && (status === "running" || status === "approved" || status === "awaiting_approval")) await onCancel("Stopped by voice");
      return;
    }
    if (CANCEL.test(text)) {
      if (status === "awaiting_approval") { await onCancel("Cancelled by voice before motion"); say("Cancelled. Nothing moved."); }
      else say("There is no plan waiting to cancel.");
      return;
    }
    if (APPROVE.test(text)) {
      if (status === "awaiting_approval") await onApprove();
      else say("There is no plan waiting for approval.");
      return;
    }
    if (/^\W*repeat\W*$/i.test(text)) { if (run?.status === "awaiting_approval") say(readBack(run)); else say("Nothing to repeat."); return; }
    if (/^\W*status\W*$/i.test(text)) {
      if (status === "running" && run?.moves) say(`Move ${run.step} of ${run.moves.length}. ${run.moves[(run.step ?? 1) - 1] ?? ""}`.replace(/[()_]/g, " "));
      else say(status ? `The run is ${status.replace("_", " ")}.` : "No run yet.");
      return;
    }
    await onRequest(text);
  }

  const { start, stop, recording: rec, working, error: recError } = useHoldToTalk(name, handle);

  const error = recError || handleError;
  return (
    <div className="voice">
      <button
        className={`mic ${rec ? "on" : ""}`}
        onPointerDown={(e) => { e.preventDefault(); void start(); }}
        onPointerUp={stop} onPointerLeave={() => rec && stop()}
        disabled={working}
        aria-label="Hold to talk"
      >
        <svg width="20" height="20" viewBox="0 0 24 24" fill="none" stroke="currentColor" strokeWidth="1.8" strokeLinecap="round"><path d="M9 5a3 3 0 0 1 6 0v6a3 3 0 0 1-6 0V5M5 10v1a7 7 0 0 0 14 0v-1M12 18v3" /></svg>
      </button>
      <div style={{ minWidth: 0, flex: 1 }}>
        <div className="label">{rec ? "Listening… release to send" : working ? "Transcribing with Cartesia Ink…" : speaking ? "Speaking (Cartesia Sonic)…" : "Hold to talk · say a request, approve, cancel, status or stop"}</div>
        {heard && <div className="mono" style={{ fontSize: "0.875rem", marginTop: 2, overflow: "hidden", textOverflow: "ellipsis", whiteSpace: "nowrap" }}>“{heard}”</div>}
        {error && <div className="err">{error}</div>}
      </div>
      <label className="row" style={{ gap: 6, fontSize: "0.8125rem", color: "var(--muted)" }}>
        <input type="checkbox" checked={enabled} onChange={(e) => setEnabled(e.target.checked)} /> Speak
      </label>
    </div>
  );
}
