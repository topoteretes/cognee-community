import re

def clean_and_split_doc(input_file, output_file1, output_file2):
    # Match from www.loom.com to the Base64 image line
    loom_blocked_pattern = re.compile(
        r'www\.loom\.com.*?!\[\]\(<Base64-Image-Removed>\)!\[\]\(<Base64-Image-Removed>\)',
        re.DOTALL
    )
    
    # Lines starting with "[Skip to main content]"
    skip_content_pattern = re.compile(r'^.*\[Skip to main content\].*$', re.MULTILINE)
    
    # Cookie consent sections
    cookie_pattern = re.compile(
        r'we use essential cookies.*?PreferencesDeclineAccept',
        re.DOTALL | re.IGNORECASE
    )
    
    # Demo/codespaces sections
    demo_pattern = re.compile(
        r'This demo works on codespaces.*?coding help reimagined with AI prowess\.',
        re.DOTALL | re.IGNORECASE
    )
    
    with open(input_file, 'r', encoding='utf-8') as f:
        content = f.read()
    
    content = loom_blocked_pattern.sub('', content)
    
    content = skip_content_pattern.sub('', content)
    
    content = cookie_pattern.sub('', content)
    
    content = demo_pattern.sub('', content)
    
    # Remove multiple consecutive newlines that result from the deletions
    content = re.sub(r'\n{3,}', '\n\n', content)
    
    # Find page separators (----- url -----)
    page_pattern = re.compile(r'^----- .* -----$', re.MULTILINE)
    page_matches = list(page_pattern.finditer(content))
    
    if not page_matches:
        print("No page separators found. Splitting at middle character.")
        middle = len(content) // 2
        first_half = content[:middle]
        second_half = content[middle:]
    else:
        middle_char = len(content) // 2
        
        # the page separator 
        best_split_pos = 0
        for match in page_matches:
            if match.start() < middle_char:
                best_split_pos = match.start()
            else:
                break
        
        if best_split_pos == 0 and page_matches:
            best_split_pos = page_matches[0].start()
        
        first_half = content[:best_split_pos].rstrip()
        second_half = content[best_split_pos:].lstrip()
    
    with open(output_file1, 'w', encoding='utf-8') as f:
        f.write(first_half)
    
    with open(output_file2, 'w', encoding='utf-8') as f:
        f.write(second_half)
    
    print(f"Successfully cleaned {input_file} and split into:")
    print(f"  First half: {output_file1} ({len(first_half)} characters)")
    print(f"  Second half: {output_file2} ({len(second_half)} characters)")

if __name__ == "__main__":
    input_file = "docs_dlt.md"
    output_file1 = "docs_dlt_cleaned_part1.md"
    output_file2 = "docs_dlt_cleaned_part2.md"
    clean_and_split_doc(input_file, output_file1, output_file2) 