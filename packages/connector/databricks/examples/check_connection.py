"""Check the Databricks token and what you selected, before the first sync.

Prints the workspace, what each selected folder and table
selection holds, whether lineage answers, and the warehouse's state (read
without starting it). Problems are listed in plain words.

Run:
    export DATABRICKS_HOST=https://dbc-1234abcd-5678.cloud.databricks.com
    export DATABRICKS_TOKEN=dapi...
    export DATABRICKS_PATHS=~/analytics            (folders, comma-separated)
    export DATABRICKS_TABLES=main.sales            (catalogs, schemas or tables)
    export DATABRICKS_WAREHOUSE_ID=1234abcd5678    (optional, for named queries)
    python examples/check_connection.py
"""

from cognee_community_connector_databricks import DatabricksConnectorError, check_connection


def main():
    try:
        report = check_connection()
    except (DatabricksConnectorError, ValueError) as error:
        print(f"Could not check the connection: {error}")
        return
    print(f"The token works for workspace {report['workspace_id']}.")
    for path, status in report["paths"].items():
        print(f"Notebooks under {path}: {status}")
    for scope, status in report["tables"].items():
        print(f"Tables in {scope}: {status}")
    print(f"Lineage: {report['lineage']}")
    print(f"Warehouse: {report['warehouse']}")
    for problem in report["problems"]:
        print(f"Problem: {problem}")
    if not report["problems"]:
        print("Ready to sync.")


if __name__ == "__main__":
    main()
