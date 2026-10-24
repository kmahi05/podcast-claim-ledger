import { useState } from "react";
import { Link, Navigate, useNavigate } from "react-router";
import { request } from "../lib/api";
import { useAuth } from "../lib/auth";
import { inputClass } from "../lib/ui";
import { AuthForm } from "./AuthForm";

export function Register() {
  const { login, signedIn } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [displayName, setDisplayName] = useState("");
  const [password, setPassword] = useState("");
  if (signedIn) return <Navigate to="/" replace />;

  return (
    <AuthForm
      title="Create account"
      submitLabel="Create account"
      onSubmit={async () => {
        await request("/auth/register", {
          method: "POST",
          body: JSON.stringify({ email, password, display_name: displayName }),
        });
        await login(email, password);
        navigate("/");
      }}
      footer={
        <>
          Have an account?{" "}
          <Link to="/login" className="text-indigo-600 underline">
            Sign in
          </Link>
        </>
      }
    >
      <label className="block text-sm">
        Email
        <input
          type="email"
          required
          autoComplete="email"
          value={email}
          onChange={(e) => setEmail(e.target.value)}
          className={inputClass}
        />
      </label>
      <label className="block text-sm">
        Display name
        <input
          required
          maxLength={80}
          autoComplete="name"
          value={displayName}
          onChange={(e) => setDisplayName(e.target.value)}
          className={inputClass}
        />
      </label>
      <label className="block text-sm">
        Password (10 to 128 characters)
        <input
          type="password"
          required
          minLength={10}
          maxLength={128}
          autoComplete="new-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className={inputClass}
        />
      </label>
    </AuthForm>
  );
}
