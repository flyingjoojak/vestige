import { useRef } from "react"

// 상호배타 선택(테마/언어/색인 모드 등)을 위한 접근성 세그먼트 컨트롤.
// role=radiogroup + role=radio/aria-checked + 로빙 tabindex + 화살표키 이동(WAI-ARIA radiogroup 패턴).
interface Option<T extends string> {
  value: T
  label: React.ReactNode
}
interface Props<T extends string> {
  label: string
  value: T
  options: readonly Option<T>[]
  onChange: (v: T) => void
  className?: string
}

export function SegmentedRadioGroup<T extends string>({ label, value, options, onChange, className }: Props<T>) {
  const refs = useRef<(HTMLButtonElement | null)[]>([])

  function handleKeyDown(e: React.KeyboardEvent, i: number) {
    const n = options.length
    let j = -1
    if (e.key === "ArrowRight" || e.key === "ArrowDown") j = (i + 1) % n
    else if (e.key === "ArrowLeft" || e.key === "ArrowUp") j = (i - 1 + n) % n
    else if (e.key === "Home") j = 0
    else if (e.key === "End") j = n - 1
    if (j >= 0) {
      e.preventDefault()
      onChange(options[j].value)
      refs.current[j]?.focus()
    }
  }

  return (
    <div role="radiogroup" aria-label={label} className={className ?? "inline-flex overflow-hidden rounded-lg border"}>
      {options.map((o, i) => {
        const active = value === o.value
        return (
          <button
            key={o.value}
            ref={(el) => { refs.current[i] = el }}
            type="button"
            role="radio"
            aria-checked={active}
            tabIndex={active ? 0 : -1}
            onClick={() => onChange(o.value)}
            onKeyDown={(e) => handleKeyDown(e, i)}
            className={`flex items-center gap-1 px-3 py-1.5 text-xs transition-colors focus-visible:outline-none focus-visible:ring-[3px] focus-visible:ring-ring ${active ? "bg-primary/10 font-semibold text-primary" : "text-muted-foreground hover:text-foreground"}`}
          >
            {o.label}
          </button>
        )
      })}
    </div>
  )
}
