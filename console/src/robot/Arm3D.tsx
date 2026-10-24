/**
 * Live 3D view of the B601-RS. Primitives-only (boxes + cylinders) rendered off the URDF
 * joint origins we exported in chain.json, driven by robot.latestTelemetry.
 *
 * To keep motion smooth at 60 fps while telemetry arrives at 10 Hz we hold a ring buffer of
 * the last ~20 samples and read the joint vector 150 ms behind the newest sample time, linearly
 * interpolating between the two straddling samples. The whole r3f subtree is memoised so a new
 * telemetry row doesn't rerender the rest of the Live page.
 */
import { Suspense, memo, useEffect, useMemo, useRef } from "react";
import { Canvas, useFrame } from "@react-three/fiber";
import { OrbitControls, ContactShadows } from "@react-three/drei";
import * as THREE from "three";
import chainData from "./chain.json";

type Segment = {
  name: string;
  type: "revolute" | "fixed";
  xyz: [number, number, number];
  R: number[]; // row-major 3x3
  axis: [number, number, number] | null;
};
const CHAIN = chainData as Segment[];

const STATUS_COLOR: Record<string, string> = {
  idle:              "#8792a3",
  queued:            "#8792a3",
  planning:          "#8792a3",
  awaiting_approval: "#e8c257",
  approved:          "#7dd3fc",
  running:           "#7dd3fc",
  pouring:           "#4a9aff",
  done:              "#68d391",
  failed:            "#ff5e5e",
  stalled:           "#ff5e5e",
  estop:             "#ff5e5e",
};

export type Telem = { t: number; joints: number[]; commanded?: number[] | null };

type ArmProps = {
  samples: Telem[];       // ring buffer, oldest -> newest
  status: string;
  gripperDeg?: number;
  pourChannel?: number | null;
  cameraPreset?: "front" | "top" | "shoulder";
};

const PRESETS: Record<string, [number, number, number]> = {
  front:    [-0.55, 0.42, 0.55],
  top:      [ 0.00, 0.80, 0.05],
  shoulder: [-0.35, 0.55, -0.35],
};

/** Interpolate joints 150 ms behind the newest sample. Returns [6]. */
function interpolate(samples: Telem[], latencyMs = 150): number[] {
  if (samples.length === 0) return [0, 0, 0, 0, 0, 0];
  if (samples.length === 1) return samples[0].joints.slice(0, 6);
  const now = Date.now();
  const t = now - latencyMs;
  const last = samples[samples.length - 1];
  // if we're ahead of the newest sample, clamp to newest
  if (t >= last.t) return last.joints.slice(0, 6);
  // find the pair that straddles t
  for (let i = samples.length - 1; i > 0; i--) {
    const b = samples[i];
    const a = samples[i - 1];
    if (a.t <= t && t <= b.t) {
      const f = b.t === a.t ? 1 : (t - a.t) / (b.t - a.t);
      return Array.from({ length: 6 }, (_, k) =>
        (a.joints[k] ?? 0) + ((b.joints[k] ?? 0) - (a.joints[k] ?? 0)) * f,
      );
    }
  }
  return samples[0].joints.slice(0, 6);
}

function Joint({
  seg, children, jointAngleRad,
}: { seg: Segment; children: React.ReactNode; jointAngleRad: number }) {
  // Compose origin: translate then rotate by URDF R, then (if revolute) by the joint angle on its axis.
  const originMatrix = useMemo(() => {
    const m = new THREE.Matrix4();
    m.makeTranslation(seg.xyz[0], seg.xyz[1], seg.xyz[2]);
    const r = new THREE.Matrix4();
    const R = seg.R;
    r.set(
      R[0], R[1], R[2], 0,
      R[3], R[4], R[5], 0,
      R[6], R[7], R[8], 0,
      0, 0, 0, 1,
    );
    m.multiply(r);
    return m;
  }, [seg]);

  const axisRef = useRef<THREE.Group>(null!);
  useFrame(() => {
    if (!axisRef.current || seg.type !== "revolute" || !seg.axis) return;
    const q = new THREE.Quaternion().setFromAxisAngle(
      new THREE.Vector3(...seg.axis).normalize(),
      jointAngleRad,
    );
    axisRef.current.quaternion.copy(q);
  });

  return (
    <group matrix={originMatrix} matrixAutoUpdate={false}>
      <group ref={axisRef}>
        {children}
      </group>
    </group>
  );
}

function Link({ color, length = 0.08, thickness = 0.03 }: { color: string; length?: number; thickness?: number }) {
  return (
    <mesh castShadow receiveShadow position={[0, 0, length / 2]}>
      <boxGeometry args={[thickness, thickness, length]} />
      <meshStandardMaterial color={color} roughness={0.4} metalness={0.3} />
    </mesh>
  );
}

function Gripper({ openDeg, color }: { openDeg: number; color: string }) {
  // 0 = closed, 270 = fully open; map to jaw spread 0..0.04 m
  const spread = Math.min(1, Math.max(0, openDeg / 270)) * 0.04;
  return (
    <group>
      <mesh castShadow position={[0, spread, 0.015]}>
        <boxGeometry args={[0.02, 0.006, 0.03]} />
        <meshStandardMaterial color={color} roughness={0.3} metalness={0.5} />
      </mesh>
      <mesh castShadow position={[0, -spread, 0.015]}>
        <boxGeometry args={[0.02, 0.006, 0.03]} />
        <meshStandardMaterial color={color} roughness={0.3} metalness={0.5} />
      </mesh>
    </group>
  );
}

function ArmChain({ samplesRef, gripperDeg, color }: {
  samplesRef: React.MutableRefObject<Telem[]>;
  gripperDeg: number;
  color: string;
}) {
  // live joint angles, updated per-frame
  const anglesRef = useRef<number[]>([0, 0, 0, 0, 0, 0]);
  useFrame(() => {
    anglesRef.current = interpolate(samplesRef.current).map((d) => (d * Math.PI) / 180);
  });

  // Build the nested structure recursively from the chain. Each revolute joint reads from anglesRef[i].
  // Fixed segments are just a transform.
  let revoluteIdx = 0;
  type Node = (children: React.ReactNode) => React.ReactNode;
  const nodes: Node[] = CHAIN.map((seg) => {
    if (seg.type === "revolute") {
      const idx = revoluteIdx++;
      return (children) => (
        <JointLive key={seg.name} seg={seg} angleIdx={idx} anglesRef={anglesRef} color={color}>
          {children}
        </JointLive>
      );
    }
    return (children) => (
      <Joint key={seg.name} seg={seg} jointAngleRad={0}>
        {/* fixed segment: a short link stub */}
        <Link color={color} length={0.04} thickness={0.025} />
        {children}
      </Joint>
    );
  });
  // Compose: nodes[0]( nodes[1]( ... <Gripper /> ... ) )
  let tree: React.ReactNode = <Gripper openDeg={gripperDeg} color={color} />;
  for (let i = nodes.length - 1; i >= 0; i--) {
    tree = nodes[i](tree);
  }
  return (
    <group position={[0, 0, 0]}>
      {/* base plinth */}
      <mesh receiveShadow position={[0, 0, 0.015]}>
        <cylinderGeometry args={[0.06, 0.07, 0.03, 24]} />
        <meshStandardMaterial color="#2b303b" roughness={0.7} />
      </mesh>
      {tree}
    </group>
  );
}

/** Revolute-joint wrapper that reads its angle from a ref so useFrame updates don't re-render React. */
function JointLive({
  seg, angleIdx, anglesRef, children, color,
}: {
  seg: Segment; angleIdx: number;
  anglesRef: React.MutableRefObject<number[]>;
  children: React.ReactNode; color: string;
}) {
  const originMatrix = useMemo(() => {
    const m = new THREE.Matrix4();
    m.makeTranslation(seg.xyz[0], seg.xyz[1], seg.xyz[2]);
    const r = new THREE.Matrix4();
    const R = seg.R;
    r.set(
      R[0], R[1], R[2], 0,
      R[3], R[4], R[5], 0,
      R[6], R[7], R[8], 0,
      0, 0, 0, 1,
    );
    m.multiply(r);
    return m;
  }, [seg]);
  const axisRef = useRef<THREE.Group>(null!);
  useFrame(() => {
    if (!axisRef.current || !seg.axis) return;
    const q = new THREE.Quaternion().setFromAxisAngle(
      new THREE.Vector3(...seg.axis).normalize(),
      anglesRef.current[angleIdx] ?? 0,
    );
    axisRef.current.quaternion.copy(q);
  });
  return (
    <group matrix={originMatrix} matrixAutoUpdate={false}>
      <group ref={axisRef}>
        <Link color={color} length={0.07} thickness={0.028} />
        {children}
      </group>
    </group>
  );
}

function PourChannelMarker({ channel }: { channel: number | null | undefined }) {
  if (channel == null) return null;
  // reservoir sits ~0.25 m in front of the base; 12 channels along +y at 15 mm pitch
  const y = (channel - 6.5) * 0.015;
  return (
    <mesh position={[0.25, y, 0.01]}>
      <boxGeometry args={[0.03, 0.013, 0.02]} />
      <meshStandardMaterial color="#4a9aff" emissive="#4a9aff" emissiveIntensity={0.6} />
    </mesh>
  );
}

const Scene = memo(function Scene({
  samplesRef, status, gripperDeg = 0, pourChannel = null, cameraPreset = "front",
}: {
  samplesRef: React.MutableRefObject<Telem[]>;
  status: string; gripperDeg?: number; pourChannel?: number | null;
  cameraPreset?: "front" | "top" | "shoulder";
}) {
  const color = STATUS_COLOR[status] ?? STATUS_COLOR.idle;
  const cam = PRESETS[cameraPreset] ?? PRESETS.front;
  return (
    <>
      <color attach="background" args={["#0b0d12"]} />
      <ambientLight intensity={0.4} />
      <directionalLight
        position={[-2, 3, 2]}
        intensity={1.1}
        castShadow
        shadow-mapSize-width={1024}
        shadow-mapSize-height={1024}
      />
      <group rotation={[-Math.PI / 2, 0, 0]} position={[0, -0.05, 0]}>
        {/* URDF has z-up; r3f has y-up — rotate the whole arm so z becomes y. */}
        <ArmChain samplesRef={samplesRef} gripperDeg={gripperDeg} color={color} />
        <PourChannelMarker channel={pourChannel} />
      </group>
      <ContactShadows position={[0, -0.051, 0]} opacity={0.35} scale={1.2} blur={2.2} far={0.5} />
      <OrbitControls makeDefault target={[0, 0.15, 0]} enablePan={false} />
      <SyncCamera position={cam} target={[0, 0.15, 0]} />
    </>
  );
});

function SyncCamera({ position, target }: { position: [number, number, number]; target: [number, number, number] }) {
  // When cameraPreset changes, move camera and OrbitControls target to match once (user can still orbit from there).
  const applied = useRef<string>("");
  const key = `${position.join(",")};${target.join(",")}`;
  useFrame(({ camera, controls }) => {
    if (applied.current === key) return;
    camera.position.set(...position);
    camera.lookAt(...target);
    // @ts-expect-error drei OrbitControls expose `target`
    if (controls?.target) controls.target.set(...target);
    // @ts-expect-error drei OrbitControls expose `update`
    controls?.update?.();
    applied.current = key;
  });
  return null;
}

const Arm3D = memo(function Arm3D(props: ArmProps) {
  const samplesRef = useRef<Telem[]>([]);
  useEffect(() => {
    samplesRef.current = props.samples;
  }, [props.samples]);
  const pixelRatio: [number, number] = [1, 2];
  return (
    <Canvas
      shadows
      dpr={pixelRatio}
      camera={{ position: PRESETS[props.cameraPreset ?? "front"], fov: 40, near: 0.01, far: 10 }}
      gl={{ antialias: true }}
    >
      <Suspense fallback={null}>
        <Scene
          samplesRef={samplesRef}
          status={props.status}
          gripperDeg={props.gripperDeg}
          pourChannel={props.pourChannel}
          cameraPreset={props.cameraPreset}
        />
      </Suspense>
    </Canvas>
  );
});

export default Arm3D;
