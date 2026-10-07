import { useEffect, useMemo, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import { ArrowLeft } from "lucide-react"
import * as THREE from "three"
import { getGraph3D, type Graph3DData, type GraphPoint3D } from "@/lib/api"

// 대화 클릭 시 이동할 대상(해당 탭 + 그 대화 열기).
export type OpenTurn = (kind: "sessions" | "clusters", id: string, turn: string, session: string) => void

const PALETTE = [
  "#6ea8fe", "#f4845f", "#5cc8a8", "#c78be0", "#e6b34a", "#7ed957", "#ef6f9b",
  "#5bc0de", "#b58a63", "#9aa0ff", "#4fb477", "#e05c5c", "#a3c644", "#c96bb3", "#59b0a3", "#d98a5b",
]
const colorOf = (c: number) => PALETTE[((c % PALETTE.length) + PALETTE.length) % PALETTE.length]
const EXTENT = 150

// onOpenTurn: 지도에서 대화(턴)를 클릭하면 그 탭(3분할·왼쪽 검색창)으로 이동해 대화를 연다.
export function GraphView3D({ onOpenTurn }: { onOpenTurn: OpenTurn }) {
  const { t } = useTranslation()
  const [data, setData] = useState<Graph3DData | null>(null)
  const [tip, setTip] = useState<{ sx: number; sy: number; p: GraphPoint3D } | null>(null)
  const [err, setErr] = useState<string | null>(null)   // 로드 실패를 '데이터 없음'과 구분
  const wrapRef = useRef<HTMLDivElement | null>(null)
  const labelRefs = useRef<Map<number, HTMLDivElement | null>>(new Map())
  const flyToRef = useRef<((id: number) => void) | null>(null)   // 군집 클릭 → 중앙 이동+확대
  const focusClusterRef = useRef<((id: number | null) => void) | null>(null)   // 지도에서 그 군집만 밝게
  const focusSessionRef = useRef<((sid: string | null) => void) | null>(null)  // 지도에서 그 세션만 밝게
  const clearRef = useRef<() => void>(() => {})            // 빈 공간 클릭 → 격리 해제(three.js에서 호출)
  const pointClickRef = useRef<(session: string, turn: string) => void>(() => {})   // 점 클릭 → 세션 격리+대화목록
  const [selCluster, setSelCluster] = useState<number | null>(null)            // 군집 격리+대화목록 대상
  const [selSession, setSelSession] = useState<string | null>(null)            // 점 클릭 → 세션 격리+대화목록
  const [clickedTurn, setClickedTurn] = useState<string | null>(null)          // 방금 클릭한 점의 턴(목록서 강조)

  const [refreshing, setRefreshing] = useState(false)
  useEffect(() => {
    getGraph3D().then(setData).catch((e) => {
      console.error("[graph3d] load failed", e)
      setErr(String(e)); setData({ points: [], clusters: [], method: null })
    })
  }, [])
  // 군집·라벨 강제 재계산(정제로 태그가 바뀌면 캐시된 라벨이 옛것이라 새로 계산).
  async function regenerate() {
    setRefreshing(true)
    try { setData(await getGraph3D(true)) }
    catch (e) { setErr(String(e)) }
    finally { setRefreshing(false) }
  }

  useEffect(() => {
    const wrap = wrapRef.current
    if (!wrap || !data || data.points.length === 0) return

    const pts = data.points
    // 정규화: 중심·스케일 맞춰 큐브에 담기.
    const xs = pts.map((p) => p.x), ys = pts.map((p) => p.y), zs = pts.map((p) => p.z)
    const mid = (a: number[]) => (Math.min(...a) + Math.max(...a)) / 2
    const cx = mid(xs), cy = mid(ys), cz = mid(zs)
    const span = Math.max(Math.max(...xs) - Math.min(...xs), Math.max(...ys) - Math.min(...ys), Math.max(...zs) - Math.min(...zs)) || 1
    const s = (EXTENT * 2) / span
    const at = (p: GraphPoint3D) => new THREE.Vector3((p.x - cx) * s, (p.y - cy) * s, (p.z - cz) * s)

    let w = wrap.clientWidth || 800, h = wrap.clientHeight || 600
    const dark = document.documentElement.classList.contains("dark")

    const scene = new THREE.Scene()
    const camera = new THREE.PerspectiveCamera(55, w / h, 1, 5000)
    camera.position.set(0, 0, EXTENT * 3)
    const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: true })
    renderer.setPixelRatio(window.devicePixelRatio)
    renderer.setSize(w, h)
    renderer.domElement.style.borderRadius = "12px"
    // 캔버스를 컨테이너에 CSS로 꽉 채운다(px 아님) → 창 리사이즈 중엔 브라우저가 마지막 프레임을
    // 늘려 즉시 채우고(값싼 스케일), 무거운 드로잉 버퍼 재할당은 아래 ResizeObserver 디바운스로
    // 리사이즈가 멈춘 뒤 한 번만 수행. 리사이즈 중 캔버스가 안 따라오거나 찢기는 현상을 줄인다.
    renderer.domElement.style.display = "block"
    renderer.domElement.style.width = "100%"
    renderer.domElement.style.height = "100%"
    // 접근성: 캔버스가 무엇인지 스크린리더에 알림(상세는 우측 '주제 군집' 패널이 텍스트로 제공).
    // aria-label 문구는 별도 경량 effect에서 t/data 변경 시 갱신(여기서 t를 참조하면 언어 전환마다 씬 전체 재빌드).
    renderer.domElement.setAttribute("role", "img")
    wrap.appendChild(renderer.domElement)

    // 포인트 지오메트리 + 군집색.
    const geo = new THREE.BufferGeometry()
    const pos = new Float32Array(pts.length * 3)
    const col = new Float32Array(pts.length * 3)
    const tmp = new THREE.Color()
    pts.forEach((p, i) => {
      const v = at(p)
      pos[i * 3] = v.x; pos[i * 3 + 1] = v.y; pos[i * 3 + 2] = v.z
      // 노이즈(군집 미배정, c<0)는 팔레트색 대신 중립 회색.
      tmp.set(p.c < 0 ? (dark ? "#3b3f47" : "#c9ccd2") : colorOf(p.c))
      col[i * 3] = tmp.r; col[i * 3 + 1] = tmp.g; col[i * 3 + 2] = tmp.b
    })
    geo.setAttribute("position", new THREE.BufferAttribute(pos, 3))
    geo.setAttribute("color", new THREE.BufferAttribute(col, 3))
    const colBase = col.slice()   // 원본 군집색(hover 복원용)
    // 원형 점 스프라이트(PointsMaterial 기본은 사각형) — 흰 원 텍스처 + alphaTest로 모서리 제거.
    const dotTex = (() => {
      const s = 64, cv = document.createElement("canvas"); cv.width = cv.height = s
      const g = cv.getContext("2d")!
      g.beginPath(); g.arc(s / 2, s / 2, s / 2 - 2, 0, Math.PI * 2); g.fillStyle = "#fff"; g.fill()
      return new THREE.CanvasTexture(cv)
    })()
    const material = new THREE.PointsMaterial({
      // 화면 고정 픽셀 크기(거리로 안 커지고 안 작아짐) → 확대하면 3D 간격만 벌어져 밀집부가 갈라짐.
      size: 5.5, sizeAttenuation: false, vertexColors: true, transparent: true,
      map: dotTex, alphaTest: 0.5,   // 원형
      opacity: dark ? 0.85 : 0.9, depthWrite: false,
      blending: dark ? THREE.AdditiveBlending : THREE.NormalBlending,
    })
    const points = new THREE.Points(geo, material)
    scene.add(points)

    // ── hover 광원: 커서 아래 점을 부드러운 원형 글로우로 강조(무엇에 올렸는지 한눈에) ──
    const glowTex = (() => {
      const s = 128, cv = document.createElement("canvas"); cv.width = cv.height = s
      const g = cv.getContext("2d")!
      const grd = g.createRadialGradient(s / 2, s / 2, 0, s / 2, s / 2, s / 2)
      grd.addColorStop(0, "rgba(255,255,255,1)")
      grd.addColorStop(0.28, "rgba(255,255,255,0.55)")
      grd.addColorStop(1, "rgba(255,255,255,0)")
      g.fillStyle = grd; g.fillRect(0, 0, s, s)
      return new THREE.CanvasTexture(cv)
    })()
    const glowMat = new THREE.SpriteMaterial({
      map: glowTex, transparent: true, depthWrite: false, depthTest: false,
      blending: dark ? THREE.AdditiveBlending : THREE.NormalBlending, opacity: dark ? 0.95 : 0.85,
    })
    const glow = new THREE.Sprite(glowMat); glow.visible = false; glow.renderOrder = 5
    scene.add(glow)
    let glowIdx = -1
    const _gc = new THREE.Color()
    function setGlow(i: number) {
      if (i < 0) { glow.visible = false; glowIdx = -1; return }
      glowIdx = i
      glow.position.set(pos[i * 3], pos[i * 3 + 1], pos[i * 3 + 2])
      _gc.set(pts[i].c < 0 ? (dark ? "#8a8f98" : "#9aa0a8") : colorOf(pts[i].c))
      glowMat.color.copy(_gc); glow.visible = true
    }

    // ── hover: 호버 세션만 밝게, 나머지는 렌더 루프에서 부드럽게(페이드) 흐려짐 ──
    // 비강조는 '실제 배경색'으로 수렴시켜 거의 사라지게(색만 빼면 흰 점이 남아 애매하던 문제).
    // 라이트(Normal 블렌딩)=카드 배경색과 동일 → 안 보임 / 다크(Additive)=검정 → 더해도 0이라 소멸.
    const _bg = new THREE.Color(0.97, 0.97, 0.98)
    try { _bg.setStyle(getComputedStyle(wrap).backgroundColor) } catch { /* oklch 등 파싱 불가 시 폴백 유지 */ }
    const dimPt = dark ? new THREE.Color(0, 0, 0) : _bg

    let focusSess: string | null = null   // 대상 세션(hover, 일시)
    let activeSess: string | null = null  // 페이드 동안 밝게 유지할 세션(hover)
    let clusterFocus: number | null = null   // 선택 군집(고정) — 이 군집만 밝게
    let activeCluster: number | null = null
    let sessionFocus: string | null = null   // 선택 세션(고정, 점 클릭) — 이 세션만 밝게
    let activeSession: string | null = null
    let focusT = 0                         // 0=전부 밝음 … 1=대상만 밝고 나머지 흐림
    let focusDirty = false
    const sticky = () => clusterFocus != null || sessionFocus != null   // 고정 모드면 hover 무시
    function setHover(sess: string | null) {
      if (sticky()) return
      if (sess === focusSess) return
      focusSess = sess
      if (sess) activeSess = sess
      focusDirty = true
    }
    focusClusterRef.current = (id: number | null) => {   // 군집 선택 → 지도 격리(고정)
      if (id === clusterFocus) return
      clusterFocus = id
      if (id != null) { activeCluster = id; sessionFocus = null; activeSession = null; focusSess = null; activeSess = null }
      focusDirty = true
    }
    focusSessionRef.current = (sid: string | null) => {  // 점 클릭 → 그 세션 격리(고정)
      if (sid === sessionFocus) return
      sessionFocus = sid
      if (sid != null) { activeSession = sid; clusterFocus = null; activeCluster = null; focusSess = null; activeSess = null }
      focusDirty = true
    }
    // t(0~1)만큼 비대상 점/선을 흐리게. 대상 우선순위: 군집 고정 > 세션 고정 > hover 세션.
    function applyFocus(t: number) {
      const keepSess = activeSession ?? activeSess   // 세션 기준으로 밝게 유지할 세션(고정/ hover)
      for (let i = 0; i < pts.length; i++) {
        const keep = activeCluster != null ? pts[i].c === activeCluster : (keepSess != null && pts[i].s === keepSess)
        if (keep) {
          col[i * 3] = colBase[i * 3]; col[i * 3 + 1] = colBase[i * 3 + 1]; col[i * 3 + 2] = colBase[i * 3 + 2]
        } else {
          col[i * 3] = colBase[i * 3] * (1 - t) + dimPt.r * t
          col[i * 3 + 1] = colBase[i * 3 + 1] * (1 - t) + dimPt.g * t
          col[i * 3 + 2] = colBase[i * 3 + 2] * (1 - t) + dimPt.b * t
        }
      }
      geo.attributes.color.needsUpdate = true
    }

    // ── 커스텀 카메라 컨트롤 ──
    // 좌드래그 = 화면축 기준 자유 회전(관성) · 휠클릭/우드래그 = 이동(pan) · 휠 = 커서 기준 줌.
    const target = new THREE.Vector3(0, 0, 0)
    const ROT = 0.006
    let drag: "rotate" | "pan" | null = null
    let lastX = 0, lastY = 0, downX = 0, downY = 0
    const av = { x: 0, y: 0 }   // 회전 관성 속도(px/frame)

    const _off = new THREE.Vector3(), _dir = new THREE.Vector3()
    const _right = new THREE.Vector3(), _up = new THREE.Vector3()
    const _q = new THREE.Quaternion(), _q2 = new THREE.Quaternion()
    const cursorPt = new THREE.Vector3()
    const _v1 = new THREE.Vector3()
    let flyGoal: { center: THREE.Vector3; camTo: THREE.Vector3 } | null = null   // 군집 이동+확대 목표

    function basis() {
      _dir.copy(target).sub(camera.position).normalize()
      _right.crossVectors(_dir, camera.up).normalize()
      _up.crossVectors(_right, _dir).normalize()
    }
    function rotate(dx: number, dy: number) {
      basis()
      _q.setFromAxisAngle(_up, -dx * ROT).multiply(_q2.setFromAxisAngle(_right, -dy * ROT))
      _off.copy(camera.position).sub(target).applyQuaternion(_q)
      camera.up.applyQuaternion(_q)
      camera.position.copy(target).add(_off)
      camera.lookAt(target)
    }
    function panBy(dx: number, dy: number) {
      basis()
      const scale = camera.position.distanceTo(target) * 0.0009
      const mv = _right.multiplyScalar(-dx * scale).add(_up.multiplyScalar(dy * scale))
      target.add(mv); camera.position.add(mv)
    }

    const cluVecs = data.clusters.map((c) => ({ c, v: at({ x: c.x, y: c.y, z: c.z } as GraphPoint3D) }))
    // 군집별 {중심, 반경(구성원 최대거리)} — fly-to 확대 거리 계산용.
    const cluInfo = new Map<number, { center: THREE.Vector3; radius: number }>()
    for (const { c, v } of cluVecs) cluInfo.set(c.id, { center: v.clone(), radius: 0 })
    { const _r = new THREE.Vector3()
      for (let i = 0; i < pts.length; i++) {
        const info = cluInfo.get(pts[i].c); if (!info) continue
        const d = _r.set(pos[i * 3], pos[i * 3 + 1], pos[i * 3 + 2]).distanceTo(info.center)
        if (d > info.radius) info.radius = d
      } }
    // 군집 클릭 → 그 중심으로 이동 + 화면에 꽉 차게 확대(FOV 기반 거리).
    flyToRef.current = (id: number) => {
      const info = cluInfo.get(id); if (!info) return
      const dir = _v1.copy(target).sub(camera.position).normalize()   // 카메라→타깃 방향
      const fov = THREE.MathUtils.degToRad(camera.fov / 2)
      const radius = Math.max(info.radius, EXTENT * 0.05)
      const dist = Math.min(Math.max((radius / Math.tan(fov)) * 1.35, EXTENT * 0.4), EXTENT * 9)
      flyGoal = { center: info.center.clone(), camTo: info.center.clone().addScaledVector(dir, -dist) }
      av.x = 0; av.y = 0
    }
    const proj = new THREE.Vector3()
    const _pp = new THREE.Vector3()
    const PICK_PX = 3   // 커서와 이 반경(px) 이내일 때만 점으로 인식(정밀 선택)

    // 화면 투영 후 커서 반경 안의 점들 중 '가장 앞(카메라에 가까운)' 점을 선택 →
    // 겹친 3D에서 뒤쪽이 아니라 실제로 위에 보이는 점을 집는다.
    function hitIndex(clientX: number, clientY: number): number {
      const r = renderer.domElement.getBoundingClientRect()
      const mx = clientX - r.left, my = clientY - r.top
      const maxD = PICK_PX * PICK_PX
      let best = -1, bestZ = Infinity
      for (let i = 0; i < pts.length; i++) {
        _pp.set(pos[i * 3], pos[i * 3 + 1], pos[i * 3 + 2]).project(camera)
        if (_pp.z > 1) continue   // 뒤/클립된 점 제외
        const sx = (_pp.x * 0.5 + 0.5) * w, sy = (-_pp.y * 0.5 + 0.5) * h
        const dx = sx - mx, dy = sy - my
        if (dx * dx + dy * dy <= maxD && _pp.z < bestZ) { bestZ = _pp.z; best = i }
      }
      return best
    }

    function onWheel(e: WheelEvent) {
      e.preventDefault()
      flyGoal = null   // 사용자가 줌하면 진행 중인 fly 애니메이션 즉시 중단(고정감 제거)
      const r = renderer.domElement.getBoundingClientRect()
      const ndc = new THREE.Vector2(((e.clientX - r.left) / r.width) * 2 - 1, -((e.clientY - r.top) / r.height) * 2 + 1)
      const rc = new THREE.Raycaster(); rc.setFromCamera(ndc, camera)
      // 커서 아래(타깃 깊이) 월드 지점 — 이 점을 화면에 고정한 채 확대/축소.
      cursorPt.copy(camera.position).addScaledVector(rc.ray.direction, camera.position.distanceTo(target))
      const unit = e.deltaMode === 1 ? 16 : e.deltaMode === 2 ? 400 : 1   // deltaMode 정규화
      const step = Math.min((Math.abs(e.deltaY) * unit) / 400, 0.35)
      const factor = Math.pow(e.deltaY < 0 ? 0.82 : 1 / 0.82, step)
      const dist = camera.position.distanceTo(target)
      if (factor < 1 && dist < EXTENT * 0.3) return    // 충분히 확대되면 수렴(멈춤)
      if (factor > 1 && dist > EXTENT * 12) return
      // 카메라·타깃을 커서 지점으로 함께 접근/후퇴 → 커서 기준 줌(그 점은 화면 고정).
      camera.position.lerpVectors(cursorPt, camera.position, factor)
      target.lerpVectors(cursorPt, target, factor)
      camera.lookAt(target)
    }
    function onDown(e: PointerEvent) {
      flyGoal = null   // 드래그 시작하면 fly 중단(회전/이동이 즉시 먹히게)
      downX = lastX = e.clientX; downY = lastY = e.clientY
      drag = e.button === 0 ? "rotate" : "pan"   // 좌=회전, 휠클릭/우클릭=이동
      setGlow(-1)
      av.x = 0; av.y = 0
      if (e.button !== 0) e.preventDefault()
      try { renderer.domElement.setPointerCapture(e.pointerId) } catch { /* noop */ }
    }
    function onMove(e: PointerEvent) {
      if (drag) {
        const dx = e.clientX - lastX, dy = e.clientY - lastY
        lastX = e.clientX; lastY = e.clientY
        if (drag === "rotate") { rotate(dx, dy); av.x = dx; av.y = dy } else panBy(dx, dy)
      } else {
        const i = hitIndex(e.clientX, e.clientY)
        setTip(i >= 0 ? { sx: e.clientX, sy: e.clientY, p: pts[i] } : null)
        setHover(i >= 0 ? pts[i].s : null)   // 강조는 렌더 루프에서 부드럽게 페이드
        setGlow(i)                            // 커서 아래 점에 광원
      }
    }
    function onUp(e: PointerEvent) {
      const click = Math.abs(e.clientX - downX) + Math.abs(e.clientY - downY) <= 5
      const wasRotate = drag === "rotate"
      drag = null
      try { renderer.domElement.releasePointerCapture(e.pointerId) } catch { /* noop */ }
      if (click && wasRotate) {
        const i = hitIndex(e.clientX, e.clientY)
        if (i >= 0) pointClickRef.current(pts[i].s, pts[i].t)   // 점 클릭 → 그 세션 격리+우측 대화목록
        else clearRef.current()                                 // 빈 공간 클릭 → 격리 해제
      }
    }
    const onCtx = (e: Event) => e.preventDefault()
    renderer.domElement.addEventListener("wheel", onWheel, { passive: false })
    renderer.domElement.addEventListener("pointerdown", onDown)
    renderer.domElement.addEventListener("pointermove", onMove)
    renderer.domElement.addEventListener("pointerup", onUp)
    renderer.domElement.addEventListener("pointercancel", onUp)
    renderer.domElement.addEventListener("pointerleave", () => { setTip(null); setHover(null); setGlow(-1) })
    renderer.domElement.addEventListener("contextmenu", onCtx)

    let raf = 0
    const loop = () => {
      raf = requestAnimationFrame(loop)
      // 회전 관성(놓은 뒤 잔여 속도로 계속 돌다 감쇠).
      if (!drag && (Math.abs(av.x) > 0.05 || Math.abs(av.y) > 0.05)) {
        rotate(av.x, av.y); av.x *= 0.95; av.y *= 0.95   // 회전 관성만(줌/이동은 즉시 반영, 루프가 안 건드림)
      }
      // 군집 fly-to: 중심으로 이동 + 확대(target·camera 동시 보간).
      if (flyGoal) {
        target.lerp(flyGoal.center, 0.12)
        camera.position.lerp(flyGoal.camTo, 0.12)
        camera.lookAt(target)
        if (target.distanceTo(flyGoal.center) < 0.5 && camera.position.distanceTo(flyGoal.camTo) < 0.5) flyGoal = null
      }
      // hover 강조 부드럽게 페이드(대상 있으면 t→1, 없으면 t→0).
      const ftarget = (focusSess != null || clusterFocus != null || sessionFocus != null) ? 1 : 0
      if (focusDirty || Math.abs(focusT - ftarget) > 0.002) {
        focusT += (ftarget - focusT) * 0.2
        if (Math.abs(focusT - ftarget) < 0.004) { focusT = ftarget; focusDirty = false; if (ftarget === 0) { activeSess = null; activeCluster = null; activeSession = null } }
        applyFocus(focusT)
      }
      // 확대할수록 점 조금 커짐(가까울수록 큼).
      const dist = camera.position.distanceTo(target)
      const zt = Math.min(Math.max((dist - EXTENT * 0.3) / (EXTENT * 5), 0), 1)
      material.size = 5.5 * (1 + (1 - zt) * 0.6)
      // hover 광원: 화면상 크기 대략 일정하게.
      if (glow.visible && glowIdx >= 0) {
        const gs = camera.position.distanceTo(glow.position) * 0.05
        glow.scale.set(gs, gs, 1)
      }
      renderer.render(scene, camera)
      for (const { c, v } of cluVecs) {
        const el = labelRefs.current.get(c.id); if (!el) continue
        proj.copy(v).project(camera)
        if (proj.z > 1) { el.style.display = "none"; continue }
        el.style.display = "block"
        el.style.left = (proj.x * 0.5 + 0.5) * w + "px"; el.style.top = (-proj.y * 0.5 + 0.5) * h + "px"
      }
    }
    loop()

    // 리사이즈 디바운스: 라이브 리사이즈 매 틱마다 setSize(=드로잉 버퍼 재할당)를 부르면 무거워
    // 화면이 밀리거나 찢긴다. 리사이즈가 ~120ms 멈춘 뒤 한 번만 버퍼를 실제 크기로 재할당한다.
    // setSize의 세 번째 인자 false → three.js가 캔버스 CSS 크기(100%)를 건드리지 않게 유지.
    let resizeTimer: ReturnType<typeof setTimeout> | null = null
    const applyResize = () => {
      w = wrap.clientWidth; h = wrap.clientHeight
      if (w === 0 || h === 0) return
      camera.aspect = w / h; camera.updateProjectionMatrix()
      renderer.setSize(w, h, false)
    }
    const ro = new ResizeObserver(() => {
      if (resizeTimer) clearTimeout(resizeTimer)
      resizeTimer = setTimeout(applyResize, 120)
    })
    ro.observe(wrap)

    return () => {
      cancelAnimationFrame(raf); ro.disconnect()
      if (resizeTimer) clearTimeout(resizeTimer)
      renderer.domElement.removeEventListener("wheel", onWheel)
      renderer.domElement.removeEventListener("pointerdown", onDown)
      renderer.domElement.removeEventListener("pointermove", onMove)
      renderer.domElement.removeEventListener("pointerup", onUp)
      renderer.domElement.removeEventListener("pointercancel", onUp)
      renderer.domElement.removeEventListener("contextmenu", onCtx)
      flyToRef.current = null; focusClusterRef.current = null; focusSessionRef.current = null
      glowTex.dispose(); glowMat.dispose()
      dotTex.dispose(); geo.dispose(); material.dispose(); renderer.dispose()
      if (renderer.domElement.parentNode === wrap) wrap.removeChild(renderer.domElement)
    }
  }, [data])

  // 캔버스 aria-label만 언어/데이터 변경 시 갱신(무거운 3D 씬 재빌드 없이 — 씬 effect는 [data]만 의존).
  useEffect(() => {
    if (!data) return
    const canvas = wrapRef.current?.querySelector("canvas")
    canvas?.setAttribute("aria-label", t("graph.canvasAria", { count: data.points.length.toLocaleString(), clusters: data.clusters.length }))
  }, [t, data])

  const clusters = data?.clusters ?? []
  const hasData = data && data.points.length > 0

  // 선택 군집의 대화 목록(점=청크 → turn 기준 중복 제거).
  const clusterTurns = useMemo(() => {
    if (selCluster == null || !data) return []
    const seen = new Set<string>(); const out: { t: string; s: string; h: string }[] = []
    for (const p of data.points) {
      if (p.c !== selCluster || seen.has(p.t)) continue
      seen.add(p.t); out.push({ t: p.t, s: p.s, h: p.h })
    }
    return out
  }, [selCluster, data])
  // 선택 세션의 대화 목록(점=청크 → turn 기준 중복 제거).
  const sessionTurns = useMemo(() => {
    if (selSession == null || !data) return []
    const seen = new Set<string>(); const out: { t: string; s: string; h: string }[] = []
    for (const p of data.points) { if (p.s !== selSession || seen.has(p.t)) continue; seen.add(p.t); out.push({ t: p.t, s: p.s, h: p.h }) }
    return out
  }, [selSession, data])

  // 군집/세션 선택 시 지도 격리.
  useEffect(() => { focusClusterRef.current?.(selCluster) }, [selCluster])
  useEffect(() => { focusSessionRef.current?.(selSession) }, [selSession])

  // 우측 목록 스크롤 제어: 새 군집 목록은 맨 위로, 점 클릭 세션은 강조 대화로 이동.
  const listScrollRef = useRef<HTMLDivElement | null>(null)
  const clickedRef = useRef<HTMLButtonElement | null>(null)
  useEffect(() => { if (selCluster != null) listScrollRef.current?.scrollTo({ top: 0 }) }, [selCluster])
  useEffect(() => {
    if (selSession == null) return
    if (clickedRef.current) clickedRef.current.scrollIntoView({ behavior: matchMedia("(prefers-reduced-motion: reduce)").matches ? "instant" as ScrollBehavior : "smooth", block: "center" })
    else listScrollRef.current?.scrollTo({ top: 0 })
  }, [selSession, clickedTurn])

  const openCluster = (id: number) => { setSelSession(null); setClickedTurn(null); setSelCluster(id); flyToRef.current?.(id) }
  const openPointSession = (session: string, turn: string) => { setSelCluster(null); setSelSession(session); setClickedTurn(turn) }
  pointClickRef.current = openPointSession
  const clearAll = () => { setSelCluster(null); setSelSession(null); setClickedTurn(null) }   // 격리 해제
  clearRef.current = clearAll
  // 대화 클릭 → 그 탭(세션/군집)으로 이동해서 대화 열기(바로 이동하지 않고 목록에서 고른 것만).
  const openTurn = (it: { s: string; t: string }) => {
    if (selCluster != null) onOpenTurn("clusters", String(selCluster), it.t, it.s)
    else if (selSession != null) onOpenTurn("sessions", selSession, it.t, it.s)
  }
  const selClusterLabel = selCluster != null ? clusters.find((c) => c.id === selCluster)?.label ?? "" : ""

  return (
    <div className="flex h-full flex-col">
      <div className="flex flex-wrap items-center justify-between gap-2 px-6 pt-5">
        <h2 className="text-lg font-semibold">{t("graph.title")}</h2>
        <div className="flex items-center gap-3">
          <button
            type="button"
            onClick={regenerate}
            disabled={refreshing}
            title={t("graph.regenTitle")}
            className="rounded-md border px-2.5 py-1 text-xs text-muted-foreground transition-colors hover:bg-muted disabled:opacity-60"
          >
            {refreshing ? t("graph.recalculating") : t("graph.regenerate")}
          </button>
        </div>
      </div>

      <div className="relative flex-1 px-4 pb-4 pt-3">
        {!data ? <div className="grid h-full place-items-center text-muted-foreground">{t("graph.loadingFirst")}</div>
          : err ? <div className="grid h-full place-items-center px-6 text-center text-destructive">{t("graph.loadError", { err })}</div>
          : !hasData ? <div className="grid h-full place-items-center text-muted-foreground">{t("graph.empty")}</div>
            : (
              <div ref={wrapRef} className="relative h-full w-full overflow-hidden rounded-xl border bg-card">
                {/* 조작 힌트(좌하단) — 캔버스가 드래그·클릭 가능하다는 걸 첫 사용자에게 알림 */}
                <div className="pointer-events-none absolute bottom-3 left-3 z-20 rounded-md bg-card/80 px-2 py-1 text-[11px] text-muted-foreground backdrop-blur">
                  {t("graph.controlsHint")}
                </div>
                {/* 떠다니는 라벨은 큰 군집 상위 18개만(과밀 방지) — 전체 목록은 우측 범례에 */}
                {clusters.slice(0, 18).map((c) => (
                  <div key={c.id} ref={(el) => { labelRefs.current.set(c.id, el) }}
                    onClick={() => openCluster(c.id)} title={t("graph.goToLabel", { label: c.label })}
                    role="button" tabIndex={0} aria-label={t("graph.goToCluster", { label: c.label })}
                    onKeyDown={(e) => { if (e.key === "Enter" || e.key === " " || e.key === "Spacebar") { e.preventDefault(); openCluster(c.id) } }}
                    className="absolute z-10 flex -translate-x-1/2 -translate-y-1/2 cursor-pointer select-none items-center gap-1.5 whitespace-nowrap text-[12px] font-bold"
                    style={{
                      display: "none", color: colorOf(c.id),
                      // 채운 박스 대신 배경색 후광(halo) — 점을 안 가리고 글자만 또렷.
                      textShadow: "0 0 2px var(--card),0 0 2px var(--card),0 0 4px var(--card),0 0 4px var(--card),0 0 6px var(--card)",
                    }}>
                    <span className="size-2 rounded-full ring-1 ring-[var(--card)]" style={{ background: colorOf(c.id) }} />
                    {c.label}
                  </div>
                ))}
                <div className="absolute right-6 top-4 z-20 flex max-h-[86%] w-80 flex-col rounded-xl border bg-card/90 text-xs shadow-md backdrop-blur">
                  {selCluster != null ? (
                    // ── 군집 대화 목록 ──
                    <>
                      <div className="flex items-center gap-1.5 border-b px-2 py-2">
                        <button type="button" onClick={clearAll} className="grid size-6 shrink-0 place-items-center rounded-md hover:bg-muted" aria-label={t("graph.clearIsolation")}>
                          <ArrowLeft className="size-3.5" />
                        </button>
                        <span className="size-2.5 shrink-0 rounded-full" style={{ background: colorOf(selCluster) }} />
                        <span className="min-w-0 flex-1 truncate font-medium">{selClusterLabel}</span>
                        <span className="shrink-0 tabular-nums text-muted-foreground">{clusterTurns.length}</span>
                      </div>
                      <div ref={listScrollRef} className="overflow-y-auto p-1.5">
                        {clusterTurns.map((it) => (
                          <button key={it.t} type="button" onClick={() => openTurn(it)}
                            className="w-full rounded-md px-1.5 py-1.5 text-left hover:bg-muted">
                            <span className="block truncate text-foreground">{it.h || t("graph.untitled")}</span>
                            <span className="block truncate text-[10px] text-muted-foreground">{t("graph.sessionLabel", { id: it.s.slice(0, 8) })}</span>
                          </button>
                        ))}
                      </div>
                    </>
                  ) : selSession != null ? (
                    // ── 세션 대화 목록(점 클릭) — 클릭한 턴 강조 ──
                    <>
                      <div className="flex items-center gap-1.5 border-b px-2 py-2">
                        <button type="button" onClick={clearAll} className="grid size-6 shrink-0 place-items-center rounded-md hover:bg-muted" aria-label={t("graph.clearIsolation")}>
                          <ArrowLeft className="size-3.5" />
                        </button>
                        <span className="min-w-0 flex-1 truncate font-medium">{t("graph.sessionLabel", { id: selSession.slice(0, 8) })}</span>
                        <span className="shrink-0 tabular-nums text-muted-foreground">{sessionTurns.length}</span>
                      </div>
                      <div ref={listScrollRef} className="overflow-y-auto p-1.5">
                        {sessionTurns.map((it) => (
                          <button key={it.t} type="button" onClick={() => openTurn(it)}
                            ref={clickedTurn === it.t ? clickedRef : undefined}
                            className={`w-full rounded-md px-1.5 py-1.5 text-left ${clickedTurn === it.t ? "bg-primary/10 ring-1 ring-primary/40" : "hover:bg-muted"}`}>
                            <span className="block truncate text-foreground">{it.h || t("graph.untitled")}</span>
                            {clickedTurn === it.t && <span className="text-[9.5px] font-medium text-primary">{t("graph.justClicked")}</span>}
                          </button>
                        ))}
                      </div>
                    </>
                  ) : (
                    // ── 범례(주제 군집) ──
                    <>
                      <div className="border-b px-2.5 py-2 font-medium text-muted-foreground">{t("graph.legendTitle")}</div>
                      <div className="overflow-y-auto p-1.5">
                        {clusters.map((c) => (
                          <button key={c.id} type="button" onClick={() => openCluster(c.id)}
                            className="flex w-full items-center gap-2 rounded-md px-1.5 py-1 text-left hover:bg-muted">
                            <span className="size-2.5 shrink-0 rounded-full" style={{ background: colorOf(c.id) }} />
                            <span className="min-w-0 flex-1 truncate">{c.label}</span>
                            <span className="shrink-0 tabular-nums text-muted-foreground">{c.n}</span>
                          </button>
                        ))}
                      </div>
                    </>
                  )}
                </div>
                {tip && (
                  <div role="status" aria-live="polite"
                    className="pointer-events-none fixed z-50 max-w-xs rounded-lg border bg-popover px-3 py-2 text-xs shadow-md"
                    style={{ left: tip.sx + 14, top: tip.sy + 14 }}>
                    <div className="font-medium text-foreground line-clamp-2">{tip.p.h || t("graph.untitled")}</div>
                    <div className="mt-1 tabular-nums text-muted-foreground">{t("graph.sessionLabel", { id: tip.p.s.slice(0, 8) })}</div>
                  </div>
                )}
              </div>
            )}
      </div>
    </div>
  )
}
