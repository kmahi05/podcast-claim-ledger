import { useState, type FormEvent, type ReactNode } from "react";
import { buttonClass, errorMessage } from "../lib/ui";

type Props = {
  title: string;
  submitLabel: string;
  onSubmit: () => Promise<void>;
  children: ReactNode;
  footer: ReactNode;
};

/** Shared card and submit handling for the login and register pages. */
export function AuthForm({ title, submitLabel, onSubmit, children, footer }: Props) {
  const [error, setError] = useState<string | null>(null);
  const [busy, setBusy] = useState(false);

  async function handle(e: FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError(null);
    try {
      await onSubmit();
    } catch (err) {
      setError(errorMessage(err));
    } finally {
      setBusy(false);
    }
  }

  return (
    <main className="mx-auto mt-24 max-w-sm px-4">
      <h1 className="mb-6 text-2xl font-semibold">{title}</h1>
      <form onSubmit={handle} className="space-y-4 rounded-lg bg-white p-6 shadow">
        {children}
        {error && (
          <p role="alert" className="text-sm text-red-600">
            {error}
          </p>
        )}
        <button type="submit" disabled={busy} className={`${buttonClass} w-full`}>
          {submitLabel}
        </button>
      </form>
      <p className="mt-4 text-center text-sm text-slate-600">{footer}</p>
    </main>
  );
}
