import { useCallback, useEffect, useRef, useState } from "react";

/** Voice in the browser: speech-to-text for requests (Chrome, Edge, Safari), and replies read aloud. */

type Recognition = {
  lang: string; interimResults: boolean; continuous: boolean;
  start(): void; stop(): void; abort(): void;
  onresult: ((e: { resultIndex: number; results: ArrayLike<ArrayLike<{ transcript: string }> & { isFinal: boolean }> }) => void) | null;
  onerror: ((e: { error: string }) => void) | null;
  onend: (() => void) | null;
};

function recognitionClass(): (new () => Recognition) | null {
  const w = window as unknown as { SpeechRecognition?: new () => Recognition; webkitSpeechRecognition?: new () => Recognition };
  return w.SpeechRecognition ?? w.webkitSpeechRecognition ?? null;
}

export const canListen = typeof window !== "undefined" && recognitionClass() !== null;
export const canSpeak = typeof window !== "undefined" && "speechSynthesis" in window;

/** Press to talk: `interim` while speaking, `onFinal(text)` once when the sentence ends. */
export function useListen(onFinal: (text: string) => void) {
  const [listening, setListening] = useState(false);
  const [interim, setInterim] = useState("");
  const [error, setError] = useState<string | null>(null);
  const rec = useRef<Recognition | null>(null);
  const cb = useRef(onFinal);
  cb.current = onFinal;

  const stop = useCallback(() => rec.current?.stop(), []);
  const start = useCallback(() => {
    const R = recognitionClass();
    if (!R) {
      setError("This browser can't listen. Use Chrome, Edge or Safari, or type instead.");
      return;
    }
    window.speechSynthesis?.cancel();      // don't transcribe our own voice
    const r = new R();
    r.lang = "en-US";
    r.interimResults = true;
    r.continuous = false;
    r.onresult = (e) => {
      let text = "";
      let final = false;
      for (let i = e.resultIndex; i < e.results.length; i++) {
        text += e.results[i][0].transcript;
        final = final || e.results[i].isFinal;
      }
      setInterim(text);
      if (final && text.trim()) cb.current(text.trim());
    };
    r.onerror = (e) => setError(e.error === "not-allowed" ? "Microphone blocked: allow it in the browser's site settings." : e.error);
    r.onend = () => { setListening(false); setInterim(""); };
    rec.current = r;
    setError(null);
    setListening(true);
    r.start();
  }, []);
  useEffect(() => () => rec.current?.abort(), []);
  return { listening, interim, error, start, stop };
}

export function speak(text: string) {
  if (!canSpeak || !text.trim()) return;
  window.speechSynthesis.cancel();
  const u = new SpeechSynthesisUtterance(text.replace(/[*_`#]/g, ""));
  u.rate = 1.05;
  window.speechSynthesis.speak(u);
}

/** Words that approve / reject the plan waiting on screen. A person saying them is the approval. */
export function approvalWord(text: string): "approve" | "reject" | null {
  const t = text.toLowerCase().replace(/[.!?,]/g, "").trim();
  if (/^(approve|approved|yes approve|i approve|go ahead|run it|start it|confirm)( it| the plan| plan)?( please)?$/.test(t)) return "approve";
  if (/^(reject|rejected|cancel|cancel it|don't run it|do not run it|no)( the plan| plan)?( please)?$/.test(t)) return "reject";
  return null;
}
