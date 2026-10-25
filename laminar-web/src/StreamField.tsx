/**
 * The landing page's streamline field, running quietly behind the console.
 * One sine along x in a <defs>, ~22 near-parallel copies translated and scaled;
 * stroke-dasharray drifts so the field reads as moving laminar flow, not a graphic.
 */

const VB_W = 1200, VB_H = 800;
const PERIOD = 420, HALF = PERIOD / 2;
// Cubic approximation of sine, amplitude 1. One cubic per half-period; the
// control-y offset 1.3714 pushes the midpoint close to ±1 (the standard sine
// Bezier tangent at the zero crossings).
const CTRL = 1.3714;
const X_MIN = -600, X_MAX = 1800;

function buildPath() {
  const parts: string[] = [`M ${X_MIN} 0`];
  let sign = 1;
  for (let x = X_MIN; x < X_MAX; x += HALF) {
    const x3 = x + HALF;
    const cy = sign * CTRL;
    parts.push(`C ${x + HALF / 3} ${cy}, ${x + (2 * HALF) / 3} ${cy}, ${x3} 0`);
    sign *= -1;
  }
  return parts.join(" ");
}

const PATH = buildPath();

const N = 22;
const LINES = Array.from({ length: N }, (_, i) => {
  const f = N <= 1 ? 0 : i / (N - 1);
  const y = 20 + f * (VB_H - 40);                        // spread across the band
  const amp = 4 + 7 * Math.sin(Math.PI * f);              // 4..11, max mid-height
  const phase = (i * 11) % PERIOD;
  const opacity = 0.05 + 0.04 * Math.sin(Math.PI * f);    // 0.05..0.09
  const duration = 3.0 + ((i * 7) % 13) * 0.1;            // 3.0..4.2 ish
  const delay = -((i * 17) % 100) / 100 * duration;       // phase the drift
  return { y, amp, phase, opacity, duration, delay };
});

export default function StreamField() {
  return (
    <svg
      className="streamfield"
      viewBox={`0 0 ${VB_W} ${VB_H}`}
      preserveAspectRatio="none"
      aria-hidden="true"
    >
      <defs>
        <path id="sl" d={PATH} />
      </defs>
      <g>
        {LINES.map(({ y, amp, phase, opacity, duration, delay }, i) => (
          <use
            key={i}
            href="#sl"
            transform={`translate(${-phase} ${y}) scale(1 ${amp})`}
            stroke="var(--reagent)"
            strokeWidth={1 / amp}
            fill="none"
            opacity={opacity}
            style={{
              strokeDasharray: "150 30",
              animation: `sl-drift ${duration.toFixed(2)}s linear infinite`,
              animationDelay: `${delay.toFixed(2)}s`,
            }}
          />
        ))}
      </g>
    </svg>
  );
}
