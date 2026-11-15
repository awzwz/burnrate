import "@testing-library/jest-dom/vitest";
import { afterEach } from "vitest";
import { cleanup } from "@testing-library/react";

// Recent Node versions ship their own half-configured `localStorage` global that shadows jsdom's.
// Tests get a plain in-memory one so storage behaves the same on every Node version.
function memoryStorage(): Storage {
  const data = new Map<string, string>();
  return {
    get length() {
      return data.size;
    },
    clear: () => data.clear(),
    getItem: (k: string) => (data.has(k) ? (data.get(k) as string) : null),
    key: (i: number) => [...data.keys()][i] ?? null,
    removeItem: (k: string) => void data.delete(k),
    setItem: (k: string, v: string) => void data.set(k, String(v)),
  };
}
for (const name of ["localStorage", "sessionStorage"] as const) {
  Object.defineProperty(globalThis, name, { value: memoryStorage(), configurable: true, writable: true });
}

afterEach(() => cleanup());
