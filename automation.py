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
    print("[LOG] Fetching active upload server from Vidara API...")
    try:
        res = requests.get("https://api.vidara.so/v1/upload/server", params={"api_key": VIDARA_API_KEY}, timeout=30)
        res.raise_for_status()
        js = res.json()
        if js.get("status") == 200:
            server = js["result"]["upload_server"]
            print(f"[LOG] Connected to Vidara Upload Server: {server}")
            return server
    except Exception as e:
        print(f"[ERROR] Failed to fetch Vidara Server: {e}")
    return None

def scrape_links_with_unblocked_engine(actress_url):
    """Uses optimized Playwright flags to bypass cloud hangs and dumps navigation checkpoints."""
    video_pages = []
    mp4_links = []

    print(f"[LOG] Launching Playwright engine...")
    with sync_playwright() as p:
        browser = p.chromium.launch(
            headless=True,
            args=[
                "--no-sandbox", 
                "--disable-setuid-sandbox", 
                "--disable-gpu", 
                "--disable-dev-shm-usage"
            ]
        )
        
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
            viewport={"width": 1920, "height": 1080},
            extra_http_headers={"Accept-Language": "en-US,en;q=0.9"}
        )
        page = context.new_page()

        try:
            print(f"[LOG] Requesting URL via browser context: {actress_url}")
            page.goto(actress_url, wait_until="commit", timeout=45000)
            print("[LOG] Page reached. Sleeping briefly to let DOM stabilize...")
            page.wait_for_timeout(3000)
            
            main_html = page.content()
            print(f"[LOG] Successfully extracted HTML content size: {len(main_html)} characters.")
            soup = BeautifulSoup(main_html, "html.parser")
        except Exception as e:
            print(f"[ERROR] Playwright failed to navigate or extract hub page: {str(e)}")
            browser.close()
            return [], f"Failed to load main hub page: {str(e)}"

        print("[LOG] Processing main page HTML via BeautifulSoup selectors...")
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
            print("[LOG] Warning: Selectors parsed successfully, but found 0 video subpages.")
            browser.close()
            return [], None

        print(f"[LOG] Found {len(video_pages)} video subpages. Processing streams...")

        for idx, video_page in enumerate(video_pages, start=1):
            try:
                print(f"  -> [{idx}/{len(video_pages)}] Parsing subpage: {video_page}")
                page.goto(video_page, wait_until="commit", timeout=25000)
                page.wait_for_timeout(1000)
                
                sub_html = page.content()
                page_soup = BeautifulSoup(sub_html, "html.parser")
                
                found_on_page = False
                for a in page_soup.select("a[href]"):
                    href = a.get("href", "")
                    if href.endswith(".mp4") or ".mp4?" in href:
                        if not href.startswith("http"):
                            href = "https://www.aznude.com" + href
                        if href not in mp4_links:
                            mp4_links.append(href)
                            print(f"     [✓] Discovered MP4 resource link: {href}")
                        found_on_page = True
                        break
                if not found_on_page:
                    print("     [!] No direct .mp4 reference link detected on this subpage layout.")
            except Exception as sub_e:
                print(f"     [!] Failed to extract details from subpage due to exception: {sub_e}")
                continue

        browser.close()
        print(f"[LOG] Playwright engine shut down. Unique video links parsed: {len(mp4_links)}")

    return list(set(mp4_links)), None

def normal_merge_clips(clips_list, output_path):
    """
    Combines mixed videos cleanly via an on-the-fly matrix. Low-res and vertical clips 
    are placed inside a unified canvas naturally without distortion or losing quality.
    """
    if not clips_list:
        return False, "No clips provided for merging."

    print(f"[LOG] Building dynamic canvas matrix for {len(clips_list)} mixed clips...")
    
    cmd = ["ffmpeg", "-y"]
    for clip in clips_list:
        cmd.extend(["-i", clip])

    filter_complex = ""
    # 1. Take each clip and safely position it into a standard 1280x720 box
    for idx in range(len(clips_list)):
        filter_complex += (
            f"[{idx}:v]scale=1280:720:force_original_aspect_ratio=decrease,"
            f"pad=1280:720:(ow-iw)/2:(oh-ih)/2,setsar=1[v{idx}];"
        )
    
    # 2. Line up the prepared video and audio channels back-to-back
    for idx in range(len(clips_list)):
        filter_complex += f"[v{idx}][{idx}:a]"
        
    filter_complex += f"concat=n={len(clips_list)}:v=1:a=1[v][a]"
    
    # 3. Export using CRF 16 (Visually Lossless Quality Control)
    cmd.extend([
        "-filter_complex", filter_complex,
        "-map", "[v]", "-map", "[a]",
        "-c:v", "libx264", "-preset", "faster", "-crf", "16",
        "-c:a", "aac", "-b:a", "192k",
        "-loglevel", "error", output_path
    ])

    try:
        print("[LOG] Stitching videos via high-fidelity rendering pipeline...")
        result = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
        if result.returncode == 0 and os.path.exists(output_path):
            return True, None
        return False, f"FFmpeg native engine stitching failed: {result.stderr}"
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
    print("[LOG] Script initiated. Grabbing target Google Sheet records...")
    records = sheet.get_all_records()
    print(f"[LOG] Successfully pulled {len(records)} records from spreadsheet.")
    
    max_num = 0
    for r in records:
        try:
            val = int(r.get("Number", 0) or 0)
            if val > max_num:
                max_num = val
        except ValueError:
            continue
    print(f"[LOG] Current highest video index number sequence located: {max_num}")

    upload_server = get_vidara_server()
    if not upload_server:
        print("[ERROR] Could not acquire active Vidara upload target. Exiting script.")
        return

    for idx, row in enumerate(records, start=2):
        status = str(row.get("Status", "")).strip().lower()
        title = str(row.get("Title", "")).strip()
        url = str(row.get("Link", "")).strip()

        if status in ["success", "failed"]:
            continue
        if not title or not url:
            continue

        print(f"\n========================================================")
        print(f"[+] START PROCESSING ROW {idx}: {title}")
        print(f"========================================================")

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

        print(f"[LOG] Starting concurrent stream extraction pipeline ({video_count} items)...")
        downloaded_clips = []
        
        with ThreadPoolExecutor(max_workers=MAX_DOWNLOAD_WORKERS) as download_executor:
            futures = {download_executor.submit(download_single_clip, u, p): p for u, p in download_tasks}
            for future in as_completed(futures):
                clip_path = futures[future]
                if future.result():
                    downloaded_clips.append(clip_path)
                    print(f"  [✓] Fragment downloaded successfully: {os.path.basename(clip_path)}")
                else:
                    print(f"  [!] Target slice download failed: {clip_path}")

        downloaded_clips.sort()

        next_assign_num = max_num + 1
        clean_name = sanitize_filename(title)
        final_filename = f"{next_assign_num}. {clean_name}.mp4"
        final_output_file = os.path.abspath(f"./{final_filename}")

        if len(downloaded_clips) == video_count and video_count > 0:
            merge_success, merge_err = normal_merge_clips(downloaded_clips, final_output_file)
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

        print(f"[LOG] Dispatching completed block artifact payload out to Vidara host...")
        up_ok, up_err = upload_to_vidara(upload_server, final_output_file)
        
        if os.path.exists(final_output_file):
            os.remove(final_output_file)

        if up_ok:
            sheet.update(range_name=f'C{idx}:F{idx}', values=[[next_assign_num, video_count, "success", ""]])
            print(f"[✓] Row {idx} fully verified and recorded in spreadsheet context! Sequence Index: {next_assign_num}")
            max_num = next_assign_num
        else:
            sheet.update(range_name=f'D{idx}:F{idx}', values=[[video_count, "failed", f"Upload error: {up_err}"]])
            print(f"[ERROR] Row {idx} Upload Failed.")

if __name__ == "__main__":
    main()
