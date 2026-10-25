/**
 * From the team console (github.com/Omm2005/laminar, branch console, console/src/robot/Arm3D.tsx). Changes: a "wide" camera preset so the taught well poses stay in frame, and a night-colored scene (#0F1618) to match the site.
 *
 * Isaac-Sim-ish view of the real B601-RS. Scene follows public/robot/viewer.html as the baseline
 * (dark background, hemisphere + key light, shadow disc, 70%-of-view framing) and layers on:
 *   - ACES filmic tone mapping, exposure 1.1
 *   - drei Environment "city" at low intensity for reflections
 *   - GridHelper floor (two greys), scene fog so the grid fades
 *   - PBR tweaks: aluminium metal/rough, lime+black plastic matte, slight clearcoat on lime
 *   - @react-three/postprocessing SSAO + bloom (lime-only via luminance threshold), auto-off
 *     when running fps drops below 45
 *   - translucent cylinder at the vision target
 *   - thin trail of the gripper tip over the last ~2 s
 *
 * Joints: ring buffer of the last 20 telemetry samples; render 150 ms behind newest for smooth
 * 60 fps motion. The whole Canvas subtree is memoised so a new telemetry row doesn't rerender
 * the rest of the Live/ROBOT page.
 */
import { Suspense, memo, useEffect, useMemo, useRef, useState } from "react";
import { Canvas, useFrame, useThree } from "@react-three/fiber";
import { ContactShadows, Environment, OrbitControls } from "@react-three/drei";
import { Bloom, EffectComposer, SSAO } from "@react-three/postprocessing";
import * as THREE from "three";
import { GLTFLoader } from "three/examples/jsm/loaders/GLTFLoader.js";

const BASE_URL = "/robot/";
const INTERP_LAG_MS = 150;
const TRAIL_WINDOW_S = 2.0;
const TRAIL_HZ = 60;
const TRAIL_MAX = TRAIL_WINDOW_S * TRAIL_HZ;

export type Telem = { t: number; joints: number[]; commanded?: number[] | null };

type JointSpec = {
  name: string; type: "revolute" | "prismatic" | "fixed";
  parent: string; child: string;
  xyz: [number, number, number]; rpy: [number, number, number]; axis: [number, number, number];
  limit: [number, number] | null;
};
type RobotSpec = { links: Record<string, { glb: string }>; joints: JointSpec[] };

type MaterialClass = "lime" | "aluminium" | "plastic_black" | "other";

type RobotHandle = {
  root: THREE.Group;
  tipTracker: THREE.Group;
  setFromTelemetry: (deg6: number[], gripperDeg?: number) => void;
  dispose: () => void;
  limeMaterials: THREE.MeshPhysicalMaterial[];
};

/* ----------------------------------------------------------- status + colours */

const STATUS_TINT: Record<string, string> = {
  idle: "#6c7585",
  queued: "#6c7585",
  planning: "#6c7585",
  awaiting_approval: "#e8c257",
  approved: "#7dd3fc",
  running: "#7dd3fc",
  pouring: "#4a9aff",
  done: "#68d391",
  failed: "#ff5e5e",
  stalled: "#ff5e5e",
  estop: "#ff5e5e",
};
const STATUS_TRAIL: Record<string, string> = {
  idle: "#6c7585",
  running: "#7dd3fc",
  pouring: "#4a9aff",
  estop: "#ff5e5e",
};
const _hsl = { h: 0, s: 0, l: 0 };
function classify(c: THREE.Color): MaterialClass {
  c.getHSL(_hsl);
  if (_hsl.h >= 50 / 360 && _hsl.h <= 85 / 360 && _hsl.s > 0.3 && _hsl.l > 0.25 && _hsl.l < 0.7) return "lime";
  if (_hsl.l < 0.08) return "plastic_black";
  if (_hsl.s < 0.1 && _hsl.l > 0.25) return "aluminium";
  return "other";
}

/* --------------------------------------------------------------- loadRobot (ported from viewer) */

async function loadRobot(base: string): Promise<RobotHandle> {
  const spec: RobotSpec = await (await fetch(base + "robot.json")).json();
  const loader = new GLTFLoader();
  const links: Record<string, THREE.Group> = {};
  for (const name of Object.keys(spec.links)) links[name] = new THREE.Group();
  for (const j of spec.joints) {
    if (!links[j.child]) links[j.child] = new THREE.Group();
    if (!links[j.parent]) links[j.parent] = new THREE.Group();
  }
  const joints: Record<string, { group: THREE.Group; axis: THREE.Vector3 | null; type: string; limit: [number, number] | null }> = {};
  for (const j of spec.joints) {
    const jointFrame = new THREE.Group();
    jointFrame.position.set(...j.xyz);
    jointFrame.rotation.set(j.rpy[0], j.rpy[1], j.rpy[2], "ZYX");
    const moving = new THREE.Group();
    jointFrame.add(moving);
    moving.add(links[j.child]);
    links[j.parent].add(jointFrame);
    joints[j.name] = { group: moving, axis: j.axis ? new THREE.Vector3(...j.axis) : null, type: j.type, limit: j.limit };
  }
  const root = new THREE.Group();
  root.add(links["base_link"]);
  root.rotation.x = -Math.PI / 2;

  const limeMaterials: THREE.MeshPhysicalMaterial[] = [];
  await Promise.all(
    Object.entries(spec.links).map(([name, info]) =>
      new Promise<void>((resolve) => {
        loader.load(
          base + info.glb,
          (g) => {
            g.scene.traverse((o) => {
              const mesh = o as THREE.Mesh;
              if (!mesh.isMesh) return;
              mesh.castShadow = true;
              mesh.receiveShadow = true;
              const mats = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
              const upgraded: THREE.MeshPhysicalMaterial[] = [];
              for (const m of mats) {
                const sm = m as THREE.MeshStandardMaterial;
                const upgrade = new THREE.MeshPhysicalMaterial();
                // copy what we care about (keep vertex colours, basic maps)
                upgrade.vertexColors = !!sm.vertexColors;
                upgrade.color = sm.color ? sm.color.clone() : new THREE.Color(0xffffff);
                upgrade.map = sm.map ?? null;
                upgrade.normalMap = sm.normalMap ?? null;
                upgrade.name = sm.name;

                const cls = sm.color ? classify(sm.color) : "other";
                if (cls === "lime") {
                  upgrade.metalness = 0.0;
                  upgrade.roughness = 0.55;
                  upgrade.clearcoat = 0.6;
                  upgrade.clearcoatRoughness = 0.25;
                  (upgrade as unknown as { _origColor: THREE.Color })._origColor = upgrade.color.clone();
                  limeMaterials.push(upgrade);
                } else if (cls === "aluminium") {
                  upgrade.metalness = 0.6;
                  upgrade.roughness = 0.35;
                } else if (cls === "plastic_black") {
                  upgrade.metalness = 0.0;
                  upgrade.roughness = 0.55;
                } else {
                  upgrade.metalness = 0.15;
                  upgrade.roughness = 0.6;
                }
                upgraded.push(upgrade);
                sm.dispose?.();
              }
              mesh.material = upgraded.length === 1 ? upgraded[0] : upgraded;
            });
            links[name].add(g.scene);
            resolve();
          },
          undefined,
          (e) => { console.error("mesh", name, e); resolve(); },
        );
      }),
    ),
  );

  const tipTracker = links["gripper_end"] ?? new THREE.Group();

  function setJoint(name: string, value: number) {
    const j = joints[name];
    if (!j || !j.axis) return;
    const v = j.limit ? Math.min(j.limit[1], Math.max(j.limit[0], value)) : value;
    if (j.type === "prismatic") j.group.position.copy(j.axis).multiplyScalar(v);
    else j.group.quaternion.setFromAxisAngle(j.axis, v);
  }
  function setFromTelemetry(deg6: number[], gripperDeg?: number) {
    for (let i = 0; i < 6; i++) setJoint("joint" + (i + 1), THREE.MathUtils.degToRad(deg6[i] ?? 0));
    if (gripperDeg != null) {
      const m = Math.max(0, Math.min(0.05, (gripperDeg / 100) * 0.05));
      setJoint("gripper_joint1", m);
      setJoint("gripper_joint2", m);
    }
  }
  function dispose() {
    root.traverse((o) => {
      const mesh = o as THREE.Mesh;
      if (mesh.isMesh) {
        mesh.geometry?.dispose();
        const mats = Array.isArray(mesh.material) ? mesh.material : [mesh.material];
        for (const m of mats) m?.dispose?.();
      }
    });
  }
  return { root, tipTracker, setFromTelemetry, dispose, limeMaterials };
}

/* ------------------------------------------------------------- interpolation */

function interpAt(samples: Telem[], t: number): number[] {
  if (samples.length === 0) return [0, 0, 0, 0, 0, 0];
  if (samples.length === 1) return samples[0].joints.slice(0, 6);
  const last = samples[samples.length - 1];
  if (t >= last.t) return last.joints.slice(0, 6);
  for (let i = samples.length - 1; i > 0; i--) {
    const b = samples[i], a = samples[i - 1];
    if (a.t <= t && t <= b.t) {
      const f = b.t === a.t ? 1 : (t - a.t) / (b.t - a.t);
      return Array.from({ length: 6 }, (_, k) =>
        (a.joints[k] ?? 0) + ((b.joints[k] ?? 0) - (a.joints[k] ?? 0)) * f,
      );
    }
  }
  return samples[0].joints.slice(0, 6);
}

/* -------------------------------------------------------------- scene parts */

export type ChannelFill = {
  number: number;
  currentMl: number;
  capacityMl: number;
  contents?: string;
};

type ArmProps = {
  samples: Telem[];
  status: string;
  gripperDeg?: number;
  pourChannel?: number | null;
  cameraPreset?: "front" | "top" | "shoulder" | "wide";
  /** When the external vision model is pushing fresh samples, draw the reservoir + vial. */
  vision?: {
    live: boolean;
    channels?: ChannelFill[];
  };
  /** True while the pour pipeline has the vial in the jaws (grasp..return_vial). */
  vialInGripper?: boolean;
  /** Vial contents label; used only to pick a liquid colour. */
  vialColor?: string;
};

const PRESETS: Record<string, { pos: [number, number, number]; target: [number, number, number] }> = {
  front:    { pos: [0.65, 0.42, 0.65],  target: [0, 0.25, 0] }, // 3/4 front, slightly above base
  top:      { pos: [0.001, 1.1, 0.001], target: [0, 0.25, 0] },
  shoulder: { pos: [-0.6, 0.6, -0.4],   target: [0, 0.25, 0] },
  wide:     { pos: [0.55, 0.7, 1.15],   target: [0.05, 0.18, 0.05] },
};

function ToneMappingSetup() {
  const { gl } = useThree();
  useEffect(() => {
    gl.toneMapping = THREE.ACESFilmicToneMapping;
    gl.toneMappingExposure = 1.1;
    gl.shadowMap.enabled = true;
    gl.shadowMap.type = THREE.PCFSoftShadowMap;
  }, [gl]);
  return null;
}

function SyncCamera({ preset }: { preset: "front" | "top" | "shoulder" | "wide" }) {
  const applied = useRef("");
  useFrame(({ camera, controls }) => {
    if (applied.current === preset) return;
    const { pos, target } = PRESETS[preset];
    camera.position.set(...pos);
    camera.lookAt(...target);
    // @ts-expect-error drei OrbitControls expose `target` / `update`
    controls?.target?.set?.(...target);
    // @ts-expect-error drei OrbitControls expose `update`
    controls?.update?.();
    applied.current = preset;
  });
  return null;
}

function GripperTrail({ tipRef, status }: { tipRef: React.MutableRefObject<THREE.Object3D | null>; status: string }) {
  const geomRef = useRef<THREE.BufferGeometry>(null!);
  const posAttr = useMemo(() => {
    const arr = new Float32Array(TRAIL_MAX * 3);
    const attr = new THREE.BufferAttribute(arr, 3);
    attr.setUsage(THREE.DynamicDrawUsage);
    return attr;
  }, []);
  const bufIdx = useRef(0);
  const bufLen = useRef(0);
  useFrame(() => {
    if (!tipRef.current) return;
    const p = new THREE.Vector3();
    tipRef.current.getWorldPosition(p);
    const arr = posAttr.array as Float32Array;
    arr[bufIdx.current * 3 + 0] = p.x;
    arr[bufIdx.current * 3 + 1] = p.y;
    arr[bufIdx.current * 3 + 2] = p.z;
    bufIdx.current = (bufIdx.current + 1) % TRAIL_MAX;
    bufLen.current = Math.min(bufLen.current + 1, TRAIL_MAX);
    posAttr.needsUpdate = true;
    if (geomRef.current) {
      geomRef.current.setDrawRange(0, bufLen.current);
    }
  });
  const trailColor = STATUS_TRAIL[status] ?? STATUS_TRAIL.idle;
  return (
    <line>
      <bufferGeometry ref={geomRef}>
        <primitive object={posAttr} attach="attributes-position" />
      </bufferGeometry>
      <lineBasicMaterial color={trailColor} transparent opacity={0.65} />
    </line>
  );
}

function VisionTarget({ pourChannel }: { pourChannel: number | null | undefined }) {
  if (pourChannel == null) return null;
  // Place the reservoir ~250 mm in front of the base; 12 channels along +y at 15 mm pitch.
  const y = (pourChannel - 6.5) * 0.015;
  return (
    <mesh position={[0.25, 0.015, y]}>
      <cylinderGeometry args={[0.012, 0.012, 0.04, 24]} />
      <meshStandardMaterial color="#4a9aff" transparent opacity={0.45} emissive="#4a9aff" emissiveIntensity={0.4} />
    </mesh>
  );
}

/* ----- reservoir + vial, shown only when the external camera is streaming levels ----- */

const CHANNEL_PITCH = 0.015;   // 15 mm trough-to-trough, matches our bench spec
const TROUGH_W = 0.012;        // 12 mm along the pitch axis
const TROUGH_L = 0.030;        // 30 mm along the arm's approach direction
const TROUGH_H = 0.070;        // 70 mm deep
const RESERVOIR_X = 0.25;      // 250 mm in front of the base
const RESERVOIR_FRAME_PAD = 0.004;

function Reservoir({ channels, highlight }: { channels: ChannelFill[]; highlight?: number | null }) {
  // 12 troughs by default; use whatever the DB holds up to 12
  const slots: (ChannelFill | null)[] = Array.from({ length: 12 }, (_, i) =>
    channels.find((c) => c.number === i + 1) ?? null);
  const totalW = 12 * CHANNEL_PITCH;
  return (
    <group position={[RESERVOIR_X, 0, 0]}>
      {/* tray / frame */}
      <mesh position={[0, 0.003, 0]} receiveShadow>
        <boxGeometry args={[TROUGH_L + 2 * RESERVOIR_FRAME_PAD, 0.006, totalW + 2 * RESERVOIR_FRAME_PAD]} />
        <meshStandardMaterial color="#2a3242" roughness={0.6} metalness={0.2} />
      </mesh>
      {slots.map((c, i) => {
        const n = i + 1;
        const y0 = -totalW / 2 + i * CHANNEL_PITCH + CHANNEL_PITCH / 2;
        const curr = c ? c.currentMl : 0;
        const cap = c?.capacityMl ?? 10;
        const fillRatio = cap > 0 ? Math.max(0, Math.min(1, curr / cap)) : 0;
        const fillH = fillRatio * (TROUGH_H - 0.004);
        const isHi = highlight === n;
        return (
          <group key={n} position={[0, 0, y0]}>
            {/* trough well: a hollow box, open at top */}
            <mesh position={[0, TROUGH_H / 2 + 0.006, 0]} receiveShadow>
              <boxGeometry args={[TROUGH_L, TROUGH_H, TROUGH_W]} />
              <meshStandardMaterial color={isHi ? "#2a3a4e" : "#1b2130"}
                roughness={0.7}
                emissive={isHi ? "#4a9aff" : "#000000"} emissiveIntensity={isHi ? 0.08 : 0} />
            </mesh>
            {/* liquid: a shorter box inside */}
            {fillH > 0.0005 && (
              <mesh position={[0, 0.008 + fillH / 2, 0]}>
                <boxGeometry args={[TROUGH_L * 0.9, fillH, TROUGH_W * 0.9]} />
                <meshStandardMaterial
                  color={c?.contents ? liquidColorFor(c.contents) : "#4a9aff"}
                  transparent opacity={0.78} roughness={0.1} metalness={0.0} />
              </mesh>
            )}
          </group>
        );
      })}
    </group>
  );
}

function liquidColorFor(contents: string): string {
  const c = contents.toLowerCase();
  if (c.includes("red")) return "#d94a4a";
  if (c.includes("blue")) return "#4a9aff";
  if (c.includes("green")) return "#68d391";
  if (c.includes("yellow")) return "#e8c257";
  return "#7dd3fc";
}

/** 15 mL Corning-style conical centrifuge tube: 17 mm OD, ~120 mm long with a conical bottom.
 *  Rendered as a cylinder (cap+body) joined to a cone (tapered bottom). Translucent plastic. */
function CentrifugeTube({ color = "#dde6f0" }: { color?: string }) {
  const R = 0.0085;
  const BODY_L = 0.090;     // straight part
  const CONE_L = 0.030;     // tapered bottom
  const CAP_L  = 0.006;     // cap height
  // tube hangs along -y in local frame (jaws at top)
  return (
    <group>
      {/* cap */}
      <mesh position={[0, -CAP_L / 2, 0]} castShadow>
        <cylinderGeometry args={[R + 0.0005, R + 0.0005, CAP_L, 20]} />
        <meshStandardMaterial color="#dde6f0" roughness={0.4} metalness={0.1} />
      </mesh>
      {/* straight body */}
      <mesh position={[0, -CAP_L - BODY_L / 2, 0]} castShadow>
        <cylinderGeometry args={[R, R, BODY_L, 20, 1, true]} />
        <meshPhysicalMaterial color={color} transmission={0.6} transparent opacity={0.6}
          thickness={0.001} roughness={0.2} metalness={0.0} side={2} />
      </mesh>
      {/* conical tip */}
      <mesh position={[0, -CAP_L - BODY_L - CONE_L / 2, 0]} castShadow>
        <coneGeometry args={[R, CONE_L, 20, 1, true]} />
        <meshPhysicalMaterial color={color} transmission={0.6} transparent opacity={0.6}
          thickness={0.001} roughness={0.2} metalness={0.0} side={2} />
      </mesh>
    </group>
  );
}

/** The tube rides with the gripper. Positions itself under gripper_end each frame. */
function GrippedTube({ tipRef, color }: { tipRef: React.MutableRefObject<THREE.Object3D | null>; color?: string }) {
  const groupRef = useRef<THREE.Group>(null!);
  const _p = useMemo(() => new THREE.Vector3(), []);
  const _q = useMemo(() => new THREE.Quaternion(), []);
  useFrame(() => {
    const tip = tipRef.current;
    const g = groupRef.current;
    if (!tip || !g) return;
    tip.getWorldPosition(_p);
    tip.getWorldQuaternion(_q);
    g.position.copy(_p);
    g.quaternion.copy(_q);
  });
  return (
    <group ref={groupRef}>
      {/* orient so the tube's local -y is along the gripper's approach axis */}
      <group rotation={[Math.PI, 0, 0]} position={[0, 0.015, 0]}>
        <CentrifugeTube color={color} />
      </group>
    </group>
  );
}

function FpsMonitor({ onFps }: { onFps: (fps: number) => void }) {
  const frames = useRef(0);
  const t0 = useRef(0);
  useFrame((state) => {
    if (t0.current === 0) t0.current = state.clock.elapsedTime;
    frames.current++;
    const dt = state.clock.elapsedTime - t0.current;
    if (dt >= 1.0) {
      onFps(frames.current / dt);
      frames.current = 0;
      t0.current = state.clock.elapsedTime;
    }
  });
  return null;
}

function RobotScene({ samplesRef, status, gripperDeg, pourChannel, cameraPreset, onFps,
                      vision, vialInGripper, vialColor }: {
  samplesRef: React.MutableRefObject<Telem[]>;
  status: string;
  gripperDeg: number;
  pourChannel: number | null | undefined;
  cameraPreset: "front" | "top" | "shoulder" | "wide";
  onFps: (fps: number) => void;
  vision?: { live: boolean; channels?: ChannelFill[] };
  vialInGripper?: boolean;
  vialColor?: string;
}) {
  const [robot, setRobot] = useState<RobotHandle | null>(null);
  const tipRef = useRef<THREE.Object3D | null>(null);

  useEffect(() => {
    let cancelled = false;
    loadRobot(BASE_URL)
      .then((h) => { if (cancelled) h.dispose(); else { tipRef.current = h.tipTracker; setRobot(h); } })
      .catch((e) => console.error("loadRobot", e));
    return () => { cancelled = true; };
  }, []);
  useEffect(() => () => { robot?.dispose(); }, [robot]);

  useEffect(() => {
    if (!robot) return;
    const tint = new THREE.Color(STATUS_TINT[status] ?? STATUS_TINT.idle);
    const strength = status === "idle" || status === "queued" || status === "planning" ? 0.3 : 0.65;
    for (const m of robot.limeMaterials) {
      const orig = (m as unknown as { _origColor?: THREE.Color })._origColor ?? m.color;
      m.color.copy(orig).lerp(tint, strength);
      m.emissive = new THREE.Color(tint).multiplyScalar(status === "pouring" || status === "running" ? 0.15 : 0.0);
    }
  }, [status, robot]);

  useFrame(() => {
    if (!robot) return;
    const deg = interpAt(samplesRef.current, Date.now() - INTERP_LAG_MS);
    robot.setFromTelemetry(deg, gripperDeg);
  });

  return (
    <>
      <color attach="background" args={["#0f1618"]} />
      <fog attach="fog" args={["#0f1618", 1.4, 3.0]} />
      <hemisphereLight args={[0xdfe8ff, 0x202834, 0.55]} />
      <directionalLight
        castShadow
        position={[1.2, 2.0, 0.9]}
        intensity={1.3}
        shadow-mapSize-width={2048}
        shadow-mapSize-height={2048}
        shadow-camera-left={-1}
        shadow-camera-right={1}
        shadow-camera-top={1}
        shadow-camera-bottom={-1}
        shadow-bias={-0.0002}
      />
      <Environment preset="city" background={false} environmentIntensity={0.35} />
      <gridHelper args={[2.4, 24, "#26363a", "#162124"]} position={[0, 0.001, 0]} />
      <ContactShadows position={[0, 0, 0]} opacity={0.5} scale={1.8} blur={2.8} far={0.9} />
      <VisionTarget pourChannel={pourChannel} />
      {vision?.live && (
        <Reservoir channels={vision.channels ?? []} highlight={pourChannel ?? null} />
      )}
      {robot && <primitive object={robot.root} />}
      {robot && vision?.live && vialInGripper && (
        <GrippedTube tipRef={tipRef} color={vialColor} />
      )}
      {robot && <GripperTrail tipRef={tipRef} status={status} />}
      <OrbitControls makeDefault target={PRESETS[cameraPreset].target} enablePan={false} />
      <SyncCamera preset={cameraPreset} />
      <FpsMonitor onFps={onFps} />
    </>
  );
}

/* ----------------------------------------------------------- top-level Canvas */

const Arm3D = memo(function Arm3D(props: ArmProps) {
  const samplesRef = useRef<Telem[]>([]);
  useEffect(() => { samplesRef.current = props.samples; }, [props.samples]);
  const [fps, setFps] = useState(60);
  const highQuality = fps >= 45;
  return (
    <Canvas
      shadows="soft"
      dpr={[1, 2]}
      camera={{ position: PRESETS[props.cameraPreset ?? "front"].pos, fov: 38, near: 0.01, far: 10 }}
      gl={{ antialias: true, powerPreference: "high-performance" }}
    >
      <ToneMappingSetup />
      <Suspense fallback={null}>
        <RobotScene
          samplesRef={samplesRef}
          status={props.status}
          gripperDeg={props.gripperDeg ?? 0}
          pourChannel={props.pourChannel ?? null}
          cameraPreset={props.cameraPreset ?? "front"}
          onFps={setFps}
          vision={props.vision}
          vialInGripper={props.vialInGripper}
          vialColor={props.vialColor}
        />
        {highQuality && (
          <EffectComposer multisampling={0} enableNormalPass>
            <SSAO
              samples={16}
              radius={0.08}
              intensity={14}
              luminanceInfluence={0.5}
              worldDistanceThreshold={1}
              worldDistanceFalloff={0.2}
              worldProximityThreshold={0.5}
              worldProximityFalloff={0.2}
            />
            <Bloom
              intensity={0.5}
              luminanceThreshold={0.78}
              luminanceSmoothing={0.3}
              mipmapBlur
            />
          </EffectComposer>
        )}
      </Suspense>
    </Canvas>
  );
});

export default Arm3D;
