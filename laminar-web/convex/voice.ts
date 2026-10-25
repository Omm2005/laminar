"use node";
/**
 * Cartesia voice, server side only (the API key never reaches the browser).
 *   transcribe: Ink speech-to-text (ink-whisper) on a short push-to-talk clip. The audio is not stored.
 *   speak:      Sonic text-to-speech (sonic-3.6). Each phrase is synthesized once and cached in Convex file
 *               storage, so repeated lines ("Approved. Starting now.") play instantly.
 */
import { v } from "convex/values";
import { action } from "./_generated/server";
import { internal } from "./_generated/api";

const BASE = "https://api.cartesia.ai";
const VERSION = "2026-08-14";
// The Cal Hacks starter voice, as in the team's voice/voice.py. CARTESIA_VOICE_ID overrides it.
const VOICE = process.env.CARTESIA_VOICE_ID ?? "a0e99841-438c-4a64-b679-ae501e7d6091";

function key() {
  const k = process.env.CARTESIA_API_KEY;
  if (!k) throw new Error("Voice is off: CARTESIA_API_KEY is not set on the Convex deployment.");
  return k;
}

// Things speech-to-text often "hears" in silence.
const NOISE = new Set(["", "you", "thank you", "thank you.", "thanks for watching!", "um", "uh", "."]);

export const transcribe = action({
  args: { audio: v.bytes(), mime: v.string(), by: v.string() },
  handler: async (ctx, { audio, mime, by }): Promise<{ text: string }> => {
    await ctx.runMutation(internal.voiceStore.limit, { key: by.trim().toLowerCase() || "anon" });
    if (audio.byteLength > 2_000_000) throw new Error("That clip is too long. Hold the button only while you speak.");
    const form = new FormData();
    const ext = mime.includes("ogg") ? "ogg" : mime.includes("mp4") ? "m4a" : mime.includes("wav") ? "wav" : "webm";
    form.append("file", new Blob([audio], { type: mime }), `clip.${ext}`);
    form.append("model", "ink-whisper");
    form.append("language", "en");
    const res = await fetch(`${BASE}/stt`, { method: "POST", headers: { Authorization: `Bearer ${key()}`, "Cartesia-Version": VERSION }, body: form });
    if (!res.ok) throw new Error(`Speech-to-text failed (HTTP ${res.status}). Type the request instead.`);
    const out = (await res.json()) as { text?: string };
    const text = (out.text ?? "").trim();
    return { text: NOISE.has(text.toLowerCase()) ? "" : text };
  },
});

export const speak = action({
  args: { text: v.string(), urgent: v.optional(v.boolean()) },
  handler: async (ctx, { text, urgent }): Promise<{ url: string }> => {
    const line = text.trim().slice(0, 600);
    if (!line) throw new Error("Nothing to say.");
    const cacheKey = `${urgent ? "!" : ""}${line}`;
    const hit = await ctx.runQuery(internal.voiceStore.lookup, { key: cacheKey });
    if (hit) return { url: hit };
    const res = await fetch(`${BASE}/tts/bytes`, {
      method: "POST",
      headers: { Authorization: `Bearer ${key()}`, "Cartesia-Version": VERSION, "Content-Type": "application/json" },
      body: JSON.stringify({
        model_id: "sonic-3.6",
        transcript: line,
        voice: { id: VOICE },
        output_format: { container: "mp3", sample_rate: 44100, bit_rate: 128000 },
        language: "en",
        // Alerts cut through: a little faster and louder, as on the bench.
        generation_config: urgent ? { speed: 1.1, volume: 1.6 } : { speed: 1, volume: 1 },
      }),
    });
    if (!res.ok) throw new Error(`Text-to-speech failed (HTTP ${res.status}).`);
    const storageId = await ctx.storage.store(new Blob([await res.arrayBuffer()], { type: "audio/mpeg" }));
    return { url: await ctx.runMutation(internal.voiceStore.save, { key: cacheKey, storageId }) };
  },
});
