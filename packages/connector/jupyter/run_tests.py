import os
import sys
import tempfile
from pathlib import Path

# Add current dir to path
sys.path.insert(0, os.path.abspath("."))

from tests.test_parser import (
    test_clean_source_invalid,
    test_clean_source_list,
    test_clean_source_string,
    test_parse_notebook_empty_cells,
    test_parse_notebook_invalid_input,
    test_parse_notebook_json_string,
    test_parse_notebook_simple_markdown_and_code,
    test_parse_notebook_v3_fallback,
    test_parse_notebook_with_outputs_disabled,
    test_parse_notebook_with_outputs_enabled,
    test_render_outputs_error_traceback,
    test_render_outputs_execute_result,
    test_render_outputs_max_chars_truncation,
    test_render_outputs_stream,
    test_strip_ansi,
)
from tests.test_redaction import (
    test_redact_api_key,
    test_redact_aws_key,
    test_redact_bearer_token,
    test_redact_empty_or_clean_text,
    test_redact_github_pat,
    test_redact_openai_key,
)
from tests.test_source import (
    test_find_notebook_files,
    test_find_notebook_files_deduplication,
    test_find_notebook_files_explicit_paths,
    test_jupyter_source_decorations,
    test_jupyter_source_requires_dlt_error,
    test_notebook_row_generation,
)

simple_tests = [
    test_clean_source_string,
    test_clean_source_list,
    test_clean_source_invalid,
    test_strip_ansi,
    test_render_outputs_stream,
    test_render_outputs_execute_result,
    test_render_outputs_error_traceback,
    test_render_outputs_max_chars_truncation,
    test_parse_notebook_simple_markdown_and_code,
    test_parse_notebook_json_string,
    test_parse_notebook_with_outputs_enabled,
    test_parse_notebook_with_outputs_disabled,
    test_parse_notebook_v3_fallback,
    test_parse_notebook_empty_cells,
    test_parse_notebook_invalid_input,
    test_redact_bearer_token,
    test_redact_api_key,
    test_redact_aws_key,
    test_redact_github_pat,
    test_redact_openai_key,
    test_redact_empty_or_clean_text,
    test_jupyter_source_requires_dlt_error,
]

tmp_path_tests = [
    test_find_notebook_files,
    test_find_notebook_files_explicit_paths,
    test_find_notebook_files_deduplication,
    test_jupyter_source_decorations,
    test_notebook_row_generation,
]

passed = 0
failed = 0

for fn in simple_tests:
    try:
        fn()
        passed += 1
        print(f"PASS: {fn.__name__}")
    except Exception as e:
        failed += 1
        print(f"FAIL: {fn.__name__}: {e}")

for fn in tmp_path_tests:
    with tempfile.TemporaryDirectory() as td:
        try:
            fn(Path(td))
            passed += 1
            print(f"PASS: {fn.__name__}")
        except Exception as e:
            failed += 1
            print(f"FAIL: {fn.__name__}: {e}")

total = len(simple_tests) + len(tmp_path_tests)
print("\n==========================================")
print(f"TEST RUN COMPLETE: {passed}/{total} PASSED ({failed} failed)")
print("==========================================")
if failed > 0:
    sys.exit(1)
