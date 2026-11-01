"""Verify an installed wheel using mock Airtable HTTP and real local document ingestion.

Run this file from a directory outside the checkout. It needs no Airtable or
model credentials and does not build or query a graph. Storage is temporary.
"""

import asyncio
import os
import tempfile
from pathlib import Path

BASE_ID = "appOfflineSmoke"
TABLE_ID = "tblOfflineSmoke"
FACT = "The offline connector smoke fact is violet-orbit-937."


class Response:
    status_code = 200
    headers = {}

    def __init__(self, payload):
        self.payload = payload

    def json(self):
        return self.payload


class Session:
    def get(self, url, **kwargs):
        if url.endswith(f"/meta/bases/{BASE_ID}/tables"):
            return Response(
                {
                    "tables": [
                        {
                            "id": TABLE_ID,
                            "name": "Smoke facts",
                            "primaryFieldId": "fldFact",
                            "fields": [
                                {"id": "fldFact", "name": "Fact", "type": "singleLineText"},
                                {
                                    "id": "fldModified",
                                    "name": "Last modified time",
                                    "type": "lastModifiedTime",
                                    "options": {
                                        "isValid": True,
                                        "result": {
                                            "type": "dateTime",
                                            "options": {
                                                "dateFormat": {"name": "iso"},
                                                "timeFormat": {"name": "24hour"},
                                                "timeZone": "utc",
                                            },
                                        },
                                    },
                                },
                            ],
                        }
                    ]
                }
            )
        if url.endswith(f"/{BASE_ID}/{TABLE_ID}"):
            return Response(
                {
                    "records": [
                        {
                            "id": "recOfflineSmoke",
                            "createdTime": "2026-01-01T00:00:00.000Z",
                            "fields": {
                                "fldFact": FACT,
                                "fldModified": "2026-01-01T00:00:00.000Z",
                            },
                        }
                    ]
                }
            )
        raise AssertionError(f"Unexpected HTTP request: {url}")


async def main():
    with tempfile.TemporaryDirectory(prefix="airtable-wheel-smoke-") as directory:
        os.environ["DATA_ROOT_DIRECTORY"] = str(Path(directory) / "data")
        os.environ["SYSTEM_ROOT_DIRECTORY"] = str(Path(directory) / "system")
        os.environ["DLT_DATA_DIR"] = str(Path(directory) / "dlt")
        os.environ["RUNTIME__DLTHUB_TELEMETRY"] = "false"
        os.environ["DLT_DATA_DIR"] = str(Path(directory) / "dlt")
        os.environ["ENABLE_BACKEND_ACCESS_CONTROL"] = "false"
        os.environ["TELEMETRY_DISABLED"] = "true"
        os.environ["LLM_API_KEY"] = "offline-unused"
        os.environ["EMBEDDING_API_KEY"] = "offline-unused"

        import cognee
        from cognee.infrastructure.files.utils.open_data_file import open_data_file
        from cognee.modules.data.methods.get_dataset_data import get_dataset_data
        from cognee.modules.data.methods.get_datasets_by_name import get_datasets_by_name
        from cognee.modules.users.methods.get_default_user import get_default_user

        from cognee_community_connector_airtable import airtable_source

        cognee.config.system_root_directory(str(Path(directory) / "system"))
        cognee.config.data_root_directory(str(Path(directory) / "data"))
        result = await cognee.add(
            airtable_source(
                base_id=BASE_ID,
                token="offline-fixture",
                include_schema=False,
                include_comments=False,
                session=Session(),
            ),
            dataset_name="airtable_wheel_smoke",
            skip_connection_test=True,
            dlt_config={
                "primary_key": "id",
                "write_disposition": "merge",
                "max_rows_per_table": 0,
            },
        )
        user = await get_default_user()
        datasets = await get_datasets_by_name("airtable_wheel_smoke", user.id)
        assert len(datasets) == 1, result
        rows = await get_dataset_data(datasets[0].id)
        assert len(rows) == 1, result
        async with open_data_file(rows[0].raw_data_location, mode="r", encoding="utf-8") as stored:
            assert FACT in stored.read()
        print("Installed-wheel smoke passed: real dlt staging and Cognee document ingestion.")


if __name__ == "__main__":
    asyncio.run(main())
