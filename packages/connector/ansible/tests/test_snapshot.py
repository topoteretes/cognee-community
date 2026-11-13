"""Tests for full-snapshot sync and deletion behavior with dlt pipeline."""

from __future__ import annotations

import shutil
from pathlib import Path

import pytest

from cognee_community_connector_ansible import ANSIBLE_TABLE_NAME, ansible_source


@pytest.fixture
def dlt_mod():
    return pytest.importorskip("dlt")


def _read_documents(pipeline) -> dict[str, dict]:
    """Read all rows from the ansible_documents table in the SQLite destination."""
    with (
        pipeline.sql_client() as client,
        client.execute_query(f"SELECT id, title, content FROM {ANSIBLE_TABLE_NAME}") as cursor,
    ):
        rows = cursor.fetchall()
    return {row[0]: {"id": row[0], "title": row[1], "content": row[2]} for row in rows}


def test_snapshot_sync_initial_and_deletion(dlt_mod, sample_ansible_project: Path, tmp_path: Path):
    # Copy project so modifications don't affect other tests
    project_copy = tmp_path / "working_ansible_repo"
    shutil.copytree(sample_ansible_project, project_copy)

    db_path = (tmp_path / "ansible_test.db").as_posix()
    pipeline = dlt_mod.pipeline(
        pipeline_name="ansible_snapshot_test",
        destination=dlt_mod.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="ansible_dataset",
        pipelines_dir=str(tmp_path / "dlt_state"),
    )

    # 1. Initial snapshot sync
    pipeline.run(ansible_source(project_copy))
    docs = _read_documents(pipeline)

    assert "ansible:role:app" in docs
    assert "ansible:role:web" in docs
    assert "ansible:playbook:playbooks/deploy_app.yml" in docs
    assert "ansible:group:webservers" in docs

    # 2. Delete role 'app' and playbook 'deploy_app.yml'
    shutil.rmtree(project_copy / "roles" / "app")
    (project_copy / "playbooks" / "deploy_app.yml").unlink()

    # Re-sync: full snapshot replace
    pipeline.run(ansible_source(project_copy))
    updated_docs = _read_documents(pipeline)

    # Deleted role and playbook MUST be absent from the staging table
    assert "ansible:role:app" not in updated_docs
    assert "ansible:playbook:playbooks/deploy_app.yml" not in updated_docs

    # Other roles and inventory groups must remain
    assert "ansible:role:web" in updated_docs
    assert "ansible:role:common" in updated_docs
    assert "ansible:group:webservers" in updated_docs


def test_snapshot_sync_updates_content(dlt_mod, sample_ansible_project: Path, tmp_path: Path):
    project_copy = tmp_path / "editing_ansible_repo"
    shutil.copytree(sample_ansible_project, project_copy)

    db_path = (tmp_path / "ansible_edit_test.db").as_posix()
    pipeline = dlt_mod.pipeline(
        pipeline_name="ansible_edit_pipeline",
        destination=dlt_mod.destinations.sqlalchemy(f"sqlite:///{db_path}"),
        dataset_name="ansible_edit_ds",
        pipelines_dir=str(tmp_path / "dlt_state_edit"),
    )

    # Initial sync
    pipeline.run(ansible_source(project_copy))
    docs = _read_documents(pipeline)
    assert "Install nginx package" in docs["ansible:role:web"]["content"]
    assert "Configure SSL certificates" not in docs["ansible:role:web"]["content"]

    # Add a new task to web role
    web_tasks_file = project_copy / "roles" / "web" / "tasks" / "main.yml"
    existing_content = web_tasks_file.read_text(encoding="utf-8")
    web_tasks_file.write_text(
        existing_content
        + "\n- name: Configure SSL certificates\n  service: name=ssl state=started\n",
        encoding="utf-8",
    )

    # Re-sync
    pipeline.run(ansible_source(project_copy))
    updated_docs = _read_documents(pipeline)

    assert "Configure SSL certificates" in updated_docs["ansible:role:web"]["content"]
