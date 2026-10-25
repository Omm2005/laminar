import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import { ConvexProvider, ConvexReactClient } from "convex/react";
import App from "./App.tsx";
import "./index.css";
import "./ui.css";

const url = import.meta.env.VITE_CONVEX_URL as string | undefined;
if (!url) {
  // eslint-disable-next-line no-console
  console.error("VITE_CONVEX_URL is not set — run `npx convex dev` in console/ to create .env.local");
}
const convex = new ConvexReactClient(url ?? "");

createRoot(document.getElementById("root")!).render(
  <StrictMode>
    <ConvexProvider client={convex}>
      <BrowserRouter>
        <App />
      </BrowserRouter>
    </ConvexProvider>
  </StrictMode>,
);
