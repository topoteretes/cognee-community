import re
from typing import List, Set

def extract_specific_pages(input_files: List[str], output_file: str, main_urls: List[str]):
    """
    Extract specific pages and their subpages from the cleanedmarkdown files.
    """
    
    all_content = ""
    for input_file in input_files:
        print(f"Reading {input_file}...")
        with open(input_file, 'r', encoding='utf-8') as f:
            content = f.read()
            all_content += content + "\n\n"
    
    print(f"Total content loaded: {len(all_content)} characters")
    
    page_pattern = re.compile(r'^----- (.*?) -----$', re.MULTILINE)
    page_matches = list(page_pattern.finditer(all_content))
    
    print(f"Found {len(page_matches)} total pages in documentation")
    
    urls_to_extract: Set[str] = set()
    
    for main_url in main_urls:
        urls_to_extract.add(main_url)
        
        for match in page_matches:
            page_url = match.group(1)
            if page_url.startswith(main_url):
                urls_to_extract.add(page_url)
    
    print(f"\nFound {len(urls_to_extract)} pages to extract:")
    for url in sorted(urls_to_extract):
        print(f"  - {url}")
    
    extracted_content = []
    
    for i, match in enumerate(page_matches):
        page_url = match.group(1)
        
        if page_url in urls_to_extract:
            page_start = match.start()
            
            if i + 1 < len(page_matches):
                page_end = page_matches[i + 1].start()
            else:
                page_end = len(all_content)
            
            page_content = all_content[page_start:page_end].rstrip()
            extracted_content.append(page_content)
            
            print(f"Extracted: {page_url} ({len(page_content)} characters)")
    
    final_content = '\n\n'.join(extracted_content)
    
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(final_content)
    
    print(f"\nSuccessfully extracted {len(extracted_content)} pages to {output_file}")
    print(f"Total content: {len(final_content)} characters")

def main():
    # Define the main URLs to extract
    main_urls = [
        "https://dlthub.com/docs/tutorial/load-data-from-an-api",
        "https://dlthub.com/docs/general-usage/resource",
        "https://dlthub.com/docs/general-usage/source",
        "https://dlthub.com/docs/walkthroughs/create-a-pipeline",
        "https://dlthub.com/docs/general-usage/incremental-loading",
        "https://dlthub.com/docs/general-usage/incremental/cursor",
        "https://dlthub.com/docs/general-usage/incremental/lag",
        "https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads",

        ]
    
    input_files = ["docs_dlt_cleaned_part1.md", "docs_dlt_cleaned_part2.md"]
    output_file = "docs_dlt_some_pages.md"
    
    print("Extracting specific pages and their subpages...")
    print("Main URLs:")
    for url in main_urls:
        print(f"  - {url}")
    print()
    
    extract_specific_pages(input_files, output_file, main_urls)

if __name__ == "__main__":
    main() 