import { useState } from "react";
import { Link, Navigate, useNavigate } from "react-router";
import { useAuth } from "../lib/auth";
import { inputClass } from "../lib/ui";
import { AuthForm } from "./AuthForm";

export function Login() {
  const { login, signedIn } = useAuth();
  const navigate = useNavigate();
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  if (signedIn) return <Navigate to="/" replace />;

  return (
    <AuthForm
      title="Sign in"
      submitLabel="Sign in"
      onSubmit={async () => {
        await login(email, password);
        navigate("/");
      }}
      footer={
        <>
          No account?{" "}
          <Link to="/register" className="text-indigo-600 underline">
            Create one
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
        Password
        <input
          type="password"
          required
          autoComplete="current-password"
          value={password}
          onChange={(e) => setPassword(e.target.value)}
          className={inputClass}
        />
      </label>
    </AuthForm>
  );
}
