import { createContext, useCallback, useContext, useEffect, useMemo, useRef, useState, type ReactNode } from "react";
import { ApiError, refreshSession, request, type TokenPair } from "./api";

type Auth = {
  ready: boolean;
  signedIn: boolean;
  login: (email: string, password: string) => Promise<void>;
  logout: () => Promise<void>;
  /** Authenticated call: adds the access token, refreshes once on 401, then retries once. */
  api: <T>(path: string, init?: RequestInit) => Promise<T>;
};

const AuthContext = createContext<Auth | null>(null);

export function AuthProvider({ children }: { children: ReactNode }) {
  // the access token lives only in memory; the refresh token is an httpOnly cookie
  const token = useRef<string | null>(null);
  const [signedIn, setSignedIn] = useState(false);
  const [ready, setReady] = useState(false);

  const setToken = useCallback((pair: TokenPair | null) => {
    token.current = pair?.access_token ?? null;
    setSignedIn(pair !== null);
  }, []);

  useEffect(() => {
    refreshSession().then((pair) => {
      setToken(pair);
      setReady(true);
    });
  }, [setToken]);

  const api = useCallback(
    async <T,>(path: string, init: RequestInit = {}): Promise<T> => {
      try {
        return await request<T>(path, init, token.current);
      } catch (err) {
        if (!(err instanceof ApiError) || err.status !== 401) throw err;
        const pair = await refreshSession();
        setToken(pair);
        if (!pair) throw err;
        return request<T>(path, init, pair.access_token);
      }
    },
    [setToken],
  );

  const login = useCallback(
    async (email: string, password: string) => {
      const pair = await request<TokenPair>("/auth/login", {
        method: "POST",
        body: JSON.stringify({ email, password }),
      });
      setToken(pair);
    },
    [setToken],
  );

  const logout = useCallback(async () => {
    await request("/auth/logout", { method: "POST" }).catch(() => undefined);
    setToken(null);
  }, [setToken]);

  const value = useMemo(() => ({ ready, signedIn, login, logout, api }), [ready, signedIn, login, logout, api]);
  return <AuthContext.Provider value={value}>{children}</AuthContext.Provider>;
}

// eslint-disable-next-line react-refresh/only-export-components
export function useAuth(): Auth {
  const auth = useContext(AuthContext);
  if (!auth) throw new Error("useAuth must be used inside <AuthProvider>");
  return auth;
}
