import { NavLink, Navigate, Route, Routes } from "react-router-dom";
import StatusBar from "./components/StatusBar";
import Bench from "./pages/Bench";
import Work from "./pages/Work";
import Robot from "./pages/Robot";
import Audit from "./pages/Audit";
import Runs from "./pages/Runs";
import Jobs from "./pages/Jobs";
import History from "./pages/History";
import Settings from "./pages/Settings";

export default function App() {
  return (
    <div className="shell">
      <header className="nav">
        <div className="brand">Laminar <span className="brand-accent">Console</span></div>
        <nav>
          <NavLink to="/bench">Bench</NavLink>
          <NavLink to="/work">Work</NavLink>
          <NavLink to="/robot">Robot</NavLink>
          <NavLink to="/runs">Runs</NavLink>
          <NavLink to="/audit">Audit</NavLink>
        </nav>
      </header>
      <main className="page">
        <StatusBar />
        <Routes>
          <Route path="/" element={<Navigate to="/bench" replace />} />
          <Route path="/bench" element={<Bench />} />
          <Route path="/work" element={<Work />} />
          <Route path="/robot" element={<Robot />} />
          <Route path="/runs" element={<Runs />} />
          <Route path="/audit" element={<Audit />} />
          {/* keep the older views reachable but not in the primary nav */}
          <Route path="/live" element={<Navigate to="/robot" replace />} />
          <Route path="/jobs" element={<Jobs />} />
          <Route path="/history" element={<History />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </main>
    </div>
  );
}
