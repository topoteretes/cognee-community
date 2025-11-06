"""Demonstrate Ladybug adapter with Cognee's multi-user permissions."""

import asyncio
import os

import cognee
from cognee.modules.search.types import SearchType
from cognee.modules.users.exceptions import PermissionDeniedError
from cognee.modules.users.methods import create_user
from cognee.modules.users.permissions.methods import authorized_give_permission_on_datasets

from cognee_community_graph_adapter_ladybug import register


async def main() -> None:
    register()

    # Enable backend access control and force datasets to use Ladybug
    os.environ.setdefault("ENABLE_BACKEND_ACCESS_CONTROL", "True")
    cognee.config.set_graph_database_provider("ladybug")

    # Clean slate for the demo
    await cognee.prune.prune_data()
    await cognee.prune.prune_system()

    user_a = await create_user("ladybug_user_a@example.com", "example")
    user_b = await create_user("ladybug_user_b@example.com", "example")

    dataset_a = "ladybug_shared_notes"
    dataset_b = "ladybug_private_notes"

    await cognee.add(
        [
            "Ladybug databases are optimized for analytics-heavy workloads.",
            "They replaced the deprecated Kùzu project inside Cognee.",
        ],
        dataset_name=dataset_a,
        user=user_a,
    )
    await cognee.add(
        [
            "Quantum computers use superposition to evaluate many states at once.",
        ],
        dataset_name=dataset_b,
        user=user_b,
    )

    ai_cognify_result = await cognee.cognify([dataset_a], user=user_a)
    private_cognify_result = await cognee.cognify([dataset_b], user=user_b)

    dataset_b_id = next(iter(private_cognify_result.keys()))

    # User A cannot access User B's dataset until a permission is granted.
    try:
        await cognee.search(
            query_type=SearchType.GRAPH_COMPLETION,
            query_text="What do the private notes say?",
            user=user_a,
            datasets=[dataset_b_id],
        )
    except PermissionDeniedError:
        print("User A cannot read the private dataset yet (permission denied as expected).")

    # Give read access on dataset B to user A
    await authorized_give_permission_on_datasets(
        user_a.id,
        [dataset_b_id],
        "read",
        user_b.id,
    )

    shared_results = await cognee.search(
        query_type=SearchType.GRAPH_COMPLETION,
        query_text="Summarize the shared private notes",
        user=user_a,
        dataset_ids=[dataset_b_id],
    )

    for idx, item in enumerate(shared_results, start=1):
        print(f"{idx}. {item}")

    # Show dataset ids for reference
    dataset_a_id = next(iter(ai_cognify_result.keys()))
    print(f"Dataset IDs => public: {dataset_a_id}, private: {dataset_b_id}")


if __name__ == "__main__":
    asyncio.run(main())
