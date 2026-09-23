"use client";

import { useCallback, useEffect, useState } from "react";
import {
  Copy,
  Check,
  RefreshCw,
  Zap,
  Target,
  FileText,
  AlertCircle,
  Clock,
  Mic,
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
import { VideoDubbingModal } from "./video-dubbing-modal";

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
  const [activeTab, setActiveTab] = useState<"script" | "storyboard">("script");
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [copied, setCopied] = useState(false);
  const [dubbingOpen, setDubbingOpen] = useState(false);
  const isAnalyzing = loading || (open && !analysis && !error);

  const fetchAnalysis = useCallback(
    async (forceRefresh: boolean) => {
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
    },
    [video.id]
  );

  useEffect(() => {
    if (!open || analysis) return;
    let cancelled = false;
    analyzeVideoScript(video.id, false)
      .then((data) => {
        if (!cancelled) setAnalysis(data);
      })
      .catch((err) => {
        if (!cancelled) setError((err as Error).message || "Không thể phân tích kịch bản video");
      });
    return () => {
      cancelled = true;
    };
  }, [open, analysis, video.id]);

  function handleCopyAll() {
    if (!analysis) return;
    let formatted = "";

    if (activeTab === "storyboard" && analysis.storyboard && analysis.storyboard.length > 0) {
      formatted = [
        `🎬 BẢNG PHÂN CẢNH VIDEO (STORYBOARD):`,
        `Video: ${video.title || video.caption?.slice(0, 60)}`,
        `Phiên bản: ${analysis.analysis_version || "v2.0"} (${analysis.model_used || "gemini"})`,
        ``,
        ...analysis.storyboard.map((s) => (
          `--- CẢNH ${s.scene_number} [${s.timestamp_range}] ---\n` +
          `🎥 HÌNH ẢNH / GÓC MÁY: ${s.visual_action}\n` +
          `🎙️ LỜI THOẠI / VOICE: ${s.spoken_narration}\n` +
          (s.on_screen_text ? `💬 CHỮ TRÊN MÀN HÌNH: ${s.on_screen_text}\n` : "") +
          (s.sound_music ? `🎵 ÂM THANH / SFX: ${s.sound_music}\n` : "")
        )),
      ].join("\n");
    } else {
      formatted = [
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
    }

    navigator.clipboard.writeText(formatted);
    setCopied(true);
    setTimeout(() => setCopied(false), 2000);
  }

  return (
    <>
      <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <div style={{ maxWidth: "680px", maxHeight: "80vh", overflowY: "auto", paddingRight: "0.25rem" }}>
          <div style={{ display: "flex", alignItems: "center", justifyContent: "space-between", gap: "0.5rem", marginBottom: "0.5rem" }}>
            <div style={{ display: "flex", alignItems: "center", gap: "0.65rem" }}>
              <div
                style={{
                  width: "34px",
                  height: "34px",
                  borderRadius: "8px",
                  background: "var(--surface-raised)",
                  border: "1px solid var(--line)",
                  display: "flex",
                  alignItems: "center",
                  justifyContent: "center",
                  color: "var(--ink)",
                }}
              >
                <FileText size={18} />
              </div>
              <div>
                <DialogTitle style={{ fontSize: "1.1rem", fontWeight: 600, color: "var(--ink)" }}>
                  Phân tích kịch bản & Cấu trúc video
                </DialogTitle>
                <DialogDescription style={{ fontSize: "0.82rem", color: "var(--muted)" }}>
                  Bóc tách luận điểm, kỹ thuật giữ chân và kịch bản chuyển thể tiếng Việt
                </DialogDescription>
              </div>
            </div>

            {analysis && (
              <div style={{ display: "flex", gap: "0.35rem", alignItems: "center" }}>
                <span className="studio-badge">
                  {analysis.source === "gemini" ? "Gemini" : "Nhanh"}
                </span>
                {analysis.analysis_version && (
                  <span className="studio-badge">
                    {analysis.analysis_version}
                  </span>
                )}
                {analysis.cached && (
                  <span className="studio-badge" title="Được lấy từ bộ nhớ đệm">
                    Đã lưu
                  </span>
                )}
              </div>
            )}
          </div>

          <div
            style={{
              background: "var(--surface-raised)",
              padding: "0.6rem 0.8rem",
              borderRadius: "8px",
              marginBottom: "1rem",
              fontSize: "0.84rem",
              border: "1px solid var(--line)",
              color: "var(--ink)",
            }}
          >
            <strong style={{ color: "var(--muted)", marginRight: "6px" }}>Video:</strong>
            {video.title || video.caption?.slice(0, 80) || "Không có tiêu đề"}
          </div>

          {isAnalyzing ? (
            <div style={{ display: "flex", flexDirection: "column", alignItems: "center", justifyContent: "center", padding: "3rem 1rem", gap: "0.75rem" }}>
              <RefreshCw size={28} className="animate-spin" style={{ color: "var(--accent)" }} />
              <p style={{ fontSize: "0.88rem", color: "var(--muted)", fontWeight: 500 }}>
                Đang bóc tách kịch bản và phân tích cấu trúc...
              </p>
            </div>
          ) : error ? (
            <div style={{ padding: "1.5rem", textAlign: "center", background: "var(--surface-raised)", borderRadius: "8px", border: "1px solid var(--line)" }}>
              <AlertCircle size={28} style={{ color: "var(--danger)", margin: "0 auto 0.5rem" }} />
              <p style={{ color: "var(--danger)", fontSize: "0.9rem", marginBottom: "1rem" }}>{error}</p>
              <Button size="sm" onClick={() => fetchAnalysis(true)}>
                <RefreshCw size={14} style={{ marginRight: "0.4rem" }} /> Thử lại
              </Button>
            </div>
          ) : analysis ? (
            <div style={{ display: "flex", flexDirection: "column", gap: "0.85rem" }}>
              {/* 1. Hook mở đầu */}
              <div className="studio-card-accent">
                <div style={{ display: "flex", alignItems: "center", gap: "0.45rem", color: "var(--ink)", fontWeight: 600, fontSize: "0.88rem", marginBottom: "0.35rem" }}>
                  <Zap size={15} style={{ color: "var(--accent)" }} />
                  <span>Hook mở đầu (Giữ chân người xem)</span>
                </div>
                <p style={{ fontSize: "0.88rem", color: "var(--ink)", lineHeight: "1.55", opacity: 0.9 }}>
                  {analysis.hook_3s}
                </p>
              </div>

              {/* 2. Luận Điểm Cốt Lõi & Kỹ Thuật Giữ Chân */}
              <div style={{ display: "grid", gridTemplateColumns: "1fr 1fr", gap: "0.75rem" }}>
                <div className="studio-card">
                  <div style={{ display: "flex", alignItems: "center", gap: "0.4rem", color: "var(--ink)", fontWeight: 600, fontSize: "0.86rem", marginBottom: "0.4rem" }}>
                    <Target size={15} style={{ color: "var(--accent)" }} />
                    <span>Luận điểm cốt lõi</span>
                  </div>
                  <ul style={{ margin: 0, paddingLeft: "1.15rem", fontSize: "0.82rem", color: "var(--ink)", opacity: 0.88, lineHeight: "1.5" }}>
                    {analysis.core_points.map((pt, idx) => (
                      <li key={idx} style={{ marginBottom: "0.25rem" }}>{pt}</li>
                    ))}
                  </ul>
                </div>

                <div className="studio-card">
                  <div style={{ display: "flex", alignItems: "center", gap: "0.4rem", color: "var(--ink)", fontWeight: 600, fontSize: "0.86rem", marginBottom: "0.4rem" }}>
                    <Clock size={15} style={{ color: "var(--accent)" }} />
                    <span>Kỹ thuật giữ chân</span>
                  </div>
                  <ul style={{ margin: 0, paddingLeft: "1.15rem", fontSize: "0.82rem", color: "var(--ink)", opacity: 0.88, lineHeight: "1.5" }}>
                    {analysis.retention_tactics.map((tc, idx) => (
                      <li key={idx} style={{ marginBottom: "0.25rem" }}>{tc}</li>
                    ))}
                  </ul>
                </div>
              </div>

              {/* Tab Navigation */}
              <div className="segmented-control" style={{ width: "fit-content", marginTop: "0.25rem" }}>
                <button
                  type="button"
                  onClick={() => setActiveTab("script")}
                  className={`segmented-button ${activeTab === "script" ? "active" : ""}`}
                >
                  <FileText size={14} />
                  <span>Kịch bản lời thoại (Script)</span>
                </button>
                <button
                  type="button"
                  onClick={() => setActiveTab("storyboard")}
                  className={`segmented-button ${activeTab === "storyboard" ? "active" : ""}`}
                >
                  <Clock size={14} />
                  <span>Bảng phân cảnh (Storyboard)</span>
                  {analysis.storyboard && analysis.storyboard.length > 0 && (
                    <span className="metric-pill" style={{ padding: "0 5px", fontSize: "10px" }}>
                      {analysis.storyboard.length}
                    </span>
                  )}
                </button>
              </div>

              {/* Tab 1: Kịch Bản Chuyển Thể Tiếng Việt */}
              {activeTab === "script" ? (
                <div className="studio-card">
                  <div style={{ display: "flex", alignItems: "center", gap: "0.4rem", color: "var(--ink)", fontWeight: 600, fontSize: "0.9rem", marginBottom: "0.75rem" }}>
                    <FileText size={15} style={{ color: "var(--accent)" }} />
                    <span>Kịch bản chuyển thể tiếng Việt (Remake Script)</span>
                  </div>

                  {/* Intro */}
                  <div style={{ marginBottom: "0.75rem", padding: "0.6rem 0.8rem", background: "var(--surface-inset)", borderRadius: "6px", border: "1px solid var(--line)" }}>
                    <span style={{ fontSize: "0.75rem", fontWeight: 700, color: "var(--accent)", textTransform: "uppercase" }}>[Mở đầu - 3s đầu]</span>
                    <p style={{ margin: "0.3rem 0 0", fontSize: "0.88rem", color: "var(--ink)", lineHeight: "1.5" }}>
                      &ldquo;{analysis.remake_script_vi.intro}&rdquo;
                    </p>
                  </div>

                  {/* Body */}
                  <div style={{ marginBottom: "0.75rem", padding: "0.6rem 0.8rem", background: "var(--surface-inset)", borderRadius: "6px", border: "1px solid var(--line)" }}>
                    <span style={{ fontSize: "0.75rem", fontWeight: 700, color: "var(--success)", textTransform: "uppercase" }}>[Thân bài - Diễn biến]</span>
                    <div style={{ display: "flex", flexDirection: "column", gap: "0.4rem", marginTop: "0.3rem" }}>
                      {analysis.remake_script_vi.body.map((step, idx) => (
                        <div key={idx} style={{ fontSize: "0.88rem", color: "var(--ink)", display: "flex", gap: "0.4rem", lineHeight: "1.5" }}>
                          <span style={{ fontWeight: 600, color: "var(--success)" }}>{idx + 1}.</span>
                          <span>{step}</span>
                        </div>
                      ))}
                    </div>
                  </div>

                  {/* CTA */}
                  <div style={{ padding: "0.6rem 0.8rem", background: "var(--surface-inset)", borderRadius: "6px", border: "1px solid var(--line)" }}>
                    <span style={{ fontSize: "0.75rem", fontWeight: 700, color: "var(--warning)", textTransform: "uppercase" }}>[Kêu gọi hành động - CTA]</span>
                    <p style={{ margin: "0.3rem 0 0", fontSize: "0.88rem", color: "var(--ink)", lineHeight: "1.5" }}>
                      &ldquo;{analysis.remake_script_vi.cta}&rdquo;
                    </p>
                  </div>
                </div>
              ) : (
                /* Tab 2: Bảng Phân Cảnh Chi Tiết (Storyboard) */
                <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
                  {analysis.storyboard && analysis.storyboard.length > 0 ? (
                    analysis.storyboard.map((scene) => (
                      <div key={scene.scene_number} className="studio-card">
                        <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.5rem" }}>
                          <span style={{ fontWeight: 700, fontSize: "0.86rem", color: "var(--ink)" }}>
                            CẢNH {scene.scene_number}
                          </span>
                          <span className="metric-pill">
                            {scene.timestamp_range}
                          </span>
                        </div>

                        <div style={{ display: "flex", flexDirection: "column", gap: "0.45rem", fontSize: "0.84rem" }}>
                          <div style={{ color: "var(--ink)" }}>
                            <strong style={{ color: "var(--accent)" }}>Hình ảnh / Góc máy: </strong>
                            {scene.visual_action}
                          </div>
                          <div style={{ color: "var(--ink)", background: "var(--surface-inset)", padding: "0.45rem 0.65rem", borderRadius: "6px", border: "1px solid var(--line)" }}>
                            <strong style={{ color: "var(--success)" }}>Lời thoại / Voice: </strong>
                            &ldquo;{scene.spoken_narration}&rdquo;
                          </div>
                          {scene.on_screen_text && (
                            <div style={{ color: "var(--muted)" }}>
                              <strong style={{ color: "var(--warning)" }}>Chữ trên màn hình: </strong>
                              {scene.on_screen_text}
                            </div>
                          )}
                          {scene.sound_music && (
                            <div style={{ color: "var(--muted)" }}>
                              <strong style={{ color: "var(--accent)" }}>Âm thanh / SFX: </strong>
                              {scene.sound_music}
                            </div>
                          )}
                        </div>
                      </div>
                    ))
                  ) : (
                    <div style={{ padding: "1.5rem", textAlign: "center", background: "var(--surface-raised)", borderRadius: "8px", border: "1px dashed var(--line)" }}>
                      <Clock size={24} style={{ color: "var(--muted)", margin: "0 auto 0.5rem" }} />
                      <p style={{ fontSize: "0.85rem", color: "var(--muted)", margin: 0 }}>
                        Video này đã phân tích ở phiên bản cũ. Bấm <strong>&quot;Phân tích lại&quot;</strong> bên dưới để tạo Storyboard v2.0.
                      </p>
                    </div>
                  )}
                </div>
              )}

              {/* Actions */}
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginTop: "0.5rem" }}>
                <Button
                  variant="outline"
                  size="sm"
                  onClick={() => fetchAnalysis(true)}
                  disabled={isAnalyzing}
                >
                  <RefreshCw size={13} style={{ marginRight: "0.35rem" }} />
                  Phân tích lại
                </Button>

                <div style={{ display: "flex", gap: "0.5rem", alignItems: "center" }}>
                  <Button
                    variant="outline"
                    size="sm"
                    onClick={() => setDubbingOpen(true)}
                  >
                    <Mic size={14} style={{ marginRight: "0.35rem" }} />
                    Lồng tiếng
                  </Button>

                  <Button
                    size="sm"
                    onClick={handleCopyAll}
                  >
                    {copied ? (
                      <>
                        <Check size={14} style={{ marginRight: "0.35rem" }} />
                        Đã sao chép kịch bản!
                      </>
                    ) : (
                      <>
                        <Copy size={14} style={{ marginRight: "0.35rem" }} />
                        Sao chép kịch bản
                      </>
                    )}
                  </Button>
                </div>
              </div>
            </div>
          ) : null}
        </div>
      </DialogContent>
    </Dialog>
    <VideoDubbingModal
      video={video}
      open={dubbingOpen}
      onOpenChange={setDubbingOpen}
      initialScript={
        analysis
          ? [
              analysis.remake_script_vi.intro,
              ...analysis.remake_script_vi.body,
              analysis.remake_script_vi.cta,
            ].join("\n\n")
          : undefined
      }
    />
    </>
  );
}
