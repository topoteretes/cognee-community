import os
import dlt
from cognee_community_connector_chargebee import chargebee_source

def main():
    # Set your Chargebee API key and site here, or export them in your terminal
    api_key = os.getenv("CHARGEBEE_API_KEY", "your_test_api_key")
    site = os.getenv("CHARGEBEE_SITE", "your_test_site_name")

    # Initialize the dlt pipeline
    pipeline = dlt.pipeline(
        pipeline_name="chargebee_pipeline",
        destination="duckdb",
        dataset_name="chargebee_data"
    )

    # Initialize the source
    source = chargebee_source(api_key=api_key, site=site)
    
    # Run the pipeline (Note: self_improvement=False keeps local tests cheap/free)
    print("Starting Chargebee sync...")
    info = pipeline.run(source)
    print(info)

if __name__ == "__main__":
    main()