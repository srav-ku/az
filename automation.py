import requests
from bs4 import BeautifulSoup

url = "https://www.aznude.com/view/celeb/z/zendaya-103656.html"

headers = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
}

response = requests.get(url, headers=headers, timeout=30)

print("=" * 80)
print("STATUS CODE:", response.status_code)
print("FINAL URL:", response.url)
print("CONTENT LENGTH:", len(response.text))
print("=" * 80)

print("\nFIRST 3000 CHARACTERS OF RESPONSE:\n")
print(response.text[:3000])

with open("debug_response.html", "w", encoding="utf-8") as f:
    f.write(response.text)

print("\nSaved full response to debug_response.html")

soup = BeautifulSoup(response.text, "html.parser")

containers = soup.select("div.single-page_content-container")
video_items = soup.select("div.media-list-item.video-list-item > a[href]")

print("\n" + "=" * 80)
print("SELECTOR TESTS")
print("=" * 80)
print("single-page_content-container:", len(containers))
print("video-list-item links:", len(video_items))

title = soup.title.text.strip() if soup.title else "NO TITLE"
print("PAGE TITLE:", title)

keywords = [
    "cloudflare",
    "access denied",
    "forbidden",
    "captcha",
    "verify you are human",
    "blocked",
    "bot"
]

html_lower = response.text.lower()

print("\nPOSSIBLE BLOCK DETECTION:")
for k in keywords:
    if k in html_lower:
        print("FOUND:", k)
