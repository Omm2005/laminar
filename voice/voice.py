"""Laminar voice: hands-free control for gloved lab techs, built on Cartesia.

    Ink (speech-to-text)  ->  intent  ->  Claude plan + code validator  ->  spoken read-back
    "approve"  ->  robot runs  ->  Sonic (text-to-speech) callouts while pouring  ->  spoken report

Safety rules built in:
- Nothing moves without a spoken "approve" AFTER the plan is read back.
- The read-back is generated from the VALIDATED plan JSON (plan_to_speech), never from
  free LLM text, so what you hear is exactly what will run.
- "Stop" / "abort" / "halt" anywhere in a sentence = stop immediately, in any state.
  (Software stop only. The hardware e-stop button stays the primary stop.)
- Audio is never saved; only transcripts go to the audit log.
- While the speaker talks, the microphone is ignored (it won't hear itself).

Setup:
    pip install cartesia sounddevice numpy
    export CARTESIA_API_KEY=...            # never commit this
    python voice.py                        # mic + speakers, fake robot
    python voice.py --mock                 # no API, no mic: type instead of speaking
"""
from __future__ import annotations

import io
import os
import queue
import re
import threading
import time
import wave
from dataclasses import dataclass, field
from typing import Callable

import numpy as np

TTS_MODEL = os.environ.get("CARTESIA_TTS_MODEL", "sonic-3.6")
STT_MODEL = os.environ.get("CARTESIA_STT_MODEL", "ink-whisper")
# Default voice from the Cal Hacks starter pack; pick your own at play.cartesia.ai/voices
VOICE_ID = os.environ.get("CARTESIA_VOICE_ID", "a0e99841-438c-4a64-b679-ae501e7d6091")
SPEED = float(os.environ.get("CARTESIA_SPEED", "1.0"))   # 0.6 - 1.5
TTS_RATE = 24000      # Hz, speaker output
MIC_RATE = 16000      # Hz, microphone input

# Phrases spoken often: generated once at startup so they play instantly (and offline).
COMMON_PHRASES = [
    "Approved. Starting now.", "Cancelled. Nothing moved.", "Stopping now.",
    "Sorry, I didn't catch that.", "Say approve to run it, or cancel.",
    "I'm listening.",
]


# ---------------------------------------------------------------- speech out (Sonic)

class Speaker:
    """Speaks in a background thread. say() never blocks the robot code."""

    def __init__(self, client=None, mock: bool = False):
        self.client, self.mock = client, mock
        self.q: "queue.Queue[tuple[str, bool]]" = queue.Queue()
        self.cache: dict[str, np.ndarray] = {}
        self.busy = threading.Event()
        threading.Thread(target=self._loop, daemon=True).start()

    def synth(self, text: str, urgent: bool = False) -> np.ndarray:
        key = (text, urgent)
        if key in self.cache:
            return self.cache[key]
        # The SDK sends the Cartesia-Version header (2026-08-14) on every request.
        resp = self.client.tts.generate(
            model_id=TTS_MODEL, transcript=text, voice=VOICE_ID, language="en",
            output_format={"container": "raw", "encoding": "pcm_s16le", "sample_rate": TTS_RATE},
            # alerts: a bit faster and louder so they cut through lab noise
            generation_config={"speed": min(1.5, SPEED * (1.1 if urgent else 1.0)),
                               "volume": 1.6 if urgent else 1.0},
        )
        pcm = np.frombuffer(resp.read(), dtype=np.int16)
        if len(text) < 60:                      # keep short phrases for instant replay
            self.cache[key] = pcm
        return pcm

    def precache(self, phrases=COMMON_PHRASES):
        if self.mock:
            return
        for p in phrases:
            try:
                self.synth(p, urgent=(p == "Stopping now."))
            except Exception as e:              # offline at startup: speak live later
                print(f"[voice] precache failed for {p!r}: {e}")

    def say(self, text: str, urgent: bool = False):
        """Queue speech. urgent=True interrupts whatever is playing (alerts, stop)."""
        if urgent:
            while not self.q.empty():
                self.q.get_nowait()
            if not self.mock:
                import sounddevice as sd
                sd.stop()
        self.q.put((text, urgent))

    def _loop(self):
        while True:
            text, urgent = self.q.get()
            self.busy.set()
            try:
                print(f"  LAMINAR SAYS: {text}")
                if not self.mock:
                    import sounddevice as sd
                    sd.play(self.synth(text, urgent), TTS_RATE)
                    sd.wait()
            except Exception as e:
                print(f"[voice] could not speak ({e}); text was: {text}")
            finally:
                time.sleep(0.25)                # let room echo die before listening again
                self.busy.clear()


# ---------------------------------------------------------------- speech in (Ink)

class Listener:
    """Records one utterance (starts on voice, ends on ~0.8 s of silence), transcribes it."""

    def __init__(self, client=None, speaker: Speaker | None = None, mock: bool = False,
                 threshold: float = 0.02, silence_s: float = 0.8, max_s: float = 10.0):
        self.client, self.speaker, self.mock = client, speaker, mock
        self.threshold, self.silence_s, self.max_s = threshold, silence_s, max_s

    def listen(self) -> str:
        if self.mock:
            try:
                return input("  YOU SAY > ").strip()
            except EOFError:
                return "quit"
        audio = self._record()
        if audio is None:
            return ""
        text = self._transcribe(audio)
        if is_noise_transcript(text):
            print(f"  (ignored noise: {text!r})")
            return ""
        return text

    def _record(self) -> np.ndarray | None:
        import sounddevice as sd
        block = int(MIC_RATE * 0.05)                      # 50 ms blocks
        chunks, started, quiet, t0 = [], False, 0.0, time.time()
        with sd.InputStream(samplerate=MIC_RATE, channels=1, dtype="float32",
                            blocksize=block) as stream:
            while True:
                data, _ = stream.read(block)
                if self.speaker and self.speaker.busy.is_set():
                    chunks, started, quiet = [], False, 0.0  # ignore our own voice
                    continue
                rms = float(np.sqrt(np.mean(data ** 2)))
                if not started:
                    if rms > self.threshold:
                        started, t0 = True, time.time()
                        chunks.append(data.copy())
                    continue
                chunks.append(data.copy())
                quiet = quiet + 0.05 if rms < self.threshold else 0.0
                if quiet >= self.silence_s or time.time() - t0 > self.max_s:
                    break
        if not chunks or len(chunks) * 0.05 < 0.3:        # too short to be speech
            return None
        return np.concatenate(chunks)[:, 0]

    def _transcribe(self, audio: np.ndarray) -> str:
        buf = io.BytesIO()
        with wave.open(buf, "wb") as w:
            w.setnchannels(1)
            w.setsampwidth(2)
            w.setframerate(MIC_RATE)
            w.writeframes((np.clip(audio, -1, 1) * 32767).astype(np.int16).tobytes())
        buf.seek(0)
        resp = self.client.stt.transcribe(file=("utterance.wav", buf, "audio/wav"),
                                          model=STT_MODEL, language="en")
        return (resp.text or "").strip()



# ---------------------------------------------------------------- transcript clean-up

# Speech-to-text models sometimes "hear" these in silence or background noise.
NOISE_PHRASES = {"thank you", "thanks", "thank you very much", "thanks for watching",
                 "you", "bye", "okay", "ok", "so", "uh", "um", "hmm", "yeah"}


def is_noise_transcript(text: str) -> bool:
    t = re.sub(r"[^\w\s]", "", text.lower()).strip()
    return (not t) or t in NOISE_PHRASES or len(t) < 3


NUMBER_WORDS = {"zero": 0, "half": 0.5, "a half": 0.5, "one": 1, "a": 1, "an": 1,
                "two": 2, "to": 2, "too": 2, "three": 3, "four": 4, "for": 4,
                "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9, "ten": 10}
UNIT = r"(?:ml|mls|mil|mils|milliliters?|millilitres?|millimeters?|millimetres?|mL)"


def extract_ml(text: str) -> float | None:
    """'2 ml', '2.5 mL', 'two milliliters', 'one and a half ml' -> float."""
    t = text.lower().replace("-", " ")
    m = re.search(rf"(\d+(?:\.\d+)?)\s*{UNIT}\b", t)
    if m:
        return float(m.group(1))
    m = re.search(rf"\b(\w+)(?:\s+and\s+a\s+half)?(?:\s+point\s+(\w+))?\s+{UNIT}\b", t)
    if m and m.group(1) in NUMBER_WORDS:
        val = float(NUMBER_WORDS[m.group(1)])
        if "and a half" in m.group(0):
            val += 0.5
        if m.group(2) in NUMBER_WORDS:
            val += NUMBER_WORDS[m.group(2)] / 10
        return val
    return None


def extract_column(text: str) -> str | None:
    """'column A', 'column b', 'columns C', also common mishearings ('column bee')."""
    t = text.lower()
    m = re.search(r"\bcolumns?\s+(a|b|c|d|bee|be|see|sea|dee)\b", t)
    if not m:
        return None
    return {"bee": "B", "be": "B", "see": "C", "sea": "C", "dee": "D"}.get(m.group(1),
                                                                        m.group(1).upper())

# ---------------------------------------------------------------- understanding

STOP_WORDS = ("stop", "abort", "halt", "freeze", "emergency")
APPROVE_WORDS = ("approve", "approved", "confirm", "go ahead", "run it")
CANCEL_WORDS = ("cancel", "never mind", "nevermind", "don t", "do not", "reject")


def parse_intent(text: str, wake: str | None = None) -> tuple[str, str]:
    """Returns (intent, text). intent: stop, approve, cancel, status, repeat, request, none."""
    t = re.sub(r"[^\w\s.]", " ", text.lower()).strip()
    if not t:
        return "none", text
    if any(re.search(rf"\b{w}\b", t) for w in STOP_WORDS):
        return "stop", text                                   # always wins, no wake word
    if any(w in t for w in CANCEL_WORDS):              # before approve: "don't approve"
        return "cancel", text
    if any(w in t for w in APPROVE_WORDS):
        return "approve", text
    if re.search(r"\b(status|how much|progress|where are we)\b", t):
        return "status", text
    if re.search(r"\b(repeat|say again|what was the plan)\b", t):
        return "repeat", text
    if wake and wake not in t:
        return "none", text                                   # side conversation, ignore
    return "request", text


def _ml(x: float) -> str:
    x = float(x)
    return f"{x:g} millilitre" + ("" if x == 1 else "s")


def plan_to_speech(plan: dict) -> str:
    """Read-back built from the validated plan JSON, so speech = what will run."""
    # steps are {"skill", "args": {...}} (Om's executor plans); flat steps work too
    pours = [s.get("args", s) for s in plan.get("steps", []) if s.get("skill") == "pour"]
    if not pours:
        return "The plan has no pours."
    parts = []
    for s in pours:
        liquid = s.get("liquid", "")
        what = f" of {liquid}" if liquid else ""
        parts.append(f"{_ml(s['ml'])}{what} into {s.get('spoken_target') or 'column ' + s['target']}")
    total = sum(float(s["ml"]) for s in pours)
    return (f"Plan: {', then '.join(parts)}. Total {_ml(total)}. "
            f"Say approve to run it, or cancel.")


# ---------------------------------------------------------------- live pour callouts

class PourAnnouncer:
    """Feed it PourCommand objects from pour_guide.PourGuide.update(); speaks on changes."""

    LINES = {"slow": "{c}, half full.", "slower": "{c}, three quarters.",
             "final": "{c}, almost there."}

    def __init__(self, speaker: Speaker, column: str, label: str | None = None):
        self.sp, self.col, self.last = speaker, column, None
        self.label = label or f"Column {column}"       # what to call it out loud

    def update(self, cmd):
        if cmd.phase == self.last:
            return
        self.last = cmd.phase
        if cmd.phase in self.LINES:
            self.sp.say(self.LINES[cmd.phase].format(c=self.label))
        elif cmd.phase == "stop":
            self.sp.say(f"{self.label} done. {cmd.ml:.1f} millilitres.")
        elif cmd.phase == "abort":
            self.sp.say(f"Stopped {self.label}. {cmd.reason}.", urgent=True)


# ---------------------------------------------------------------- the conversation

@dataclass
class RobotHooks:
    """Om's code plugs in here. Each function is called from the voice loop."""
    make_plan: Callable[[str], dict]          # request text -> {"plan": {...}} or {"question": "..."}
    run_plan: Callable[[dict], None]          # start executing (non-blocking)
    stop: Callable[[], None]                  # software stop
    status: Callable[[], str]                 # short spoken status
    log: Callable[[str, dict], None] = field(default=lambda ev, d: None)


class VoiceLoop:
    def __init__(self, speaker: Speaker, listener: Listener, hooks: RobotHooks,
                 wake: str | None = None):
        self.sp, self.ls, self.h, self.wake = speaker, listener, hooks, wake
        self.pending: dict | None = None
        self.last_readback = ""
        self._lock = threading.Lock()

    # approve/cancel can also come from the dashboard button (same rules, same log)
    def approve(self, by: str = "voice") -> bool:
        with self._lock:
            plan, self.pending = self.pending, None
        if plan is None:
            self.sp.say("There's no plan waiting. Tell me what to pour first.")
            return False
        self.h.log("approved", {"plan": plan, "by": by})
        self.sp.say("Approved. Starting now.")
        self.h.run_plan(plan)
        return True

    def cancel(self, by: str = "voice"):
        with self._lock:
            had, self.pending = self.pending is not None, None
        self.h.log("cancelled", {"by": by})
        self.sp.say("Cancelled. Nothing moved." if had else "Nothing to cancel.")

    def handle(self, text: str) -> bool:
        """Process one utterance. Returns False to quit."""
        if text.lower() in ("quit", "exit"):
            return False
        intent, _ = parse_intent(text, self.wake)
        self.h.log("heard", {"text": text, "intent": intent})
        if intent == "stop":
            self.h.stop()
            self.pending = None
            self.sp.say("Stopping now.", urgent=True)
        elif intent == "approve":
            self.approve(by="voice")
        elif intent == "cancel":
            self.cancel(by="voice")
        elif intent == "status":
            self.sp.say(self.h.status())
        elif intent == "repeat":
            self.sp.say(self.last_readback or "There's no plan yet.")
        elif intent == "request":
            self.sp.say("Planning.")
            try:
                out = self.h.make_plan(text)
            except Exception as e:
                self.sp.say(f"I couldn't make that plan. {e}")
                return True
            if "question" in out:                       # Claude asks instead of guessing
                self.sp.say(out["question"])
            else:
                self.pending = out["plan"]
                self.last_readback = plan_to_speech(self.pending)
                self.sp.say(self.last_readback)
        return True

    def run(self):
        self.sp.say("I'm listening.")
        while True:
            try:
                text = self.ls.listen()
            except Exception as e:
                print(f"[voice] listen failed: {e}")
                time.sleep(1)
                continue
            if text:
                print(f"  heard: {text!r}")
                if not self.handle(text):
                    break


# ---------------------------------------------------------------- demo with a fake robot

def _fake_hooks(speaker: Speaker) -> RobotHooks:
    state = {"running": False}

    def make_plan(text):
        ml, col = extract_ml(text), extract_column(text)
        if ml is None or col is None:
            return {"question": "How many millilitres, and into which column, A to D?"}
        liquid = "red" if "red" in text.lower() else "blue"
        return {"plan": {"steps": [{"skill": "pour", "target": col, "ml": ml,
                                    "liquid": liquid}]}}

    def run_plan(plan):
        state["running"] = True

        def go():
            p = plan["steps"][0]
            for line in (f"Column {p['target']}, half full.",
                         f"Column {p['target']}, three quarters.",
                         f"Column {p['target']} done. {p['ml']:.1f} millilitres."):
                time.sleep(1.5)
                if not state["running"]:
                    return
                speaker.say(line)
            state["running"] = False
        threading.Thread(target=go, daemon=True).start()

    def stop():
        state["running"] = False

    return RobotHooks(make_plan, run_plan, stop,
                      lambda: "Pouring." if state["running"] else "Idle, ready.",
                      lambda ev, d: print(f"  [log] {ev}: {d}"))


if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    ap.add_argument("--mock", action="store_true", help="no API, no mic: type and read")
    ap.add_argument("--wake", default=None, help='require a wake word, e.g. --wake laminar')
    a = ap.parse_args()
    client = None
    if not a.mock:
        from cartesia import Cartesia
        client = Cartesia(api_key=os.environ["CARTESIA_API_KEY"])
    sp = Speaker(client, mock=a.mock)
    sp.precache()
    loop = VoiceLoop(sp, Listener(client, sp, mock=a.mock), _fake_hooks(sp), wake=a.wake)
    print('Try: "Pour 2 ml of blue into column A", then "approve", "status", "stop".')
    loop.run()
