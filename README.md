# oiiaw

Obsidian ↔ iCloud, 구름다리처럼 이어주는 Windows용 동기화 도구.

Obsidian을 iCloud Drive 폴더에서 직접 열면 Windows에서 저장할 때마다 충돌 파일이
생기는 문제가 있습니다. oiiaw는 Obsidian이 항상 순수 로컬 폴더만 보게 하고,
그 로컬 폴더와 실제 iCloud 폴더 사이를 백그라운드에서 안전하게 동기화합니다.

## 설치

1. [Python 3.11 이상](https://www.python.org/downloads/) 설치 (설치 시 "Add python.exe to PATH" 체크)
2. 아래 명령으로 oiiaw 설치:
   ```bash
   pip install oiiaw
   ```
3. 시작 메뉴에서 `oiiaw-setup` 검색 후 실행 (또는 터미널에서 `oiiaw setup`)
   — 로컬 폴더/iCloud 폴더 두 개만 고르고 "설치" 클릭하면 끝. YAML 편집,
   명령줄 사용 전혀 필요 없음. "Windows 시작할 때 자동으로 시작" 체크박스를
   켜두면 그 이후로는 컴퓨터를 켤 때마다 트레이 아이콘이 자동으로 뜸.

## 사용

설치 마법사를 한 번 거치면 평소에는 트레이 아이콘만 보면 됨 (idle=파란색,
동기화 중=주황색, 충돌/에러=빨간색). 아이콘 우클릭으로 로그 폴더 열기/종료
가능.

```bash
oiiaw status    # 지금 실행 중인 데몬의 실시간 상태(동기화 중/충돌 등) 확인
```

### 수동/고급 설정

`config.example.yaml`을 참고해서 직접 `config.yaml`을 만들고
`oiiaw run -c <path>`로 실행할 수도 있음. 마법사가 만든 기본 설정 파일은
`%APPDATA%\oiiaw\config.yaml`에 있음.
