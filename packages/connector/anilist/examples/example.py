import asyncio, os, cognee
from cognee_community_connector_anilist import anilist_source
DATASET = "anilist"
async def main():
    if not os.environ.get("ANILIST_USERNAME"):
        print("Set ANILIST_USERNAME")
        return
    await cognee.remember(anilist_source(), dataset_name=DATASET)
    ans = await cognee.search(query_text="What anime am I watching?", query_type=cognee.SearchType.GRAPH_COMPLETION, datasets=[DATASET])
    print(ans)
if __name__ == "__main__": asyncio.run(main())
