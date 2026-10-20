"""Check the Snowflake login before the first sync.

Logs in once with your key pair and prints who you are, the role and warehouse
a sync would use, and the key fingerprint to compare with Snowflake's. Login
problems are explained in plain words; warehouse problems are listed.

Run:
    export SNOWFLAKE_ACCOUNT=ORGNAME-ACCOUNTNAME
    export SNOWFLAKE_USER=<login name, on trial accounts your email>
    export SNOWFLAKE_PRIVATE_KEY_PATH=~/.snowflake/rsa_key.p8
    export SNOWFLAKE_WAREHOUSE=COMPUTE_WH        (optional)
    python examples/check_connection.py
"""

from cognee_community_connector_snowflake import SnowflakeConnectorError, check_connection


def main():
    try:
        report = check_connection()
    except (SnowflakeConnectorError, ValueError) as error:
        print(f"Could not log in: {error}")
        return
    print(f"Logged in as {report['user']} with role {report['role']}.")
    print(f"Warehouse: {report['warehouse']} ({report['warehouse_state']})")
    print(f"Key fingerprint: {report['key_fingerprint']}")
    print("It should match RSA_PUBLIC_KEY_FP in DESC USER.")
    for problem in report["problems"]:
        print(f"Problem: {problem}")
    if not report["problems"]:
        print("Ready to sync.")


if __name__ == "__main__":
    main()
