import { useEffect, useRef } from "react"

// 주기적으로 load 를 부른다. 두 가지를 막는다:
// - 창이 안 보이면(최소화·다른 창 뒤) 쉬고, 다시 보이면 바로 한 번 부른다
// - 앞 요청이 아직 안 끝났으면 건너뛴다 - 백엔드가 바쁠 때(재색인 중) 같은 요청이 겹쳐 쌓이지 않게
// load 는 매 렌더 새 함수여도 된다(마지막 것을 부른다). 실패 처리는 load 안에서 한다.
// 이보다 오래 응답이 없으면 멈춘 요청으로 보고 다음 요청을 보낸다
const STUCK_MS = 15000

export function usePolling(load: () => Promise<unknown>, ms: number, enabled = true): void {
  const ref = useRef(load)
  ref.current = load
  useEffect(() => {
    if (!enabled) return
    let busySince = 0   // 0 = 진행 중인 요청 없음
    const tick = () => {
      if (document.visibilityState !== "visible") return
      // 앞 요청이 끝나지 않았으면 건너뛴다. 다만 너무 오래 걸리면(멈춘 요청) 다시 보낸다 -
      // 안 그러면 응답이 끝내 안 오는 요청 하나에 폴링이 영영 멈춘다.
      if (busySince && Date.now() - busySince < Math.max(ms * 10, STUCK_MS)) return
      const mine = (busySince = Date.now())
      ref.current().catch(() => { /* load 가 처리한다 */ }).finally(() => { if (busySince === mine) busySince = 0 })
    }
    const onVisible = () => { if (document.visibilityState === "visible") tick() }
    tick()
    const id = window.setInterval(tick, ms)
    document.addEventListener("visibilitychange", onVisible)
    return () => { window.clearInterval(id); document.removeEventListener("visibilitychange", onVisible) }
  }, [ms, enabled])
}
