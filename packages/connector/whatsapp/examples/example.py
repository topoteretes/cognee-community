"""Ingest official WhatsApp exports into a dedicated Cognee dataset.

Set WHATSAPP_EXPORT_PATH to one .txt/.zip export or a folder of exports, and
install Cognee's DLT extra before running this example.
"""

import asyncio
import os

import cognee

from cognee_community_connector_whatsapp import whatsapp_export_source


async def main() -> None:
    export_path = os.environ.get("WHATSAPP_EXPORT_PATH")
    if not export_path:
        raise ValueError("Set WHATSAPP_EXPORT_PATH to a WhatsApp export or export folder.")

    await cognee.remember(
        whatsapp_export_source(
            export_path,
            timezone=os.environ.get("WHATSAPP_TIMEZONE"),
        ),
        dataset_name=os.environ.get("WHATSAPP_DATASET", "whatsapp"),
        primary_key="id",
        write_disposition="merge",
        max_rows_per_table=0,
    )


if __name__ == "__main__":
    asyncio.run(main())
