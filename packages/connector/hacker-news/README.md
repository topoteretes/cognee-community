# Hacker News Connector for cognee

This is a `dlt` source that reads top/best/new stories from the Hacker News Firebase API.

## Usage

```python
import cognee
from cognee_community_connector_hacker_news import hacker_news_stories

await cognee.remember(
    hacker_news_stories(story_type="top", max_items=200),
    dataset_name="hacker_news",
    primary_key="id",
    write_disposition="replace",
    max_rows_per_table=0,
)
```
