import asyncio, os, cognee
from cognee_community_connector_chesscom import chesscom_source
DATASET = "chesscom"
async def main():
    if not os.environ.get("CHESSCOM_USERNAME"):
        print("Set CHESSCOM_USERNAME")
        return
    await cognee.remember(chesscom_source(), dataset_name=DATASET)
    ans = await cognee.search(query_text="What are my recent games?", query_type=cognee.SearchType.GRAPH_COMPLETION, datasets=[DATASET])
    print(ans)
if __name__ == "__main__": asyncio.run(main())
