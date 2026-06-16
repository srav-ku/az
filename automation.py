import os
import re
import shutil
import json
import subprocess
import requests
import gspread
from google.oauth2.service_account import Credentials
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed
from playwright.sync_api import sync_playwright

# ==================== CONFIGURATION ====================
VIDARA_API_KEY = os.getenv("VIDARA_API_KEY")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDS_JSON")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
MAX_DOWNLOAD_WORKERS = 5
# =======================================================

scopes = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive"
]
creds_dict = json.loads(GOOGLE_CREDS_JSON)
credentials = Credentials.from_service_account_info(creds_dict, scopes=scopes)
gc = gspread.authorize(credentials)
sheet = gc.open_by_key(SPREADSHEET_ID).sheet1

def sanitize_filename(name):
    return re.sub(r'[\\/*?:"<>|]', "", name).strip()

def get_vidara_server():
    try:
        res = requests.get("https://api.vidara.so/v1/upload/server", params={"api_key": VIDARA_API_KEY}, timeout=30)
        res.raise_for_status()
        js = res.json()
        if js.get("status") == 200:
            return js["result"]["upload_server"]
    except Exception as e:
        print(f"Failed to fetch Vidara Server: {e}")
    return None

def scrape_links_with_unblocked_engine(actress_url):
    """Uses Playwright browser emulation to cleanly bypass GitHub data center IP blocks."""
    video_pages = []
    mp4_links = []

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        # Emulate a real desktop user environment perfectly
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"}
        )
        page = context.new_page()

        try:
            print(f"  [-] Fetching main hub page safely via browser context...")
            page.goto(actress_url, wait_until="networkidle", timeout=45000)
            main_html = page.content()
            soup = BeautifulSoup(main_html, "html.parser")
        except Exception as e:
            browser.close()
            return [], f"Failed to load main hub page: {str(e)}"

        # Use your working container selector logic
        for container in soup.select("div.single-page_content-container"):
            if not container.select_one("div.single-page-title-wrapper"):
                continue
            for a in container.select("div.media-list-item.video-list-item > a[href]"):
                path = a["href"]
                if not path.startswith("http"):
                    path = "https://www.aznude.com" + path
                if path not in video_pages:
                    video_pages.append(path)

        if not video_pages:
            browser.close()
            return [], None

        print(f"  [-] Identified {len(video_pages)} video subpages. Processing unblocked page extraction...")

        # Sequential browser page loop ensures firewalls are not triggered by aggressive thread spamming
        for idx, video_page in enumerate(video_pages, start=1):
            try:
                page.goto(video_page, wait_until="domcontentloaded", timeout=25000)
                sub_html = page.content()
                page_soup = BeautifulSoup(sub_html, "html.parser")
                
                for a in page_soup.select("a[href]"):
                    href = a.get("href", "")
                    if href.endswith(".mp4") or ".mp4?" in href:
                        if not href.startswith("http"):
                            href = "https://www.aznude.com" + href
                        if href not in mp4_links:
                            mp4_links.append(href)
                        break # Grab first matched stream and proceed
            except Exception:
                continue

        browser.close()

    return list(set(mp4_links)), None

def get_max_batch_dimensions(clips_list):
    max_w = 1280
    max_h = 720
    max_area = max_w * max_h

    for clip in clips_list:
        cmd = [
            "ffprobe", "-v", "error", "-select_streams", "v:0",
            "-show_entries", "stream=width,height", "-of", "json", clip
        ]
        try:
            result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            data = json.loads(result.stdout)
            w = int(data['streams'][0]['width'])
            h = int(data['streams'][0]['height'])
            if (w * h) > max_area:
                max_area = w * h
                max_w = w
                max_h = h
        except Exception:
            continue
    return max_w, max_h

def merge_large_video_batch(clips_list, output_path, temp_dir):
    if not clips_list:
        return False, "No clips provided for merging."

    target_w, target_h = get_max_batch_dimensions(clips_list)
    print(f"  [-] Highest Quality Scaling Target: {target_w}x{target_h}. Stitching...")

    standardized_clips = []
    
    for idx, clip in enumerate(clips_list):
        norm_output = os.path.join(temp_dir, f"norm_{idx}.mp4")
        cmd_norm = [
            "ffmpeg", "-y", "-i", clip,
            "-vf", f"scale={target_w}:{target_h}:force_original_aspect_ratio=decrease:flags=lanczos,pad={target_w}:{target_h}:(ow-iw)/2:(oh-ih)/2,fps=30,setsar=1",
            "-c:v", "libx264", "-crf", "0", "-preset", "ultrafast",
            "-c:a", "aac", "-b:a", "192k", "-ac", "2",
            "-loglevel", "error", norm_output
        ]
        try:
            res = subprocess.run(cmd_norm, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            if res.returncode == 0 and os.path.exists(norm_output):
                standardized_clips.append(norm_output)
            else:
                return False, f"Clip standardization failed at index {idx}"
        except Exception as e:
            return False, f"Exception during standardization of clip {idx}: {str(e)}"

    list_txt_path = os.path.join(temp_dir, "batch_list.txt")
    with open(list_txt_path, "w", encoding="utf-8") as f:
        for clip_path in standardized_clips:
            f.write(f"file '{os.path.abspath(clip_path)}'\n")

    cmd_merge = [
        "ffmpeg", "-y", "-f", "concat", "-safe", "0", "-i", list_txt_path,
        "-c:v", "libx264", "-preset", "faster", "-crf", "16", 
        "-c:a", "copy", "-loglevel", "error", output_path
    ]

    try:
        result = subprocess.run(cmd_merge, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode == 0 and os.path.exists(output_path):
            return True, None
        return False, f"Stitching engine error: {result.stderr}"
    except Exception as e:
        return False, str(e)

def download_single_clip(url, target_path):
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    try:
        with requests.get(url, stream=True, timeout=45, headers=headers) as r:
            r.raise_for_status()
            with open(target_path, 'wb') as f:
                for chunk in r.iter_content(chunk_size=65536):
                    f.write(chunk)
        return True
    except Exception:
        return False

def upload_to_vidara(server, video_path):
    filename = os.path.basename(video_path)
    try:
        with open(video_path, "rb") as fp:
            response = requests.post(
                server,
                files={"file": (filename, fp, "video/mp4")},
                data={"api_key": VIDARA_API_KEY},
                timeout=None
            )
        response.raise_for_status()
        data = response.json()
        if "filecode" in data:
            return True, None
        return False, f"Vidara missing filecode: {data}"
    except Exception as e:
        return False, str(e)

def main():
    records = sheet.get_all_records()
    max_num = 0
    for r in records:
        try:
            val = int(r.get("Number", 0) or 0)
            if val > max_num:
                max_num = val
        except ValueError:
            continue

    upload_server = get_vidara_server()
    if not upload_server:
        print("Could not acquire active Vidara upload target.")
        return

    for idx, row in enumerate(records, start=2):
        status = str(row.get("Status", "")).strip().lower()
        title = str(row.get("Title", "")).strip()
        url = str(row.get("Link", "")).strip()

        if status in ["success", "failed"]:
            continue
        if not title or not url:
            continue

        print(f"\n[+] Processing Row {idx}: {title}")

        # Execute safe Playwright-backed DOM parsing engine
        mp4_urls, error_msg = scrape_links_with_unblocked_engine(url)
        video_count = len(mp4_urls)

        if error_msg or video_count == 0:
            err = error_msg if error_msg else "Zero video links found (Page blocked or element path blank)."
            sheet.update(range_name=f'D{idx}:F{idx}', values=[[video_count, "failed", err]])
            print(f"[-] Skipped row {idx}: {err}")
            continue

        temp_dir = os.path.abspath(f"./temp_worker")
        os.makedirs(temp_dir, exist_ok=True)
        
        download_tasks = []
        for i, mp4_url in enumerate(sorted(mp4_urls), start=1):
            temp_clip_path = os.path.join(temp_dir, f"clip_{i:03d}.mp4")
            download_tasks.append((mp4_url, temp_clip_path))

        print(f"  [-] Downloading {video_count} clips concurrently via thread pooling...")
        downloaded_clips = []
        
        with ThreadPoolExecutor(max_workers=MAX_DOWNLOAD_WORKERS) as download_executor:
            futures = {download_executor.submit(download_single_clip, u, p): p for u, p in download_tasks}
            for future in as_completed(futures):
                clip_path = futures[future]
                if future.result():
                    downloaded_clips.append(clip_path)

        downloaded_clips.sort()

        next_assign_num = max_num + 1
        clean_name = sanitize_filename(title)
        final_filename = f"{next_assign_num}. {clean_name}.mp4"
        final_output_file = os.path.abspath(f"./{final_filename}")

        if len(downloaded_clips) == video_count and video_count > 0:
            print(f"  [-] Processing advanced hybrid high-res merge for {video_count} items...")
            merge_success, merge_err = merge_large_video_batch(downloaded_clips, final_output_file, temp_dir)
            err_text = merge_err if merge_err else ""
        else:
            merge_success = False
            err_text = f"Downloaded clips count mismatch ({len(downloaded_clips)}/{video_count})"

        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)

        if not merge_success:
            sheet.update(range_name=f'D{idx}:F{idx}', values=[[video_count, "failed", err_text]])
            if os.path.exists(final_output_file):
                os.remove(final_output_file)
            print(f"[-] Row {idx} processing failed during compilation: {err_text}")
            continue

        print(f"  [^] Initializing stream uploading payload...")
        up_ok, up_err = upload_to_vidara(upload_server, final_output_file)
        
        if os.path.exists(final_output_file):
            os.remove(final_output_file)

        if up_ok:
            sheet.update(range_name=f'C{idx}:F{idx}', values=[[next_assign_num, video_count, "success", ""]])
            print(f"[✓] Row {idx} Complete -> Assigned Number: {next_assign_num}")
            max_num = next_assign_num
        else:
            sheet.update(range_name=f'D{idx}:F{idx}', values=[[video_count, "failed", f"Upload error: {up_err}"]])
            print(f"[!] Row {idx} Upload Failed")

if __name__ == "__main__":
    main()
