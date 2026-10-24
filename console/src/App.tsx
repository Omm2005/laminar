import { NavLink, Route, Routes, Navigate } from "react-router-dom";
import Jobs from "./pages/Jobs";
import Live from "./pages/Live";
import History from "./pages/History";
import Settings from "./pages/Settings";

export default function App() {
  return (
    <div className="shell">
      <header className="nav">
        <div className="brand">Laminar <span className="brand-accent">Console</span></div>
        <nav>
          <NavLink to="/jobs">Jobs</NavLink>
          <NavLink to="/live">Live</NavLink>
          <NavLink to="/history">History</NavLink>
          <NavLink to="/settings">Settings</NavLink>
        </nav>
      </header>
      <main className="page">
        <Routes>
          <Route path="/" element={<Navigate to="/jobs" replace />} />
          <Route path="/jobs" element={<Jobs />} />
          <Route path="/live" element={<Live />} />
          <Route path="/history" element={<History />} />
          <Route path="/settings" element={<Settings />} />
        </Routes>
      </main>
    </div>
  );
}
