import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { useState, type FormEvent } from "react";
import type { Workspace } from "../lib/api";
import { useAuth } from "../lib/auth";
import { buttonClass, errorMessage, inputClass } from "../lib/ui";

export function Home() {
  const { api } = useAuth();
  const queryClient = useQueryClient();
  const [name, setName] = useState("");
  const workspaces = useQuery({
    queryKey: ["workspaces"],
    queryFn: () => api<{ items: Workspace[] }>("/workspaces"),
  });
  const create = useMutation({
    mutationFn: (wsName: string) =>
      api("/workspaces", { method: "POST", body: JSON.stringify({ name: wsName }) }),
    onSuccess: () => {
      setName("");
      queryClient.invalidateQueries({ queryKey: ["workspaces"] });
      queryClient.invalidateQueries({ queryKey: ["me"] });
    },
  });

  function submit(e: FormEvent) {
    e.preventDefault();
    if (name.trim()) create.mutate(name.trim());
  }

  return (
    <section>
      <h1 className="mb-4 text-xl font-semibold">Your workspaces</h1>
      {workspaces.isPending && <p className="text-slate-500">Loading...</p>}
      {workspaces.isError && <p className="text-red-600">{errorMessage(workspaces.error)}</p>}
      {workspaces.data?.items.length === 0 && (
        <p className="mb-4 text-slate-500">No workspaces yet. Create one below.</p>
      )}
      {!!workspaces.data?.items.length && (
        <ul className="mb-8 divide-y divide-slate-200 rounded-lg bg-white shadow">
          {workspaces.data.items.map((ws) => (
            <li key={ws.workspace_id} className="flex justify-between px-4 py-3">
              <span>{ws.name}</span>
              <span className="text-sm text-slate-500">{ws.role}</span>
            </li>
          ))}
        </ul>
      )}
      <form onSubmit={submit} className="flex items-end gap-2">
        <label className="block flex-1 text-sm">
          New workspace name
          <input
            required
            maxLength={100}
            value={name}
            onChange={(e) => setName(e.target.value)}
            className={inputClass}
          />
        </label>
        <button type="submit" disabled={create.isPending} className={buttonClass}>
          Create
        </button>
      </form>
      {create.isError && <p className="mt-2 text-sm text-red-600">{errorMessage(create.error)}</p>}
    </section>
  );
}
