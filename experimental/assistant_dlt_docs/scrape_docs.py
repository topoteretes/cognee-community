import requests
from bs4 import BeautifulSoup
from collections import deque
import logging
import time
import os

# Set your Firecrawl API key here:
FIRECRAWL_API_KEY = "FC-API-KEY"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
)
logger = logging.getLogger(__name__)


def scrape_markdown_with_firecrawl(url: str) -> str:
    """
    Calls Firecrawl to retrieve the Markdown for a single URL.
    Retries automatically if a 429 is encountered.
    """
    endpoint = "https://api.firecrawl.dev/v1/scrape"
    headers = {
        "Authorization": f"Bearer {FIRECRAWL_API_KEY}",
        "Content-Type": "application/json",
    }
    payload = {
        "url": url,
        "formats": ["markdown"],
        "mobile": False,
        "onlyMainContent": True,
    }

    max_retries = 5
    backoff_seconds = 35  # how long to wait after a 429 and 502

    for attempt in range(1, max_retries + 1):
        resp = requests.post(endpoint, json=payload, headers=headers)

        if resp.status_code == 200:
            data = resp.json()
            return data.get("data", {}).get("markdown", "")

        elif resp.status_code == 408:
            # 408 => request timed out. Wait and retry
            if attempt < max_retries:
                logger.warning(
                    f"408 Timeout. Waiting {backoff_seconds}s before retry (attempt {attempt}/{max_retries})."
                )
                time.sleep(backoff_seconds)
                continue
            else:
                raise RuntimeError(
                    f"Exceeded max retries on 408 for {url}. Last response: {resp.text}"
                )

        elif resp.status_code == 429:
            # 429 => rate limit exceeded
            if attempt < max_retries:
                logger.warning(
                    f"429 Rate Limit. Waiting {backoff_seconds}s then retrying (attempt {attempt}/{max_retries})"
                )
                time.sleep(backoff_seconds)
                continue
            else:
                raise RuntimeError(
                    f"Exceeded max retries (429). Last response: {resp.text}"
                )
        elif resp.status_code == 502:
            # 502 => Bad Gateway error
            if attempt < max_retries:
                logger.warning(
                    f"502 Bad Gateway. Waiting {backoff_seconds}s then retrying (attempt {attempt}/{max_retries})"
                )
                time.sleep(backoff_seconds)
                continue
            else:
                raise RuntimeError(
                    f"Exceeded max retries (502). Last response: {resp.text}"
                )

        else:
            raise RuntimeError(f"Firecrawl error {resp.status_code}: {resp.text}")

    raise RuntimeError("Failed to scrape after retries.")


def crawl_dlt_docs(start_url: str, max_depth: int, output_file: str):
    """
    BFS to discover links from the DLTHub documentation domain,
    then fetch & combine each page's Markdown (via Firecrawl) into one file.
    """
    domain_prefix = "https://dlthub.com/docs"
    visited = set()
    queue = deque([(start_url, 0)])

    all_markdown_fragments = []

    logger.info(f"Starting BFS crawl from {start_url}, max_depth={max_depth}")

    while queue:
        current_url, depth = queue.popleft()
        if current_url in visited:
            continue
        visited.add(current_url)

        logger.info(f"Crawling: {current_url} (depth={depth})")

        # 1) Request HTML directly to parse sub-links
        try:
            html_resp = requests.get(current_url, timeout=15)
            if html_resp.status_code != 200:
                logger.warning(
                    f"Skipping {current_url}, status={html_resp.status_code}"
                )
                continue
        except Exception as e:
            logger.warning(f"Failed to GET {current_url} - {e}")
            continue

        # 2) Parse HTML to find sub-links
        if depth < max_depth:
            soup = BeautifulSoup(html_resp.text, "html.parser")
            for a_tag in soup.find_all("a", href=True):
                href = a_tag["href"].strip()
                # Convert relative links to absolute if needed
                if href.startswith("/"):
                    href = "https://dlthub.com" + href
                # Follow only doc sub-pages
                if href.startswith(domain_prefix) and href not in visited:
                    queue.append((href, depth + 1))

        # 3) Use Firecrawl to get Markdown for the current page
        try:
            markdown_data = scrape_markdown_with_firecrawl(current_url)
            # Append it with a "header" chunk
            chunk = f"----- {current_url} -----\n\n{markdown_data}\n\n"
            all_markdown_fragments.append(chunk)
            logger.info(f"Collected markdown from {current_url}")
        except Exception as e:
            logger.error(f"Failed Firecrawl scrape for {current_url} - {e}")

    # After BFS finishes, combine everything and write to one file
    combined_markdown = "".join(all_markdown_fragments)
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(combined_markdown)

    logger.info(f"All done! Wrote combined Markdown to {output_file}")


def main():
    if not FIRECRAWL_API_KEY or "YOUR_FIRECRAWL_API_KEY" in FIRECRAWL_API_KEY:
        logger.error("Please set FIRECRAWL_API_KEY at the top of the script!")
        return

    start_url = "https://dlthub.com/docs/"
    max_depth = 5  # Adjust as needed

    script_dir = os.path.dirname(os.path.abspath(__file__))
    output_file = os.path.join(script_dir, "docs_dlt.md")

    crawl_dlt_docs(start_url, max_depth, output_file)


if __name__ == "__main__":
    main()
