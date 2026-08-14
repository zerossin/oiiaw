# oiiaw

Obsidian ↔ iCloud, Windows용 자동 동기화 도구.

Obsidian을 iCloud Drive 폴더에서 직접 열면 저장할 때마다 충돌 파일이 생깁니다.
oiiaw는 로컬 폴더와 iCloud 폴더를 백그라운드에서 대신 동기화해서 이 문제를 없애줍니다.

## 설치

1. [Python 3.11+](https://www.python.org/downloads/) 설치 — **"Add python.exe to PATH"** 체크
2. 코드 받기:
   ```
   git clone https://github.com/zerossin/oiiaw.git
   ```
   git이 없다면 이 페이지 위쪽 **Code → Download ZIP** → 압축 풀기
3. 받은 폴더에서 `cmd` 열고:
   ```
   pip install .
   ```
4. `oiiaw-setup` 실행 → 로컬 폴더 / iCloud 폴더 선택 → 설치

## 사용법

- Obsidian에서는 iCloud 폴더가 아닌 항상 **로컬 폴더**만 vault로 여세요.
- 트레이 아이콘 색: 🔵 대기 · 🟠 동기화 중 · 🔴 충돌/에러
- 트레이 클릭시 현재 상태와 최근 활동 목록이 뜹니다.
- 트레이 우클릭 → 재시작으로 안전하게 다시 시작할 수 있습니다.
- 트레이를 껐다면 `oiiaw-setup`을 다시 할 필요 없이 `oiiaw start`로
  기존 설정 그대로 다시 켤 수 있습니다.
- 터미널에서 `oiiaw status`로도 확인이 가능합니다.
- 자동 시작 등록이 실패하면 `oiiaw-setup`을 관리자 권한으로 다시 실행하세요.

## 고급: 설정 파일 직접 편집

마법사 대신 `config.example.yaml`을 `config.yaml`로 복사해 경로를 채우고
`oiiaw run -c config.yaml`로 실행할 수도 있습니다.
