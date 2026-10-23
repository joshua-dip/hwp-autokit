# hwp-autokit

한컴 한글(HWP/HWPX)을 Windows COM 으로 **대량·무인 자동화**할 때 부딪히는 함정을 코드로 막아 둔 작은 파이썬 라이브러리입니다.
수천 개 문서를 실제로 돌리며 겪은 실패(조용히 사라지는 파일, 깨지는 gen_py 캐시, 서로를 끊는 병렬 실행, 영원히 멈추는 호출)를 정리했습니다.

> English documentation follows the Korean section. / 영어 설명은 아래에 있습니다.

## 왜 필요한가

한글 COM 자동화는 "한 파일을 한 번 여는" 예제로는 잘 됩니다. 문제는 수백·수천 개를 밤새 돌릴 때입니다.

- 저장 경로가 260자를 넘으면 한글은 **저장에 실패하고도 성공을 돌려줍니다.** 로그엔 "12/12 저장", 폴더는 비어 있습니다.
- 두 파이썬 프로세스가 win32com `gen_py` 캐시를 동시에 만들면 캐시가 반쯤 깨지고, 이후 **모든** 실행이 `has no attribute 'MinorVersion'` 으로 실패합니다. 같은 프로세스 안에서는 절대 낫지 않습니다.
- 한 Windows 세션에서 한글 작업을 두 개 동시에 돌리면 서로의 연결을 끊거나, 더 나쁘게는 **오류 없이 결과가 틀립니다**(30쪽 PDF 가 2쪽으로).
- 한글은 반드시 죽습니다(평균 수십 파일마다). 죽은 뒤엔 `RPC 서버를 사용할 수 없습니다` 로 이후 전부가 연쇄 실패합니다.
- 어떤 호출은 오류도, 보이는 대화 상자도 없이 **영원히 멈춥니다.**

hwp-autokit 는 이 각각에 대한 방어를 작은 함수로 제공합니다.

## 설치

```bash
pip install hwp-autokit            # 아직 PyPI 미공개 — 지금은 소스에서: pip install .
pip install "hwp-autokit[pdf]"     # PDF 쪽 검사까지 (PyMuPDF)
```

- Windows + 한컴오피스 한글(2022 에서 시험) + Python 3.9 이상
- `pywin32`, `Pillow` 는 자동 설치됩니다.
- 순수 파이썬 부분(대기열·잠금·경로 검사·쪽 넘침 계산)은 다른 OS 에서도 동작합니다.

환경 점검:

```bash
hwp-autokit doctor
```

## 빠른 시작

```python
from hwp_autokit.session import HwpSession
from hwp_autokit.text import insert_text, insert_marked

with HwpSession(genpy_cache="my-tool") as s:      # 이 도구 전용 gen_py 캐시
    with s.watchdog(120):                          # 멈춘 호출은 120초 뒤 내 한글만 종료
        insert_text(s.hwp, "제목\n")
        insert_marked(s.hwp, "본문 <b>굵게</b> 와 <u>밑줄</u>")
        s.save_as(r"C:\out\hello.hwp")             # 길이 사전 검사 + 디스크 실물 확인
```

여러 작업이 한글을 번갈아 쓰게 하기:

```python
from hwp_autokit.lock import BuildQueue

with BuildQueue(r"C:\build\queue").turn("job-42", priority=5):
    ...  # 이 블록 안에서만 한글을 씁니다
```

PowerShell 래퍼도 같은 대기열에 줄을 섭니다: `scripts/hwp_queue.ps1` (예시 `examples/queue_wrapper.ps1`).

쪽 검사(빈 쪽·덜 찬 쪽·쪽 수):

```bash
hwp-pagecheck 결과폴더 --expect 30
hwp-pagecheck 결과폴더 --col 0-0.72 --band 0.04-0.88 --low 0.4   # 오른쪽 사이드바·아래 범례 제외
```

## 구성

| 모듈 | 하는 일 |
|---|---|
| `session` | 한글 COM 세션. 도구별 `gen_py` 캐시 격리, 시작 재시도(처음 한 번만 정리, 이후엔 기다리기만), 죽으면 재시작 후 재시도, N 개마다 선제 교체, 내가 띄운 PID 만 종료하는 안전 종료, 멈춘 호출을 끊는 watchdog |
| `text` | 평문 삽입, `<b><i><u>` 표시 삽입(서식 토글 방식), HTML 삽입, 평문+HTML 클립보드를 합쳐 밑줄을 살리는 추출 |
| `lock` | `FileLock`(죽은 주인 자동 해제), `BuildQueue`(우선순위 0~9, 선입선출, 대기열 밖 한글이 떠 있으면 기다림 — 죽이지 않음) |
| `paths` | 경로 길이 사전 검사, 긴 파일 이름 안전 축약, 저장 후 실물·크기·시각 확인, `\\?\` 로 디스크 파일 직접 세기 |
| `overflow` | PDF·HWP 를 쪽 그림으로 그려 빈 쪽·덜 찬 쪽·깨진 글자·쪽 수 검사 (PyMuPDF / 한글 / rhwp) |
| `cli` | `hwp-autokit doctor · pagecheck · queue · paths · genpy`, `hwp-pagecheck` |

## 문제 해결 — 실제로 겪은 함정

### 1. gen_py 캐시 손상 — `has no attribute 'MinorVersion'` / `'CLSIDToClassMap'`
- **원인**: win32com 은 기본적으로 `%TEMP%\gen_py\<파이썬버전>` 하나를 모든 프로세스가 같이 씁니다. 두 도구가 동시에 만들면 반쯤 쓰인 모듈이 남습니다. 한글을 강제 종료한 직후에도 생길 수 있습니다.
- **증상**: 첫 파일부터 전부 실패. 로그 첫 줄에 `Rebuilding cache` 가 보이면 이미 깨진 것입니다.
- **대책**: 도구마다 전용 캐시 — `HwpSession(genpy_cache="도구이름")` 또는 자식 프로세스를 `isolated_env()` 로 띄우기(`TMP`/`TEMP` 를 바꾸면 win32com 이 그 밑에 캐시를 만듭니다).
- **복구**: 그 캐시를 쓰는 프로세스를 **모두 멈춘 뒤** `hwp-autokit genpy rebuild --name 도구이름`. 이미 깨진 모듈을 import 한 프로세스는 디스크를 지워도 낫지 않으므로 재구축은 별도 프로세스에서 합니다. 공용 기본 캐시는 다른 도구가 쓰고 있을 수 있어 이 명령은 지우지 않습니다.
- 자동 "캐시 지우고 재시도" 를 믿지 마세요. 첫 재시도에서도 같은 오류면 즉시 멈추고 수동 재구축이 빠릅니다.

### 2. 260자 경로 — 조용한 손실
- 한글은 경로가 260자를 넘으면 저장하지 못하면서 COM 으로는 **성공**을 돌려줍니다.
- `save_as()` / `safe_save()` 는 저장 **전에** 길이를 검사하고(기본 한도 250), 저장 **후에** 파일이 실제로 있고 비어 있지 않으며 이번 저장 시각인지 확인합니다.
- 파일 이름을 이어 붙여 만드는 경우 `fit_filename()` 으로 축약하세요(앞부분 + `~` + 해시 6자리, 같은 입력은 같은 이름).
- 작업이 끝나면 **로그가 아니라 디스크를 세세요**: `hwp-autokit paths scan 폴더 --pattern "*.hwp"`. 긴 경로 파일은 탐색기·많은 도구에서 안 보이므로 `\\?\` 접두어로 셉니다.
- 레지스트리 `LongPathsEnabled` 에 기대지 마세요(한글에서 효과를 확인하지 못했습니다). 출력 폴더를 짧게 두는 것이 가장 확실합니다.

### 3. 병렬 실행 — 서로 끊기, 조용히 틀린 결과
- 한 Windows 세션에서 한글 작업은 **한 번에 하나**만 돌리세요. 실측: 2건 동시에 둘 다 "성공" 했지만 한쪽 PDF 가 30쪽 → 2쪽(오류 없음), 4건 동시엔 3건이 멈춤. 속도 이득도 1.3배뿐이었습니다.
- 한글을 쓰지 않는 단계(데이터 준비·검사·포장)는 얼마든지 겹치고, 한글 단계만 `BuildQueue` 로 줄 세우세요. 진짜 동시 처리는 PC 를 늘려서 합니다.
- 대기열은 대기열 밖에서 떠 있는 `Hwp.exe` 를 **기다리고 죽이지 않습니다.** 남의 한글을 죽이면 그쪽 작업이 날아갑니다. 창 없는 고아 `Hwp.exe` 가 남으면 대기열이 막히니, 주인이 확실한 것만 PID 로 정리하세요.
- `RPC 서버를 사용할 수 없습니다`(-2147023174)·`서버 실행이 실패했습니다`(-2146959355) 가 나면: 다른 한글 작업이 도는지 확인 → 잠깐 기다림 → 재시도. 매 재시도마다 강제 종료하면 뜨는 중인 한글을 계속 죽여 같은 오류가 무한 반복됩니다(이 라이브러리는 첫 실패에만 정리하고 6·10·14·18·22초 기다립니다).
- 정상 문서에서 텍스트가 15~45자만 나오는 것은 파일 손상이 아니라 **한글이 죽어 가는 징후**일 때가 많습니다. `SuspectOutputError` 를 던지면 `session.call()` 이 재시작 후 재시도합니다. 크래시를 기다리지 말고 200개쯤마다 선제 교체(`recycle_every`)하세요.

### 4. 보안 모듈 — "파일에 접근하려고 합니다" 승인 창
- 자동화로 파일을 열고 저장할 때마다 한글이 접근 허용을 묻는 창을 띄워 무인 실행이 멈춥니다.
- 한컴이 배포하는 자동화용 보안 모듈 DLL(FilePathCheckerModule)을 받아 두고, 레지스트리 `HKEY_CURRENT_USER\SOFTWARE\HNC\HwpAutomation\Modules` 에 문자열 값(이름 예: `FilePathCheckerModule`, 데이터: DLL 전체 경로)을 만든 뒤, 코드에서 `RegisterModule("FilePathCheckDLL", "<그 값 이름>")` 을 부릅니다.
- `HwpSession(security_module=("FilePathCheckDLL", "FilePathCheckerModule"))` 처럼 값 이름을 맞춰 주세요(기본값은 `SecurityModule`). 등록에 실패하면 경고 로그를 남기고 `session.security_registered` 가 `False` 가 됩니다. `hwp-autokit doctor` 가 등록 상태와 DLL 존재를 보여 줍니다.

### 5. 영원히 멈추는 호출 · HTML 삽입 (한컴오피스 2022 에서 확인)
- `SetTextFile(data, "HTML" 또는 "TEXT", "insertfile")`, `.htm` 파일 `Insert`, CF_HTML 붙여넣기는 오류도 보이는 창도 없이 **멈춥니다.** `"UNICODE"` 형식 `insertfile` 은 됩니다.
- 위험한 호출은 `with session.watchdog(초):` 로 감싸세요. 시간이 지나면 **내가 띄운 한글만** 종료해 호출을 풀고 `HwpTimeout` 을 던지며, 다음 사용 때 새 한글이 뜹니다.
- 문서 전체를 HTML 로 바꾸는 `SetTextFile(html, "HTML", "")` 은 됩니다(`set_html`). 커서 위치 삽입(`insert_html`)은 임시 문서에 넣고 한글 자체 복사·붙여넣기로 옮깁니다.
- 한글 HTML 가져오기는 **인라인 태그 경계의 공백을 지우고**(`<u>a</u> b` → `ab`), `<meta charset="utf-8">` 이 있으면 **한글을 깨뜨립니다.** 글자가 정확해야 하면 `insert_marked()`(서식 토글 + InsertText)를 쓰세요.

### 6. 밑줄을 살린 텍스트 추출
- `Copy` 하면 클립보드에 평문과 HTML 이 같이 올라옵니다. 밑줄은 **HTML 에만**, 자동 번호(`1.` `2.`)·글머리표(`★`)는 **평문에만** 있습니다. HTML 만 쓰면 번호 항목 26개가 3개로 뭉개진 적이 있습니다.
- `copy_document_text()` 는 평문을 뼈대로 HTML 의 밑줄 조각을 순서대로 이식합니다. 클립보드는 간헐적으로 잠기므로 읽기를 재시도합니다(사용자 클립보드를 덮어씁니다).

### 7. 그 밖에
- 콘솔이 cp949 이면 로그에 이모지·특수 문자가 있을 때 `UnicodeEncodeError` 로 죽습니다. 캐시 문제가 아닙니다 — `PYTHONIOENCODING=utf-8` 로 실행하세요(`isolated_env()` 가 기본으로 넣어 줍니다).
- 시험 출력은 짧은 경로에 두세요. 깊은 임시 폴더(에이전트 작업 폴더 등)는 260자를 쉽게 넘습니다.
- rhwp(한글 없이 HWP 를 그리는 오픈소스 렌더러)는 대기열 없이 병렬로 쪽 검사를 할 수 있지만, **덧말(dutmal)이 붙은 낱말을 통째로 빠뜨립니다.** `--engine auto` 는 덧말이 있으면 한글로 그립니다. rhwp 는 배율 1(96dpi) 이상으로 그려야 가는 대체 글꼴 때문에 생기는 거짓 "덜 참" 경보를 피합니다.

## 쪽 검사 원리

- 빈 쪽: 머리·꼬리를 뺀 본문 띠(`--band`)의 잉크 비율이 0.15% 미만
- 덜 찬 쪽: 마지막 쪽이 아닌데 **진한 글자**(밝기 150 미만)가 `--low` 보다 위에서 끝남. 연한 상자·밑줄 칸까지 세면 덜 찬 쪽을 놓치므로 글자 끝으로 잽니다.
- `--col` 은 잴 가로 범위입니다. 늘 차 있는 사이드바가 있는 짜임은 본문 칸만 재세요. 처음 쓰는 짜임은 좋은 것·나쁜 것 한 벌씩으로 문턱부터 맞추세요.

## 시험

```bash
pip install -e ".[test,pdf]"
pytest                                   # 한글 없이 도는 순수 시험
set HWP_AUTOKIT_COM_TESTS=1 && pytest tests/test_com.py   # 한글을 실제로 띄움 — 다른 한글 작업이 없을 때만
```

## 라이선스

MIT — `LICENSE` 참고.

---

# hwp-autokit (English)

A small Python library that turns hard-won lessons about **unattended, large-batch automation of Hancom Hangul (HWP/HWPX) over Windows COM** into code: verified saves, an isolated win32com cache, a serial build queue, crash-tolerant sessions, and a page-overflow checker.

## The problem

Hangul automation works fine in a one-file demo. It breaks when you run thousands of documents overnight:

- If the target path exceeds 260 characters, Hangul **fails to save but reports success** over COM. Your log says "12/12 saved"; the folder is empty.
- Two Python processes generating the win32com `gen_py` cache at once leave it half-written, and **every** later run fails with `has no attribute 'MinorVersion'`. It never heals inside the same process.
- Two Hangul jobs in one Windows session cut each other's connection, or worse, **produce wrong output without any error** (a 30-page PDF came out as 2 pages).
- Hangul *will* crash (every few dozen files on average). After that, every call fails with `The RPC server is unavailable`.
- Some calls **block forever** with no error and no visible dialog.

## Install

```bash
pip install hwp-autokit            # not on PyPI yet - from source: pip install .
pip install "hwp-autokit[pdf]"     # adds PDF page checks (PyMuPDF)
```

Requirements: Windows, Hancom Office Hangul (tested with 2022), Python 3.9+. The pure parts (queue, lock, path checks, overflow math) run on any OS. Check your setup with `hwp-autokit doctor`.

## Quick start

```python
from hwp_autokit.session import HwpSession
from hwp_autokit.text import insert_text, insert_marked
from hwp_autokit.lock import BuildQueue

with BuildQueue(r"C:\build\queue").turn("job-42"):         # one Hangul job at a time
    with HwpSession(genpy_cache="my-tool") as s:             # private gen_py cache
        with s.watchdog(120):                                 # unblock stuck calls
            insert_text(s.hwp, "Title\n")
            insert_marked(s.hwp, "Body with <b>bold</b> and <u>underline</u>")
            s.save_as(r"C:\out\hello.hwp")                    # length pre-check + on-disk check
```

```bash
hwp-pagecheck out_folder --expect 30
hwp-pagecheck out_folder --col 0-0.72 --band 0.04-0.88 --low 0.4   # skip right sidebar and bottom legend
hwp-autokit paths scan out_folder --pattern "*.hwp"               # count what is really on disk
hwp-autokit queue run C:\build\queue --tag job-42 -- python build.py
```

PowerShell pipelines can join the same queue with `scripts/hwp_queue.ps1` (see `examples/queue_wrapper.ps1`).

## Modules

| Module | Purpose |
|---|---|
| `session` | COM session: per-tool `gen_py` cache, start retries (clean up once, then only wait), restart-and-retry on a dead server, proactive recycling every N documents, safe quit that only kills the PID it started, `watchdog()` for blocking calls |
| `text` | plain insert, `<b><i><u>` insert via formatting toggles, HTML insert, plain+HTML clipboard extraction that keeps underlines |
| `lock` | `FileLock` (stale owners broken automatically) and `BuildQueue` (priorities 0-9, FIFO, waits for - never kills - Hangul outside the queue) |
| `paths` | path-length pre-checks, stable filename shortening, post-save existence/size/mtime verification, counting files via `\\?\` |
| `overflow` | render PDF/HWP pages and detect empty / under-filled pages, missing glyphs, wrong page counts (PyMuPDF, Hangul or rhwp) |
| `cli` | `hwp-autokit doctor / pagecheck / queue / paths / genpy`, `hwp-pagecheck` |

## Troubleshooting (the gotchas we actually hit)

**gen_py cache corruption** (`has no attribute 'MinorVersion'` / `'CLSIDToClassMap'`). The default cache is shared by every process; concurrent generation (or a killed Hangul) leaves a half-written module. Give each tool its own cache (`HwpSession(genpy_cache="name")`, or launch children with `isolated_env()`; win32com follows `TMP`/`TEMP`). To repair: stop all processes using that cache, then `hwp-autokit genpy rebuild --name name`. The rebuild runs in a child process because a process that imported a broken module keeps using it whatever is on disk. It refuses to wipe the shared default cache. Don't trust automatic "clear cache and retry": if the first retry fails the same way, stop and rebuild by hand.

**260-character silent loss.** `save_as()`/`safe_save()` refuse paths over the budget (default 250) *before* saving and verify *after* saving that the file exists, is non-empty and is fresh. Build long names with `fit_filename()`. At the end of a batch, count files on disk, not log lines. Don't rely on `LongPathsEnabled` for Hangul (we could not confirm it helps); keep output folders short.

**Parallel instances.** Run one Hangul job per Windows session. Measured: 2 at once both "succeeded" but one PDF lost 28 of 30 pages silently; 4 at once mostly hung; the speed-up was only 1.3x. Overlap the non-Hangul steps and queue the Hangul step; scale out with more machines. The queue waits for, and never kills, `Hwp.exe` processes outside it. Killing someone else's Hangul destroys their work. On `RPC server is unavailable` / `Server execution failed`: check for competing jobs, wait, retry. Killing on every retry keeps killing the instance that is starting up, and the error repeats forever. This library cleans up only after the first failure, then waits 6/10/14/18/22 s. A tiny result from a normal document is usually a dying instance, not a corrupt file: raise `SuspectOutputError` and `session.call()` restarts and retries. Recycle proactively (`recycle_every=200`).

**Security module / file-access prompt.** Without it, Hangul asks for permission on every automated open/save and an unattended run stalls. Install Hancom's automation security module DLL (FilePathCheckerModule), add a string value under `HKEY_CURRENT_USER\SOFTWARE\HNC\HwpAutomation\Modules` (name e.g. `FilePathCheckerModule`, data = full DLL path), and pass the same value name: `HwpSession(security_module=("FilePathCheckDLL", "FilePathCheckerModule"))` (default name: `SecurityModule`). `hwp-autokit doctor` shows what is registered.

**Calls that block forever, and HTML import** (seen on Hancom Office 2022). `SetTextFile(data, "HTML"|"TEXT", "insertfile")`, `Insert` of an .htm file and pasting CF_HTML all block with no error; `"UNICODE"` with `insertfile` works. Wrap risky calls in `session.watchdog(seconds)`, which kills only our instance, raises `HwpTimeout`, and gives you a fresh Hangul on next use. Whole-document `SetTextFile(html, "HTML", "")` works (`set_html`); `insert_html` goes through a scratch document plus Hangul's own copy/paste. The importer drops whitespace at inline-tag boundaries and garbles Korean if the HTML declares `<meta charset="utf-8">`, so use `insert_marked()` when exact text matters.

**Extracting text with underlines.** `Copy` puts plain text and HTML on the clipboard. Underline exists only in the HTML; automatic numbering and bullets exist only in the plain text (HTML-only extraction once collapsed 26 items into 3). `copy_document_text()` grafts the HTML's underlined fragments onto the plain text, retrying because the clipboard is intermittently locked. It overwrites the user's clipboard.

**Other.** A cp949 console crashes on emoji in logs (`UnicodeEncodeError`), which is not a cache problem: set `PYTHONIOENCODING=utf-8`. Keep test outputs on short paths. The open-source `rhwp` renderer can check pages without Hangul and in parallel, but it drops words carrying ruby text (덧말), so `--engine auto` falls back to Hangul for such documents. Render rhwp at scale >= 1 (96 dpi) to avoid false "under-filled" alarms from thin fallback fonts.

## How the page check works

- *Empty*: less than 0.15% ink inside the body band (`--band`, header/footer excluded).
- *Under-filled*: a non-last page whose **dark text** (luminance < 150) ends above `--low`. Measuring all ink would let light boxes and answer lines hide the problem.
- `--col` restricts the horizontal range (exclude an always-full sidebar). Calibrate on one good and one bad sample first.

## Tests

```bash
pip install -e ".[test,pdf]"
pytest                                       # pure tests, no Hangul needed
HWP_AUTOKIT_COM_TESTS=1 pytest tests/test_com.py   # launches Hangul - only when nothing else uses it
```

## License

MIT, see `LICENSE`.
