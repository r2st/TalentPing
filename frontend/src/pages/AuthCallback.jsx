import { useEffect } from "react";
import { useNavigate } from "react-router-dom";
import { setToken } from "../lib/api";

export default function AuthCallback() {
  const navigate = useNavigate();

  useEffect(() => {
    const hash = window.location.hash;
    const match = hash.match(/token=([^&]+)/);
    if (match) {
      setToken(match[1]);
      window.location.replace("/");
    } else {
      navigate("/", { replace: true });
    }
  }, [navigate]);

  return (
    <div className="flex min-h-screen items-center justify-center">
      <span className="eyebrow animate-pulse">Signing you in…</span>
    </div>
  );
}
