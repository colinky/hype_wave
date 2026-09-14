# YouTube Music ID 안정화 — 실제 적용·최종 검증 결과

검증일: 2026-09-15 KST · 최신 제품 revision: `33321aacbc9f3b888bd3b8e91b20707d5309cca7` · 운영 DB 적용 revision: `e7025de53365486ac434539f07b95770db3b1605`

**최종 판정: NO-GO. 재발 방지 코드는 main에 배포했고 DB 24건은 적용·검증했지만, 운영 목록과 공개 history의 전체 복구는 완료하지 못했다.**

07:14 KST 실제 재조회에서 운영 DB 복구 상태와 11개 목록을 확인했다. 첫 Apple 200곡 목록은 교정 도중 보류 상태이고, 다른 10개 목록과 공개 history는 수정 전 상태다. 미완료 복구가 남아 일반 동기화의 추가 게시도 차단된다. 아래의 격리 시험 성공을 운영 전체 성공으로 간주하지 않는다.

## 구현·배포와 통과한 검증

- 정상 기존 대표 ID를 보존하고, 차트의 ATV 관계·검색 결과·최신 수정 시각만으로 교체하지 않는다.
- 인증된 실행 계정과 정상 기준 곡을 확인한 뒤 재생 가능·재생 불가·판정 불가를 구분한다. 인증 실패, 통신 문제, 상충하는 응답은 잘못된 재매칭이나 삭제의 근거로 쓰지 않는다.
- 곡 동일성, 저장 결과, 후처리, 11개 출력 및 history의 일치를 검사한다. 완료된 입력만 사용하고 과거 실패 시도를 가짜 성공으로 바꾸지 않는다.
- 목록 변경은 요청·성공 응답·항목 토큰을 기록한다. 최신 보강은 누락 곡을 최대 50개씩 먼저 추가하고, 각 청크의 실제 ID·토큰·순서·재생을 확인한 후에만 기존 항목 제거를 허용한다. 명시적 복구도 같은 경로를 사용한다.
- 거절된 ID를 복구 명령으로 다시 추가하거나, 성공 응답을 잃은 작업을 현재 목록의 ID만 보고 완료하는 경로를 차단했다. 소유권이 확인된 새 항목만 제거하여 정상 기존 항목을 보존하는 복구는 유지한다.

| 검증 | 결과와 범위 |
|---|---|
| 오프라인 회귀 및 깨끗한 커밋 사본 | 584개 통과. 운영 게시 성공의 대체 증거는 아님 |
| main 배포 후 Actions | [회귀 실행 성공](https://github.com/colinky/hype_wave/actions/runs/34898198902), [Pages 실행 성공](https://github.com/colinky/hype_wave/actions/runs/34898197682). 이 Pages 실행은 교정된 history 배포가 아님 |
| Actions 인증 재생 진단 | 동일 revision의 55개 ID 두 차례 관측에서 각각 47 playable·8 unavailable. 비인증 반례는 unknown 유지 |
| 고정 입력 전체 두 회차 | 각 회차 원본 950항목·고유 원본 947개, 총 1,894개 새 시도. 후처리 변경 0. 11개 출력과 이전 history 행 보존 검사 통과. 운영 목록 쓰기는 없는 격리 시험 |
| 검색 실패 처리 | Indie 2곡의 실제 검색 실패를 실패로 보존. 임의 대체 영상으로 성공 처리하지 않음 |
| PostgreSQL 격리 검증 | 24건 적용·롤백·재개 검사 통과. 최종 실제 PG 명세의 정확한 적용/롤백 14개와 독립 검토 18개 통과 |
| 운영 DB 적용 | 24건/27개 결정 commit 후 독립 검증 12개 통과. 07:14 재조회도 동일 복구 상태 확인 |
| 실제 11개 운영 목록 | **미통과.** 4개는 애초 교정이 필요 없어 목표와 일치. 첫 Apple 목록 1개는 부분 교정, 나머지 교정 필요 목록 6개는 미적용 |
| 공개 history | **미적용.** 로컬과 공개 파일 모두 이전 SHA 유지 |
| 복구 후 실제 일반 전체 동기화 | **미실행.** 공개 표면 검증 완료가 선행 조건 |
| 실제 기기의 소리·재생 진행 | **미확인.** API 재생 판정과 구분. 잠긴 기기 및 로그인 화면을 실제 청취 검증으로 대체하지 않음 |

위 표의 584개 검사·Actions 재생 진단·고정 입력 두 회차·운영 DB 적용은 `e7025de`에서 수행한 결과다. 이후 공통 게시·복구 보호를 `33321aa`로 추가 배포했다. 최신 코드의 전체 회귀와 깨끗한 커밋 사본은 각각 **597개 통과**, 독립 SQLite 검증은 **13개 경계 및 관련 회귀 85개 통과**다. [최신 Actions 회귀](https://github.com/colinky/hype_wave/actions/runs/34905688447)와 [Pages 배포](https://github.com/colinky/hype_wave/actions/runs/34905684794)도 성공했다. 검사 범위가 겹치므로 이 숫자들을 합산하지 않는다. 최신 Pages 역시 교정 history를 설치한 배포가 아니다.

고정 입력 두 회차의 history 내용은 같았지만 `generated_at`이 달라 파일 바이트 전체가 같았던 것은 아니다. 원래 17,833개 매칭 시도와 기존 수동 정책 2개를 보존했다. 미래의 일간·월요일 예약 실행을 실제 관측했다고 주장하지 않는다.

## 제보한 다섯 곡

다음 ID는 DB 복구 선택이며, 07:14 KST 인증된 로컬 환경에서 exact ID·OK·오디오 존재를 다시 확인했다. **모든 운영 목록에서 이 ID로 교체가 끝났다는 뜻은 아니다.**

| 곡 | 확인한 정상 ID | 재조회 |
|---|---|---|
| REDRED | `5sOgJQ3N03Q` | playable |
| Pop Off Pop Off | `QbsbqekMkCU` | playable |
| RUDE! | `Q4AE3ub4nBM` | playable |
| 404 (New Era) | `MUny_GDYIDM` | playable |
| Less than a Lover | `z9ifbheDGFM` | playable |

기존 불가 ID 7개도 같은 관측에서 다시 unavailable로 확인했다. MAGIC은 마지막 진단 입력의 대소문자 오류를 독립 검토에서 발견했다. 잘못 조회한 `ag-LdVKMgiU` 기록은 보존하고, 실제 명세의 `Ag-LdVKMgiU`를 07:17에 별도 조회하여 exact ID·UNPLAYABLE을 확인했다. 두 기록을 합쳐 원래 한 번의 정확한 8개 조회였다고 보고하지 않는다.

## 운영 검증에서 추가로 확인한 원인

기존 차트·저장·후처리의 불필요한 대표 ID 변경 문제 외에, **YouTube Music이 추가를 성공 처리한 ID와 이후 목록에서 노출하는 ID가 다른 현상**을 확인했다. 원본 영상 자체의 player 검사만으로 목록에서 같은 녹음이 유지되는지 보장할 수 없다.

첫 Apple 목록의 요청은 `GxChUrrY4bc`였고 성공 응답도 같은 ID와 새 항목 토큰을 반환했다. 그러나 이후 Music 응답의 목록 항목·삭제·재생·제목 링크는 같은 토큰에서 `JJx_WQXOeK0`이었다. 설치된 파서가 ID 하나를 잘못 덮어쓴 것으로 설명되지 않는다.

반면 YouTube 웹에는 `GxChUrrY4bc`가 표시됐다. 공개 웹은 200개 중 180개만 표시하고 소유권 토큰을 노출하지 않았으므로, 전역 저장소의 ID가 덮어써졌다고 단정할 수 없다. 확인한 것은 **클라이언트별 표현의 차이**다. Music의 watch 응답은 이 두 ID의 영상/음원 관계도 제공한다. 이 관계는 라이브러리에서 설명하는 [counterpart 정보](https://ytmusicapi.readthedocs.io/en/stable/reference/watch.html)에 해당하지만, 모든 관계가 같은 언어·버전·참여자의 녹음을 보장하지는 않는다.

아래 표는 운영 목록에서 발견한 Gx 한 쌍과 시험 목록에서 발견한 네 쌍이다. 시험 목록에 원본 7곡을 한 번 추가한 결과, 원본 ID로 성공 응답을 받은 항목 중 4개가 Music에서 바뀌었다. 나머지 세 곡(편지, 방콕 IF YOU 공연, 백앤아)은 원본 ID를 유지했다.

| 원본 → Music 관측 ID | 확인한 의미 | 현재 처리 |
|---|---|---|
| `GxChUrrY4bc` → `JJx_WQXOeK0` | 같은 기존 UID·아티스트와 명시적 영상/음원 관계. 원래부터 둘 다 playable. 204/207초 차이는 그대로 기록 | Music에서 유지되는 대표 음원 선택을 별도 보상 결정으로 검토. 아직 미적용 |
| `0iiW__6izcs` → `kjNGAVT_ySg` | DAY BY DAY 일본어 원본이 한국어 버전으로 연결됨 | 잘못된 대체로 거절. 재생 가능한 공식 일본어 대안은 이번 제한된 검색에서 미확인. 제외 여부 사용자 답변 대기 |
| `y5Wi5kOiGAQ` → `CTdGQIeWQM0` | 새 Acoustic 음원 후보. 이전 잘못된 스튜디오 ID `XMWIJCaYx1M`과 다름 | Sony의 정확한 MV/발매 연결 및 공식 카탈로그 근거 확보. 두 Music 아티스트 ID가 다르다는 사실도 보존. 추가 실제 게시·재생 검증 전 운영 채택하지 않음 |
| `to3sjq-CAvA` → `i0zqeEAJEx8` | 진영·최유리의 같은 고유 에피소드 제목과 딩고 배급 근거. 206/200초 | 같은 공연으로 볼 근거는 있으나 편집 차이와 공연 동일성 검토 미완료. 자동 승인 보류 |
| `UsjsYMo3O1Q` → `ljCOHKamf4Q` | Brain Rot 영상과 음원의 상호 counterpart. 원본은 Kasane Teto 참여를 명시하지만 발매 음원 크레딧의 직접 연결 근거 부족 | 자동 승인 보류. 다른 녹음이라고 단정하거나 영구 제외하지 않음 |

원래 계획의 ‘원본 ID를 그대로 복원하면 잘못된 버전 문제가 끝난다’는 가정은 이 실제 시험으로 반증됐다. 일반적인 길이 허용치를 늘리거나, 파서의 관측 ID를 요청 ID로 바꾸거나, 같은 요청을 반복하는 방식으로 통과시키지 않았다.

## 추가 보호 보강의 실제 검증

최신 코드로 기존 시험 5곡 중 한 곡을 Gx로 바꾸는 요청을 한 번 실행했다. Gx 추가 성공 응답 뒤 Music에서는 JJx가 관측됐고, 동시에 응답에 항목 6개·보고 개수 5개의 불일치가 생겼다. 제품은 이 불완전한 관측에서 먼저 중단했다. **기존 5곡의 모든 ID·토큰·순서를 유지했고, 기존 항목 삭제와 이동은 0회였다.** 이후 실제 응답을 세 번 대조하여 소유권을 확인한 새 토큰 한 개만 제거했고, 07:46 KST 기존 5곡 복구·재생 검사·시험 미완료 감사 0건을 확인했다.

이 시험의 게시 단계는 직접적인 ID 치환 거절 이벤트를 기록하기 전에 개수 검사에서 중단됐다. 따라서 도구의 엄격한 게시 단계 직접 치환 경로 합격 조건은 **`protection_verified=false` / `NOT_REPRODUCED_BASELINE_RESTORED`**다. 실제 치환 발생, 불완전한 응답에서의 기존 항목 보존 및 정리는 확인했지만, 게시 단계의 직접 치환 분기를 실환경에서 통과 검증했다고 주장하지 않는다. 이후 명시적 복구 단계에서는 추가 영수증과 실제 항목을 비교하여 Gx→JJx 거절을 감사에 기록했다. 그 분기와 51개 이상 청크 중단은 격리 회귀에서 검증했다. 같은 시험 요청을 반복하여 성공 결과를 만들지 않았다.

이 보강은 운영 쓰기 0 또는 11개 목록의 원자적 변경을 보장하지 않는다. 거절 시 새로 추가한 항목이 남을 수 있으며, 안전한 소유권 확인과 정리가 필요하다. 기존 운영 Apple 목록의 부분 변경도 소급하여 복구하지 않는다. 구현 지문이 변경됐으므로 원래 `e7025de` 복구 명세를 최신 코드에서 그대로 재실행하지 않으며, 새 승인 범위와 실제 상태를 반영하는 후속 명세가 필요하다.

## 현재 운영 상태와 시험 정리

- 운영 DB: `ytmusic-chart-20260914` 명세의 24건 적용 유지. 추가 보상 결정은 아직 없음.
- 첫 Apple 200곡: 삭제 10개 ACK, 추가 10개 ACK, 이동 0회. 기존 190개 항목과 상대 순서 유지, 새 9개는 요청 ID, 나머지 한 항목은 Music에서 JJx로 관측. 새 10개는 끝에 있어 **최종 순서 복구도 미완료**다. 삭제된 옛 항목 토큰을 복원했다고 주장하지 않는다.
- 운영 미완료 감사: `99d5f5843ec343dbac541b15a3024da5` 한 건, `recovery_required`.
- 다른 운영 목록 10개: 07:14 재조회에서 수정 전 ID·토큰·순서와 동일.
- 공개 history: 최신 Pages 배포 후 07:52 KST에도 직접 HTTPS 200, 리디렉션 없음. 로컬과 공개 SHA 모두 `0cbd2b70e1e606ba0c2522c5c50b2e5d8d7c6c5879de8d0f4bb0f1ae69ce8a12`. 교정 미리보기는 설치하지 않음.
- 7곡 시험: 추가 1회와 성공 응답을 보존. 새 7개 토큰만 한 번 제거하여 기존 5곡의 ID·토큰·순서 복구. 추가 외부 변경 없이 실험을 `verification_failed`로 종결했고 시험 DB의 미완료 감사는 0건. 시험용 목록 자체는 다음 검증을 위해 남아 있다.
- 정상 전체 실행, 교정 history 배포, 최종 앱 재생 확인은 미완료다.

최초 운영 DB 적용 시에는 SQLite/PG 정렬 차이로 4개 사례의 7개 목록 before-image 비교가 실패했다. 그 시도는 commit을 시작하지 않았고 롤백했다. 값·타입·행 집합은 모두 동일함을 독립 확인한 뒤 실제 PG에서 새 명세를 작성하고, 그 명세 자체로 격리 적용·롤백을 통과한 후 운영에 적용했다. 실패했던 첫 명세의 비교 조건을 완화하지 않았다.

## 갱신한 실행 로드맵과 통과 조건

아래 후속 복구 명세·표면 교정 작업은 아직 구현·적용 완료가 아니다. 공통 게시·복구 보호 보강의 완료와 전체 사건 복구를 구분한다.

| 단계 | 구현·실행 내용 | 담당과 독립 확인 | 통과 조건 |
|---|---|---|---|
| 1. 표현이 바뀌는 곡의 선택 확정 | 각 원본/관측 ID의 버전·언어·참여자·공식 발매 근거를 사례별로 확정. 일본어 버전 제외는 사용자의 기존 전체 복원 계획을 바꾸므로 답변 전 실행하지 않음 | identity 담당 + 별도 검토자 | 미확인 관계를 자동 승인하지 않음. Gx/Acoustic 검토 근거를 다른 세 곡에 확대하지 않음 |
| 2. 후속 복구 명세 | 원래 24건 명세·commit 영수증은 불변 유지. 필요한 대표 ID/연결/정책 변경만 새 명세에 기재. 전체 11개 목록과 31일 history의 변경량·보존 토큰을 현재 상태에서 재계산 | 저장소/history 담당 + 독립 범위 검토 | 49개 교체·995개 보존이라는 옛 계획 숫자를 새 완료 수치로 재사용하지 않음. 원본 순위·과거 시도·기존 정책은 명시적 범위 밖에서 불변 |
| 3. 이전 실패와 후속 명세 연결 구현 | 원래 요청/ACK/오류를 보존하면서 실패한 게시와 새 명세를 연결. 실제 관측 토큰 전체의 소유권을 증명한 뒤 처리. 가능한 경우 DB 보상과 이전 감사 해소를 같은 트랜잭션으로 실행 | 구현 담당 + 복구 검증 담당 | 기존 게시를 성공으로 소급하지 않음. 후속 명세의 전체 표면 검증 전 일반 게시 차단 유지 |
| 4. 후속 명세 검증 | 부모 hash·원래 적용 후 상태·변경 대상·전체 출력 범위를 검사. 잘못된 부모, 누락된 목록/history, 중복 후속 명세·순환, stale 상태, 낯선 토큰, UNKNOWN, 응답 손실 반례 추가 | 독립 검증 담당 | 한 곡 또는 한 목록만 확인한 후속 기록으로 원래 전체 복구를 완료 처리할 수 없음 |
| 5. 실제 Music 시험 | 확정한 새 음원 선택을 시험 목록에 추가하고 실제 Music ID·토큰·순서·fresh 재생을 확인. 동일 입력 두 번째 실행의 불필요한 교체 0 확인 | 게시 담당 + 독립 읽기 담당 | 성공 응답만으로 통과하지 않음. 같은 ID 추가를 실패 후 재실행하지 않음. 시험 정리 및 감사 상태 확인 |
| 6. DB 리허설·적용 | 실제 PG의 현재 상태로 명세 생성. 격리 PG에서 그 정확한 명세의 적용/롤백/재개 검사 후 commit 및 별도 재접속 확인 | 저장소 담당 + 독립 DB 검증 담당 | 변경 대상만 바뀌고 원래 감사·source·이전 시도 보존. 만료된 근거의 관측 시각을 수정하지 않음 |
| 7. 11개 목록·history | 현재 토큰을 보존하는 수정만 수행. 모든 목록의 정확한 전체 순서와 재생 검사 후 history 설치·Pages 배포·공개 본문 SHA 대조 | 게시/history 담당 + 독립 표면 검증 담당 | DB 선택·Music 목록·Hype/history가 승인된 새 명세와 일치. 과거 history identity 키 계승 및 역방향 링크 순환 방지 |
| 8. 실제 일반 전체 실행 | 복구 종료 후 최신 원본으로 실제 10개 수집 작업과 11개 출력 수행. 새 입력·순위 변동을 허용하면서 정상 ID·기존 시도·정책·항목 소유권 보존 검증 | 전체 실행 담당 + 독립 최종 검증 담당 | private 고정 입력 시험을 이 실행의 대체로 쓰지 않음. 새 오류/불확실성을 실패로 기록. 앱 청취 및 미래 예약 실행은 실제 관측한 경우에만 완료 표시 |

독립 작업 분담은 원인·동일성 조사, 저장소/history, 게시·복구 검증의 세 역할로 유지한다. 각 역할의 산출물을 다른 역할이 확인하고, 운영 쓰기는 하나의 실행 담당만 수행한다. 추가 상시 자동화나 임의 재시도 작업은 만들지 않았다.

## 검증 증거

마지막 운영 상태에 대한 독립 감사 29개 항목과 MAGIC 보충 관측에 대한 추가 18개 대조를 통과했다. 감사 결론도 전체 복구 NO-GO다.

- [최종 상태 독립 감사](/Users/yule/Documents/GitHub/hype_wave/.secrets/incident-20260915-final-nogo-independent-audit-v2.json)
- [최신 597개 회귀](/Users/yule/Documents/GitHub/hype_wave/.secrets/incident-20260915-add-first-final-reviewed-ci-validation.json), [깨끗한 커밋 사본 검사](/Users/yule/Documents/GitHub/hype_wave/.secrets/incident-20260915-add-first-clean-archive-validation.json), [추가 보호 독립 검증](/Users/yule/Documents/GitHub/hype_wave/.secrets/incident-20260915-add-first-independent-final-go.json)
- [최신 Pages 배포 후 공개 history 확인](/Users/yule/Documents/GitHub/hype_wave/.secrets/incident-20260915-post-add-first-public-history-check.json)
- [최신 Actions 실제 검사 결과 대조](/Users/yule/Documents/GitHub/hype_wave/.secrets/incident-20260915-add-first-actions-verification.json)
- [최신 보호 실제 시험과 정리](/Users/yule/Documents/GitHub/hype_wave/.secrets/incident-20260915-add-first-live-check-2247.json)

- [마지막 운영 상태 재조회](/Users/yule/Documents/GitHub/hype_wave/.secrets/incident-20260915-final-hold-snapshot-2213.json), [MAGIC 정확한 ID 보충 조회](/Users/yule/Documents/GitHub/hype_wave/.secrets/incident-20260915-final-hold-magic-exact-supplement.json)
- [운영 DB commit 결과](/Users/yule/Documents/GitHub/hype_wave/.secrets/incident-20260915-final-production-apply-e7025de-pg.json), [독립 commit 후 검증](/Users/yule/Documents/GitHub/hype_wave/.secrets/incident-20260915-final-production-postcommit-independent-e7025de-approved.json)
- [Music 목록 항목의 원본 응답 검토](/Users/yule/Documents/GitHub/hype_wave/.secrets/incident-20260915-playlist-renderer-identity-20260914T214634Z-140afb.json), [YouTube 웹 대조](/Users/yule/Documents/GitHub/hype_wave/.secrets/incident-20260915-youtube-web-adaptive-comparison-20260914T220021Z-24c810.json)
- [Gx 한 쌍과 정상 캐시 경로 검토](/Users/yule/Documents/GitHub/hype_wave/.secrets/incident-20260915-gx-single-case-compensation-review-evidence.json)
- [7곡 실제 시험](/Users/yule/Documents/GitHub/hype_wave/.secrets/incident-20260915-native7-staging-2202.json), [소유권 읽기 대조](/Users/yule/Documents/GitHub/hype_wave/.secrets/incident-20260915-native7-staging-owned-readonly.json), [정리 및 실패 종결](/Users/yule/Documents/GitHub/hype_wave/.secrets/incident-20260915-native7-abandoned-2213.json)
- [Acoustic·Brain Rot 동일성 검토](/Users/yule/Documents/GitHub/hype_wave/.secrets/incident-20260915-native-two-recording-identity-review.json), [유리의 숲 공연 검토](/Users/yule/Documents/GitHub/hype_wave/.secrets/incident-20260915-yuri-native-performance-review.json)

증거 파일은 비공개 로컬 진단 산출물이다. 인증 자료를 공개 문서나 저장소에 포함하지 않는다.
