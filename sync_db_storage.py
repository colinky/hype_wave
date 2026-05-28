import os
import sys
import requests

# Supabase에서 관리할 DB 파일 목록
DB_FILES = [
    "hype_wave_data.db",
    "ytmusic_cache.db",
]

# .env 로더 함수
def load_env():
    env_path = "./.secrets/.env"
    if os.path.exists(env_path):
        with open(env_path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip().strip("'").strip('"')
                os.environ[k] = v

load_env()

SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")
BUCKET_NAME = "database"

if not SUPABASE_URL or not SUPABASE_KEY:
    print("Error: SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY must be set.")
    sys.exit(1)

headers = {
    "Authorization": f"Bearer {SUPABASE_KEY}",
    "apikey": SUPABASE_KEY,
}

def download_db(file_name):
    url = f"{SUPABASE_URL}/storage/v1/object/authenticated/{BUCKET_NAME}/{file_name}"
    print(f"[{file_name}] Downloading from Supabase Storage...")
    res = requests.get(url, headers=headers)
    if res.status_code == 200:
        with open(file_name, "wb") as f:
            f.write(res.content)
        size_mb = len(res.content) / 1024 / 1024
        print(f"[{file_name}] Downloaded ({size_mb:.1f} MB)")
    elif res.status_code == 404:
        print(f"[{file_name}] Not found in Supabase (새 파일로 시작)")
    else:
        print(f"[{file_name}] Download failed (Code {res.status_code}): {res.text}")

def upload_db(file_name):
    if not os.path.exists(file_name):
        print(f"[{file_name}] 파일 없음 - 업로드 생략")
        return

    url = f"{SUPABASE_URL}/storage/v1/object/{BUCKET_NAME}/{file_name}"
    print(f"[{file_name}] Uploading to Supabase Storage...")

    with open(file_name, "rb") as f:
        file_data = f.read()

    upload_headers = {
        **headers,
        "Content-Type": "application/x-sqlite3",
        "x-upsert": "true",
    }

    res = requests.put(url, headers=upload_headers, data=file_data)
    if res.status_code == 200:
        size_mb = len(file_data) / 1024 / 1024
        print(f"[{file_name}] Uploaded ({size_mb:.1f} MB)")
    else:
        print(f"[{file_name}] Upload failed: {res.text}")
        sys.exit(1)

if __name__ == "__main__":
    # 사용법:
    #   python sync_db_storage.py download              -> DB_FILES 전체 다운로드
    #   python sync_db_storage.py upload                -> DB_FILES 전체 업로드
    #   python sync_db_storage.py download ytmusic_cache.db  -> 특정 파일만
    action = sys.argv[1] if len(sys.argv) > 1 else ""
    target_files = [sys.argv[2]] if len(sys.argv) > 2 else DB_FILES

    if action == "download":
        for db in target_files:
            download_db(db)
    elif action == "upload":
        for db in target_files:
            upload_db(db)
    else:
        print("Usage: python sync_db_storage.py [download|upload] [optional: specific_file.db]")
        print(f"  관리 중인 DB 파일: {DB_FILES}")