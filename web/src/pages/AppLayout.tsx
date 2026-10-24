import { useQuery, useQueryClient } from "@tanstack/react-query";
import { Navigate, Outlet, useNavigate } from "react-router";
import type { Me } from "../lib/api";
import { useAuth } from "../lib/auth";

/** Authenticated shell: waits for the session restore, redirects to /login when signed out. */
export function AppLayout() {
  const { ready, signedIn, api, logout } = useAuth();
  const navigate = useNavigate();
  const queryClient = useQueryClient();
  const me = useQuery({ queryKey: ["me"], queryFn: () => api<Me>("/me"), enabled: signedIn });

  if (!ready) return <p className="p-8 text-slate-500">Loading...</p>;
  if (!signedIn) return <Navigate to="/login" replace />;

  async function handleLogout() {
    await logout();
    queryClient.clear();
    navigate("/login");
  }

  return (
    <div className="min-h-screen">
      <header className="flex items-center justify-between border-b border-slate-200 bg-white px-6 py-3">
        <span className="font-semibold">Podcast Claim Ledger</span>
        <div className="flex items-center gap-4 text-sm">
          <span className="text-slate-600">{me.data?.email}</span>
          <button
            type="button"
            onClick={handleLogout}
            className="rounded border border-slate-300 px-3 py-1 hover:bg-slate-100"
          >
            Log out
          </button>
        </div>
      </header>
      <main className="mx-auto max-w-3xl px-6 py-8">
        <Outlet />
      </main>
    </div>
  );
}
