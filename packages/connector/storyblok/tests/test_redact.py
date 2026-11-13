"""The scrubbing step the issue asks for: secrets must never reach a document.

Fixtures use documented fake values or strings built at runtime, so secret
scanners do not flag this file.
"""

import time

import pytest

from cognee_community_connector_storyblok.redact import scrub

AWS_KEY = "AKIA" + "IOSFODNN7EXAMPLE"
GITHUB = "ghp_" + "a1B2c3D4" * 5
JWT = "eyJ" + "hbGciOiJIUzI1NiJ9" + ".eyJzdWIiOiIxMjM0In0" + ".c2lnbmF0dXJlLXZhbHVl"
PEM = "-----BEGIN RSA PRIVATE KEY-----\nMIIEow\nIBAAK\n-----END RSA PRIVATE KEY-----"
TFC = "abcdefghijklmn.atlasv1." + "x" * 64


@pytest.mark.parametrize(
    ("text", "secret", "kind"),
    [
        (f"key id {AWS_KEY} leaked", AWS_KEY, "aws_access_key"),
        (f"token {GITHUB}", GITHUB, "github_token"),
        ("auth glpat-" + "a" * 20, "glpat-" + "a" * 20, "gitlab_token"),
        ("hook xoxb-" + "1" * 12, "xoxb-" + "1" * 12, "slack_token"),
        (f"bearer {JWT}", JWT, "jwt"),
        (PEM, "MIIEow", "private_key"),
        (f"TFE_TOKEN is {TFC}", TFC, "terraform_token"),
        ("AccountKey=Zm9vYmFyYmF6;EndpointSuffix=core", "Zm9vYmFyYmF6", "azure_key"),
        ("postgres://admin:hunter22@db:5432/app", "hunter22", "credentials"),
        ('+ password = "correct-horse"', "correct-horse", "secret"),
        ("client_secret: s3cr3tv4lu3", "s3cr3tv4lu3", "secret"),
    ],
)
def test_each_secret_kind_is_redacted(text, secret, kind):
    scrubbed, count = scrub(text)
    assert secret not in scrubbed
    assert f"[REDACTED:{kind}]" in scrubbed
    assert count >= 1


@pytest.mark.parametrize(
    ("text", "secret", "expected"),
    [
        (
            'password = "correct horse battery staple"',
            "horse battery",
            'password = "[REDACTED:secret]"',
        ),
        (
            'export DB_PASSWORD="my secret pass"',
            "secret pass",
            'export DB_PASSWORD="[REDACTED:secret]"',
        ),
        ("api_key: 'two words here'", "words here", "api_key: '[REDACTED:secret]'"),
        (
            '{"client_secret": "with \\"quoted\\" part"}',
            "quoted",
            '{"client_secret": "[REDACTED:secret]"}',
        ),
        ("token=abc123 next", "abc123", "token=[REDACTED:secret] next"),
    ],
)
def test_quoted_values_with_spaces_and_escapes_are_redacted_whole(text, secret, expected):
    scrubbed, count = scrub(text)
    assert secret not in scrubbed and count == 1
    assert scrubbed == expected


def test_long_dotted_text_is_scrubbed_in_linear_time():
    # An unbounded key pattern backtracks quadratically here (about 17 s before).
    start = time.perf_counter()
    scrub("a." * 10_000 + "password")
    assert time.perf_counter() - start < 5


def test_color_codes_cannot_hide_a_secret():
    scrubbed, count = scrub(f"\x1b[1m{AWS_KEY[:6]}\x1b[0m{AWS_KEY[6:]}")
    assert AWS_KEY not in scrubbed and count == 1


def test_plan_text_and_terraform_markers_are_left_alone():
    text = (
        "aws_db_instance.main must be replaced\n"
        "Plan: 1 to add, 0 to change, 1 to destroy.\n"
        "+ password = (sensitive value)\n"
        "~ token_ttl = (known after apply)\n"
        "Error: Unsupported argument"
    )
    assert scrub(text) == (text, 0)
