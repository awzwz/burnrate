import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { BrowserRouter } from "react-router-dom";
import App from "./App";
import { SessionProvider } from "./state/session";
import "./styles/base.css";
import "./styles/product.css";

async function start() {
  // __API_MOCK__ is a build-time constant. Without VITE_API_MOCK=1 this branch and the mock chunk are dropped.
  if (__API_MOCK__) {
    const { installMock } = await import("./mock");
    installMock();
  }
  createRoot(document.getElementById("root")!).render(
    <StrictMode>
      <BrowserRouter>
        <SessionProvider>
          <App />
        </SessionProvider>
      </BrowserRouter>
    </StrictMode>,
  );
}

void start();
