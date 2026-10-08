import { ExternalLink } from "lucide-react"
import { useTranslation } from "react-i18next"

// 채팅 머리줄에 붙이는 '세션으로 이동' - 폴더·검색처럼 대화를 그 자리에서 보여주는 화면에서,
// 세션 목록으로 넘어가 세션을 이어서 하거나 세션 안에서 검색할 때 쓴다.
export function GoToSessionButton({ onClick }: { onClick: () => void }) {
  const { t } = useTranslation()
  return (
    <button type="button" onClick={onClick} title={t("folders.goToSessionHint")}
      className="inline-flex shrink-0 items-center gap-1 rounded-md border px-1.5 py-1 text-[11px] transition-colors hover:bg-muted">
      <ExternalLink aria-hidden className="size-3.5" />{t("folders.goToSession")}
    </button>
  )
}
