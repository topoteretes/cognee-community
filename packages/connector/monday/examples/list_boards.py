"""Print the monday.com boards your token can read, with the ids to sync.

Run:
    export MONDAY_API_TOKEN=...
    python examples/list_boards.py
"""

from cognee_community_connector_monday import list_boards


def main():
    boards = list_boards()  # reads MONDAY_API_TOKEN
    if not boards:
        print("This token cannot read any boards.")
    for board in boards:
        print(f"{board['id']}  {board['name']}  ({board['workspace']})")
    print("\nPass the ids you want as MONDAY_BOARD_IDS=id1,id2 or board_ids=[...].")


if __name__ == "__main__":
    main()
