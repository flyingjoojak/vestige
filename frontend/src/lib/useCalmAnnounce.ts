import { useEffect, useRef, useState } from "react"

// 진행률처럼 1~4초마다 바뀌는 문구를 aria-live 에 그대로 걸면 스크린리더가 끊임없이 읽는다(첫 색인·동기화는
// 수 분~수십 분). 바쁜 동안에는 처음 문구를 한 번만 알리고, 바쁨이 끝나거나 바쁘지 않을 때의 변화만 알린다.
// 화면에 보이는 숫자는 그대로 두고, 이 값을 sr-only role="status" 영역에 넣어 쓴다.
export function useCalmAnnounce(text: string, busy: boolean): string {
  const [live, setLive] = useState("")
  const wasBusy = useRef(false)
  useEffect(() => {
    if (!(busy && wasBusy.current)) setLive(text)
    wasBusy.current = busy
  }, [text, busy])
  return live
}
