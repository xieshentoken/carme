import { useEffect, useRef } from "react";

type V3 = [number, number, number];
type Outline = [number, number][];
type Triangle = { indices: [number, number, number]; eye?: boolean };

function outlineFor(shape: string): Outline {
  const count = 32;
  if (shape === "triangle" || shape === "hexagon") {
    const sides = shape === "triangle" ? 3 : 6;
    const corners: Outline = sides === 3 ? [[0, 1], [-1, -.9], [1, -.9]] : Array.from({ length: sides }, (_, i) => {
      const a = Math.PI / 2 + i * Math.PI * 2 / sides;
      return [Math.cos(a), Math.sin(a)];
    });
    // Short quadratic corner arcs retain the chosen polygon's broad flat edges.
    return corners.flatMap((p, i) => {
      const previous = corners[(i + sides - 1) % sides], next = corners[(i + 1) % sides];
      const a = p.map((v, k) => v * .84 + previous[k] * .16);
      const b = p.map((v, k) => v * .84 + next[k] * .16);
      return Array.from({ length: 5 }, (_, j): [number, number] => {
        const t = j / 4;
        return [0, 1].map(k => (1 - t) ** 2 * a[k] + 2 * (1 - t) * t * p[k] + t * t * b[k]) as [number, number];
      });
    });
  }
  if (shape === "cloud" || shape === "drop") {
    // Follow the same lobes/tip as the static glyph, then give the outline depth.
    const curves = shape === "cloud" ? [
      [21, 40, 15, 11, 50, 7, 59, 23], [59, 23, 77, 11, 93, 36, 87, 48],
      [87, 48, 113, 74, 81, 94, 64, 81], [64, 81, 47, 101, 30, 84, 25, 83],
      [25, 83, 0, 88, -4, 53, 21, 40],
    ] : [
      [50, 3, 44, 18, 12, 44, 12, 62], [12, 62, 12, 113, 88, 113, 88, 62],
      [88, 62, 88, 43, 61, 17, 50, 3],
    ];
    const outline = curves.flatMap(c => Array.from({ length: 9 }, (_, j): [number, number] => {
      const t = j / 9, u = 1 - t;
      const x = u ** 3 * c[0] + 3 * u * u * t * c[2] + 3 * u * t * t * c[4] + t ** 3 * c[6];
      const y = u ** 3 * c[1] + 3 * u * u * t * c[3] + 3 * u * t * t * c[5] + t ** 3 * c[7];
      return [(x - 50) / 46, (50 - y) / 46];
    }));
    // The cloud path is clockwise after changing SVG-down to model-Z-up.
    const area = outline.reduce((sum, p, i) => {
      const q = outline[(i + 1) % outline.length];
      return sum + p[0] * q[1] - q[0] * p[1];
    }, 0);
    return area < 0 ? outline.reverse() : outline;
  }
  return Array.from({ length: count }, (_, i): [number, number] => {
    const a = i * Math.PI * 2 / count, x = Math.cos(a), z = Math.sin(a);
    if (shape === "square") return [Math.sign(x) * Math.abs(x) ** .35 * .87, Math.sign(z) * Math.abs(z) ** .35 * .87];
    if (shape === "pill") return [x * .64 + Math.sign(x) * .34, z * .64];
    if (shape === "oval") return [x, z * .9];
    return [x * .94, z * .94];
  });
}

function makeSolid(shape: string) {
  const outline = outlineFor(shape), count = outline.length;
  const vertices: V3[] = [], triangles: Triangle[] = [];
  const curved = ["circle", "oval", "pill", "drop", "cloud"].includes(shape);
  const depth = shape === "pill" ? .64 : shape === "circle" ? .94 : shape === "oval" ? .75 : shape === "square" ? .72 : .57;
  // X is horizontal, Y is depth, Z is up. Rings close the front and back,
  // producing a sphere/ellipsoid/capsule or a bevelled polygonal prism.
  const rings = curved ? Array.from({ length: 15 }, (_, i) => {
    const a = i * Math.PI / 14;
    return { y: -depth * Math.cos(a), scale: Math.sin(a) };
  }) : [
    { y: -depth, scale: .80 }, { y: -depth + .035, scale: .9 },
    { y: -depth + .11, scale: .98 }, { y: -depth + .2, scale: 1 },
    { y: depth - .2, scale: 1 }, { y: depth - .11, scale: .98 },
    { y: depth - .035, scale: .9 }, { y: depth, scale: .80 },
  ];
  for (const ring of rings) {
    for (const [x, z] of outline) {
      // A capsule keeps its straight central segment at every depth slice.
      const px = shape === "pill" ? (x - Math.sign(x) * .34) * ring.scale + Math.sign(x) * .34 : x * ring.scale;
      vertices.push([px, ring.y, z * ring.scale]);
    }
  }
  const add = (a: number, b: number, c: number, eye = false) => triangles.push({ indices: [a, b, c], eye });
  for (let j = 0; j < rings.length - 1; j++) {
    for (let i = 0; i < count; i++) {
      const a = j * count + i, b = (j + 1) * count + i;
      const c = (j + 1) * count + (i + 1) % count, d = j * count + (i + 1) % count;
      add(a, b, c); add(a, c, d);
    }
  }
  const front = vertices.push([0, -depth, 0]) - 1, back = vertices.push([0, depth, 0]) - 1;
  for (let i = 0; i < count; i++) {
    add(front, i, (i + 1) % count);
    add(back, (rings.length - 1) * count + (i + 1) % count, (rings.length - 1) * count + i);
  }

  // Eye vertices hug the front surface; they participate in the same depth
  // ordering and backface culling as the body instead of overlaying its back.
  const frontY = (x: number, z: number) => {
    if (!curved) return -depth - .014;
    if (shape === "pill") return -Math.sqrt(Math.max(.015, depth * depth - z * z - Math.max(0, Math.abs(x) - .34) ** 2)) - .014;
    // Find the radial extent of the sampled front outline at this angle.
    const r = Math.hypot(x, z), dx = x / r, dz = z / r;
    let boundary = 1;
    for (let i = 0; i < count; i++) {
      const a = outline[i], b = outline[(i + 1) % count];
      const ex = b[0] - a[0], ez = b[1] - a[1], determinant = dx * ez - dz * ex;
      if (Math.abs(determinant) < 1e-8) continue;
      const distance = (a[0] * ez - a[1] * ex) / determinant;
      const along = (a[0] * dz - a[1] * dx) / determinant;
      if (distance > 0 && along >= 0 && along <= 1) boundary = distance;
    }
    const radius = Math.min(.97, r / boundary);
    // Interpolate the actual mesh rings so eyes never sink under a facet.
    for (let j = 0; j < 7; j++) {
      const a = rings[j], b = rings[j + 1];
      if (radius <= b.scale) return a.y + (b.y - a.y) * (radius - a.scale) / (b.scale - a.scale) - .018;
    }
    return -.018;
  };
  for (const centerX of [-.20, .25]) {
    const centerZ = shape === "triangle" ? -.08 : .14;
    const eyePoints: V3[] = Array.from({ length: 16 }, (_, i) => {
      const a = i * Math.PI * 2 / 16;
      const ex = Math.cos(a) * .068, ez = Math.sin(a) * .145;
      const x = centerX + ex * .97 - ez * .24, z = centerZ + ex * .24 + ez * .97;
      return [x, frontY(x, z), z];
    });
    const center = vertices.push([centerX, frontY(centerX, centerZ), centerZ]) - 1;
    const start = vertices.length;
    vertices.push(...eyePoints);
    for (let i = 0; i < eyePoints.length; i++) add(center, start + i, start + (i + 1) % eyePoints.length, true);
  }
  return { vertices, triangles };
}

export default function BotSolid({ shape = "circle", color = "#18bfae", paused = false }: { shape?: string; color?: string; paused?: boolean }) {
  const canvasRef = useRef<HTMLCanvasElement>(null);
  useEffect(() => {
    const canvas = canvasRef.current, context = canvas?.getContext("2d");
    if (!canvas || !context) return;
    const mesh = makeSolid(shape);
    const hex = /^#[0-9a-f]{6}$/i.test(color) ? color.slice(1) : "18bfae";
    const rgb = [0, 2, 4].map(offset => parseInt(hex.slice(offset, offset + 2), 16));
    const motion = window.matchMedia("(prefers-reduced-motion: reduce)");
    let width = 0, height = 0, frame = 0, visible = true, previous = 0, elapsed = 0;
    const elevation = .24, ce = Math.cos(elevation), se = Math.sin(elevation), camera = 6;
    const draw = () => {
      if (!width || !height) return;
      const angle = .13 + elapsed * Math.PI * 2 / 4800, c = Math.cos(angle), s = Math.sin(angle);
      const scale = Math.min(width, height) * .39;
      const points = mesh.vertices.map(([x, y, z]) => {
        const rx = x * c - y * s, ry = x * s + y * c;
        const depth = -ry * ce + z * se, perspective = camera / (camera - depth);
        return { x: rx, y: ry, z, depth, sx: width / 2 + rx * scale * perspective, sy: height * .51 - (z * ce + ry * se) * scale * perspective };
      });
      const faces = mesh.triangles.flatMap(triangle => {
        const [a, b, d] = triangle.indices.map(i => points[i]);
        const u = [b.x - a.x, b.y - a.y, b.z - a.z], v = [d.x - a.x, d.y - a.y, d.z - a.z];
        const n = [u[1] * v[2] - u[2] * v[1], u[2] * v[0] - u[0] * v[2], u[0] * v[1] - u[1] * v[0]];
        const length = Math.hypot(...n);
        if (length < 1e-8 || n[0] * -a.x + n[1] * (-camera * ce - a.y) + n[2] * (camera * se - a.z) <= 0) return [];
        const light = Math.max(0, (n[0] * -.42 + n[1] * -.57 + n[2] * .70) / length);
        const brightness = .55 + light * .45;
        const highlight = light * .045 + light ** 9 * .115;
        const fill = triangle.eye ? "#ffffff" : `rgb(${rgb.map(channel => Math.round(channel * brightness + (255 - channel) * highlight)).join(",")})`;
        return [{ a, b, d, fill, eye: triangle.eye, depth: (a.depth + b.depth + d.depth) / 3 }];
      }).sort((a, b) => a.depth - b.depth);
      context.clearRect(0, 0, width, height);
      for (const face of faces) {
        context.beginPath(); context.moveTo(face.a.sx, face.a.sy); context.lineTo(face.b.sx, face.b.sy); context.lineTo(face.d.sx, face.d.sy); context.closePath();
        context.fillStyle = face.fill; context.fill();
        // A subpixel same-color seam hides antialiasing cracks between facets.
        context.strokeStyle = face.fill; context.lineWidth = face.eye ? .12 : .35; context.stroke();
      }
    };
    const canAnimate = () => !paused && !motion.matches && !document.hidden && visible && width > 0 && height > 0;
    const tick = (time: number) => {
      frame = 0;
      if (!canAnimate()) { previous = 0; return; }
      if (!previous) previous = time;
      const interval = 1000 / 30, delta = time - previous;
      if (delta >= interval) {
        elapsed += Math.min(Math.floor(delta / interval) * interval, 100);
        previous = time - delta % interval; draw();
      }
      frame = requestAnimationFrame(tick);
    };
    const update = () => {
      if (frame) cancelAnimationFrame(frame);
      frame = 0; previous = 0;
      draw();
      if (canAnimate()) frame = requestAnimationFrame(tick);
    };
    const resize = new ResizeObserver(() => {
      const rect = canvas.getBoundingClientRect(), dpr = Math.min(window.devicePixelRatio || 1, 2);
      width = rect.width; height = rect.height;
      canvas.width = Math.round(width * dpr); canvas.height = Math.round(height * dpr);
      context.setTransform(dpr, 0, 0, dpr, 0, 0); update();
    });
    const intersection = new IntersectionObserver(entries => { visible = entries[0]?.isIntersecting ?? false; update(); });
    resize.observe(canvas); intersection.observe(canvas);
    motion.addEventListener("change", update); document.addEventListener("visibilitychange", update);
    return () => {
      cancelAnimationFrame(frame); resize.disconnect(); intersection.disconnect();
      motion.removeEventListener("change", update); document.removeEventListener("visibilitychange", update);
    };
  }, [shape, color, paused]);
  return <canvas ref={canvasRef} aria-hidden="true" className="avatar-solid" />;
}
