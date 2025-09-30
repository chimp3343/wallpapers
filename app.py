import os
import subprocess
import shutil
import zipfile
from flask import Flask, request, jsonify, render_template, redirect
from dotenv import load_dotenv
from threading import Thread, Semaphore, Lock
import platform
from supabase.client import Client
from supabase import create_client
from pathlib import Path

# --- Load environment variables ---
load_dotenv()
STEAM_USER = os.getenv("STEAM_USER")
STEAM_PASS = os.getenv("STEAM_PASS")
APPID = os.getenv("APPID", "107410")
SUPABASE_URL = os.getenv("SUPABASE_URL")
SUPABASE_KEY = os.getenv("SUPABASE_KEY")

# --- Supabase client ---
supabase = create_client(SUPABASE_URL, SUPABASE_KEY)
BUCKET_NAME = "workshop"

# --- Config ---
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORKSHOP_DIR = os.path.join(BASE_DIR, "workshop")
STEAMCMD_DIR = os.path.join(BASE_DIR, "steamcmd")

os.makedirs(WORKSHOP_DIR, exist_ok=True)
os.makedirs(STEAMCMD_DIR, exist_ok=True)

# --- Detect SteamCMD executable ---
if platform.system() == "Windows":
    STEAMCMD_EXE = os.path.join(STEAMCMD_DIR, "steamcmd.exe")
else:
    STEAMCMD_EXE = os.path.join(STEAMCMD_DIR, "steamcmd.sh")

if not os.path.exists(STEAMCMD_EXE):
    raise FileNotFoundError(f"SteamCMD not found at {STEAMCMD_EXE}. Run SteamCMD manually once to initialize it.")

# --- Track downloads ---
download_status = {}   # wid -> status
user_queues = {}       # user_ip -> set of queued wids
user_lock = Lock()     # protects user_queues

# --- Limit concurrent downloads ---
MAX_CONCURRENT_DOWNLOADS = 2
download_semaphore = Semaphore(MAX_CONCURRENT_DOWNLOADS)
MAX_USER_QUEUE = 3  # max downloads per user queued at a time

# --- Flask app ---
app = Flask(__name__)

# --- Download Workshop item ---
def download_workshop_item(wid, user_ip):
    with download_semaphore:
        download_status[wid] = "Downloading..."
        dst = os.path.join(WORKSHOP_DIR, wid)

        if os.path.exists(dst):
            download_status[wid] = "Already downloaded locally"
        else:
            try:
                subprocess.run([
                    STEAMCMD_EXE,
                    "+login", STEAM_USER, STEAM_PASS,
                    "+workshop_download_item", APPID, wid, "validate",
                    "+quit"
                ], check=True)

                src = os.path.join(STEAMCMD_DIR, "steamapps", "workshop", "content", APPID, wid)
                if os.path.exists(src):
                    shutil.copytree(src, dst)
                    zip_path = zip_workshop_item(wid)
                    if zip_path:
                        upload_to_supabase(zip_path, wid)
                        download_status[wid] = "Download complete"
                    else:
                        download_status[wid] = "Failed to zip files"
                    shutil.rmtree(dst)
                else:
                    download_status[wid] = "Failed to find downloaded files"
            except subprocess.CalledProcessError:
                download_status[wid] = "SteamCMD failed"

        with user_lock:
            if user_ip in user_queues:
                user_queues[user_ip].discard(wid)

# --- Zip folder ---
def zip_workshop_item(wid):
    folder_path = os.path.join(WORKSHOP_DIR, wid)
    zip_path = os.path.join(WORKSHOP_DIR, f"{wid}.zip")

    if os.path.exists(zip_path):
        return zip_path

    if os.path.isdir(folder_path):
        with zipfile.ZipFile(zip_path, 'w', zipfile.ZIP_DEFLATED) as zipf:
            for root, dirs, files in os.walk(folder_path):
                for file in files:
                    abs_file = os.path.join(root, file)
                    rel_file = os.path.relpath(abs_file, folder_path)
                    zipf.write(abs_file, rel_file)
        return zip_path
    return None

# --- Upload to Supabase ---
def upload_to_supabase(zip_path, wid):
    file_name = f"{wid}.zip"
    with open(zip_path, "rb") as f:
        data = f.read()
    # Pass upsert as string "true"
    supabase.storage.from_(BUCKET_NAME).upload(file_name, data, {"upsert": "true"})
    os.remove(zip_path)

# --- Routes ---
@app.route("/", methods=["GET"])
def index():
    user_ip = request.remote_addr
    with user_lock:
        queued_wids = user_queues.get(user_ip, set()).copy()
    statuses = {wid: download_status.get(wid, "Not started") for wid in queued_wids}
    return render_template("index.html", queued_downloads=statuses)

@app.route("/add", methods=["POST"])
def add_item():
    wid = request.form.get("workshop_id")
    if not wid:
        return jsonify({"error": "No Workshop ID provided"}), 400

    user_ip = request.remote_addr
    with user_lock:
        queue = user_queues.setdefault(user_ip, set())
        if wid in queue or wid in download_status:
            return jsonify({"message": f"{wid} is already queued or downloaded"})
        if len(queue) >= MAX_USER_QUEUE:
            return jsonify({"error": f"Maximum {MAX_USER_QUEUE} downloads per user allowed in queue"}), 429
        queue.add(wid)

    thread = Thread(target=download_workshop_item, args=(wid, user_ip))
    thread.start()
    return jsonify({"message": f"{wid} queued for download"})

@app.route("/status/<wid>")
def status(wid):
    return jsonify({"status": download_status.get(wid, "Not started")})

@app.route("/downloads")
def downloads():
    items = []
    res = supabase.storage.from_(BUCKET_NAME).list()
    for f in res:
        wid = Path(f["name"]).stem
        url = supabase.storage.from_(BUCKET_NAME).get_public_url(f["name"])
        items.append({"id": wid, "url": url})
    return render_template("downloads.html", items=items)

@app.route("/workshop/<item_id>")
def serve_zip(item_id):
    # Redirect directly to Supabase public URL
    file_name = f"{item_id}.zip"
    url = supabase.storage.from_(BUCKET_NAME).get_public_url(file_name)["publicURL"]
    if url:
        return redirect(url)
    return "File not found", 404

# --- Run ---
if __name__ == "__main__":
    app.run(host="0.0.0.0", port=10000)
