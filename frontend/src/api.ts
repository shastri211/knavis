// In development the backend runs beside the dev server; in a built/containerised app it is served from the same origin.
export const API: string = import.meta.env.VITE_API_URL || (import.meta.env.DEV ? "http://127.0.0.1:8000/api" : "/api");

const TOKEN_KEY = "knavis_token";

export class ApiError extends Error {
  status: number;
  constructor(message: string, status: number) {
    super(message);
    this.status = status;
  }
}

export function getToken(): string | null {
  try { return localStorage.getItem(TOKEN_KEY); } catch { return null; }
}

export function setToken(token: string | null) {
  try {
    if (token) localStorage.setItem(TOKEN_KEY, token); else localStorage.removeItem(TOKEN_KEY);
  } catch { /* storage can be blocked; the session then lasts until reload */ }
}

let onUnauthorized: () => void = () => {};
export function handleUnauthorized(callback: () => void) { onUnauthorized = callback; }

/** Fetch JSON from the API with the sign-in token attached. Throws ApiError with the server's message. */
export async function api<T = any>(path: string, init: RequestInit & { json?: unknown } = {}): Promise<T> {
  const headers = new Headers(init.headers);
  const token = getToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  let body = init.body;
  if (init.json !== undefined) {
    headers.set("Content-Type", "application/json");
    body = JSON.stringify(init.json);
  }
  let response: Response;
  try {
    response = await fetch(API + path, { ...init, headers, body });
  } catch {
    throw new ApiError("Cannot reach the server. Check that it is running and try again.", 0);
  }
  if (response.status === 204) return undefined as T;
  let data: any = null;
  try { data = await response.json(); } catch { /* not JSON */ }
  if (!response.ok) {
    if (response.status === 401 && token && !path.startsWith("/auth/")) onUnauthorized();
    const detail = typeof data?.detail === "string" ? data.detail : "The request could not be completed.";
    throw new ApiError(detail, response.status);
  }
  return data as T;
}
