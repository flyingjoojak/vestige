; electron-builder NSIS custom include.
; 설치 시작 시 실행 중인 백엔드를 확실히 종료해 파일 잠금("가끔 설치 오류 → 다시 시도하면 됨")을 방지.
; Vestige은 트레이 상주 + 별도 이름의 백엔드 자식(vestige-backend.exe)을 띄우므로,
; 기본 "앱 닫기"만으로는 백엔드가 남아 resources\backend\vestige-backend.exe 를 물고 있을 수 있다.
; nsExec::Exec 는 창 없이(hidden) 실행된다.
;
; 주의 1 — nsExec::Exec 는 **종료 코드를 스택에 push 한다.** Pop 으로 비우지 않으면 값이 쌓여
;   이후 electron-builder 자신의 NSIS 코드가 스택에서 엉뚱한 값을 집는다. 반드시 Pop 한다.
; 주의 2 — Vestige.exe 는 여기서 죽이지 않는다. `taskkill /T` 는 프로세스 **트리 전체**를 죽이는데,
;   업데이트 설치는 quitAndInstall() 이 띄운 것이라 이 설치 관리자가 Vestige.exe 의 자손일 수 있다.
;   그러면 설치 관리자가 자기 자신을 죽여 설치가 중간에 끊긴다(#229 에서 실제로 backend 폴더만
;   비워진 채 끝났다). 앱 종료는 electron-builder 기본 처리에 맡긴다.

!macro customInit
  nsExec::Exec 'taskkill /F /IM vestige-backend.exe'
  Pop $0
!macroend

!macro customUnInit
  nsExec::Exec 'taskkill /F /IM vestige-backend.exe'
  Pop $0
!macroend
