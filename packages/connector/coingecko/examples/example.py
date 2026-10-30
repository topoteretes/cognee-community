import os
from cognee_community_connector_coingecko import CoinGeckoConnector

def main():
    print("=== CoinGecko Cognee Connector Demonstration ===")
    connector = CoinGeckoConnector(vs_currency="usd")

    # Fetch top coins (Bitcoin, Ethereum, Solana)
    print("Fetching market data for bitcoin, ethereum, solana...")
    documents = connector.fetch_documents(
        ids=["bitcoin", "ethereum", "solana"],
        per_page=3
    )

    print(f"Retrieved {len(documents)} structured Cognee documents.\n")
    for doc in documents:
        print(f"Document ID: {doc['id']}")
        print(f"Metadata: {doc['metadata']}")
        print("Sample Text:\n" + "\n".join(doc['text'].splitlines()[:10]))
        print("-" * 50)

if __name__ == "__main__":
    main()
