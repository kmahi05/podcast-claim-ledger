export class ApiError extends Error {
  status: number;
  code: string;
  constructor(status: number, code: string, message: string) {
    super(message);
    this.status = status;
    this.code = code;
  }
}

export type TokenPair = {
  access_token: string;
  token_type: "bearer";
  expires_in: number;
  refresh_token: string;
};

export type Role = "owner" | "editor" | "viewer";

export type Workspace = {
  workspace_id: string;
  name: string;
  role: Role;
  created_at: string;
};

export type Me = {
  user_id: string;
  email: string;
  display_name: string;
  workspaces: { workspace_id: string; name: string; role: Role }[];
};

/** Call the API (same origin via the Vite proxy). Throws ApiError built from the API's error shape. */
export async function request<T>(path: string, init: RequestInit = {}, token?: string | null): Promise<T> {
  const headers = new Headers(init.headers);
  if (init.body && !headers.has("Content-Type")) headers.set("Content-Type", "application/json");
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const res = await fetch(`/api/v1${path}`, { ...init, headers, credentials: "same-origin" });
  if (res.status === 204) return undefined as T;
  const body = await res.json().catch(() => null);
  if (!res.ok) {
    const err = body?.error ?? { code: "http_error", message: res.statusText };
    throw new ApiError(res.status, err.code, err.message);
  }
  return body as T;
}

let inflight: Promise<TokenPair | null> | null = null;

/** Refresh with the httpOnly cookie. Single-flight: the API treats a replayed refresh token as theft. */
export function refreshSession(): Promise<TokenPair | null> {
  inflight ??= request<TokenPair>("/auth/refresh", { method: "POST" })
    .catch(() => null)
    .finally(() => {
      inflight = null;
    });
  return inflight;
}
