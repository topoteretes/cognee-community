"""Real Cognee Data cleanup after losing only isolated dlt staging, without models."""

import os
import subprocess
import sys
from pathlib import Path

import cognee_community_connector_jira as package


def test_rebuilt_staging_empty_removes_only_its_corpus(tmp_path):
    script = r"""
import asyncio,importlib.util,json,os,shutil,sys
from pathlib import Path
from unittest.mock import patch
runtime=Path(sys.argv[1])
os.umask(0o077)
for key in list(os.environ):
    if key.upper()=='PIPELINES_DIR' or key.upper().endswith('__PIPELINES_DIR') or key.startswith(
        ('DB_','MIGRATION_DB_','GRAPH_DATABASE_','VECTOR_DB_')) or key in {
        'GRAPH_FILE_PATH','GRAPH_FILENAME','GRAPH_DATASET_DATABASE_HANDLER',
        'VECTOR_DATASET_DATABASE_HANDLER','DATABASE_CONNECT_ARGS','POOL_ARGS',
        'VECTOR_POOL_ARGS','LOG_FILE_NAME'}:
        os.environ.pop(key,None)
os.environ.update({
    'SYSTEM_ROOT_DIRECTORY':str(runtime/'system'),'DATA_ROOT_DIRECTORY':str(runtime/'data'),
    'CACHE_ROOT_DIRECTORY':str(runtime/'cache'),'COGNEE_LOGS_DIR':str(runtime/'logs'),
    'LOGS_ROOT_DIRECTORY':str(runtime/'logs'),'COGNEE_REPOS_DIR':str(runtime/'repos'),
    'DLT_DATA_DIR':str(runtime/'dlt'),'DLT_PROJECT_DIR':str(runtime),
    'DLT_LOCAL_DIR':str(runtime),'PIPELINES_DIR':str(runtime/'dlt/pipelines'),
    'DB_PROVIDER':'sqlite','DB_PATH':str(runtime/'system/databases'),'DB_NAME':'cognee',
    'GRAPH_DATABASE_PROVIDER':'ladybug','VECTOR_DB_PROVIDER':'lancedb',
    'GRAPH_DATASET_DATABASE_HANDLER':'ladybug','VECTOR_DATASET_DATABASE_HANDLER':'lancedb',
    'ENABLE_BACKEND_ACCESS_CONTROL':'true','CACHE_BACKEND':'sqlite',
    'CACHE_DB_URL':'sqlite+aiosqlite:///'+str(runtime/'cache.db'),
    'RUNTIME__DLTHUB_TELEMETRY':'false','DO_NOT_TRACK':'1',
})
spec=importlib.util.spec_from_file_location('fixture',sys.argv[2])
fixture=importlib.util.module_from_spec(spec);spec.loader.exec_module(fixture)
import cognee
from cognee_community_connector_jira import jira_source
import cognee_community_connector_jira as package
assert Path(package.__file__).resolve()==Path(sys.argv[3]),'Wrong package under recovery test'
from cognee.modules.users.methods import get_default_user
from cognee.tasks.ingestion.dlt_utils import pipeline_name_for_source
reader=fixture.Reader()

def source(name):
    src=jira_source(reader.site_url,'owner@example.test','token',['KAN'],
                    resource_name=name,missing_policy='mirror_visibility',client=reader)
    pipeline=pipeline_name_for_source(src,'cold_recovery')
    os.environ[pipeline.upper()+'__PIPELINES_DIR']=str(runtime/'dlt/pipelines')
    return src

async def add(name):
    await cognee.add(source(name),dataset_name='cold_recovery',run_in_background=False,
        skip_connection_test=True,
        dlt_config={'primary_key':'id','write_disposition':'replace','max_rows_per_table':0})

async def run():
    await add('left');await add('right')
    user=await get_default_user()
    dataset=next(x for x in await cognee.datasets.list_datasets(user) if x.name=='cold_recovery')
    async def data(): return await cognee.datasets.list_data(dataset.id,user)
    assert len(await data())==10
    shutil.rmtree(runtime/'dlt/pipelines')
    for path in (runtime/'system/databases').glob('dlt_database_cold_recovery*'):
        assert path.is_relative_to(runtime)
        if path.is_file():path.unlink()
    reader.issues={}
    await add('left')
    remaining=await data()
    assert len(remaining)==5,(
        f'Empty rebuilt staging left {len(remaining)} Data rows; expected sibling5')
    await add('right')
    assert len(await data())==0,'Final rebuilt corpus did not clean retained Data'
    print('REBUILT_STAGING_RECOVERY_PASS')

with patch('requests.sessions.Session.send',side_effect=AssertionError('Network forbidden')),patch(
    'httpx.Client.send',side_effect=AssertionError('Network forbidden')),patch(
    'httpx.AsyncClient.send',side_effect=AssertionError('Network forbidden')):
    asyncio.run(run())
"""
    env = os.environ.copy()
    origin = Path(package.__file__).resolve()
    env["PYTHONPATH"] = str(origin.parent.parent)
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            script,
            str(tmp_path),
            str(Path(__file__).with_name("test_jira.py")),
            str(origin),
        ],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        timeout=120,
    )
    (tmp_path / "recovery-subprocess.log").write_text(result.stdout + result.stderr)
    assert result.returncode == 0, result.stdout[-3000:] + result.stderr[-3000:]
    assert "REBUILT_STAGING_RECOVERY_PASS" in result.stdout
