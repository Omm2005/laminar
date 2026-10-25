import { useEffect, useState } from "react";
import Site from "./Site";
import Console from "./Console";

const SITE_NAV = [["method", "Method"], ["pour", "The pour"], ["hardware", "Hardware"], ["agent", "Safety"], ["live", "Live link"]];

// console.html is the console; the landing page (index.html) is plain HTML now. The old React project
// page is still reachable at console.html#project.
const viewFromHash = () => (location.hash === "#project" ? "site" : "console");

export default function App() {
  const [view, setView] = useState(viewFromHash);
  useEffect(() => {
    const on = () => { const v = viewFromHash(); setView(v); if (v === "console") window.scrollTo(0, 0); };
    window.addEventListener("hashchange", on);
    return () => window.removeEventListener("hashchange", on);
  }, []);
  const isConsole = view === "console";
  return (
    <>
      {!isConsole && (
        <header className="bar">
          <div className="wrap">
            <a className="mark" href="/">
              <svg viewBox="0 0 26 14" aria-hidden="true">
                {[3.4, 7, 10.6].map((y, i) => <path key={y} d={`M1 ${y}c6.2-2.8 12.2 2.8 18.4 0 2.3-1 4-1.6 5.8-2`} fill="none" stroke="currentColor" strokeWidth="1.4" strokeLinecap="round" opacity={[1, 0.5, 0.24][i]} />)}
              </svg>
              Laminar
            </a>
            <nav aria-label="Sections">{SITE_NAV.map(([id, l]) => <a key={id} href={`#${id}`}>{l}</a>)}</nav>
            <div className="segmented">
              <a href="/">Project</a>
              <a href="#console" aria-current={isConsole ? "page" : undefined} onClick={() => setView("console")}>Console</a>
            </div>
          </div>
        </header>
      )}
      <div id="top" />
      {isConsole ? <Console /> : <Site />}
      {!isConsole && (
        <footer>
          <div className="wrap">
            <span>Laminar · Cal Hacks 13.0 · Berkeley, October 2026</span>
            <span>Live state on Convex · history on Tiger Data · planning by Claude</span>
          </div>
        </footer>
      )}
    </>
  );
}
