# oiiaw

Obsidian ↔ iCloud, 구름다리처럼 이어주는 Windows용 동기화 도구.

Obsidian을 iCloud Drive 폴더에서 직접 열면 Windows에서 저장할 때마다 충돌 파일이
생기는 문제가 있습니다. oiiaw는 Obsidian이 항상 순수 로컬 폴더만 보게 하고,
그 로컬 폴더와 실제 iCloud 폴더 사이를 백그라운드에서 안전하게 동기화합니다.

## 설치

```bash
pip install -e .
cp config.example.yaml config.yaml   # 경로 확인/수정
oiiaw run
```

## 사용

```bash
oiiaw run       # 백그라운드 동기화 시작
oiiaw status    # 설정 확인 + vault 파일 개수 확인
```
