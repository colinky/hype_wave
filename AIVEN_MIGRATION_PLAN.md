# Supabase → Aiven 이전 및 보안 조치 기록

2026-09-19. 사용자 지시에 따라 로컬 백업·DB 이전·보안 조치·로컬 코드 전환을 수행했다. 초기 작업에서는 GitHub Actions를 보류했으며, 후속 사용자 지시에 따라 아래 GitHub Actions 전환 기록을 추가했다.

## 완료한 작업

- 원본 PostgreSQL 17.6의 public 테이블 20개와 뷰 1개를 일관된 스냅샷으로 백업했다. 백업 기준 시각은 **2026-09-19 12:25:09 KST**이며, 총 **229,555행**, 압축 덤프 **3,843,092바이트**다.
- 백업을 Aiven PostgreSQL 18.6의 비어 있던 `defaultdb`에 단일 트랜잭션으로 복원했다. 원본 소유자·ACL 및 Supabase 관리 스키마는 복사하지 않았다.
- Supabase의 모든 앱 테이블에 RLS를 적용하고 `PUBLIC`, `anon`, `authenticated`, 미사용 `service_role`의 앱 스키마 접근을 차단했다. 실제 API 역할의 조회 거부 및 기존 `postgres` 조회 성공을 확인했다.
- Aiven에서도 20개 테이블의 RLS와 전용 역할 정책을 적용하고 공개 접근을 차단했다. 실행 계정 `hype_sync`는 객체 소유자가 아니며, SUPERUSER·CREATEDB·CREATEROLE·BYPASSRLS 권한이 없다.
- `hype_sync`는 업무 테이블의 조회·삽입·수정·삭제와 뷰 조회만 허용한다. `schema_migrations`는 조회만 가능하며 `migration_reports`는 복구 감사 기록을 위해 쓰기를 허용한다. CREATE·ALTER·TRUNCATE 거부를 검증했다.
- `.secrets/.env`의 Aiven URI를 `hype_sync` 계정으로 교체하고 `HYPE_DB_BACKEND=aiven`으로 전환했다. 관리자 자격 증명은 별도 보관했다.

## 연결 설정

| 설정 | 처리 |
|---|---|
| `HYPE_DB_BACKEND=aiven` | Aiven만 사용. 설정 누락·연결·캐시 저장 실패 시 오류 처리 |
| `HYPE_DB_BACKEND=supabase` | 명시적으로 원본 DB 사용 |
| `HYPE_DB_BACKEND=sqlite` | 원격 DB에 접속하지 않는 로컬 SQLite 모드 |
| 선택값 없음 | 기존 동작 유지: SUPABASE_DB_URL이 있으면 Supabase, 없으면 SQLite |
| `AIVEN_DB_URI` | 전용 실행 계정 URI, **sslmode=require 유지** |
| `AIVEN_DB_HOST` | URI 호스트와 일치 검사 |
| `AIVEN_DB_CA_CERTIFICATE` | `.env`에서 줄바꿈을 `\n`으로 표현한 한 줄 PEM |

`hype_db_common.py`에서 PEM을 검증한 뒤 권한 600의 임시 파일로 만들고 `sslrootcert`에 경로를 전달한다. 별도 경로 환경변수는 필요하지 않다. 실제 여러 줄 PEM 환경변수도 지원하므로 Actions Secret에는 인증서 내용만 저장한다. 파일은 캐시 저장이 끝난 뒤 프로세스 종료 시 삭제된다. `require`와 CA 체인 검증을 사용하며 호스트명 검증 모드로 변경하지 않았다.

주 연결, 번역 캐시, 크롤러, 복구 도구의 DB 선택을 공통화했다. Aiven 런타임에서는 테이블·인덱스를 생성하지 않으며, 캐시 저장 실패가 성공 종료로 숨겨지지 않도록 정상 종료 전에 명시적으로 저장한다. 새 테이블은 관리자가 RLS·정책·권한을 포함한 마이그레이션으로 생성해야 한다.

로컬 `utils/`의 조회·백업·분석 도구도 같은 설정을 사용하도록 수정했다. 이름이 `backup_supabase_snapshot.py`인 원본 전용 백업 도구는 Supabase 대상으로 유지했다. 오래된 파괴적 Supabase 초기화 도구 `utils/migrate_to_postgres.py`는 Aiven 선택 시 중단한다. `utils/`와 기존 오프라인 테스트 대부분은 원래 Git 제외 대상이며 로컬에서만 변경됐다.

## 검증 결과

| 항목 | 결과 |
|---|---|
| 모든 테이블 행 수·체크섬 | 20/20 일치, 총 229,555행 |
| 스키마·기본키·필수 열·부분 유일 인덱스 | 기존 스키마 검증 통과 |
| TLS | TLS 1.3, 256bit |
| 실제 실행 계정 CRUD·캐시 저장 | 성공, 시험 데이터 전부 롤백 |
| 번역 캐시 | 아티스트 4,269개·곡 6,744개 로딩, SQLite 대체 없음 |
| 기존 이력 갱신 | 성공, 29일·최신 2026-09-18 |
| 동시 연결 | 실행 계정 3개 동시 연결 성공 |
| 연결 제한 | 서버 20개 중 관리자 예약 3개, 실행 계정 최대 5개 |
| 점검 시 전체 클라이언트 연결 | 5개 |
| 복원 후 DB 크기 | 59,700,927바이트 (서비스 전체 디스크·WAL 사용량과는 별개) |
| 오프라인 회귀 테스트 | 660개 통과, 네트워크·PostgreSQL 접속 시도 0회 |

`metadata_lookup_index`의 최초 텍스트 체크섬 차이는 원본 `extra_float_digits=0`과 대상 기본값 1의 출력 정밀도 차이였다. 백업을 임시 테이블로 읽어 실제 데이터 차이가 0행임을 확인했고, 출력 정밀도를 맞춘 검증에서 모든 체크섬이 일치했다. 앱의 점수나 저장 값을 변경하지 않았다.

**기존 데이터 제약:** 31일 전체 이력을 처음부터 재생성하면 `History chart 2026-08-19 is empty`로 중단된다. 원본 Supabase에서도 같은 오류와 0행을 확인했다. 기존 이력을 유지하며 최신 날짜를 갱신하는 운영 경로는 통과했다. 이 문제를 숨기기 위한 데이터 삭제나 과거 차트 수정은 하지 않았다.

초기 로컬 이전에서는 실제 YouTube 플레이리스트 변경이나 전체 동기화를 실행하지 않았다. 후속 GitHub 러너 검증 결과는 아래에 기록했다. 서비스 디스크·WAL의 장기 증가와 월간 실제 egress는 한 번의 실행으로 확정할 수 없다.

## 보관 위치

- `backups/aiven_migration_20260919/supabase_public_before_migration.dump`: 로컬 원본 백업, 권한 600.
- `snapshot_manifest.json`: 스냅샷 시각·테이블별 행 수·체크섬·덤프 SHA-256.
- `supabase_permissions_before.json`, `supabase_security_applied.json`: 원본 권한 변경 전후 기록.
- `aiven_data_verification.json`, `checksum_difference_analysis.json`, `aiven_capacity_validation.json`: 데이터·보안·용량 검증.
- `local_cutover.json`, `local_final_smoke.json`: 로컬 전환 및 최종 연결 검증.
- `.secrets/env-before-aiven-migration.txt`: 변경 전 전체 환경 파일.
- `.secrets/aiven-migration-admin.env`: 관리자 URI와 CA. 앱 실행에 주입하지 않는다.
- `.secrets/aiven-runtime.env`: 전용 실행 계정 설정 사본.

백업·환경 파일·인증서는 Git에서 제외한다. 원본은 복구 및 추후 최종 동기화를 위해 보존했다. 실제 자격 증명은 이 문서에 기록하지 않는다. 덤프 SHA-256: `3cf9dfb23838b9555044fbcf251618558b393eb15b07a83e60f293b46de4001e`.

적용 SQL: `db_migrations/20260918_restrict_public_api.sql`, `db_migrations/20260919_aiven_runtime_access.sql`. PostgreSQL 17 클라이언트는 `/opt/homebrew/opt/libpq@17/bin/`에 설치했다.

## GitHub Actions 전환 절차

1. 기존 Actions가 Supabase에 계속 쓸 수 있으므로, 최종 전환 시 예약·실행 중 작업과 로컬 쓰기를 멈추고 **백업 기준 시점 이후 양쪽 변경분**을 대조한다. 서로 다른 DB의 실행 잠금은 공유되지 않는다.
2. 로컬 Aiven에 새 업무 데이터가 쌓였다면 먼저 별도 백업한다. 원본과 대상의 차이를 반영한 최종 복원 방식을 정하며 기존 데이터를 무조건 덮어쓰지 않는다.
3. 운영 main과 로컬 브랜치의 차이를 확인한 뒤 이번 DB 관련 수정만 검토·반영한다.
4. GitHub에 실행 계정의 AIVEN_DB_URI·AIVEN_DB_HOST·AIVEN_DB_CA_CERTIFICATE를 저장하고 workflow에 HYPE_DB_BACKEND=aiven을 설정한다. 관리자 URI나 로컬 CA 파일 경로를 넣지 않는다.
5. 수동 실행에서 DB 저장·캐시·플레이리스트·이력 배포를 검증한 뒤 예약 실행을 재개한다.
6. 다음 예약 실행과 초기 디스크·WAL·egress를 관찰한다. Supabase Data API 대시보드 토글 비활성화 및 미사용 키 폐기도 이때 정리할 수 있다. 현재 DB 권한 수준에서는 API 접근이 차단된 상태다.

전환 전 Aiven에 장애가 나면 원본의 보안 차단은 유지한 채 `HYPE_DB_BACKEND=supabase`로 명시적으로 선택할 수 있다. Aiven에 업무 쓰기나 YouTube 변경이 발생한 뒤에는 양쪽 기록을 대조해야 하며 단순 URL 복귀만으로 복구 완료로 보지 않는다. 원본의 서비스 제한으로 접속 자체가 막히면 로컬 백업이나 Aiven 복구가 필요하다.

## 2026-09-19 GitHub Actions 전환 기록

- 예약 실행을 잠시 중지하고 원본·대상 20개 테이블이 최초 백업과 모두 동일함을 다시 확인했다. 추가 데이터 복사는 필요하지 않았다.
- 운영 `main` 기준으로 DB 관련 변경만 별도 작성했다. 로컬 브랜치의 별도 음악 매칭 개선은 함께 배포하지 않았다.
- [PR #1](https://github.com/colinky/hype_wave/pull/1)을 병합했다. 운영 커밋: `1bb7ca3a48e2c45da08c27689e1be123e0bf66b3`.
- PR 및 main의 DB 테스트 17개가 통과했다. 로컬 전체 오프라인 테스트는 664개가 통과했다.
- workflow에 Aiven Secrets 3개, 명시적 backend, 읽기 전용 사전 점검, 인증 파일 정리를 적용했다. 당시 예약 기준은 매일 08:02 UTC / 17:02 KST였다.
- [실제 러너 읽기 전용 점검 #195](https://github.com/colinky/hype_wave/actions/runs/35420898480)는 등록된 `AIVEN_DB_URI`의 URI 형식 오류로 연결 전에 중단됐다. 전체 동기화·DB 쓰기·플레이리스트 변경은 시작하지 않았다. 이후 사용자 Secret 수정으로 해소했다.
- 증빙: `github_actions_cutover.json`, `github_cutover_data_audit.json`, `github_deployment_manifest.json`, `github_deployment.patch`, `actions_offline_verification.json`.
- 설정 오류 후 예약 실행을 다시 중지했고 GitHub API에서 `disabled_manually` 상태를 확인했다. 이후 Secret 수정·전체 동기화·예약 재활성화를 완료했다.

### Secret 수정 후 재검증

사용자가 URI Secret을 수정한 뒤 [별도 읽기 전용 점검](https://github.com/colinky/hype_wave/actions/runs/35421265366)이 통과했다. 운영 동기화를 비활성화한 상태에서 점검 전용 브랜치 `codex/aiven-secret-check`로 검증했다. 실행 계정 `hype_sync`, TLS 1.3 및 20개 테이블 RLS를 GitHub 러너에서 확인했다. 이후 예약 워크플로를 다시 활성화하고 [전체 동기화 #196](https://github.com/colinky/hype_wave/actions/runs/35421360283)을 시작했다.

### 운영 전환 완료

- [전체 동기화 #196](https://github.com/colinky/hype_wave/actions/runs/35421360283): 성공. 6개 차트 550곡 전부 매칭 및 DB 저장 완료, 실패 0곡. Hype를 포함한 7개 플레이리스트는 4개 게시·3개 최신 상태 확인으로 정상 종료했다.
- 이력 갱신 커밋 `4edd55fa0e98c11b12a4fdf6223e17f5511de972`이 main에 반영됐다. [Pages 배포](https://github.com/colinky/hype_wave/actions/runs/35421965519)도 성공했고 공개 대시보드의 실제 history.json을 확인했다.
- 당시 Daily Unified Music Sync를 `active`로 전환하고 매일 08:02 UTC / 17:02 KST 예약을 재개했다. GitHub의 실제 실행 시작 시각은 대기열에 따라 지연될 수 있다.
- 실행 뒤에도 20개 테이블 모두 RLS가 활성화돼 있다. DB 크기는 61,388,479바이트, 캐시는 아티스트 4,289개·곡 6,779개였다.
- 30초 간격 25회 관측에서 실행 계정 연결은 최대 2개(계정 제한 5), 점검 세션을 포함한 전체 클라이언트 연결은 최대 4개였다. 순간적인 최대치를 모두 측정한 것은 아니다. 실행 종료 뒤 runtime 연결이 0개가 된 것도 확인했다.
- 추가 증빙: `actions_final_database_audit.json`, `actions_connection_samples.jsonl`, `published_history_after_actions.json`. 점검용 DB 연결은 종료했다.

### 연결 테스트 코드 정리

운영 전환 검증 완료 후 사용자 요청에 따라 연결 점검 스크립트, 수동 점검 입력, DB 연결 테스트 파일과 전용 테스트 워크플로를 제거했다. 실제 Aiven 접속 설정, CA 처리, 제한 계정과 RLS, 동기화 잠금 및 캐시 오류 처리는 운영 기능으로 유지한다. 기존 오프라인 회귀 실행 목록에서도 삭제된 모듈의 참조를 제거했다.

삭제 후 기존 오프라인 회귀 테스트 647개가 통과했으며 네트워크·PostgreSQL 접속 시도는 0회였다. 운영 main의 DB 이전 변경은 이미 반영돼 있어, main에는 연결 테스트 제거와 운영 문서 정리만 추가한다.

## 2026-09-24 브랜치 통합

모든 브랜치의 변경을 운영 `main` 기준으로 대조했다. 이미 반영된 Aiven 운영 코드와 연결 테스트 삭제 상태를 유지하고, 로컬 이전 문서와 예약 시간 변경을 통합했다. 현재 예약은 매일 **07:05 UTC / 16:05 KST**다. 과거에 되돌린 음악 매칭 변경은 다시 적용하지 않았다. 기존 이력은 로컬 `backups/git_history_reset_20260924/`에 보관하고, 통합 결과를 새 초기 커밋으로 게시한다. 위 테스트 수와 실행 기록은 각 작업 당시의 검증 결과다.
