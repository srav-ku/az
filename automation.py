import os
import re
import shutil
import json
import requests
import gspread
from google.oauth2.service_account import Credentials
from playwright.sync_api import sync_playwright

# ==================== CONFIGURATION ====================
VIDARA_API_KEY = os.getenv("VIDARA_API_KEY")
GOOGLE_CREDS_JSON = os.getenv("GOOGLE_CREDS_JSON")
SPREADSHEET_ID = os.getenv("SPREADSHEET_ID")
# =======================================================

# Authentication Setup
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

def scrape_links_with_playwright(actress_url):
    """Launches a real headless browser instance to bypass Cloudflare/WAF checks."""
    video_pages = []
    mp4_links = []
    
    with sync_playwright() as p:
        # Emulate a real desktop Chrome device profile context
        browser = p.chromium.launch(headless=True)
        context = browser.new_context(
            user_agent="Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
            viewport={"width": 1280, "height": 720}
        )
        page = context.new_page()
        
        try:
            print(f"  [-] Navigating browser to hub target...")
            page.goto(actress_url, wait_until="domcontentloaded", timeout=30000)
            
            # Extract video viewer page links via anchor evaluations
            hrefs = page.locator("a[href]").evaluate_all("elements => elements.map(e => e.getAttribute('href'))")
            for href in hrefs:
                if "/view/video/" in href or "video-list-item" in href:
                    full_path = href if href.startswith("http") else f"https://www.aznude.com{href}"
                    if full_path not in video_pages:
                        video_pages.append(full_path)
                        
            print(f"  [-] Identified {len(video_pages)} standalone media viewers. Extracting raw stream points...")
            
            # Crawl out direct media locations
            for video_page in video_pages:
                try:
                    page.goto(video_page, wait_until="domcontentloaded", timeout=20000)
                    stream_hrefs = page.locator("a[href]").evaluate_all("elements => elements.map(e => e.getAttribute('href'))")
                    for s_href in stream_hrefs:
                        if s_href and (".mp4" in s_href or s_href.split('?')[0].endswith('.mp4')):
                            mp4_links.append(s_href)
                            break # Found primary media chunk for this panel page
                except Exception:
                    continue
                    
        except Exception as e:
            browser.close()
            return [], f"Headless browser failure: {str(e)}"
            
        browser.close()
        
    return list(set(mp4_links)), None

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

    # Use a standard network session for downloading binary files
    session = requests.Session()

    for idx, row in enumerate(records, start=2):
        status = str(row.get("Status", "")).strip().lower()
        title = str(row.get("Title", "")).strip()
        url = str(row.get("Link", "")).strip()

        if status in ["success", "failed"]:
            continue
        if not title or not url:
            continue

        print(f"\n[+] Processing Row {idx}: {title}")

        # 1. Scrape via Headless Browser Strategy
        mp4_urls, error_msg = scrape_links_with_playwright(url)
        video_count = len(mp4_urls)

        if error_msg or video_count == 0:
            err = error_msg if error_msg else "Zero video links found (Firewall blocked or empty selectors)."
            sheet.update(range_name=f'D{idx}:F{idx}', values=[[video_count, "failed", err]])
            print(f"[-] Skipped row {idx}: {err}")
            continue

        # 2. Download fragments locally
        temp_dir = f"./temp_worker"
        os.makedirs(temp_dir, exist_ok=True)
        downloaded_clips = []

        print(f"  [-] Extracting direct stream resources...")
        for i, mp4_url in enumerate(mp4_urls, start=1):
            temp_clip_path = os.path.join(temp_dir, f"clip_{i}.mp4")
            try:
                headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
                with session.get(mp4_url, headers=headers, stream=True, timeout=30) as r:
                    r.raise_for_status()
                    with open(temp_clip_path, 'wb') as f:
                        for chunk in r.iter_content(chunk_size=16384):
                            f.write(chunk)
                downloaded_clips.append(temp_clip_path)
            except Exception as e:
                print(f"   [!] Clip {i} download error: {e}")

        # 3. Compile Lossless Output Container via FFmpeg
        merge_success = False
        next_assign_num = max_num + 1
        clean_name = sanitize_filename(title)
        final_filename = f"{next_assign_num}. {clean_name}.mp4"
        final_output_file = f"./{final_filename}"
        err_text = ""

        if len(downloaded_clips) == video_count:
            list_file_path = os.path.join(temp_dir, "file_list.txt")
            with open(list_file_path, "w", encoding="utf-8") as f:
                for file_path in downloaded_clips:
                    f.write(f"file '{os.path.abspath(file_path)}'\n")

            ffmpeg_cmd = f"ffmpeg -y -f concat -safe 0 -i \"{list_file_path}\" -c copy \"{final_output_file}\" -loglevel error"
            if os.system(ffmpeg_cmd) == 0 and os.path.exists(final_output_file):
                merge_success = True
            else:
                err_text = "FFmpeg merging process failed."
        else:
            err_text = f"Downloaded clips count mismatch ({len(downloaded_clips)}/{video_count})"

        if os.path.exists(temp_dir):
            shutil.rmtree(temp_dir)

        if not merge_success:
            sheet.update(range_name=f'D{idx}:F{idx}', values=[[video_count, "failed", err_text]])
            if os.path.exists(final_output_file):
                os.remove(final_output_file)
            continue

        # 4. Process Cloud Handoff to Vidara
        print(f"  [^] Initializing stream uploading payload...")
        up_ok, up_err = upload_to_vidara(upload_server, final_output_file)
        
        if os.path.exists(final_output_file):
            os.remove(final_output_file)

        # 5. One single batch update write call back to Google Sheet
        if up_ok:
            sheet.update(range_name=f'C{idx}:F{idx}', values=[[next_assign_num, video_count, "success", ""]])
            print(f"[✓] Row {idx} Complete -> Assigned Number: {next_assign_num}")
            max_num = next_assign_num
        else:
            sheet.update(range_name=f'D{idx}:F{idx}', values=[[video_count, "failed", f"Upload error: {up_err}"]])
            print(f"[!] Row {idx} Upload Failed")

if __name__ == "__main__":
    main()
