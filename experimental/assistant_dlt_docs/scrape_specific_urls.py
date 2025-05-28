import requests
import logging
import time
import os

# Set your Firecrawl API key here:
FIRECRAWL_API_KEY = "fc-api-key"

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


def scrape_specific_urls(urls: list, output_file: str):
    """
    Scrape a list of specific URLs and combine their Markdown content into one file.
    """
    all_markdown_fragments = []

    logger.info(f"Starting to scrape {len(urls)} specific URLs")

    for url in urls:
        logger.info(f"Scraping: {url}")
        
        try:
            markdown_data = scrape_markdown_with_firecrawl(url)
            # Append it with a "header" chunk
            chunk = f"----- {url} -----\n\n{markdown_data}\n\n"
            all_markdown_fragments.append(chunk)
            logger.info(f"Successfully collected markdown from {url}")
        except Exception as e:
            logger.error(f"Failed to scrape {url} - {e}")
            # Continue with other URLs even if one fails
            continue

    # Combine everything and write to one file
    combined_markdown = "".join(all_markdown_fragments)
    with open(output_file, "w", encoding="utf-8") as f:
        f.write(combined_markdown)

    logger.info(f"All done! Wrote combined Markdown to {output_file}")


def main():
    if not FIRECRAWL_API_KEY or "YOUR_FIRECRAWL_API_KEY" in FIRECRAWL_API_KEY:
        logger.error("Please set FIRECRAWL_API_KEY at the top of the script!")
        return

    # List of specific URLs to scrape
    urls_to_scrape = [
        "https://www.devzery.com/post/exploring-the-hacker-news-api-a-guide-for-developers",
        "https://medium.com/chris-opperwall/using-the-hacker-news-api-9904e9ab2bc1",
    ]

    script_dir = os.path.dirname(os.path.abspath(__file__))
    output_file = os.path.join(script_dir, "extra_docs.md")

    scrape_specific_urls(urls_to_scrape, output_file)


if __name__ == "__main__":
    main() 