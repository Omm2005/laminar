import { Component, StrictMode, type ReactNode } from "react";
import { createRoot } from "react-dom/client";
import { ConvexProvider, ConvexReactClient } from "convex/react";
import "./index.css";
import App from "./App";

/** A failed backend call (deployment down, over its plan limits, a function not deployed) used to unmount the whole
 *  page, leaving it white. Show what happened instead, and retry on its own every 30 s. */
class BackendDown extends Component<{ children: ReactNode }, { error: string | null }> {
  state = { error: null as string | null };
  static getDerivedStateFromError(e: unknown) {
    const msg = String((e as Error)?.message ?? e);
    return { error: msg.replace(/^\[CONVEX [^\]]*\]\s*(\[Request ID: [^\]]*\]\s*)?(Server Error\s*)?/, "").split("\n")[0] };
  }
  componentDidUpdate() { if (this.state.error) setTimeout(() => location.reload(), 30_000); }
  render() {
    if (!this.state.error) return this.props.children;
    return (
      <div style={{ padding: 32, maxWidth: 720, fontFamily: "system-ui", lineHeight: 1.5 }}>
        <h2 style={{ margin: "0 0 8px" }}>The dashboard can't reach its backend</h2>
        <p style={{ margin: "0 0 12px" }}>{this.state.error}</p>
        <p style={{ color: "#666", margin: 0 }}>Retrying every 30 s. <button onClick={() => location.reload()}>Retry now</button></p>
      </div>
    );
  }
}

const url = import.meta.env.VITE_CONVEX_URL as string | undefined;
const convex = url ? new ConvexReactClient(url) : null;

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {convex ? (
      <ConvexProvider client={convex}>
        <BackendDown>
          <App />
        </BackendDown>
      </ConvexProvider>
    ) : (
      <p style={{ padding: 24, fontFamily: "system-ui" }}>VITE_CONVEX_URL is not set. Run <code>npx convex dev</code> once in this folder.</p>
    )}
  </StrictMode>,
);
