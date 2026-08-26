# codex-auto3d — Codex CLI 연동 자동 3D 생성 툴 (한국어 가이드)

한 줄 개념(한국어/영어)만 넣으면 아래 네 단계를 사람 개입 없이 자동으로 진행합니다.

```
개념 텍스트 ──▶ ① 프롬프트 작성(Codex) ──▶ ② 이미지 생성(Codex $imagegen / gpt-image-2)
           ──▶ ③ img2threejs 3D 재구성 (Codex가 스킬 실행, 파이썬이 렌더·게이트) ──▶ ④ 리포트·갤러리
```

산출물은 img2threejs 본연의 결과물, 즉 **코드로 된 절차적 Three.js 모델**(TypeScript 팩토리 + ObjectSculptSpec)과
브라우저에서 바로 열어 돌려볼 수 있는 **preview.html**, 정면/측면/후면 턴테이블 스크린샷, 참조 이미지 대비 **비교 시트**,
그리고 작업별 **report.html** 입니다. 메시(GLB) 파일을 다운로드하는 방식이 아니라 img2threejs의 "code-only" 계약을 그대로 따릅니다.

---

## 0. 저장소 구조

이 저장소 하나만 클론하면 파이프라인이 필요로 하는 모든 것이 들어 있습니다.

```
auto3d.py · auto3d/          오케스트레이터 본체 (Python 3.10+ 표준 라이브러리만 사용)
tests/ · examples/ · viewer/ 가짜 Codex 테스트, 배치 입력 예시, 프리뷰 뷰어
node/ · .venv/               setup 이 설치하는 선택 툴체인 (git 무시)
tools/sync_upstream.py       vendor 사본 점검·갱신 도구
vendor/img2threejs/          img2threejs 스킬 사본 — SKILL.md, forge/, grimoire/, docs/,
                             skills/, scripts/ — VENDORED.json 에 커밋이 기록됨
forge, grimoire, docs …      vendor/img2threejs 로 향하는 루트 심볼릭 링크. SKILL.md 와
                             프롬프트가 쓰는 `forge/...` 같은 경로가 저장소 루트에서 그대로 동작
work/                        작업 결과물 (git 무시)
```

서브모듈 대신 사본을 넣은 이유는 클론 한 번으로 끝내기 위해서입니다. 다른 체크아웃(예: 최신
upstream 클론)을 쓰려면 `IMG2THREEJS_ROOT=/경로/img2threejs` 를 지정하세요. `doctor` 가 현재
어떤 스킬 루트를 쓰는지 출력합니다.

사본이 원본과 어긋나지 않게 관리하는 방법:

```bash
python3 tools/sync_upstream.py check                          # VENDORED.json 대비 변경 점검
python3 tools/sync_upstream.py update --from ../img2threejs   # 재복사 + 커밋 기록 갱신
```

`check` 는 로컬에서 수정된 vendor 파일을 모두 나열하고, `update` 는 `--force` 없이는 그 파일을
덮어쓰지 않습니다. 직접 손본 부분이 갱신 과정에서 조용히 사라지지 않습니다.

## 1. 준비물

| 항목 | 설명 |
|---|---|
| Python 3.10+ | img2threejs 자체 요구사항. macOS 기본 python3가 3.9라면 `brew install python@3.11` 후 `python3.11`로 실행 |
| Node.js 18+ / npm | 프리뷰 번들러(esbuild)와 three.js 런타임 설치용 |
| Codex CLI | `npm i -g @openai/codex` 후 `codex login` (ChatGPT 계정). 이미지 생성은 Codex 내장 `image_gen`(gpt-image-2) 도구를 쓰므로 **API 키가 필요 없습니다** |
| (선택) `OPENAI_API_KEY` | `--image-backend api` 또는 `auto`(Codex 실패 시 API 폴백)를 쓸 때만 필요 |

> Codex의 이미지 생성은 텍스트 턴보다 플랜 사용량을 3~5배 빨리 소모합니다. 대량 배치는 API 키 방식도 고려하세요.

## 2. 설치 (최초 1회)

저장소 루트(`codex-auto3d/`)에서 실행합니다.

```bash
python3 auto3d.py setup --link-skill
python3 auto3d.py doctor
```

`setup`은 다음을 수행합니다.

- `node/`에 three / esbuild / typescript 설치 (`npm install`)
- `.venv/`에 Playwright + Chromium 설치 (헤드리스 렌더 캡처용, 약 150MB 다운로드)
- `--link-skill`: `~/.codex/skills/img2threejs`, `~/.agents/skills/img2threejs` 심볼릭 링크 생성 (선택 — 빌드 프롬프트가 `./SKILL.md`를 직접 읽도록 지시하므로 없어도 동작)
- 기본 설정 파일 `auto3d.config.json` 생성

`doctor`는 codex 로그인 상태, imagegen 시스템 스킬, node 런타임, Playwright/Chromium, 사용 중인 img2threejs 스킬 루트와 forge 스크립트 실행 여부를 점검합니다. 모두 `OK`여야 `run`이 정상 동작합니다.

## 3. 사용법

### 단건 실행

```bash
python3 auto3d.py run --prompt "빨간 장난감 로봇, 둥근 머리와 안테나"
```

옵션 예시:

```bash
# 캐릭터(전신 인물/동물) · 최고 품질(모든 패스) · 정면/측면 추가 참조 뷰까지 생성
python3 auto3d.py run -p "노란 원피스를 입은 동화책 소녀 캐릭터" \
    --profile character --quality full --views front,side

# 빠른 초안(형태까지만) · 리뷰 턴 6회 제한 · Codex 모델/추론 강도 지정
python3 auto3d.py run -p "나무 장난감 기차" --quality draft --max-review-turns 6 -m gpt-5.4 --reasoning-effort high

# 이미지까지만 만들고 멈추기 (프롬프트·이미지 확인 후 resume 로 이어가기)
python3 auto3d.py run -p "파란 물뿌리개" --until image
python3 auto3d.py resume --job work/auto3d/20260825-1530-blue-watering-can
```

### 가지고 있는 이미지로 만들기 (`--reference`)

캐릭터 원화·턴어라운드 시트·제품 사진처럼 **이미 이미지가 있을 때**는 생성 단계를 건너뛰고 그
파일을 그대로 참조로 넣습니다. 인테이크 턴이 이미지를 "읽어서" 피사체·프로파일·복잡도·정체성
특징을 정리하므로, Codex 턴 1회만 쓰고 이미지 크레딧은 들지 않습니다.

```bash
# 1) 턴어라운드 시트를 뷰별 참조로 자르기 (도형 분리 → 투명 배경 합성 → 공통 스케일로 배치)
python3 tools/prepare_reference.py sheet.png --split --out work/refs          # contact.png 확인
python3 tools/prepare_reference.py sheet.png --split --out work/refs \
    --views front,hero,side,back,skip

# 2) 그 참조로 3D 빌드
python3 auto3d.py run --reference work/refs/hero.png \
    --view front=work/refs/front.png --view side=work/refs/side.png --view back=work/refs/back.png \
    --reference-camera 35,0 --profile character --quality standard
```

- `--reference` 는 PNG/JPEG 한 장(대표 뷰), `--view 이름=경로` 는 추가 뷰(front|side|back|top)이며
  반복해서 줄 수 있습니다. 각 뷰는 생성 이미지와 똑같은 admission 게이트를 통과해야 하고, hero와
  사실상 같은 그림이면 "새 각도가 아니다"라고 판단해 제외합니다.
- 잡의 첫 렌더에서 **카메라 프레이밍을 참조에 맞춰 자동 보정**합니다(프로브 1장 → 실루엣 bbox 측정 → 마진 보정).
  이게 없으면 렌더가 참조보다 33% 작게 잡혀 Tier-1 스케일 게이트가 형태와 무관하게 실패합니다. 잡당 20초, 결과는 캐시됩니다.
- `--reference-camera 방위각,고도` 는 대표 뷰의 카메라(도 단위)입니다. 0°는 정면, 양수는 카메라가
  피사체의 **왼쪽**으로 도는 방향이라 3/4 뷰는 보통 `35,0`. 생략하면 인테이크 턴이 추정합니다.
- 넣어준 hero가 admission에서 탈락해도 중단하지 않습니다(다시 만들 수 없는 사용자 이미지이므로).
  탈락 사유를 기록하고 그대로 진행합니다.
- `tools/prepare_reference.py` 는 시트를 도형별로 자르고, 투명 배경을 파이프라인 기본 배경(#f2f2f2)
  으로 합성하고, **모든 뷰를 하나의 배율·같은 바닥선**으로 배치합니다. 뷰마다 따로 맞추면 뷰 간
  비율이 달라지고 그 오차가 그대로 3D에 들어갑니다. `--views` 없이 `--split` 만 주면 contact.png와
  다음에 실행할 명령을 출력하니, 그림을 보고 왼쪽부터 이름을 붙이면 됩니다.

### 배치 실행 (CSV / 엑셀 / 텍스트 / JSON)

```bash
python3 auto3d.py batch --file examples/prompts.example.csv
```

- 열 이름은 한글/영문 모두 인식: `개념|concept|prompt`, `이름|name`, `프로파일|profile`, `품질|quality`, `뷰|views`, `복잡도|complexity`, `스타일|style`
- `.xlsx`는 첫 번째 시트를 읽습니다(추가 라이브러리 불필요). `.txt`는 한 줄에 개념 하나(`개념 | 이름`).
- 기본은 오류가 나도 다음 항목으로 계속(`--stop-on-error`로 변경). 결과 요약은 `work/auto3d/batch-<시각>.json`.

### 기타 명령

| 명령 | 역할 |
|---|---|
| `resume --job <dir>` | 중단된 작업 이어가기 (`--restart-stage build`로 빌드만 처음부터) |
| `preview --job <dir>` | Codex 없이 현재 팩토리만 다시 번들·렌더·캡처·게이트 실행 |
| `preview --factory some.ts --reference ref.png` | 아무 img2threejs 팩토리나 단독 프리뷰 |
| `report --job <dir>` / `gallery` | 리포트·갤러리 재생성 |
| `list` | 작업 목록과 상태 |
| `prompt -p "..."` | 이미지 프롬프트만 작성해 보기 (크레딧 소모 없음) |

## 4. 결과물 위치

`work/auto3d/<날짜-시각>-<영문슬러그>/` (work/는 git에서 무시됨)

```
report.html / report.json      ← 작업 요약 리포트 (여기서 시작하세요)
preview/preview.html           ← 인터랙티브 3D 프리뷰 (드래그 회전, 1~5 키로 뷰 전환, R 턴테이블, W 와이어프레임)
preview/captures/*.png         ← hero · 0/90/180/270° · 오빗 · 상단 · 헤드 클로즈업 캡처
preview/cmp.png                ← 참조 vs 렌더 비교 시트 (make_comparison_sheet.py)
preview/history/turn-NN-*.png  ← 패스(턴)별 비교 시트 진행 기록
preview/gates/*.json           ← turntable_gate · self_intersection · diagnose_render · interior_difference 결과
preview/render-manifest.json   ← forge render_bridge 형식의 캡처 증거
src/create<Name>Model.ts       ← 생성된 Three.js 팩토리 (최종)
object-sculpt-spec.json        ← ObjectSculptSpec (reviewHistory 포함)
reference/hero.png (+front…)   ← 생성된 참조 이미지와 admission 판정
prompt/prompt.json             ← 작성된 이미지 프롬프트·카메라·정체성 특징
codex/*.events.jsonl           ← 각 Codex 턴의 전체 이벤트 로그(명령·출력·토큰)
job.json · auto3d.log          ← 작업 상태와 로그
```

전체 갤러리: `work/auto3d/index.html`.

## 5. 동작 원리 (왜 이렇게 나눴나)

img2threejs의 원칙("스크립트가 강제하고, 모델은 판단한다")을 그대로 따릅니다.

1. **프롬프트 작성** — Codex(read-only, ephemeral)가 개념을 3D 복원에 적합한 이미지 프롬프트로 바꿉니다: 단일 피사체, 균일한 연회색 배경, 3/4 뷰(카메라 방위각 35°, 고도 15°), 균일 조명, 텍스트/워터마크 금지, 재질을 PBR 용어로 서술. `--prompt-author template`을 쓰면 LLM 없이 템플릿으로 작성합니다.
2. **이미지 생성** — `codex exec` + `$imagegen`(내장 `image_gen`, gpt-image-2)로 생성하고 작업 폴더로 복사합니다. 생성 직후 forge의 `check_reference_admission.py`로 참조 적합성(전경 비율·실루엣 응집도·해상도)을 검사하고, 탈락하면 이유를 프롬프트에 반영해 최대 3회 재생성합니다. 추가 뷰(`--views`)는 같은 Codex 스레드를 이어 받아(hero를 참조로) 생성합니다.
3. **3D 빌드 루프** — 카메라를 우리가 정해서 이미지를 만들었으므로 `referenceCamera`(yaw/pitch)를 정확히 알려줄 수 있습니다.
   - 턴 1: Codex가 `SKILL.md`대로 상태 초기화 → 이미지 분석 → 사전 평가 → 디테일 인벤토리 → 스펙 작성 → strict 검증 → blockout 팩토리 생성 후 JSON(`stage=factory-ready`)으로 보고.
   - 파이썬: 팩토리를 esbuild로 번들 → 헤드리스 Chromium 렌더 → 12개 뷰 캡처 → 비교 시트 → 결정적 게이트(turntable/self-intersection/Tier-1/interior-difference/tsc) 실행.
   - 턴 N: 비교 시트를 이미지로 첨부해 Codex가 자기 vision으로 리뷰하고 `append_review.py`로 기록, `continue`면 다음 패스를 생성, 아니면 spec/code를 수정 → 다시 렌더. 목표 패스(`--quality`: draft=form-refinement, standard=material-pass, full=optimization-pass)가 `continue`를 받으면 종료.
   - 오케스트레이터는 Codex의 보고를 그대로 믿지 않고 스펙의 `reviewHistory`와 팩토리 해시로 진행 상황을 검증합니다. 리뷰 턴 상한(`max_review_turns`), 패스당/전체 교정 상한(state.py), 작업 시간 상한을 넘으면 마지막 리뷰만 기록하고 `partial`/`blocked`로 마감합니다.
4. **리포트** — report.html(참조 vs 렌더, 턴테이블, 게이트 판정, 리뷰 점수 추이, 토큰 사용량)과 갤러리 index.html.

## 6. 설정

저장소 루트의 `auto3d.config.json` (예시: `auto3d.config.example.json`). 우선순위: 기본값 < 설정 파일 < 환경변수 `AUTO3D_<KEY>` < CLI 플래그(`--set key=value` 포함).

자주 쓰는 키: `model`, `reasoning_effort`, `quality`, `views`, `image_backend`, `image_size`, `max_review_turns`, `turn_timeout_min`, `job_timeout_min`, `sandbox`(기본 `workspace-write`; Codex가 브라우저 등 외부 도구를 직접 써야 할 때만 `danger-full-access`), `network_in_sandbox`.

## 7. 비용·시간 감

- 이미지 1장 + 추가 뷰 n장: Codex 플랜 사용량(이미지 턴 가중) 또는 API 과금(gpt-image-2, 장당 수 센트~수십 센트).
- 3D 빌드(실측): 캐릭터 1종을 blockout까지 돌린 실제 실행에서 **신규 입력 약 100만 토큰, 출력 약 16.5만**이 들었습니다.
  캐시된 입력을 포함한 스레드 누적 입력은 4,300만이지만 그중 98%는 캐시 재사용분입니다. `vendor/img2threejs/docs/TOKEN_COST.md`
  의 8만~35만은 사람이 스킬을 직접 몰 때의 수치로, 무인 파이프라인은 리뷰 턴마다 스레드를 이어가므로 훨씬 큽니다.
- 시간: 이미지 1~3분, 빌드는 캐릭터 기준 **패스 하나에 1~2시간**을 잡으세요(첫 턴의 인테이크·스펙 작성만 40~60분).
  렌더·캡처는 턴당 30~40초, 프레이밍 보정은 잡당 20초 한 번입니다.

## 8. 문제 해결

| 증상 | 조치 |
|---|---|
| `doctor`에서 `codex login FAIL` | `codex login` 실행(브라우저 인증). 회사 프록시 환경이면 `codex login --with-api-key` |
| 이미지가 생성되지 않음 (`Codex did not produce a usable PNG`) | `work/.../codex/image-hero-1.events.jsonl` 확인. imagegen 스킬 비활성/플랜 한도 초과가 대부분. `--image-backend api`로 우회 가능 |
| 참조 이미지가 admission에서 반복 탈락 | 개념을 더 구체적으로(단일 물체, 배경 없음). `--image-size 1536x1536` 등으로 해상도 상향 |
| Codex가 `blocked`로 일찍 끝남 | 대개 strict-quality(스펙이 얕음). 오케스트레이터가 1회 자동 재시도하며, 그래도 안 되면 `resume --restart-stage build --reasoning-effort high` |
| 렌더 실패(`esbuild failed` / factory runtime error) | Codex에게 오류 전문을 넘겨 수정 턴을 돌립니다(리뷰 턴 1회 소모). 계속 실패하면 `preview --job`으로 재현 후 팩토리 확인 |
| macOS에서 Chromium 실행 실패 | `.venv/bin/python -m playwright install chromium` 재실행 |
| 파이썬 3.9 오류(`X | None` 등) | Homebrew python 3.10+ 로 실행 |

## 9. 한계와 정직한 안내

- 단일 이미지(추가 뷰 포함해도 2~4장)로는 뒷면·숨은 구조를 확정할 수 없습니다. 파이프라인은 대칭 미러링과 신뢰도 기록으로 처리하며, 리포트에 그대로 남깁니다.
- 결과 품질은 Codex 모델의 vision 판단과 스펙 작성 품질에 좌우됩니다. 정밀 재현이 목표라면 `--quality full`, `--views front,side,back`, `--reasoning-effort high`를 권장합니다.
- 이 툴은 `vendor/img2threejs/` 의 forge 스크립트와 SKILL.md 계약을 그대로 사용합니다. `sync_upstream.py update` 로 스킬 사본을 올리고 나면 프롬프트 템플릿(`auto3d/prompts.py`)의 스크립트 플래그가 여전히 맞는지 `tests/`로 확인하세요.
- 검증 범위: 오케스트레이션·렌더·게이트·리포트는 가짜 Codex(`tests/fake_codex.py`)로 엔드투엔드 검증했습니다. 실제 Codex 로그인 환경에서의 완주는 사용자 환경에서 `doctor` → `run --until image` → `run`(또는 `resume`) 순으로 확인해 주세요.

## 10. 테스트

```bash
cd tests
python3 -m unittest -v            # 브라우저·node 런타임이 없으면 해당 테스트는 자동 skip

cd ..
python3 tools/sync_upstream.py check        # vendor 사본이 기록된 커밋과 일치하는지
python3 tools/vendored_forge_tests.py       # vendor 안의 forge 테스트 약 1,080건
```

`vendored_forge_tests.py` 는 업스트림 저장소 자체의 포장(.gitignore·git 인덱스, 사용하지 않는 vision
통합)을 검사하는 두 건만 제외하고 전부 돌립니다. 제외 목록과 이유는 `--list-excluded`, 그래도 다
돌리려면 `--all`.
