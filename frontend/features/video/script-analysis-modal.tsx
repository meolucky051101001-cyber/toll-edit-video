"use client";

import { useEffect, useState } from "react";
import {
  Sparkles,
  Copy,
  Check,
  RefreshCw,
  Zap,
  Target,
  FileText,
  AlertCircle,
  Clock,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import { analyzeVideoScript } from "@/lib/api";
import type { Video, ScriptAnalysis } from "@/types";

export function ScriptAnalysisModal({
  video,
  open,
  onOpenChange,
}: {
  video: Video;
  open: boolean;
  onOpenChange: (open: boolean) => void;
}) {
  const [analysis, setAnalysis] = useState<ScriptAnalysis | null>(
    video.script_analysis || null
  );
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);

  useEffect(() => {
    if (open) {
      if (!analysis) {
        fetchAnalysis(false);
      }
    }
  }, [open, video.id]);

  async function fetchAnalysis(forceRefresh: boolean) {
    setLoading(true);
    setError(null);
    try {
      const data = await analyzeVideoScript(video.id, forceRefresh);
      setAnalysis(data);
    } catch (err) {
      setError((err as Error).message || "Không thể phân tích kịch bản video");
    } finally {
      setLoading(false);
    }
  }

  function handleCopyAll() {
    if (!analysis) return;
    const formatted = [
      `🎣 HOOK 3 GIÂY ĐẦU:`,
      `${analysis.hook_3s}`,
      ``,
      `💡 LUẬN ĐIỂM CỐT LÕI:`,
      ...analysis.core_points.map((p, idx) => `${idx + 1}. ${p}`),
      ``,
      `🎯 KỸ THUẬT GIỮ CHÂN:`,
      ...analysis.retention_tactics.map((t) => `• ${t}`),
      ``,
      `📝 KỊCH BẢN CHUYỂN THỂ TIẾNG VIỆT (REMAKE SCRIPT):`,
      `[MỞ ĐẦU (3-5S)]:`,
      `${analysis.remake_script_vi.intro}`,
      ``,
      `[THÂN BÀI]:`,
      ...analysis.remake_script_vi.body.map((b, idx) => `Phần ${idx + 1}: ${b}`),
      ``,
      `[KÊU GỌI HÀNH ĐỘNG (CTA)]:`,
      `${analysis.remake_script_vi.cta}`,
    ].join("\n");

    navigator.clipboard.writeText(formatted);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <div style={{ maxWidth: "680px", maxHeight: "80vh", overflowY: "auto", paddingRight: "0.25rem" }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "0.5rem", marginBottom: "0.5rem" }}>
            <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
              <div
                style={{
                  width: "32px",
                  height: "32px",
                  borderRadius: "8px",
                  background: "linear-gradient(135deg, #6366f1 0%, #a855f7 100%)",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  color: "#fff",
                }}
              >
                <Sparkles size={18} />
              </div>
              <div>
                <DialogTitle style={{ fontSize: "1.15rem", fontWeight: 600 }}>
                  Bóc Tách Kịch Bản & Gợi Ý Hook AI
                </DialogTitle>
                <DialogDescription style={{ fontSize: "0.82rem", color: "#6b7280" }}>
                  Phân tích cấu trúc video thành công và chuyển thể kịch bản tiếng Việt
                </DialogDescription>
              </div>
            </div>

            {analysis && (
              <div style={{ display: "flex", gap: "0.35rem", alignItems: "center" }}>
                <span
                  style={{
                    fontSize: "0.72rem",
                    padding: "2px 8px",
                    borderRadius: "9999px",
                    background: analysis.source === "gemini" ? "#e0e7ff" : "#f3f4f6",
                    color: analysis.source === "gemini" ? "#4338ca" : "#4b5563",
                    fontWeight: 600,
                  }}
                >
                  {analysis.source === "gemini" ? "✨ Gemini AI" : "⚡ Phân tích nhanh"}
                </span>
                {analysis.cached && (
                  <span
                    style={{
                      fontSize: "0.72rem",
                      padding: "2px 6px",
                      borderRadius: "9999px",
                      background: "#f0fdf4",
                      color: "#166534",
                    }}
                    title="Được lấy từ bộ nhớ đệm"
                  >
                    Đã lưu
                  </span>
                )}
              </div>
            )}
          </div>

          <div style={{ background: "#f9fafb", padding: "0.6rem 0.75rem", borderRadius: "8px", marginBottom: "1rem", fontSize: "0.85rem", border: "1px solid #e5e7eb" }}>
            <strong>Video gốc:</strong> {video.title || video.caption?.slice(0, 80) || "Không có tiêu đề"}
          </div>

          {loading ? (
            <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", padding: "3rem 1rem", gap: "0.75rem" }}>
              <RefreshCw size={32} className="animate-spin" style={{ color: "#6366f1" }} />
              <p style={{ fontSize: "0.9rem", color: "#4b5563", fontWeight: 500 }}>
                Đang dùng AI bóc tách hook và viết kịch bản...
              </p>
            </div>
          ) : error ? (
            <div style={{ padding: "1.5rem", textAlign: "center", background: "#fef2f2", borderRadius: "8px", border: "1px solid #fecaca" }}>
              <AlertCircle size={28} style={{ color: "#ef4444", margin: "0 auto 0.5rem" }} />
              <p style={{ color: "#b91c1c", fontSize: "0.9rem", marginBottom: "1rem" }}>{error}</p>
              <Button size="sm" onClick={() => fetchAnalysis(true)}>
                <RefreshCw size={14} style={{ marginRight: "0.4rem" }} /> Thử lại
              </Button>
            </div>
          ) : analysis ? (
            <div style={{ display: "flex", flexDirection: "column", gap: "1rem" }}>
              {/* 1. Hook 3 Giây Đầu */}
              <div
                style={{
                  background: "#fffbeb",
                  border: "1px solid #fde68a",
                  borderRadius: "8px",
                  padding: "0.85rem 1rem",
                }}
              >
                <div style={{ display: "flex", alignItems: "center", gap: "0.4rem", color: "#b45309", fontWeight: 600, fontSize: "0.92rem", marginBottom: "0.4rem" }}>
                  <Zap size={16} />
                  <span>🎣 Hook 3 Giây Đầu (Giữ chân người xem)</span>
                </div>
                <p style={{ fontSize: "0.88rem", color: "#78350f", lineHeight: "1.45" }}>
                  {analysis.hook_3s}
                </p>
              </div>

              {/* 2. Luận Điểm Cốt Lõi & Kỹ Thuật Giữ Chân */}
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0.75rem" }}>
                <div style={{ background: "#f0fdf4", border: "1px solid #bbf7d0", borderRadius: "8px", padding: "0.75rem" }}>
                  <div style={{ display: "flex", alignItems: "center", gap: "0.35rem", color: "#15803d", fontWeight: 600, fontSize: "0.88rem", marginBottom: "0.4rem" }}>
                    <Target size={15} />
                    <span>💡 Luận Điểm Cốt Lõi</span>
                  </div>
                  <ul style={{ margin: 0, paddingLeft: "1.2rem", fontSize: "0.82rem", color: "#166534", lineHeight: "1.4" }}>
                    {analysis.core_points.map((pt, idx) => (
                      <li key={idx} style={{ marginBottom: "0.25rem" }}>{pt}</li>
                    ))}
                  </ul>
                </div>

                <div style={{ background: "#f5f3ff", border: "1px solid #ddd6fe", borderRadius: "8px", padding: "0.75rem" }}>
                  <div style={{ display: "flex", alignItems: "center", gap: "0.35rem", color: "#6d28d9", fontWeight: 600, fontSize: "0.88rem", marginBottom: "0.4rem" }}>
                    <Clock size={15} />
                    <span>🎯 Kỹ Thuật Giữ Chân</span>
                  </div>
                  <ul style={{ margin: 0, paddingLeft: "1.2rem", fontSize: "0.82rem", color: "#5b21b6", lineHeight: "1.4" }}>
                    {analysis.retention_tactics.map((tc, idx) => (
                      <li key={idx} style={{ marginBottom: "0.25rem" }}>{tc}</li>
                    ))}
                  </ul>
                </div>
              </div>

              {/* 3. Kịch Bản Chuyển Thể Tiếng Việt */}
              <div style={{ background: "#ffffff", border: "1px solid #e5e7eb", borderRadius: "8px", padding: "1rem", boxShadow: "0 1px 3px rgba(0,0,0,0.05)" }}>
                <div style={{ display: "flex", alignItems: "center", gap: "0.4rem", color: "#1f2937", fontWeight: 600, fontSize: "0.95rem", marginBottom: "0.75rem" }}>
                  <FileText size={16} style={{ color: "#6366f1" }} />
                  <span>📝 Kịch Bản Chuyển Thể Tiếng Việt (Remake Script)</span>
                </div>

                {/* Intro */}
                <div style={{ marginBottom: "0.75rem", padding: "0.5rem 0.75rem", background: "#f9fafb", borderRadius: "6px" }}>
                  <span style={{ fontSize: "0.78rem", fontWeight: 700, color: "#4f46e5", textTransform: "uppercase" }}>[Mở đầu - 3s đầu]</span>
                  <p style={{ margin: "0.25rem 0 0", fontSize: "0.86rem", color: "#111827", lineHeight: "1.4" }}>
                    &ldquo;{analysis.remake_script_vi.intro}&rdquo;
                  </p>
                </div>

                {/* Body */}
                <div style={{ marginBottom: "0.75rem", padding: "0.5rem 0.75rem", background: "#f9fafb", borderRadius: "6px" }}>
                  <span style={{ fontSize: "0.78rem", fontWeight: 700, color: "#059669", textTransform: "uppercase" }}>[Thân bài - Diễn biến]</span>
                  <div style={{ display: "flex", flexDirection: "column", gap: "0.35rem", marginTop: "0.25rem" }}>
                    {analysis.remake_script_vi.body.map((step, idx) => (
                      <div key={idx} style={{ fontSize: "0.85rem", color: "#1f2937", display: "flex", gap: "0.4rem" }}>
                        <span style={{ fontWeight: 600, color: "#059669" }}>{idx + 1}.</span>
                        <span>{step}</span>
                      </div>
                    ))}
                  </div>
                </div>

                {/* CTA */}
                <div style={{ padding: "0.5rem 0.75rem", background: "#f9fafb", borderRadius: "6px" }}>
                  <span style={{ fontSize: "0.78rem", fontWeight: 700, color: "#d97706", textTransform: "uppercase" }}>[Kêu gọi hành động - CTA]</span>
                  <p style={{ margin: "0.25rem 0 0", fontSize: "0.86rem", color: "#111827", lineHeight: "1.4" }}>
                    &ldquo;{analysis.remake_script_vi.cta}&rdquo;
                  </p>
                </div>
              </div>

              {/* Actions */}
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: "0.5rem" }}>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => fetchAnalysis(true)}
                  disabled={loading}
                >
                  <RefreshCw size={13} style={{ marginRight: "0.35rem" }} />
                  Phân tích lại
                </Button>

                <Button
                  size="sm"
                  onClick={handleCopyAll}
                  style={{
                    background: copied ? "#059669" : "linear-gradient(135deg, #6366f1 0%, #a855f7 100%)",
                    color: "#fff",
                  }}
                >
                  {copied ? (
                    <>
                      <Check size={14} style={{ marginRight: "0.35rem" }} />
                      Đã sao chép kịch bản!
                    </>
                  ) : (
                    <>
                      <Copy size={14} style={{ marginRight: "0.35rem" }} />
                      Sao chép toàn bộ kịch bản
                    </>
                  )}
                </Button>
              </div>
            </div>
          ) : null}
        </div>
      </DialogContent>
    </Dialog>
  );
}
