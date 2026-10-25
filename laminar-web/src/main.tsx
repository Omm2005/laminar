import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { ConvexProvider, ConvexReactClient } from "convex/react";
import "./index.css";
import App from "./App";

const url = import.meta.env.VITE_CONVEX_URL as string | undefined;
const convex = url ? new ConvexReactClient(url) : null;

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    {convex ? (
      <ConvexProvider client={convex}>
        <App />
      </ConvexProvider>
    ) : (
      <p style={{ padding: 24, fontFamily: "system-ui" }}>VITE_CONVEX_URL is not set. Run <code>npx convex dev</code> once in this folder.</p>
    )}
  </StrictMode>,
);
