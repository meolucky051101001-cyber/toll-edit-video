import React from "react";
import { Composition } from "remotion";
import { DoodleScenePoC, type DoodleSceneProps } from "./DoodleScenePoC";

/**
 * Đăng ký composition cho Studio Kịch Bản.
 * durationSec trong props quyết định độ dài video (đồng bộ theo thời lượng TTS thật).
 */
const defaultProps: DoodleSceneProps = {
  title: "TẠI SAO MÈO LẠI THÍCH ĐUA XE ĐIỆN?",
  keywordAccent: "ĐUA XE",
  accentColor: "#FF1E1E",
  badgeText: "★ KHÁM PHÁ BÍ MẬT",
  narration: "Hãy tưởng tượng một chú mèo nhỏ lái chiếc xe đua điện lao vút trên đường đua.",
  doodleLabel: "ĐUA XE",
  durationSec: 5,
  fps: 24,
  actionType: "race",
  cameraMotion: "zoom_in",
  numbers: [20, 45, 70, 95],
};

export const RemotionRoot: React.FC = () => {
  return (
    <Composition
      id="DoodleScenePoC"
      component={DoodleScenePoC}
      durationInFrames={120}
      fps={24}
      width={720}
      height={1280}
      defaultProps={defaultProps}
      calculateMetadata={({ props }) => {
        const fps = props.fps ?? 24;
        const seconds = Math.max(1.5, props.durationSec ?? 5);
        return {
          fps,
          durationInFrames: Math.max(36, Math.round(seconds * fps)),
        };
      }}
    />
  );
};
