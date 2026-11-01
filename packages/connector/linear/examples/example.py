import asyncio
import os
import cognee

from cognee_community_connector_linear import linear_source

async def main():
    # Provide your Linear API Key in the environment
    # os.environ["LINEAR_API_KEY"] = "lin_api_..."
    
    # 1. Initialize the Linear dlt source
    # You can optionally pass team_ids to filter issues to specific teams
    source = linear_source()
    
    print("Ingesting Linear issues into cognee...")
    # 2. Add the source to cognee
    await cognee.add(source)
    
    print("Cognifying the ingested issues...")
    # 3. Cognify to extract entities and knowledge graphs
    await cognee.cognify()
    
    print("Done! You can now search your Linear issues.")
    
    # Example search
    # results = await cognee.search("What are the main tasks?")
    # for r in results:
    #     print(r)

if __name__ == "__main__":
    asyncio.run(main())
