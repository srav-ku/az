def main():
    import requests
    from bs4 import BeautifulSoup

    url = "https://www.aznude.com/view/celeb/z/zendaya-103656.html"

    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        )
    }

    print("=" * 80)
    print("AZNUDE DEBUG TEST")
    print("=" * 80)

    try:
        response = requests.get(url, headers=headers, timeout=30)

        print("STATUS CODE:", response.status_code)
        print("FINAL URL:", response.url)
        print("CONTENT LENGTH:", len(response.text))

        with open("debug_response.html", "w", encoding="utf-8") as f:
            f.write(response.text)

        print("\nHTML SAVED TO debug_response.html")

        soup = BeautifulSoup(response.text, "html.parser")

        print("\nTITLE:")
        print(soup.title.text if soup.title else "NO TITLE")

        print("\nSELECTOR COUNTS:")
        print(
            "single-page_content-container:",
            len(soup.select("div.single-page_content-container"))
        )
        print(
            "video-list-item:",
            len(soup.select("div.media-list-item.video-list-item > a[href]"))
        )

        print("\nFIRST 5000 CHARACTERS OF HTML:")
        print("-" * 80)
        print(response.text[:5000])
        print("-" * 80)

    except Exception as e:
        print("REQUEST FAILED:", str(e))

    return
