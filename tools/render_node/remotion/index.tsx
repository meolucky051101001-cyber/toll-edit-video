/**
 * Remotion entry cho Tool V1 — Studio Kịch Bản.
 * Chạy render:  npx remotion render remotion/index.tsx DoodleScenePoC out.mp4 --props=remotion/props.json
 * Xem Studio:   npx remotion studio remotion/index.tsx
 */
import { registerRoot } from "remotion";
import { RemotionRoot } from "./Root";

registerRoot(RemotionRoot);
