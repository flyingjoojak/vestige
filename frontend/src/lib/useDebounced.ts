import { useEffect, useState } from "react"

// 값이 delay 동안 멈춘 뒤에야 바뀐다. 타이핑 중 매 글자마다 서버를 때리지 않으려고 쓴다.
// (폴더 검색은 한 번에 임베딩 추론 ~100ms + 하위트리 턴 집합 재구축이라, 디바운스 없이는
//  "authentication" 한 단어에 요청 14번이 나가고 마지막 하나 빼고 전부 버려진다.)
export function useDebounced<T>(value: T, delay = 300): T {
  const [debounced, setDebounced] = useState(value)
  useEffect(() => {
    const id = setTimeout(() => setDebounced(value), delay)
    return () => clearTimeout(id)   // 값이 또 바뀌면 이전 타이머는 취소 → 마지막 것만 남는다
  }, [value, delay])
  return debounced
}
