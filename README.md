# oiiaw

Obsidian ↔ iCloud, Windows용 자동 동기화 도구.

Obsidian을 iCloud Drive 폴더에서 직접 열면 저장할 때마다 충돌 파일이 생깁니다.
oiiaw는 로컬 폴더와 iCloud 폴더를 백그라운드에서 대신 동기화해서 이 문제를 없애줍니다.

> [!WARNING]
> 현재 Windows용 베타 버전입니다. 처음 사용하기 전에 로컬 및 iCloud 문서를 백업하세요.

## 설치

1. [Python 3.11+](https://www.python.org/downloads/) 설치 — **"Add python.exe to PATH"** 체크
2. 터미널(`PowerShell` 또는 `cmd`)에서 설치:
   ```powershell
   py -m pip install oiiaw
   ```
3. 설치 마법사 실행:
   ```powershell
   oiiaw-setup
   ```
4. 로컬 폴더와 iCloud 폴더를 선택해 설정 완료

업데이트할 때는 다음 명령을 실행하세요.

```powershell
py -m pip install --upgrade oiiaw
```

### 소스에서 설치

최신 개발 버전을 직접 설치하려면:

```powershell
git clone https://github.com/zerossin/oiiaw.git
cd oiiaw
py -m pip install .
```

## 사용법

- Obsidian에서는 iCloud 폴더가 아닌 항상 **로컬 폴더**만 vault로 여세요.
- 트레이 아이콘 색: 🔵 대기 · 🟠 동기화 중 · 🔴 충돌/에러
- 트레이 클릭시 현재 상태와 최근 활동 목록이 뜹니다.
- 충돌 기록을 선택하면 현재 문서와 보관된 충돌본을 Obsidian에서 바로 열 수 있습니다.
- 트레이 우클릭 → 재시작으로 안전하게 다시 시작할 수 있습니다.
- 온라인 전용 iCloud 파일은 자동으로 내려받고, 일시적인 파일 잠금이나 iCloud 연결 끊김은 자동으로 다시 시도합니다.
- 동기화 엔진이 예기치 않게 종료돼도 트레이가 새 엔진을 만들어 자동 복구합니다.
- 트레이를 껐다면 `oiiaw-setup`을 다시 할 필요 없이 `oiiaw start`로
  기존 설정 그대로 다시 켤 수 있습니다.
- 터미널에서 `oiiaw status`로도 확인이 가능합니다.
- 작업 스케줄러 등록이 거부되면 관리자 권한이 필요 없는 사용자 자동 시작 방식으로 대체됩니다.

## 고급: 설정 파일 직접 편집

마법사 대신 `config.example.yaml`을 `config.yaml`로 복사해 경로를 채우고
`oiiaw run -c config.yaml`로 실행할 수도 있습니다.
