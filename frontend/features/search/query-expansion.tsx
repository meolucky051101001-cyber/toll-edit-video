"use client";
import { useRef, useState } from "react";
import { Languages, Sparkles } from "lucide-react";
import { Button } from "@/components/ui/button";
import { ErrorBanner } from "@/components/error-banner";
import { api } from "@/lib/api";
import type { ExpansionOutcome } from "@/types";

export interface QueryChoice {
  topic: string;
  text: string;
}
export function selectedTerms(text: string): string[] {
  return [
    ...new Set(
      text
        .split("\n")
        .map((v) => v.trim())
        .filter(Boolean),
    ),
  ];
}
export function QueryExpansionPanel({
  query,
  enabled,
  disabled,
  choice,
  onChange,
}: {
  query: string;
  enabled: boolean;
  disabled: boolean;
  choice: QueryChoice | null;
  onChange: (choice: QueryChoice | null) => void;
}) {
  const [result, setResult] = useState<ExpansionOutcome | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const requestId = useRef(0);
  const topic = query.trim().replace(/\s+/g, " ");
  const current = result?.original_query === topic ? result : null;
  const text = choice?.topic === topic ? choice.text : "";
  const terms = selectedTerms(text);
  function change(text: string) {
    onChange({ topic, text });
  }
  async function preview(withAI = enabled) {
    const id = ++requestId.current;
    setBusy(true);
    setError("");
    try {
      const response = await api<ExpansionOutcome>("/search/expand", {
        method: "POST",
        body: JSON.stringify({ query: topic, use_ai: withAI }),
        signal: AbortSignal.timeout(95000),
      });
      if (id !== requestId.current) return;
      setResult(response);
      onChange({
        topic: response.original_query,
        text: response.queries.join("\n"),
      });
    } catch (e) {
      setError((e as Error).message);
    } finally {
      if (id === requestId.current) setBusy(false);
    }
  }
  const groups = current?.expansion
    ? ([
        ["Từ khóa chính", current.expansion.primary_keywords],
        ["Từ khóa liên quan", current.expansion.related_keywords],
        ["Hashtag", current.expansion.hashtags],
      ] as const)
    : [];
  return (
    <div className="ai-expansion">
      <div className="row wrap" style={{ gap: "8px", alignItems: "center" }}>
        {enabled && (
          <Button
            type="button"
            variant="outline"
            disabled={!topic || busy || disabled}
            onClick={() => preview(true)}
          >
            <Sparkles size={15} />
            {busy ? "Đang tạo từ khóa…" : "Tạo từ khóa AI"}
          </Button>
        )}
        <Button
          type="button"
          variant={enabled ? "ghost" : "outline"}
          disabled={!topic || busy || disabled}
          onClick={() => preview(false)}
          title="Xem trước 3 tầng truy vấn tiếng Trung dịch từ từ điển nội bộ (0 phí AI)"
        >
          <Languages size={15} />
          {busy ? "Đang tra cứu…" : "Xem từ điển (0 AI)"}
        </Button>
        <span className="muted">
          {enabled
            ? "Gemini hoặc Từ điển cục bộ · chuyển chủ đề sang tiếng Trung chuẩn và phân tầng truy vấn."
            : "Chế độ 0 AI · sử dụng bộ từ điển ngữ nghĩa và bảo toàn thuộc tính chủ đề."}
        </span>
      </div>
      <ErrorBanner message={error} />
      {current && (
        <div className="ai-preview" aria-live="polite">
          <p>
            <strong>
              {current.source === "gemini"
                ? "Bản dịch & Mở rộng Gemini AI"
                : current.source === "dictionary"
                  ? "Bản dịch từ điển cục bộ (3 tầng truy vấn)"
                  : "Dùng query gốc"}
            </strong>
            {current.cached ? " · đã lưu tạm, không gọi lại" : ""}
          </p>
          {current.plan?.mandatory_attributes && current.plan.mandatory_attributes.length > 0 && (
            <p className="muted" style={{ fontSize: "0.85rem" }}>
              Thuộc tính bắt buộc nhận diện:{" "}
              <strong>{current.plan.mandatory_attributes.join(", ")}</strong>
            </p>
          )}
          {current.source === "dictionary" && current.queries.length > 0 && (
            <div className="keyword-group">
              <span className="muted">3 tầng truy vấn đề xuất (có thể sửa bên dưới):</span>
              <div className="keyword-list">
                {current.queries.map((q, idx) => (
                  <span
                    key={q}
                    style={{
                      display: "inline-flex",
                      alignItems: "center",
                      gap: "4px",
                      background: "var(--surface-subtle, #f1f3f5)",
                      padding: "2px 8px",
                      borderRadius: "4px",
                      fontSize: "0.85rem",
                    }}
                  >
                    <small style={{ color: "var(--text-muted, #888)" }}>
                      {idx === 0 ? "Tầng 1 (Chính):" : idx === 1 ? "Tầng 2 (Tự nhiên):" : "Tầng 3 (Ngách):"}
                    </small>
                    <strong>{q}</strong>
                  </span>
                ))}
              </div>
            </div>
          )}
          {current.expansion && (
            <p className="ai-translation">
              {current.expansion.translated_query}
            </p>
          )}
          <ErrorBanner message={current.warning ?? ""} />
          {groups.map(([label, values]) => (
            <div className="keyword-group" key={label}>
              <span className="muted">{label}</span>
              <div className="keyword-list">
                {values.map((term) => (
                  <label key={term}>
                    <input
                      type="checkbox"
                      checked={terms.includes(term)}
                      disabled={
                        disabled ||
                        (!terms.includes(term) && terms.length >= 10)
                      }
                      onChange={(e) =>
                        change(
                          (e.target.checked
                            ? [...terms, term]
                            : terms.filter((v) => v !== term)
                          ).join("\n"),
                        )
                      }
                    />
                    {label === "Hashtag" ? "#" : ""}
                    {term}
                  </label>
                ))}
              </div>
            </div>
          ))}
          {current.expansion && (
            <p className="muted">
              Chủ đề liên quan: {current.expansion.topics.join(" · ") || "—"}
            </p>
          )}
          {!!current.expansion?.negative_keywords.length && (
            <p className="muted">
              Gợi ý loại trừ: {current.expansion.negative_keywords.join(" · ")}{" "}
              (chưa áp dụng bộ lọc loại trừ).
            </p>
          )}
        </div>
      )}
      <details open={current ? true : undefined}>
        <summary>Chỉnh từ khóa tìm kiếm · {terms.length}/10</summary>
        <label className="query-editor">
          Mỗi dòng là một truy vấn. Để trống để dùng query gốc hoặc tạo AI khi
          tìm.
          <textarea
            aria-label="Từ khóa tìm kiếm"
            value={text}
            disabled={disabled}
            maxLength={3010}
            rows={4}
            placeholder="Nhập từ khóa tiếng Trung hoặc tiếng Việt…"
            onChange={(e) => change(e.target.value)}
          />
        </label>
        {terms.length > 10 || terms.some((v) => v.length > 300) ? (
          <ErrorBanner message="Tối đa 10 truy vấn, mỗi truy vấn không quá 300 ký tự." />
        ) : null}
      </details>
    </div>
  );
}
