"use client";
import { useState } from "react";
import { Button } from "@/components/ui/button";
import { ErrorBanner } from "@/components/error-banner";
import { api } from "@/lib/api";
import type { Settings } from "@/types";

export function AISettings({
  settings,
  refresh,
}: {
  settings: Settings;
  refresh: () => void;
}) {
  const [provider, setProvider] = useState(settings.ai_provider);
  const [model, setModel] = useState(settings.ai_model);
  const [key, setKey] = useState("");
  const [confirmed, setConfirmed] = useState(settings.ai_free_tier_confirmed);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [message, setMessage] = useState("");
  async function save(e: React.FormEvent) {
    e.preventDefault();
    setBusy(true);
    setError("");
    setMessage("");
    try {
      await api("/settings", {
        method: "PATCH",
        body: JSON.stringify({
          ai_provider: provider,
          ai_model: model,
          ai_api_key: key || undefined,
          ai_free_tier_confirmed: confirmed,
        }),
        signal: AbortSignal.timeout(95000),
      });
      setKey("");
      setMessage("Đã lưu cấu hình trên máy này.");
      refresh();
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  async function testConnection() {
    setBusy(true);
    setError("");
    setMessage("");
    try {
      const result = await api<{ ok: boolean; message: string }>(
        "/settings/ai/test",
        { method: "POST", signal: AbortSignal.timeout(95000) },
      );
      if (result.ok) setMessage(result.message);
      else setError(result.message);
    } catch (e) {
      setError((e as Error).message);
    } finally {
      setBusy(false);
    }
  }
  return (
    <>
      <p>
        {settings.ai_available ? "Gemini đã cấu hình" : "AI chưa sẵn sàng"} ·{" "}
        {settings.ai_key_configured ? "Đã lưu khóa" : "Chưa có khóa"}
      </p>
      <form className="ai-settings-form" onSubmit={save}>
        <label>
          Nhà cung cấp
          <select
            aria-label="Nhà cung cấp AI"
            value={provider}
            onChange={(e) => setProvider(e.target.value)}
          >
            <option value="gemini">Google Gemini</option>
            <option value="">Tắt AI</option>
          </select>
        </label>
        <label>
          Model
          <select
            aria-label="Model AI"
            value={model}
            onChange={(e) => setModel(e.target.value)}
          >
            <option value="gemini-3.1-flash-lite">Gemini 3.1 Flash Lite</option>
            <option value="gemini-3.8-flash">Gemini 3.8 Flash</option>
          </select>
        </label>
        <label>
          Khóa API mới
          <input
            aria-label="Khóa API mới"
            type="password"
            autoComplete="new-password"
            value={key}
            maxLength={500}
            placeholder="Để trống để giữ khóa hiện tại"
            onChange={(e) => {
              setKey(e.target.value);
              setConfirmed(false);
            }}
          />
        </label>
        <label className="row">
          <input
            type="checkbox"
            checked={confirmed}
            onChange={(e) => setConfirmed(e.target.checked)}
          />
          Dự án của khóa này ở Free Tier và chưa bật Billing
        </label>
        <p className="muted">
          Chỉ gửi chủ đề khi bạn yêu cầu AI. Khoảng cách gọi tối thiểu{" "}
          {settings.ai_min_interval} giây; không tự chuyển model trả phí. Free
          Tier có hạn mức do Google quy định.
        </p>
        <div className="row wrap">
          <Button type="submit" disabled={busy}>
            Lưu cấu hình AI
          </Button>
          <Button
            type="button"
            variant="outline"
            disabled={busy || !settings.ai_available}
            onClick={testConnection}
          >
            Kiểm tra kết nối đã lưu
          </Button>
        </div>
      </form>
      {busy && <p role="status">Đang xử lý…</p>}
      {message && <p role="status">{message}</p>}
      <ErrorBanner message={error} />
    </>
  );
}
