// 手机端「画面即触控板」的纯计算部分：不碰 DOM，也不持有状态，便于单独验证。
// 规则：单指拖动只产生**相对位移**，电脑原本的光标位置只当"从哪儿继续走"的基准。

export type Delta = { dx: number; dy: number };
export type Point = { x: number; y: number };
export type ScreenSize = { screen_width?: number; screen_height?: number } | null | undefined;
export type Rect = { width: number; height: number };

/** 后端 /desktop/mouse 单次相对位移上限是 1200 像素，这里留出余量分段下发。 */
export const REL_STEP_LIMIT = 600;

/** 画面在 object-fit: contain 之后的显示比例（屏幕像素 → 画面像素）。 */
export function frameScale(rect: Rect, screen: ScreenSize): number {
  const width = Number(screen?.screen_width) || 0;
  const height = Number(screen?.screen_height) || 0;
  if (!rect.width || !rect.height || !width || !height) return 0;
  const scale = Math.min(rect.width / width, rect.height / height);
  return Number.isFinite(scale) && scale > 0 ? scale : 0;
}

/**
 * 手指位移（CSS 像素）换算成电脑屏幕像素的相对位移。
 * 除以显示比例后，画面上的光标与手指同速；比例拿不到时返回 null（本次不动）。
 */
export function dragDelta(dxClient: number, dyClient: number, rect: Rect, screen: ScreenSize): Delta | null {
  const scale = frameScale(rect, screen);
  if (!scale) return null;
  const dx = dxClient / scale;
  const dy = dyClient / scale;
  if (!Number.isFinite(dx) || !Number.isFinite(dy)) return null;
  return { dx, dy };
}

/** 把坐标夹到当前主屏范围内（画面光标可视化用）。 */
export function clampToScreen(point: Point, screen: ScreenSize): Point {
  const width = Number(screen?.screen_width) || 1;
  const height = Number(screen?.screen_height) || 1;
  return {
    x: Math.max(0, Math.min(width - 1, point.x)),
    y: Math.max(0, Math.min(height - 1, point.y)),
  };
}

/** 从待发位移里取出一次可下发的分段，剩下的留在队列里继续发。 */
export function takeRelativeStep(pending: Delta, limit = REL_STEP_LIMIT): { step: Delta; rest: Delta } {
  const bound = Math.abs(limit);
  const step: Delta = {
    dx: Math.max(-bound, Math.min(bound, pending.dx)),
    dy: Math.max(-bound, Math.min(bound, pending.dy)),
  };
  return { step, rest: { dx: pending.dx - step.dx, dy: pending.dy - step.dy } };
}
