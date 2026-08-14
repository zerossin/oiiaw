# oiiaw

Obsidian ↔ iCloud, Windows용 자동 동기화 도구.

Obsidian을 iCloud Drive 폴더에서 직접 열면 저장할 때마다 충돌 파일이 생깁니다.
oiiaw는 로컬 폴더와 iCloud 폴더를 백그라운드에서 대신 동기화해서 이 문제를 없애줍니다.

## 설치 (5분)

1. [Python 3.11+](https://www.python.org/downloads/) 설치 — 설치 화면에서
   **"Add python.exe to PATH"** 꼭 체크
2. 이 페이지 위쪽 초록색 **Code → Download ZIP** → 압축 풀기
3. 압축 푼 폴더 안 주소창에 `cmd` 입력 → 엔터
4. `pip install .` 입력
5. 시작 메뉴에서 **`oiiaw-setup`** 검색해서 실행
6. 로컬 폴더 / iCloud 폴더 선택 → **설치** 클릭

## 사용법

- Obsidian에서는 항상 **로컬 폴더**만 vault로 여세요 (iCloud 폴더는 절대 X —
  그러면 원래 문제가 그대로 재발합니다)
- 트레이 아이콘 색: 🔵 대기 · 🟠 동기화 중 · 🔴 충돌/에러
- 지금 상태가 궁금하면 터미널에 `oiiaw status`

## 고급: 수동 설정

`config.example.yaml`을 참고해서 직접 `config.yaml`을 만들고
`oiiaw run -c <path>`로 실행할 수도 있습니다. 마법사가 만든 설정 파일은
`%APPDATA%\oiiaw\config.yaml`에 있습니다.
