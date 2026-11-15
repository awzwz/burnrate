// Loaded only when the app is built or served with VITE_API_MOCK=1 (see main.tsx).
// It replaces window.fetch for /api/* with the in-browser server; every other request passes through.
import { handle } from "./server";

export function installMock(): void {
  const real = window.fetch.bind(window);
  window.fetch = (input: RequestInfo | URL, init?: RequestInit): Promise<Response> => {
    const raw = typeof input === "string" ? input : input instanceof URL ? input.href : input.url;
    const url = new URL(raw, window.location.origin);
    if (url.origin === window.location.origin && (url.pathname === "/api" || url.pathname.startsWith("/api/"))) {
      return handle(url, init);
    }
    return real(input, init);
  };
  console.info("[burn rate] API mock is on: requests to /api are answered in the browser with sample data.");
}
