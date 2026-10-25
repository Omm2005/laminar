import { Link, NavLink, Navigate, Route, Routes } from "react-router-dom";
import { useMutation, useQuery } from "convex/react";
import { api } from "../convex/_generated/api";
import Bench from "./pages/Bench";
import Lab from "./pages/Lab";
import Robot from "./pages/Robot";
import Audit from "./pages/Audit";
import Jobs from "./pages/Jobs";
import History from "./pages/History";
import Settings from "./pages/Settings";
import Toasts from "./components/Toasts";
import { useStation } from "./station";
import { useTheme, type Theme } from "./theme";

const THEMES: { value: Theme; label: string }[] = [
  { value: "system", label: "Auto" }, { value: "light", label: "Light" }, { value: "dark", label: "Dark" },
];

export default function App() {
  const [theme, setTheme] = useTheme();
  return (
    <div className="x-shell">
      <header className="x-nav">
        <div className="x-nav-in">
          <Link to="/lab" className="x-logo"><span className="x-mark">L</span>Laminar</Link>
          <nav className="x-links">
            <NavLink to="/lab">Lab</NavLink>
            <NavLink to="/bench">Bench</NavLink>
            <NavLink to="/robot">Robot</NavLink>
            <NavLink to="/audit">Audit</NavLink>
          </nav>
          <div className="x-right">
            <Readiness />
            <div className="x-seg" role="radiogroup" aria-label="Theme">
              {THEMES.map((t) => (
                <button key={t.value} role="radio" aria-checked={theme === t.value} className={theme === t.value ? "on" : ""}
                        onClick={() => setTheme(t.value)}>{t.label}</button>
              ))}
            </div>
            <StopButton />
          </div>
        </div>
      </header>
      <main className="x-frame">
        <Routes>
          <Route path="/" element={<Navigate to="/lab" replace />} />
          <Route path="/lab" element={<Lab />} />
          <Route path="/work" element={<Navigate to="/lab" replace />} />
          <Route path="/bench" element={<Bench />} />
          <Route path="/robot" element={<section className="x-section"><Robot /></section>} />
          <Route path="/audit" element={<Audit />} />
          <Route path="/live" element={<Navigate to="/robot" replace />} />
          <Route path="/jobs" element={<section className="x-section"><Jobs /></section>} />
          <Route path="/history" element={<section className="x-section"><History /></section>} />
          <Route path="/settings" element={<section className="x-section"><Settings /></section>} />
        </Routes>
      </main>
      <Toasts />
    </div>
  );
}

/** Arm / camera / calibration at a glance (details on the Bench page). */
function Readiness() {
  const st = useStation();
  const live = st?.connected && "arm" in st ? st : null;
  const dot = (ok: boolean | undefined, warn = false) => `x-dot ${ok ? "ok" : warn ? "warn" : "bad"}`;
  return (
    <Link to="/bench" className="x-dots" style={{ textDecoration: "none" }} title="Bench status">
      <span><i className={dot(live?.arm.connected)} />Arm</span>
      <span><i className={dot(live?.camera.connected)} />Camera</span>
      <span><i className={dot(live?.calibration.done, !!live)} />Calib</span>
    </Link>
  );
}

/** Always visible: stops a pour at once and the job before its next step. */
function StopButton() {
  const stop = useQuery(api.control.stopRequested, {});
  const estop = useMutation(api.control.estop);
  const clear = useMutation(api.control.clearStop);
  const on = !!stop?.stop;
  return on
    ? <button className="x-stop on" onClick={() => clear({ source: "web" })} title="STOP is on: click to clear">Clear stop</button>
    : <button className="x-stop" onClick={() => estop({ source: "web", message: "STOP pressed on the dashboard" })}
              title="Stop the robot: ends a pour at once, and the job before its next step">STOP</button>;
}
