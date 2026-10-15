"""What a Rollbar item looks like as a document."""

from fake_rollbar import PROJECT, SECRETS, FakeRollbar

from cognee_community_connector_rollbar.rollbar import render_excerpt, render_item


def _render(fake: FakeRollbar, item: dict, **kwargs) -> dict:
    return render_item(item, fake.occurrences.get(item["activating_occurrence_id"]), **kwargs)


def test_an_item_renders_its_state_and_an_excerpt_of_its_trace():
    fake = FakeRollbar()
    item = fake.add_item("ZeroDivisionError: division by zero", framework="django")

    row = _render(fake, item, account_slug="acme")

    assert row["id"] == f"projects/{PROJECT}/items/{item['id']}"
    assert row["title"] == "ZeroDivisionError: division by zero"
    assert row["url"] == "https://rollbar.com/acme/FirstProject/items/1/"
    assert row["_deleted"] is False
    assert row["cognee_node_set"] == [f"rollbar:{PROJECT}", f"rollbar:{PROJECT}:production"]
    assert row["content"] == (
        "Rollbar item #1 in project FirstProject\n"
        "Environment: production\n"
        "Level: error\n"
        "Framework: django\n"
        "Status: active\n"
        "First seen: 2026-10-14\n"
        "Last activated: 2026-10-14\n"
        "Occurrences since last activated: fewer than 10\n"
        "\n"
        "Exception: ZeroDivisionError: division by zero\n"
        "Stack (most recent call last):\n"
        "  app/views.py:12 in dispatch\n"
        "    return handler(request)\n"
        "  app/reports/margin.py:23 in margin\n"
        "    return profit / revenue\n"
        "Code version: 2.4.0"
    )


def test_request_person_locals_arguments_and_custom_data_never_reach_the_document():
    fake = FakeRollbar()
    item = fake.add_item("ZeroDivisionError: division by zero")

    row = _render(fake, item)

    for secret in SECRETS:
        assert secret not in repr(row)


def test_new_occurrences_alone_do_not_change_the_document():
    fake = FakeRollbar()
    item = fake.add_item("ValueError: invalid coupon code")
    for _ in range(5):
        fake.occur(item)
    before = _render(fake, item)

    for _ in range(3):
        fake.occur(item)

    assert _render(fake, item) == before


def test_volume_is_an_order_of_magnitude():
    fake = FakeRollbar()
    item = fake.add_item("Error")

    def volume(count):
        item["total_occurrences"] = count
        line = [ln for ln in _render(fake, item)["content"].splitlines() if "Occurrences" in ln]
        return line[0].split(": ", 1)[1] if line else None

    assert volume(0) is None
    assert volume(9) == "fewer than 10"
    assert volume(10) == "10+"
    assert volume(999) == "100+"
    assert volume(123456) == "100,000+"


def test_a_resolved_item_says_when_and_in_which_version():
    fake = FakeRollbar()
    item = fake.add_item("KeyError: 'plan_tier'", assigned_user_id=7)
    fake.resolve(item, version="2.4.1")

    content = _render(fake, item)["content"]

    assert "Status: resolved" in content
    assert "Last resolved: 2026-10-14" in content
    assert "Resolved in version: 2.4.1" in content
    assert "Assigned: yes" in content


def test_a_chained_exception_reads_outermost_first():
    body = {
        "trace_chain": [
            {
                "exception": {"class": "PaymentGatewayError", "message": "charge failed"},
                "frames": [
                    {"filename": "app/payments/charge.py", "lineno": 40, "method": "charge"}
                ],
            },
            {
                "exception": {"class": "TimeoutError", "message": "read timed out"},
                "frames": [{"filename": "app/payments/client.py", "lineno": 17}],
            },
        ]
    }

    assert render_excerpt({"data": {"body": body, "code_version": "2.4.0"}}) == [
        "Exception: PaymentGatewayError: charge failed",
        "Stack (most recent call last):",
        "  app/payments/charge.py:40 in charge",
        "Caused by: TimeoutError: read timed out",
        "Stack (most recent call last):",
        "  app/payments/client.py:17",
        "Code version: 2.4.0",
    ]


def test_only_the_innermost_frames_are_kept():
    frames = [{"filename": f"app/f{n}.py", "lineno": n} for n in range(12)]
    lines = render_excerpt({"data": {"body": {"trace": {"frames": frames}}}})

    assert lines[0] == "Exception: Exception"
    assert [line.strip() for line in lines[2:]] == [f"app/f{n}.py:{n}" for n in range(7, 12)]


def test_a_message_item_keeps_its_text_but_not_its_extra_fields():
    body = {"message": {"body": "Slow query: orders report took 12.4s", "customer": "card-4242"}}

    lines = render_excerpt({"data": {"body": body, "server": {"code_version": "abc123"}}})

    assert lines == ["Message: Slow query: orders report took 12.4s", "Code version: abc123"]


def test_a_crash_report_is_named_but_its_raw_text_is_not_ingested():
    body = {"crash_report": {"raw": "Thread 0 crashed with card-4242"}}

    assert render_excerpt({"data": {"body": body}}) == [
        "Crash report (the raw report is not ingested)"
    ]


def test_long_messages_are_clipped_onto_one_line():
    body = {"message": {"body": "line one\nline two " + "x" * 2000}}

    (line,) = render_excerpt({"data": {"body": body}})

    assert line.startswith("Message: line one line two x")
    assert len(line) == len("Message: ") + 500


def test_an_item_whose_occurrence_is_gone_keeps_its_state_without_an_excerpt():
    fake = FakeRollbar()
    item = fake.add_item("ZeroDivisionError: division by zero")

    content = render_item(item, None)["content"]

    assert "Status: active" in content
    assert "Exception" not in content


def test_without_an_account_slug_there_is_no_link():
    fake = FakeRollbar()
    item = fake.add_item("Error")

    assert _render(fake, item)["url"] is None


def test_unknown_framework_and_platform_are_left_out():
    fake = FakeRollbar()
    item = fake.add_item("Error", framework="unknown", environment="")

    row = _render(fake, item)

    assert "Framework" not in row["content"]
    assert "Platform" not in row["content"]
    assert row["cognee_node_set"] == [f"rollbar:{PROJECT}"]
