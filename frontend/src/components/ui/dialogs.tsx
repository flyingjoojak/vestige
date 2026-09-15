import { createContext, useCallback, useContext, useEffect, useRef, useState } from "react"
import { useTranslation } from "react-i18next"
import {
  AlertDialog, AlertDialogAction, AlertDialogCancel, AlertDialogContent,
  AlertDialogDescription, AlertDialogFooter, AlertDialogHeader, AlertDialogTitle,
} from "@/components/ui/alert-dialog"
import { Input } from "@/components/ui/input"

// 브라우저 기본 confirm/prompt 대신 앱 안에서 뜨는 모달. 기본 다이얼로그는 창 최상단에 OS 스타일로
// 떠서 앱과 따로 놀고(데스크톱 앱에선 더 이질적), 스타일·번역도 못 맞춘다.
// 호출부가 기존 `await window.confirm(...)` 흐름을 그대로 쓰도록 Promise 를 돌려준다.
type ConfirmOpts = { title: string; description?: React.ReactNode; confirmLabel?: string; danger?: boolean }
type PromptOpts = { title: string; description?: React.ReactNode; defaultValue?: string; placeholder?: string; confirmLabel?: string }

type Pending =
  | { kind: "confirm"; opts: ConfirmOpts; resolve: (v: boolean) => void }
  | { kind: "prompt"; opts: PromptOpts; resolve: (v: string | null) => void }

const Ctx = createContext<{
  confirm: (o: ConfirmOpts) => Promise<boolean>
  prompt: (o: PromptOpts) => Promise<string | null>
} | null>(null)

export function DialogProvider({ children }: { children: React.ReactNode }) {
  const { t } = useTranslation()
  const [pending, setPending] = useState<Pending | null>(null)
  const [value, setValue] = useState("")
  const inputRef = useRef<HTMLInputElement>(null)

  const confirm = useCallback((opts: ConfirmOpts) =>
    new Promise<boolean>((resolve) => setPending({ kind: "confirm", opts, resolve })), [])
  const prompt = useCallback((opts: PromptOpts) =>
    new Promise<string | null>((resolve) => {
      setValue(opts.defaultValue ?? "")
      setPending({ kind: "prompt", opts, resolve })
    }), [])

  // 입력 모달은 열리자마자 입력칸에 포커스 + 기존 값 전체 선택(바로 덮어쓰기 좋게).
  useEffect(() => {
    if (pending?.kind === "prompt") {
      const id = setTimeout(() => { inputRef.current?.focus(); inputRef.current?.select() }, 0)
      return () => clearTimeout(id)
    }
  }, [pending])

  // 닫힘은 한 경로로만 — 취소/바깥클릭/Esc 모두 '취소'로 resolve 되어 호출부가 멈추지 않는다.
  function settle(result: boolean | string | null) {
    if (!pending) return
    if (pending.kind === "confirm") pending.resolve(result === true)
    else pending.resolve(typeof result === "string" ? result : null)
    setPending(null)
  }

  const opts = pending?.opts
  const isPrompt = pending?.kind === "prompt"
  const canSubmit = !isPrompt || value.trim().length > 0

  return (
    <Ctx.Provider value={{ confirm, prompt }}>
      {children}
      <AlertDialog open={pending !== null} onOpenChange={(o) => { if (!o) settle(null) }}>
        <AlertDialogContent>
          <AlertDialogHeader>
            <AlertDialogTitle>{opts?.title}</AlertDialogTitle>
            {opts?.description && <AlertDialogDescription>{opts.description}</AlertDialogDescription>}
          </AlertDialogHeader>
          {isPrompt && (
            <Input ref={inputRef} value={value} onChange={(e) => setValue(e.target.value)}
              aria-label={opts?.title}
              placeholder={(opts as PromptOpts | undefined)?.placeholder}
              onKeyDown={(e) => { if (e.key === "Enter" && canSubmit) { e.preventDefault(); settle(value.trim()) } }}
              className="mt-1" />
          )}
          <AlertDialogFooter>
            <AlertDialogCancel onClick={() => settle(null)}>{t("common.cancel")}</AlertDialogCancel>
            <AlertDialogAction disabled={!canSubmit}
              onClick={() => settle(isPrompt ? value.trim() : true)}>
              {opts?.confirmLabel ?? t("common.confirm")}
            </AlertDialogAction>
          </AlertDialogFooter>
        </AlertDialogContent>
      </AlertDialog>
    </Ctx.Provider>
  )
}

export function useDialogs() {
  const ctx = useContext(Ctx)
  if (!ctx) throw new Error("useDialogs must be used within DialogProvider")
  return ctx
}
