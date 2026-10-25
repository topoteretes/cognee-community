import asyncio
from cognee_community_connector_helicone import HeliconeConnector

async def main():
    connector = HeliconeConnector(
        api_key="sk_helicone_your_api_key",
        base_url="https://api.helicone.ai",
        fetch_prompts_flag=True,
        fetch_datasets_flag=True,
        fetch_evaluators_flag=True,
        fetch_feedback_traces_flag=True,
        max_records=10,
    )

    print("Fetching prompt templates and evaluation benchmarks from Helicone...")
    data = await connector.fetch_all()

    print(f"Prompts ({len(data['prompts'])}):")
    for p in data["prompts"]:
        print(f" - [{p['model']}] {p['prompt_id']} (v{p['version']})")

    print(f"\nDatasets ({len(data['datasets'])}):")
    for d in data["datasets"]:
        print(f" - {d['name']} (Rows: {d['rows_count']})")

    print(f"\nEvaluators ({len(data['evaluators'])}):")
    for ev in data["evaluators"]:
        print(f" - [{ev['type']}] {ev['name']}")

    print(f"\nFeedback Traces ({len(data['feedbacks'])}):")
    for fb in data["feedbacks"]:
        print(f" - [{fb['rating']}] {fb['feedback_id']}: {fb['comment'][:40]}...")

if __name__ == "__main__":
    asyncio.run(main())
