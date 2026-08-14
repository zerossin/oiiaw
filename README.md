# oiiaw

Obsidian ↔ iCloud, Windows용 자동 동기화 도구.

Obsidian을 iCloud Drive 폴더에서 직접 열면 저장할 때마다 충돌 파일이 생깁니다.
oiiaw는 로컬 폴더와 iCloud 폴더를 백그라운드에서 대신 동기화해서 이 문제를 없애줍니다.

## 설치

1. [Python 3.11+](https://www.python.org/downloads/) 설치 — **"Add python.exe to PATH"** 체크
2. 이 페이지 위쪽 **Code → Download ZIP** → 압축 풀기
3. 압축 푼 폴더에서 `cmd` 열고:
   ```
   pip install .
   ```

그다음 아래 둘 중 하나로 설정:

**마법사로 설정** (추천)
```
oiiaw-setup
```
로컬 폴더 / iCloud 폴더 선택 → 설치.

**설정 파일로 직접 설정**
```
copy config.example.yaml config.yaml
```
`config.yaml`에 경로 채우고:
```
oiiaw run -c config.yaml
```

## 사용법

- Obsidian에서는 항상 **로컬 폴더**만 vault로 여세요 (iCloud 폴더는 절대 X —
  그러면 원래 문제가 그대로 재발합니다)
- 트레이 아이콘 색: 🔵 대기 · 🟠 동기화 중 · 🔴 충돌/에러 — **클릭하면** 현재
  상태와 최근 활동 목록이 뜸
- 터미널에서 `oiiaw status`로도 확인 가능
- 자동 시작 등록이 실패하면 `oiiaw-setup`을 관리자 권한으로 다시 실행
