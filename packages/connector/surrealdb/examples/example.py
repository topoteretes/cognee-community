import asyncio
from cognee_community_connector_surrealdb import SurrealDBConnector

async def main():
    connector = SurrealDBConnector(
        namespace="test_ns",
        database="test_db",
        url="http://localhost:8000",
        username="root",
        password="root",
        tables=["user", "product", "purchased"],
        limit_per_table=20,
    )

    print("Extracting multi-model records and graph edges from SurrealDB...")
    documents = await connector.fetch_all()
    print(f"Retrieved {len(documents)} records from SurrealDB.\n")

    for doc in documents[:5]:
        print(f"Table: {doc['table']} | ID: {doc['record_id']}")
        if doc['edge_in'] or doc['edge_out']:
            print(f"  Graph Edge: in={doc['edge_in']} -> out={doc['edge_out']}")
        print(f"  Preview: {doc['clean_text'][:120]}...\n")

if __name__ == "__main__":
    asyncio.run(main())
