#!/usr/bin/env python3

import re

def clean_doc(input_file, output_file):
    privacy_section_pattern = re.compile(r'## Privacy Preference Center.*?(?=-----|\Z)', re.DOTALL)
    cookie_list_pattern = re.compile(r'### Cookie List.*?(?=-----|\Z)', re.DOTALL)
    
    with open(input_file, 'r', encoding='utf-8') as f:
        content = f.read()
    
    # Remove privacy sections
    content = privacy_section_pattern.sub('', content)
    
    # Remove cookie list sections
    content = cookie_list_pattern.sub('', content)
    
    # Remove any multiple consecutive newlines that might result from the deletions
    content = re.sub(r'\n{3,}', '\n\n', content)
    
    # Write the cleaned content to the output file
    with open(output_file, 'w', encoding='utf-8') as f:
        f.write(content)
    
    print(f"Successfully cleaned {input_file} and saved to {output_file}")

if __name__ == "__main__":
    input_file = "docs_quadrant.md"
    output_file = "docs_quadrant_cleaned.md"
    clean_doc(input_file, output_file) 