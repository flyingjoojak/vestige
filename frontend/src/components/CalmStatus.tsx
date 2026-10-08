import { useCalmAnnounce } from "@/lib/useCalmAnnounce"

// 화면에는 보이지 않고 스크린리더에만 알린다 - 바쁜 동안은 처음 문구 한 번, 끝나면 결과(useCalmAnnounce).
export function CalmStatus({ text, busy }: { text: string; busy: boolean }) {
  const live = useCalmAnnounce(text, busy)
  return <span role="status" className="sr-only">{live}</span>
}
