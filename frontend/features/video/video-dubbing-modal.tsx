"use client";

import { useCallback, useEffect, useState } from "react";
import {
  Mic,
  Sliders,
  AlertCircle,
  RefreshCw,
  Download,
  Loader2,
  Music,
  Radio,
} from "lucide-react";
import { Button } from "@/components/ui/button";
import {
  Dialog,
  DialogContent,
  DialogDescription,
  DialogTitle,
} from "@/components/ui/dialog";
import {
  createDubbingJob,
  getDubbingVoices,
  getVideoDubbings,
} from "@/lib/api";
import { useSSEListener } from "@/lib/use-event-source";
import type { DubbingJobOut, Video, VoiceOption } from "@/types";

function computeDefaultScript(initScript?: string, v?: Video): string {
  if (initScript) return initScript;
  if (v?.script_analysis) {
    const sa = v.script_analysis;
    const parts = [
      sa.remake_script_vi.intro,
      ...sa.remake_script_vi.body,
      sa.remake_script_vi.cta,
    ].filter(Boolean);
    return parts.join("\n\n");
  }
  return v?.title || v?.caption || "";
}

export function VideoDubbingModal({
  video,
  open,
  onOpenChange,
  initialScript,
}: {
  video: Video;
  open: boolean;
  onOpenChange: (open: boolean) => void;
  initialScript?: string;
}) {
  const [voices, setVoices] = useState<VoiceOption[]>([]);
  const [selectedVoice, setSelectedVoice] = useState<string>("vi-VN-HoaiMyNeural");
  const [prevVideoId, setPrevVideoId] = useState(video.id);
  const [scriptText, setScriptText] = useState<string>(() =>
    computeDefaultScript(initialScript, video)
  );
  const [speed, setSpeed] = useState<number>(1.0);
  const [bgmVolume, setBgmVolume] = useState<number>(15);
  const [ducking, setDucking] = useState<boolean>(true);
  const [jobs, setJobs] = useState<DubbingJobOut[]>([]);
  const [loading, setLoading] = useState<boolean>(false);
  const [submitting, setSubmitting] = useState<boolean>(false);
  const [error, setError] = useState<string | null>(null);

  if (video.id !== prevVideoId) {
    setPrevVideoId(video.id);
    setScriptText(computeDefaultScript(initialScript, video));
  }

  const loadJobs = useCallback(async () => {
    setLoading(true);
    try {
      const data = await getVideoDubbings(video.id);
      setJobs(data);
    } catch (e) {
      console.warn("Could not load dubbing jobs", e);
    } finally {
      setLoading(false);
    }
  }, [video.id]);

  // Load voices and existing dubbing jobs on modal open
  useEffect(() => {
    if (!open) return;
    let cancelled = false;

    getDubbingVoices()
      .then((res) => {
        if (!cancelled) {
          setVoices(res.voices);
          if (res.voices.length > 0 && !selectedVoice) {
            setSelectedVoice(res.voices[0].id);
          }
        }
      })
      .catch((e) => console.warn("Could not load voices", e));

    getVideoDubbings(video.id)
      .then((data) => {
        if (!cancelled) setJobs(data);
      })
      .catch((e) => console.warn("Could not load dubbing jobs", e));

    return () => {
      cancelled = true;
    };
  }, [open, video.id, selectedVoice]);

  // Listen to SSE dubbing events
  useSSEListener("dubbing:update", (data: { job_id?: string; video_id?: string; status?: string }) => {
    if (data?.video_id === video.id || (data?.job_id && jobs.some((j) => j.id === data.job_id))) {
      loadJobs();
    }
  });

  async function handleCreateDubbing() {
    if (!scriptText.trim()) {
      setError("Vui lòng nhập nội dung kịch bản cần lồng tiếng.");
      return;
    }
    setError(null);
    setSubmitting(true);
    try {
      const job = await createDubbingJob({
        video_id: video.id,
        script_text: scriptText,
        voice_id: selectedVoice,
        speed,
        bgm_volume_percent: bgmVolume,
        ducking,
      });
      setJobs((prev) => [job, ...prev.filter((j) => j.id !== job.id)]);
    } catch (e) {
      setError((e as Error).message || "Không thể tạo tác vụ lồng tiếng");
    } finally {
      setSubmitting(false);
    }
  }

  const latestJob = jobs[0];
  const isJobActive =
    latestJob &&
    (latestJob.status === "pending" ||
      latestJob.status === "synthesizing" ||
      latestJob.status === "mixing");

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent>
        <div
          style={{
            maxWidth: "760px",
            width: "95vw",
            maxHeight: "85vh",
            overflowY: "auto",
            paddingRight: "0.25rem",
          }}
        >
        <DialogTitle style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
          <Mic size={22} color="#6366f1" />
          Tự động lồng tiếng AI tiếng Việt (Auto-Dubbing)
        </DialogTitle>
        <DialogDescription>
          Chuyển kịch bản chuyển thể thành giọng đọc AI tự nhiên (Microsoft Neural TTS) và tự động hòa âm (Audio Ducking) với âm nền gốc.
        </DialogDescription>

        {error && (
          <div
            style={{
              padding: "0.75rem 1rem",
              background: "#fef2f2",
              border: "1px solid #fecaca",
              borderRadius: "8px",
              color: "#dc2626",
              fontSize: "0.9rem",
              display: "flex",
              alignItems: "center",
              gap: "0.5rem",
              margin: "0.75rem 0",
            }}
          >
            <AlertCircle size={18} />
            <span>{error}</span>
          </div>
        )}

        <div style={{ display: "flex", flexDirection: "column", gap: "1.25rem", marginTop: "1rem" }}>
          {/* Voice selector */}
          <div>
            <label style={{ display: "block", fontWeight: 600, fontSize: "0.9rem", marginBottom: "0.4rem" }}>
              1. Chọn giọng đọc AI:
            </label>
            <div style={{ display: "grid", gridTemplateColumns: "repeat(auto-fit, minmax(220px, 1fr))", gap: "0.5rem" }}>
              {(voices.length > 0
                ? voices
                : [
                    {
                      id: "vi-VN-HoaiMyNeural",
                      name: "Hoài My (Nữ - Bắc Bộ)",
                      description: "Giọng nữ truyền cảm, tự nhiên",
                    },
                    {
                      id: "vi-VN-NamMinhNeural",
                      name: "Nam Minh (Nam - Bắc Bộ)",
                      description: "Giọng nam trầm ấm, chuyên nghiệp",
                    },
                  ]
              ).map((v) => {
                const isSelected = selectedVoice === v.id;
                return (
                  <button
                    key={v.id}
                    type="button"
                    onClick={() => setSelectedVoice(v.id)}
                    style={{
                      display: "flex",
                      flexDirection: "column",
                      alignItems: "flex-start",
                      padding: "0.6rem 0.8rem",
                      borderRadius: "8px",
                      border: isSelected ? "2px solid #6366f1" : "1px solid #e2e8f0",
                      background: isSelected ? "rgba(99, 102, 241, 0.08)" : "#fff",
                      cursor: "pointer",
                      textAlign: "left",
                    }}
                  >
                    <div style={{ display: "flex", alignItems: "center", gap: "0.4rem", fontWeight: 600, fontSize: "0.85rem", color: isSelected ? "#4f46e5" : "inherit" }}>
                      <Radio size={14} color={isSelected ? "#4f46e5" : "#94a3b8"} />
                      {v.name}
                    </div>
                    {v.description && (
                      <span style={{ fontSize: "0.75rem", color: "#64748b", marginTop: "0.2rem" }}>
                        {v.description}
                      </span>
                    )}
                  </button>
                );
              })}
            </div>
          </div>

          {/* Script input */}
          <div>
            <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.4rem" }}>
              <label style={{ fontWeight: 600, fontSize: "0.9rem" }}>
                2. Lời thoại / Kịch bản lồng tiếng:
              </label>
              <span style={{ fontSize: "0.75rem", color: "#64748b" }}>
                {scriptText.length} ký tự
              </span>
            </div>
            <textarea
              value={scriptText}
              onChange={(e) => setScriptText(e.target.value)}
              placeholder="Nhập nội dung tiếng Việt cần thuyết minh cho video này..."
              rows={5}
              style={{
                width: "100%",
                padding: "0.75rem",
                borderRadius: "8px",
                border: "1px solid #cbd5e1",
                fontFamily: "inherit",
                fontSize: "0.9rem",
                lineHeight: 1.5,
              }}
            />
          </div>

          {/* Audio mixing & speed controls */}
          <div
            style={{
              display: "grid",
              gridTemplateColumns: "repeat(auto-fit, minmax(180px, 1fr))",
              gap: "1rem",
              padding: "0.85rem",
              background: "#f8fafc",
              borderRadius: "8px",
              border: "1px solid #f1f5f9",
            }}
          >
            <div>
              <label style={{ display: "flex", alignItems: "center", gap: "0.3rem", fontSize: "0.8rem", fontWeight: 600, marginBottom: "0.3rem" }}>
                <Sliders size={13} />
                Tốc độ đọc: {speed.toFixed(1)}x
              </label>
              <input
                type="range"
                min="0.8"
                max="1.5"
                step="0.1"
                value={speed}
                onChange={(e) => setSpeed(parseFloat(e.target.value))}
                style={{ width: "100%" }}
              />
            </div>

            <div>
              <label style={{ display: "flex", alignItems: "center", gap: "0.3rem", fontSize: "0.8rem", fontWeight: 600, marginBottom: "0.3rem" }}>
                <Music size={13} />
                Âm lượng nhạc nền gốc: {bgmVolume}%
              </label>
              <input
                type="range"
                min="0"
                max="50"
                step="5"
                value={bgmVolume}
                onChange={(e) => setBgmVolume(parseInt(e.target.value, 10))}
                style={{ width: "100%" }}
              />
            </div>

            <div style={{ display: "flex", alignItems: "center", gap: "0.5rem" }}>
              <input
                type="checkbox"
                id="ducking-check"
                checked={ducking}
                onChange={(e) => setDucking(e.target.checked)}
                style={{ cursor: "pointer", width: "16px", height: "16px" }}
              />
              <label htmlFor="ducking-check" style={{ fontSize: "0.8rem", cursor: "pointer" }}>
                <strong>Audio Ducking</strong> (Tự động hạ nhỏ nhạc nền khi có tiếng đọc)
              </label>
            </div>
          </div>

          {/* Submit action */}
          <div style={{ display: "flex", justifyContent: "flex-end", gap: "0.5rem" }}>
            <Button
              onClick={handleCreateDubbing}
              disabled={submitting || isJobActive || !scriptText.trim()}
              style={{
                background: "linear-gradient(135deg, #4f46e5 0%, #7c3aed 100%)",
                color: "#fff",
                display: "flex",
                alignItems: "center",
                gap: "0.4rem",
              }}
            >
              {submitting ? (
                <>
                  <Loader2 size={16} className="animate-spin" />
                  Đang khởi tạo tác vụ…
                </>
              ) : isJobActive ? (
                <>
                  <Loader2 size={16} className="animate-spin" />
                  Đang xử lý âm thanh…
                </>
              ) : (
                <>
                  <Mic size={16} />
                  Bắt đầu tạo lồng tiếng AI
                </>
              )}
            </Button>
          </div>

          {/* Output / History */}
          {jobs.length > 0 && (
            <div style={{ marginTop: "0.5rem", borderTop: "1px solid #e2e8f0", paddingTop: "1rem" }}>
              <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.6rem" }}>
                <h4 style={{ fontSize: "0.9rem", fontWeight: 600, margin: 0 }}>
                  Kết quả lồng tiếng ({jobs.length})
                </h4>
                <Button variant="ghost" size="sm" onClick={loadJobs} disabled={loading}>
                  <RefreshCw size={13} style={{ marginRight: "0.3rem" }} />
                  Làm mới
                </Button>
              </div>

              <div style={{ display: "flex", flexDirection: "column", gap: "0.75rem" }}>
                {jobs.map((job) => (
                  <div
                    key={job.id}
                    style={{
                      padding: "0.85rem",
                      borderRadius: "8px",
                      border: "1px solid #e2e8f0",
                      background: job.status === "completed" ? "#f0fdf4" : "#f8fafc",
                    }}
                  >
                    <div style={{ display: "flex", justifyContent: "space-between", alignItems: "center", marginBottom: "0.4rem" }}>
                      <span style={{ fontSize: "0.8rem", fontWeight: 600, color: "#334155" }}>
                        Giọng: {job.voice_id} · {new Date(job.created_at).toLocaleTimeString("vi-VN")}
                      </span>
                      <span
                        style={{
                          fontSize: "0.75rem",
                          fontWeight: 600,
                          padding: "0.2rem 0.5rem",
                          borderRadius: "4px",
                          background:
                            job.status === "completed"
                              ? "#dcfce7"
                              : job.status === "failed"
                              ? "#fee2e2"
                              : "#e0e7ff",
                          color:
                            job.status === "completed"
                              ? "#15803d"
                              : job.status === "failed"
                              ? "#b91c1c"
                              : "#4338ca",
                        }}
                      >
                        {job.status === "completed"
                          ? "✓ Hoàn thành"
                          : job.status === "failed"
                          ? "✗ Lỗi"
                          : job.status === "synthesizing"
                          ? "Đang tổng hợp giọng nói…"
                          : job.status === "mixing"
                          ? "Đang hòa âm (Ducking)…"
                          : "Đang chờ…"}
                      </span>
                    </div>

                    {job.error_message && (
                      <div style={{ fontSize: "0.8rem", color: "#dc2626", marginTop: "0.3rem" }}>
                        Lỗi: {job.error_message}
                      </div>
                    )}

                    {job.audio_url && (
                      <div style={{ marginTop: "0.5rem", display: "flex", flexDirection: "column", gap: "0.4rem" }}>
                        <audio controls style={{ width: "100%", height: "36px" }} src={job.audio_url} />
                        <div style={{ display: "flex", gap: "0.5rem", justifyContent: "flex-end" }}>
                          <a
                            href={job.audio_url}
                            download={`dubbing_${video.id}.mp3`}
                            className="text-link"
                            style={{
                              display: "inline-flex",
                              alignItems: "center",
                              gap: "0.3rem",
                              fontSize: "0.8rem",
                              padding: "0.25rem 0.6rem",
                              background: "#e0f2fe",
                              borderRadius: "4px",
                              color: "#0369a1",
                              textDecoration: "none",
                            }}
                          >
                            <Download size={13} />
                            Tải file âm thanh (.mp3)
                          </a>
                          {job.video_url && (
                            <a
                              href={job.video_url}
                              download={`dubbed_${video.id}.mp4`}
                              className="text-link"
                              style={{
                                display: "inline-flex",
                                alignItems: "center",
                                gap: "0.3rem",
                                fontSize: "0.8rem",
                                padding: "0.25rem 0.6rem",
                                background: "#dcfce7",
                                borderRadius: "4px",
                                color: "#15803d",
                                textDecoration: "none",
                              }}
                            >
                              <Download size={13} />
                              Tải video đã lồng tiếng (.mp4)
                            </a>
                          )}
                        </div>
                      </div>
                    )}
                  </div>
                ))}
              </div>
            </div>
          )}
        </div>
        </div>
      </DialogContent>
    </Dialog>
  );
}
