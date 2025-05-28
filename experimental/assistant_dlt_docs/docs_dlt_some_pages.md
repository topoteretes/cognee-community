----- https://dlthub.com/docs/tutorial/load-data-from-an-api -----

Version: 1.11.0 (latest)

On this page

This tutorial introduces you to foundational dlt concepts, demonstrating how to build a custom data pipeline that loads data from pure Python data structures to DuckDB. It starts with a simple example and progresses to more advanced topics and usage scenarios.

## What you will learn [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#what-you-will-learn "Direct link to What you will learn")

- Loading data from a list of Python dictionaries into DuckDB.
- Low-level API usage with a built-in HTTP client.
- Understand and manage data loading behaviors.
- Incrementally load new data and deduplicate existing data.
- Dynamic resource creation and reducing code redundancy.
- Group resources into sources.
- Securely handle secrets.
- Make reusable data sources.

## Prerequisites [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#prerequisites "Direct link to Prerequisites")

- Python 3.9 or higher installed
- Virtual environment set up

## Installing dlt [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#installing-dlt "Direct link to Installing dlt")

Before we start, make sure you have a Python virtual environment set up. Follow the instructions in the [installation guide](https://dlthub.com/docs/reference/installation) to create a new virtual environment and install dlt.

Verify that dlt is installed by running the following command in your terminal:

```codeBlockLines_RjmQ
dlt --version

```

## Quick start [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#quick-start "Direct link to Quick start")

For starters, let's load a list of Python dictionaries into DuckDB and inspect the created dataset. Here is the code:

```codeBlockLines_RjmQ
import dlt

data = [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]

pipeline = dlt.pipeline(
    pipeline_name="quick_start", destination="duckdb", dataset_name="mydata"
)
load_info = pipeline.run(data, table_name="users")

print(load_info)

```

When you look at the code above, you can see that we:

1. Import the `dlt` library.
2. Define our data to load.
3. Create a pipeline that loads data into DuckDB. Here we also specify the `pipeline_name` and `dataset_name`. We'll use both in a moment.
4. Run the pipeline.

Save this Python script with the name `quick_start_pipeline.py` and run the following command:

```codeBlockLines_RjmQ
python quick_start_pipeline.py

```

The output should look like:

```codeBlockLines_RjmQ
Pipeline quick_start completed in 0.59 seconds
1 load package(s) were loaded to destination duckdb and into dataset mydata
The duckdb destination used duckdb:////home/user-name/quick_start/quick_start.duckdb location to store data
Load package 1692364844.460054 is LOADED and contains no failed jobs

```

`dlt` just created a database schema called **mydata** (the `dataset_name`) with a table **users** in it.

### Explore data in Python [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#explore-data-in-python "Direct link to Explore data in Python")

You can use dlt [datasets](https://dlthub.com/docs/general-usage/dataset-access/dataset) to easily query the data in pure Python.

```codeBlockLines_RjmQ
# get the dataset
dataset = pipeline.dataset("mydata")

# get the user relation
table = dataset.users

# query the full table as dataframe
print(table.df())

# query the first 10 rows as arrow table
print(table.limit(10).arrow())

```

### Explore data in Streamlit [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#explore-data-in-streamlit "Direct link to Explore data in Streamlit")

To allow a sneak peek and basic discovery, you can take advantage of [built-in integration with Streamlit](https://dlthub.com/docs/reference/command-line-interface#dlt-pipeline-show):

```codeBlockLines_RjmQ
dlt pipeline quick_start show

```

**quick\_start** is the name of the pipeline from the script above. If you do not have Streamlit installed yet, do:

```codeBlockLines_RjmQ
pip install streamlit

```

Now you should see the **users** table:

![Streamlit Explore data](https://dlthub.com/docs/assets/images/streamlit-new-1a373dbae5d5d59643d6336317ad7c43.png)
Streamlit Explore data. Schema and data for a test pipeline “quick\_start”.

tip

`dlt` works in Jupyter Notebook and Google Colab! See our [Quickstart Colab Demo.](https://colab.research.google.com/drive/1NfSB1DpwbbHX9_t5vlalBTf13utwpMGx?usp=sharing)

Looking for the source code of all the snippets? You can find and run them [from this repository](https://github.com/dlt-hub/dlt/blob/devel/docs/website/docs/getting-started-snippets.py).

Now that you have a basic understanding of how to get started with dlt, you might be eager to dive deeper. For that, we need to switch to a more advanced data source - the GitHub API. We will load issues from our [dlt-hub/dlt](https://github.com/dlt-hub/dlt) repository.

note

This tutorial uses the GitHub REST API for demonstration purposes only. If you need to read data from a REST API, consider using dlt's REST API source. Check out the [REST API source tutorial](https://dlthub.com/docs/tutorial/rest-api) for a quick start or the [REST API source reference](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api) for more details.

## Create a pipeline [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#create-a-pipeline "Direct link to Create a pipeline")

First, we need to create a [pipeline](https://dlthub.com/docs/general-usage/pipeline). Pipelines are the main building blocks of `dlt` and are used to load data from sources to destinations. Open your favorite text editor and create a file called `github_issues.py`. Add the following code to it:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers import requests

# Specify the URL of the API endpoint
url = "https://api.github.com/repos/dlt-hub/dlt/issues"
# Make a request and check if it was successful
response = requests.get(url)
response.raise_for_status()

pipeline = dlt.pipeline(
    pipeline_name="github_issues",
    destination="duckdb",
    dataset_name="github_data",
)
# The response contains a list of issues
load_info = pipeline.run(response.json(), table_name="issues")

print(load_info)

```

Here's what the code above does:

1. It makes a request to the GitHub API endpoint and checks if the response is successful.
2. Then, it creates a dlt pipeline with the name `github_issues` and specifies that the data should be loaded to the `duckdb` destination and the `github_data` dataset. Nothing gets loaded yet.
3. Finally, it runs the pipeline with the data from the API response ( `response.json()`) and specifies that the data should be loaded to the `issues` table. The `run` method returns a `LoadInfo` object that contains information about the loaded data.

## Run the pipeline [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#run-the-pipeline "Direct link to Run the pipeline")

Save `github_issues.py` and run the following command:

```codeBlockLines_RjmQ
python github_issues.py

```

Once the data has been loaded, you can inspect the created dataset using the Streamlit app:

```codeBlockLines_RjmQ
dlt pipeline github_issues show

```

## Append or replace your data [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#append-or-replace-your-data "Direct link to Append or replace your data")

Try running the pipeline again with `python github_issues.py`. You will notice that the **issues** table contains two copies of the same data. This happens because the default load mode is `append`. It is very useful, for example, when you have daily data updates and you want to ingest them.

To get the latest data, we'd need to run the script again. But how to do that without duplicating the data?
One option is to tell `dlt` to replace the data in existing tables in the destination by using the `replace` write disposition. Change the `github_issues.py` script to the following:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers import requests

# Specify the URL of the API endpoint
url = "https://api.github.com/repos/dlt-hub/dlt/issues"
# Make a request and check if it was successful
response = requests.get(url)
response.raise_for_status()

pipeline = dlt.pipeline(
    pipeline_name='github_issues',
    destination='duckdb',
    dataset_name='github_data',
)
# The response contains a list of issues
load_info = pipeline.run(
    response.json(),
    table_name="issues",
    write_disposition="replace"  # <-- Add this line
)

print(load_info)

```

Run this script twice to see that the **issues** table still contains only one copy of the data.

tip

What if the API has changed and new fields get added to the response?
`dlt` will migrate your tables!
See the `replace` mode and table schema migration in action in our [Schema evolution colab demo](https://colab.research.google.com/drive/1H6HKFi-U1V4p0afVucw_Jzv1oiFbH2bu#scrollTo=e4y4sQ78P_OM).

Learn more:

- [Full load - how to replace your data](https://dlthub.com/docs/general-usage/full-loading).
- [Append, replace, and merge your tables](https://dlthub.com/docs/general-usage/incremental-loading).

## Declare loading behavior [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#declare-loading-behavior "Direct link to Declare loading behavior")

So far, we have been passing the data to the `run` method directly. This is a quick way to get started. However, frequently, you receive data in chunks, and you want to load it as it arrives. For example, you might want to load data from an API endpoint with pagination or a large file that does not fit in memory. In such cases, you can use Python generators as a data source.

You can pass a generator to the `run` method directly or use the `@dlt.resource` decorator to turn the generator into a [dlt resource](https://dlthub.com/docs/general-usage/resource). The decorator allows you to specify the loading behavior and relevant resource parameters.

### Load only new data (incremental loading) [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#load-only-new-data-incremental-loading "Direct link to Load only new data (incremental loading)")

Let's improve our GitHub API example and get only issues that were created since the last load.
Instead of using the `replace` write disposition and downloading all issues each time the pipeline is run, we do the following:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers import requests

@dlt.resource(table_name="issues", write_disposition="append")
def get_issues(
    created_at=dlt.sources.incremental("created_at", initial_value="1970-01-01T00:00:00Z")
):
    # NOTE: we read only open issues to minimize number of calls to the API.
    # There's a limit of ~50 calls for not authenticated Github users.
    url = (
        "https://api.github.com/repos/dlt-hub/dlt/issues"
        "?per_page=100&sort=created&directions=desc&state=open"
    )

    while True:
        response = requests.get(url)
        response.raise_for_status()
        yield response.json()

        # Stop requesting pages if the last element was already
        # older than initial value
        # Note: incremental will skip those items anyway, we just
        # do not want to use the api limits
        if created_at.start_out_of_range:
            break

        # get next page
        if "next" not in response.links:
            break
        url = response.links["next"]["url"]

pipeline = dlt.pipeline(
    pipeline_name="github_issues_incremental",
    destination="duckdb",
    dataset_name="github_data_append",
)

load_info = pipeline.run(get_issues)
row_counts = pipeline.last_trace.last_normalize_info

print(row_counts)
print("------")
print(load_info)

```

Let's take a closer look at the code above.

We use the `@dlt.resource` decorator to declare the table name into which data will be loaded and specify the `append` write disposition.

We request issues for the dlt-hub/dlt repository ordered by the **created\_at** field (descending) and yield them page by page in the `get_issues` generator function.

We also use `dlt.sources.incremental` to track the `created_at` field present in each issue to filter in the newly created ones.

Now run the script. It loads all the issues from our repo to `duckdb`. Run it again, and you can see that no issues got added (if no issues were created in the meantime).

Now you can run this script on a daily schedule, and each day you’ll load only issues created after the time of the previous pipeline run.

tip

Between pipeline runs, `dlt` keeps the state in the same database it loaded data into.
Peek into that state, the tables loaded, and get other information with:

```codeBlockLines_RjmQ
dlt pipeline -v github_issues_incremental info

```

Learn more:

- Declare your [resources](https://dlthub.com/docs/general-usage/resource) and group them in [sources](https://dlthub.com/docs/general-usage/source) using Python decorators.
- [Set up "last value" incremental loading.](https://dlthub.com/docs/general-usage/incremental/cursor)
- [Inspect pipeline after loading.](https://dlthub.com/docs/walkthroughs/run-a-pipeline#4-inspect-a-load-process)
- [`dlt` command line interface.](https://dlthub.com/docs/reference/command-line-interface)

### Update and deduplicate your data [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#update-and-deduplicate-your-data "Direct link to Update and deduplicate your data")

The script above finds **new** issues and adds them to the database.
It will ignore any updates to **existing** issue text, emoji reactions, etc.
To always get fresh content of all the issues, combine incremental load with the `merge` write disposition,
like in the script below.

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers import requests

@dlt.resource(
    table_name="issues",
    write_disposition="merge",
    primary_key="id",
)
def get_issues(
    updated_at=dlt.sources.incremental("updated_at", initial_value="1970-01-01T00:00:00Z")
):
    # NOTE: we read only open issues to minimize number of calls to
    # the API. There's a limit of ~50 calls for not authenticated
    # Github users
    url = (
        "https://api.github.com/repos/dlt-hub/dlt/issues"
        f"?since={updated_at.last_value}&per_page=100&sort=updated"
        "&directions=desc&state=open"
    )

    while True:
        response = requests.get(url)
        response.raise_for_status()
        yield response.json()

        # Get next page
        if "next" not in response.links:
            break
        url = response.links["next"]["url"]

pipeline = dlt.pipeline(
    pipeline_name="github_issues_merge",
    destination="duckdb",
    dataset_name="github_data_merge",
)
load_info = pipeline.run(get_issues)
row_counts = pipeline.last_trace.last_normalize_info

print(row_counts)
print("------")
print(load_info)

```

Above, we add the `primary_key` argument to the `dlt.resource()` that tells `dlt` how to identify the issues in the database to find duplicates whose content it will merge.

Note that we now track the `updated_at` field — so we filter in all issues **updated** since the last pipeline run (which also includes those newly created).

Pay attention to how we use the **since** parameter from the [GitHub API](https://docs.github.com/en/rest/issues/issues?apiVersion=2022-11-28#list-repository-issues)
and `updated_at.last_value` to tell GitHub to return issues updated only **after** the date we pass. `updated_at.last_value` holds the last `updated_at` value from the previous run.

[Learn more about merge write disposition](https://dlthub.com/docs/general-usage/merge-loading).

## Using pagination helper [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#using-pagination-helper "Direct link to Using pagination helper")

In the previous examples, we used the `requests` library to make HTTP requests to the GitHub API and handled pagination manually. `dlt` has a built-in [REST client](https://dlthub.com/docs/general-usage/http/rest-client) that simplifies API requests. We'll use the `paginate()` helper from it for the next example. The `paginate` function takes a URL and optional parameters (quite similar to `requests`) and returns a generator that yields pages of data.

Here's how the updated script looks:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers.rest_client import paginate

@dlt.resource(
    table_name="issues",
    write_disposition="merge",
    primary_key="id",
)
def get_issues(
    updated_at=dlt.sources.incremental("updated_at", initial_value="1970-01-01T00:00:00Z")
):
    for page in paginate(
        "https://api.github.com/repos/dlt-hub/dlt/issues",
        params={
            "since": updated_at.last_value,
            "per_page": 100,
            "sort": "updated",
            "direction": "desc",
            "state": "open",
        },
    ):
        yield page

pipeline = dlt.pipeline(
    pipeline_name="github_issues_merge",
    destination="duckdb",
    dataset_name="github_data_merge",
)
load_info = pipeline.run(get_issues)
row_counts = pipeline.last_trace.last_normalize_info

print(row_counts)
print("------")
print(load_info)

```

Let's zoom in on the changes:

1. The `while` loop that handled pagination is replaced with reading pages from the `paginate()` generator.
2. `paginate()` takes the URL of the API endpoint and optional parameters. In this case, we pass the `since` parameter to get only issues updated after the last pipeline run.
3. We're not explicitly setting up pagination; `paginate()` handles it for us. Magic! Under the hood, `paginate()` analyzes the response and detects the pagination method used by the API. Read more about pagination in the [REST client documentation](https://dlthub.com/docs/general-usage/http/rest-client#paginating-api-responses).

If you want to take full advantage of the `dlt` library, then we strongly suggest that you build your sources out of existing building blocks:
To make the most of `dlt`, consider the following:

## Use source decorator [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#use-source-decorator "Direct link to Use source decorator")

In the previous step, we loaded issues from the GitHub API. Now we'll load comments from the API as well. Here's a sample [dlt resource](https://dlthub.com/docs/general-usage/resource) that does that:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers.rest_client import paginate

@dlt.resource(
    table_name="comments",
    write_disposition="merge",
    primary_key="id",
)
def get_comments(
    updated_at=dlt.sources.incremental("updated_at", initial_value="1970-01-01T00:00:00Z")
):
    for page in paginate(
        "https://api.github.com/repos/dlt-hub/dlt/comments",
        params={"per_page": 100}
    ):
        yield page

```

We can load this resource separately from the issues resource; however, loading both issues and comments in one go is more efficient. To do that, we'll use the `@dlt.source` decorator on a function that returns a list of resources:

```codeBlockLines_RjmQ
@dlt.source
def github_source():
    return [get_issues, get_comments]

```

`github_source()` groups resources into a [source](https://dlthub.com/docs/general-usage/source). A dlt source is a logical grouping of resources. You use it to group resources that belong together, for example, to load data from the same API. Loading data from a source can be run in a single pipeline. Here's what our updated script looks like:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers.rest_client import paginate

@dlt.resource(
    table_name="issues",
    write_disposition="merge",
    primary_key="id",
)
def get_issues(
    updated_at=dlt.sources.incremental("updated_at", initial_value="1970-01-01T00:00:00Z")
):
    for page in paginate(
        "https://api.github.com/repos/dlt-hub/dlt/issues",
        params={
            "since": updated_at.last_value,
            "per_page": 100,
            "sort": "updated",
            "direction": "desc",
            "state": "open",
        }
    ):
        yield page

@dlt.resource(
    table_name="comments",
    write_disposition="merge",
    primary_key="id",
)
def get_comments(
    updated_at=dlt.sources.incremental("updated_at", initial_value="1970-01-01T00:00:00Z")
):
    for page in paginate(
        "https://api.github.com/repos/dlt-hub/dlt/comments",
        params={
            "since": updated_at.last_value,
            "per_page": 100,
        }
    ):
        yield page

@dlt.source
def github_source():
    return [get_issues, get_comments]

pipeline = dlt.pipeline(
    pipeline_name='github_with_source',
    destination='duckdb',
    dataset_name='github_data',
)

load_info = pipeline.run(github_source())
print(load_info)

```

### Dynamic resources [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#dynamic-resources "Direct link to Dynamic resources")

You've noticed that there's a lot of code duplication in the `get_issues` and `get_comments` functions. We can reduce that by extracting the common fetching code into a separate function and using it in both resources. Even better, we can use `dlt.resource` as a function and pass it the `fetch_github_data()` generator function directly. Here's the refactored code:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers.rest_client import paginate

BASE_GITHUB_URL = "https://api.github.com/repos/dlt-hub/dlt"

def fetch_github_data(endpoint, params={}):
    url = f"{BASE_GITHUB_URL}/{endpoint}"
    return paginate(url, params=params)

@dlt.source
def github_source():
    for endpoint in ["issues", "comments"]:
        params = {"per_page": 100}
        yield dlt.resource(
            fetch_github_data(endpoint, params),
            name=endpoint,
            write_disposition="merge",
            primary_key="id",
        )

pipeline = dlt.pipeline(
    pipeline_name='github_dynamic_source',
    destination='duckdb',
    dataset_name='github_data',
)
load_info = pipeline.run(github_source())
row_counts = pipeline.last_trace.last_normalize_info

```

## Handle secrets [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#handle-secrets "Direct link to Handle secrets")

For the next step, we'd want to get the [number of repository clones](https://docs.github.com/en/rest/metrics/traffic?apiVersion=2022-11-28#get-repository-clones) for our dlt repo from the GitHub API. However, the `traffic/clones` endpoint that returns the data requires [authentication](https://docs.github.com/en/rest/overview/authenticating-to-the-rest-api?apiVersion=2022-11-28).

Let's handle this by changing our `fetch_github_data()` function first:

```codeBlockLines_RjmQ
from dlt.sources.helpers.rest_client.auth import BearerTokenAuth

def fetch_github_data_with_token(endpoint, params={}, access_token=None):
    url = f"{BASE_GITHUB_URL}/{endpoint}"
    return paginate(
        url,
        params=params,
        auth=BearerTokenAuth(token=access_token) if access_token else None,
    )

@dlt.source
def github_source_with_token(access_token: str):
    for endpoint in ["issues", "comments", "traffic/clones"]:
        params = {"per_page": 100}
        yield dlt.resource(
            fetch_github_data_with_token(endpoint, params, access_token),
            name=endpoint,
            write_disposition="merge",
            primary_key="id",
        )

...

```

Here, we added an `access_token` parameter and now we can use it to pass the access token to the request:

```codeBlockLines_RjmQ
load_info = pipeline.run(github_source_with_token(access_token="ghp_XXXXX"))

```

It's a good start. But we'd want to follow the best practices and not hardcode the token in the script. One option is to set the token as an environment variable, load it with `os.getenv()`, and pass it around as a parameter. dlt offers a more convenient way to handle secrets and credentials: it lets you inject the arguments using a special `dlt.secrets.value` argument value.

To use it, change the `github_source()` function to:

```codeBlockLines_RjmQ
@dlt.source
def github_source_with_token(
    access_token: str = dlt.secrets.value,
):
    ...

```

When you add `dlt.secrets.value` as a default value for an argument, `dlt` will try to load and inject this value from different configuration sources in the following order:

1. Special environment variables.
2. `secrets.toml` file.

The `secrets.toml` file is located in the `~/.dlt` folder (for global configuration) or in the `.dlt` folder in the project folder (for project-specific configuration).

Let's add the token to the `~/.dlt/secrets.toml` file:

```codeBlockLines_RjmQ
[github_with_source_secrets]
access_token = "ghp_A...3aRY"

```

Now we can run the script and it will load the data from the `traffic/clones` endpoint:

```codeBlockLines_RjmQ
...

@dlt.source
def github_source_with_token(
    access_token: str = dlt.secrets.value,
):
    for endpoint in ["issues", "comments", "traffic/clones"]:
        params = {"per_page": 100}
        yield dlt.resource(
            fetch_github_data_with_token(endpoint, params, access_token),
            name=endpoint,
            write_disposition="merge",
            primary_key="id",
        )

pipeline = dlt.pipeline(
    pipeline_name="github_with_source_secrets",
    destination="duckdb",
    dataset_name="github_data",
)
load_info = pipeline.run(github_source())

```

## Configurable sources [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#configurable-sources "Direct link to Configurable sources")

The next step is to make our dlt GitHub source reusable so it can load data from any GitHub repo. We'll do that by changing both the `github_source()` and `fetch_github_data()` functions to accept the repo name as a parameter:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers.rest_client import paginate

BASE_GITHUB_URL = "https://api.github.com/repos/{repo_name}"

def fetch_github_data_with_token_and_params(repo_name, endpoint, params={}, access_token=None):
    """Fetch data from the GitHub API based on repo_name, endpoint, and params."""
    url = BASE_GITHUB_URL.format(repo_name=repo_name) + f"/{endpoint}"
    return paginate(
        url,
        params=params,
        auth=BearerTokenAuth(token=access_token) if access_token else None,
    )

@dlt.source
def github_source_with_token_and_repo(
    repo_name: str = dlt.config.value,
    access_token: str = dlt.secrets.value,
):
    for endpoint in ["issues", "comments", "traffic/clones"]:
        params = {"per_page": 100}
        yield dlt.resource(
            fetch_github_data_with_token_and_params(repo_name, endpoint, params, access_token),
            name=endpoint,
            write_disposition="merge",
            primary_key="id",
        )

pipeline = dlt.pipeline(
    pipeline_name="github_with_source_secrets",
    destination="duckdb",
    dataset_name="github_data",
)
load_info = pipeline.run(github_source())

```

Next, create a `.dlt/config.toml` file in the project folder and add the `repo_name` parameter to it:

```codeBlockLines_RjmQ
[github_with_source_secrets]
repo_name = "dlt-hub/dlt"

```

That's it! Now you have a reusable source that can load data from any GitHub repo.

## What’s next [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#whats-next "Direct link to What’s next")

Congratulations on completing the tutorial! You've come a long way since the [getting started](https://dlthub.com/docs/intro) guide. By now, you've mastered loading data from various GitHub API endpoints, organizing resources into sources, managing secrets securely, and creating reusable sources. You can use these skills to build your own pipelines and load data from any source.

Interested in learning more? Here are some suggestions:

1. You've been running your pipelines locally. Learn how to [deploy and run them in the cloud](https://dlthub.com/docs/walkthroughs/deploy-a-pipeline/).
2. Dive deeper into how dlt works by reading the [Using dlt](https://dlthub.com/docs/general-usage) section. Some highlights:
   - [Set up "last value" incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor).
   - Learn about data loading strategies: append, [replace](https://dlthub.com/docs/general-usage/full-loading), and [merge](https://dlthub.com/docs/general-usage/merge-loading).
   - [Connect the transformers to the resources](https://dlthub.com/docs/general-usage/resource#process-resources-with-dlttransformer) to load additional data or enrich it.
   - [Customize your data schema—set primary and merge keys, define column nullability, and specify data types](https://dlthub.com/docs/general-usage/resource#define-schema).
   - [Create your resources dynamically from data](https://dlthub.com/docs/general-usage/source#create-resources-dynamically).
   - [Transform your data before loading](https://dlthub.com/docs/general-usage/resource#customize-resources) and see some [examples of customizations like column renames and anonymization](https://dlthub.com/docs/general-usage/customising-pipelines/renaming_columns).
   - Employ data transformations using [SQL](https://dlthub.com/docs/dlt-ecosystem/transformations/sql) or [Pandas](https://dlthub.com/docs/dlt-ecosystem/transformations/sql).
   - [Pass config and credentials into your sources and resources](https://dlthub.com/docs/general-usage/credentials).
   - [Run in production: inspecting, tracing, retry policies, and cleaning up](https://dlthub.com/docs/running-in-production/running).
   - [Run resources in parallel, optimize buffers, and local storage](https://dlthub.com/docs/reference/performance)
   - [Use REST API client helpers](https://dlthub.com/docs/general-usage/http/rest-client) to simplify working with REST APIs.
3. Explore [destinations](https://dlthub.com/docs/dlt-ecosystem/destinations/) and [sources](https://dlthub.com/docs/dlt-ecosystem/verified-sources/) provided by us and the community.
4. Explore the [Examples](https://dlthub.com/docs/examples) section to see how dlt can be used in real-world scenarios.

- [What you will learn](https://dlthub.com/docs/tutorial/load-data-from-an-api#what-you-will-learn)
- [Prerequisites](https://dlthub.com/docs/tutorial/load-data-from-an-api#prerequisites)
- [Installing dlt](https://dlthub.com/docs/tutorial/load-data-from-an-api#installing-dlt)
- [Quick start](https://dlthub.com/docs/tutorial/load-data-from-an-api#quick-start)
  - [Explore data in Python](https://dlthub.com/docs/tutorial/load-data-from-an-api#explore-data-in-python)
  - [Explore data in Streamlit](https://dlthub.com/docs/tutorial/load-data-from-an-api#explore-data-in-streamlit)
- [Create a pipeline](https://dlthub.com/docs/tutorial/load-data-from-an-api#create-a-pipeline)
- [Run the pipeline](https://dlthub.com/docs/tutorial/load-data-from-an-api#run-the-pipeline)
- [Append or replace your data](https://dlthub.com/docs/tutorial/load-data-from-an-api#append-or-replace-your-data)
- [Declare loading behavior](https://dlthub.com/docs/tutorial/load-data-from-an-api#declare-loading-behavior)
  - [Load only new data (incremental loading)](https://dlthub.com/docs/tutorial/load-data-from-an-api#load-only-new-data-incremental-loading)
  - [Update and deduplicate your data](https://dlthub.com/docs/tutorial/load-data-from-an-api#update-and-deduplicate-your-data)
- [Using pagination helper](https://dlthub.com/docs/tutorial/load-data-from-an-api#using-pagination-helper)
- [Use source decorator](https://dlthub.com/docs/tutorial/load-data-from-an-api#use-source-decorator)
  - [Dynamic resources](https://dlthub.com/docs/tutorial/load-data-from-an-api#dynamic-resources)
- [Handle secrets](https://dlthub.com/docs/tutorial/load-data-from-an-api#handle-secrets)
- [Configurable sources](https://dlthub.com/docs/tutorial/load-data-from-an-api#configurable-sources)
- [What’s next](https://dlthub.com/docs/tutorial/load-data-from-an-api#whats-next)

----- https://dlthub.com/docs/general-usage/incremental-loading -----

PreferencesDeclineAccept

Version: 1.11.0 (latest)

On this page

Incremental loading is the act of loading only new or changed data and not old records that we have already loaded. It enables low-latency and low-cost data transfer.

The challenge of incremental pipelines is that if we do not keep track of the state of the load (i.e., which increments were loaded and which are to be loaded), we may encounter issues. Read more about state [here](https://dlthub.com/docs/general-usage/state).

## Choosing a write disposition [​](https://dlthub.com/docs/general-usage/incremental-loading\#choosing-a-write-disposition "Direct link to Choosing a write disposition")

### The 3 write dispositions: [​](https://dlthub.com/docs/general-usage/incremental-loading\#the-3-write-dispositions "Direct link to The 3 write dispositions:")

- **Full load**: replaces the destination dataset with whatever the source produced on this run. To achieve this, use `write_disposition='replace'` in your resources. Learn more in the [full loading docs](https://dlthub.com/docs/general-usage/full-loading).

- **Append**: appends the new data to the destination. Use `write_disposition='append'`.

- **Merge**: Merges new data into the destination using `merge_key` and/or deduplicates/upserts new data using `primary_key`. Use `write_disposition='merge'`.

### How to choose the right write disposition [​](https://dlthub.com/docs/general-usage/incremental-loading\#how-to-choose-the-right-write-disposition "Direct link to How to choose the right write disposition")

![write disposition flowchart](https://storage.googleapis.com/dlt-blog-images/flowchart_for_scd2.png)

The "write disposition" you choose depends on the dataset and how you can extract it.

To find the "write disposition" you should use, the first question you should ask yourself is "Is my data stateful or stateless"? Stateful data has a state that is subject to change - for example, a user's profile. Stateless data cannot change - for example, a recorded event, such as a page view.

Because stateless data does not need to be updated, we can just append it.

For stateful data, comes a second question - Can I extract it incrementally from the source? If yes, you should use [slowly changing dimensions (Type-2)](https://dlthub.com/docs/general-usage/merge-loading#scd2-strategy), which allow you to maintain historical records of data changes over time.

If not, then we need to replace the entire dataset. However, if we can request the data incrementally, such as "all users added or modified since yesterday," then we can simply apply changes to our existing dataset with the merge write disposition.

## Incremental loading strategies [​](https://dlthub.com/docs/general-usage/incremental-loading\#incremental-loading-strategies "Direct link to Incremental loading strategies")

dlt provides several approaches to incremental loading:

1. [Merge strategies](https://dlthub.com/docs/general-usage/merge-loading#merge-strategies) \- Choose between delete-insert, SCD2, and upsert approaches to incrementally update your data
2. [Cursor-based incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor) \- Track changes using a cursor field (like timestamp or ID)
3. [Lag / Attribution window](https://dlthub.com/docs/general-usage/incremental/lag) \- Refresh data within a specific time window
4. [Advanced state management](https://dlthub.com/docs/general-usage/incremental/advanced-state) \- Custom state tracking

## Doing a full refresh [​](https://dlthub.com/docs/general-usage/incremental-loading\#doing-a-full-refresh "Direct link to Doing a full refresh")

You may force a full refresh of `merge` and `append` pipelines:

1. In the case of a `merge`, the data in the destination is deleted and loaded fresh. Currently, we do not deduplicate data during the full refresh.
2. In the case of `dlt.sources.incremental`, the data is deleted and loaded from scratch. The state of the incremental is reset to the initial value.

Example:

```codeBlockLines_RjmQ
p = dlt.pipeline(destination="bigquery", dataset_name="dataset_name")
# Do a full refresh
p.run(merge_source(), write_disposition="replace")
# Do a full refresh of just one table
p.run(merge_source().with_resources("merge_table"), write_disposition="replace")
# Run a normal merge
p.run(merge_source())

```

Passing write disposition to `replace` will change the write disposition on all the resources in
`repo_events` during the run of the pipeline.

## Next steps [​](https://dlthub.com/docs/general-usage/incremental-loading\#next-steps "Direct link to Next steps")

- [Cursor-based incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor) \- Use timestamps or IDs to track changes
- [Advanced state management](https://dlthub.com/docs/general-usage/incremental/advanced-state) \- Advanced techniques for state tracking
- [Walkthroughs: Add incremental configuration to SQL resources](https://dlthub.com/docs/walkthroughs/sql-incremental-configuration) \- Step-by-step examples
- [Troubleshooting incremental loading](https://dlthub.com/docs/general-usage/incremental/troubleshooting) \- Common issues and how to fix them

- [Choosing a write disposition](https://dlthub.com/docs/general-usage/incremental-loading#choosing-a-write-disposition)
  - [The 3 write dispositions:](https://dlthub.com/docs/general-usage/incremental-loading#the-3-write-dispositions)
  - [How to choose the right write disposition](https://dlthub.com/docs/general-usage/incremental-loading#how-to-choose-the-right-write-disposition)
- [Incremental loading strategies](https://dlthub.com/docs/general-usage/incremental-loading#incremental-loading-strategies)
- [Doing a full refresh](https://dlthub.com/docs/general-usage/incremental-loading#doing-a-full-refresh)
- [Next steps](https://dlthub.com/docs/general-usage/incremental-loading#next-steps)

----- https://dlthub.com/docs/general-usage/resource -----

Version: 1.11.0 (latest)

On this page

## Declare a resource [​](https://dlthub.com/docs/general-usage/resource\#declare-a-resource "Direct link to Declare a resource")

A [resource](https://dlthub.com/docs/general-usage/glossary#resource) is an ( [optionally async](https://dlthub.com/docs/reference/performance#parallelism-within-a-pipeline)) function that yields data. To create a resource, we add the `@dlt.resource` decorator to that function.

Commonly used arguments:

- `name`: The name of the table generated by this resource. Defaults to the decorated function name.
- `write_disposition`: How should the data be loaded at the destination? Currently supported: `append`,
`replace`, and `merge`. Defaults to `append.`

Example:

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows():
	for i in range(10):
		yield {'id': i, 'example_string': 'abc'}

@dlt.source
def source_name():
    return generate_rows

```

To get the data of a resource, we could do:

```codeBlockLines_RjmQ
for row in generate_rows():
    print(row)

for row in source_name().resources.get('table_name'):
    print(row)

```

Typically, resources are declared and grouped with related resources within a [source](https://dlthub.com/docs/general-usage/source) function.

### Define schema [​](https://dlthub.com/docs/general-usage/resource\#define-schema "Direct link to Define schema")

`dlt` will infer the [schema](https://dlthub.com/docs/general-usage/schema) for tables associated with resources from the resource's data.
You can modify the generation process by using the table and column hints. The resource decorator accepts the following arguments:

1. `table_name`: the name of the table, if different from the resource name.
2. `primary_key` and `merge_key`: define the name of the columns (compound keys are allowed) that will receive those hints. Used in [incremental loading](https://dlthub.com/docs/general-usage/incremental-loading) and [merge loading](https://dlthub.com/docs/general-usage/merge-loading).
3. `columns`: lets you define one or more columns, including the data types, nullability, and other hints. The column definition is a `TypedDict`: `TTableSchemaColumns`. In the example below, we tell `dlt` that the column `tags` (containing a list of tags) in the `user` table should have type `json`, which means that it will be loaded as JSON/struct and not as a separate nested table.

```codeBlockLines_RjmQ
@dlt.resource(name="user", columns={"tags": {"data_type": "json"}})
def get_users():
  ...

# the `table_schema` method gets the table schema generated by a resource
print(get_users().compute_table_schema())

```

note

You can pass dynamic hints which are functions that take the data item as input and return a hint value. This lets you create table and column schemas depending on the data. See an [example below](https://dlthub.com/docs/general-usage/resource#adjust-schema-when-you-yield-data).

### Put a contract on tables, columns, and data [​](https://dlthub.com/docs/general-usage/resource\#put-a-contract-on-tables-columns-and-data "Direct link to Put a contract on tables, columns, and data")

Use the `schema_contract` argument to tell dlt how to [deal with new tables, data types, and bad data types](https://dlthub.com/docs/general-usage/schema-contracts). For example, if you set it to **freeze**, `dlt` will not allow for any new tables, columns, or data types to be introduced to the schema - it will raise an exception. Learn more about available contract modes [here](https://dlthub.com/docs/general-usage/schema-contracts#setting-up-the-contract).

### Define schema of nested tables [​](https://dlthub.com/docs/general-usage/resource\#define-schema-of-nested-tables "Direct link to Define schema of nested tables")

`dlt` creates [nested tables](https://dlthub.com/docs/general-usage/schema#nested-references-root-and-nested-tables) to store [list of objects](https://dlthub.com/docs/general-usage/destination-tables#nested-tables) if present in your data.
You can define the schema of such tables with `nested_hints` argument to `@dlt.resource`:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(
    nested_hints={
        "purchases": dlt.mark.make_nested_hints(
            columns=[{"name": "price", "data_type": "decimal"}],
            schema_contract={"columns": "freeze"},
        )
    },
)
def customers():
    """Load customer data from a simple python list."""
    yield [\
        {\
            "id": 1,\
            "name": "simon",\
            "city": "berlin",\
            "purchases": [{"id": 1, "name": "apple", "price": "1.50"}],\
        },\
    ]

```

Here we convert the `price` field in list of `purchases` to decimal type and set the schema contract to lock the list
of columns in it. We use convenience function `dlt.mark.make_nested_hints` to generate nested hints dictionary. You are
free to use it directly.

Mind that `purchases` list will be stored as table with name `customers__purchases`. When declaring nested hints you just need
to specify nested field(s) name(s). In case of deeper nesting ie. let's say each `purchase` has a list of `coupons` applied,
you can apply hints to coupons and define `customers__purchases__coupons` table schema:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(
    nested_hints={
        "purchases": {},
        ("purchases", "coupons"): {
            "columns": {"registered_at": {"data_type": "timestamp"}}
        }
    },
)
def customers():
    ...

```

Here we use `("purchases", "coupons")` to locate list at the depth of 2 and set the data type on `registered_at` column
to `timestamp`. We do that by directly using nested hints dict.
Note that we specified `purchases` with an empty list of hints. **You are required to specify all parent hints, even if they**
**are empty. Currently we are not adding missing path elements automatically**.

You can use `nested_hints` primarily to set column hints and schema contract, those work exactly as in case of root tables.

- `file_format` has no effect (not implemented yet)
- `write_disposition` works as expected but leads to unintended consequences (ie. you can set nested table to `replace`) while root table is `append`.
- `references` will create [table references](https://dlthub.com/docs/general-usage/schema#table-references-1) (annotations) as expected.
- `primary_key` and `merge_key`: **setting those will convert nested table into a regular table, with a separate write disposition, file format etc.** [It allows you to create custom table relationships ie. using natural primary and foreign keys present in the data.](https://dlthub.com/docs/general-usage/schema#generate-custom-linking-for-nested-tables)

tip

[REST API Source](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic) accepts `nested_hints` argument as well.

You can apply nested hints after the resource was created by using [apply\_hints](https://dlthub.com/docs/general-usage/resource#set-table-name-and-adjust-schema).

### Define a schema with Pydantic [​](https://dlthub.com/docs/general-usage/resource\#define-a-schema-with-pydantic "Direct link to Define a schema with Pydantic")

You can alternatively use a [Pydantic](https://pydantic-docs.helpmanual.io/) model to define the schema.
For example:

```codeBlockLines_RjmQ
from pydantic import BaseModel
from typing import List, Optional, Union

class Address(BaseModel):
    street: str
    city: str
    postal_code: str

class User(BaseModel):
    id: int
    name: str
    tags: List[str]
    email: Optional[str]
    address: Address
    status: Union[int, str]

@dlt.resource(name="user", columns=User)
def get_users():
    ...

```

The data types of the table columns are inferred from the types of the Pydantic fields. These use the same type conversions
as when the schema is automatically generated from the data.

Pydantic models integrate well with [schema contracts](https://dlthub.com/docs/general-usage/schema-contracts) as data validators.

Things to note:

- Fields with an `Optional` type are marked as `nullable`.
- Fields with a `Union` type are converted to the first (not `None`) type listed in the union. For example, `status: Union[int, str]` results in a `bigint` column.
- `list`, `dict`, and nested Pydantic model fields will use the `json` type, which means they'll be stored as a JSON object in the database instead of creating nested tables.

You can override this by configuring the Pydantic model:

```codeBlockLines_RjmQ
from typing import ClassVar
from dlt.common.libs.pydantic import DltConfig

class UserWithNesting(User):
  dlt_config: ClassVar[DltConfig] = {"skip_nested_types": True}

@dlt.resource(name="user", columns=UserWithNesting)
def get_users():
    ...

```

`"skip_nested_types"` omits any `dict`/ `list`/ `BaseModel` type fields from the schema, so dlt will fall back on the default
behavior of creating nested tables for these fields.

We do not support `RootModel` that validate simple types. You can add such a validator yourself, see [data filtering section](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data).

### Dispatch data to many tables [​](https://dlthub.com/docs/general-usage/resource\#dispatch-data-to-many-tables "Direct link to Dispatch data to many tables")

You can load data to many tables from a single resource. The most common case is a stream of events
of different types, each with a different data schema. To deal with this, you can use the `table_name`
argument on `dlt.resource`. You could pass the table name as a function with the data item as an
argument and the `table_name` string as a return value.

For example, a resource that loads GitHub repository events wants to send `issue`, `pull request`,
and `comment` events to separate tables. The type of the event is in the "type" field.

```codeBlockLines_RjmQ
# send item to a table with name item["type"]
@dlt.resource(table_name=lambda event: event['type'])
def repo_events() -> Iterator[TDataItems]:
    yield item

# the `table_schema` method gets the table schema generated by a resource and takes an optional
# data item to evaluate dynamic hints
print(repo_events().compute_table_schema({"type": "WatchEvent", "id": ...}))

```

In more advanced cases, you can dispatch data to different tables directly in the code of the
resource function:

```codeBlockLines_RjmQ
@dlt.resource
def repo_events() -> Iterator[TDataItems]:
    # mark the "item" to be sent to the table with the name item["type"]
    yield dlt.mark.with_table_name(item, item["type"])

```

### Parametrize a resource [​](https://dlthub.com/docs/general-usage/resource\#parametrize-a-resource "Direct link to Parametrize a resource")

You can add arguments to your resource functions like to any other. Below we parametrize our
`generate_rows` resource to generate the number of rows we request:

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

for row in generate_rows(10):
    print(row)

for row in generate_rows(20):
    print(row)

```

tip

You can mark some resource arguments as [configuration and credentials](https://dlthub.com/docs/general-usage/credentials) values so `dlt` can pass them automatically to your functions.

### Process resources with `dlt.transformer` [​](https://dlthub.com/docs/general-usage/resource\#process-resources-with-dlttransformer "Direct link to process-resources-with-dlttransformer")

You can feed data from one resource into another. The most common case is when you have an API that returns a list of objects (i.e., users) in one endpoint and user details in another. You can deal with this by declaring a resource that obtains a list of users and another resource that receives items from the list and downloads the profiles.

```codeBlockLines_RjmQ
@dlt.resource(write_disposition="replace")
def users(limit=None):
    for u in _get_users(limit):
        yield u

# Feed data from users as user_item below,
# all transformers must have at least one
# argument that will receive data from the parent resource
@dlt.transformer(data_from=users)
def users_details(user_item):
    for detail in _get_details(user_item["user_id"]):
        yield detail

# Just load the users_details.
# dlt figures out dependencies for you.
pipeline.run(users_details)

```

In the example above, `users_details` will receive data from the default instance of the `users` resource (with `limit` set to `None`). You can also use the **pipe \|** operator to bind resources dynamically.

```codeBlockLines_RjmQ
# You can be more explicit and use a pipe operator.
# With it, you can create dynamic pipelines where the dependencies
# are set at run time and resources are parametrized, i.e.,
# below we want to load only 100 users from the `users` endpoint.
pipeline.run(users(limit=100) | users_details)

```

tip

Transformers are allowed not only to **yield** but also to **return** values and can decorate **async** functions and [**async generators**](https://dlthub.com/docs/reference/performance#extract). Below we decorate an async function and request details on two pokemons. HTTP calls are made in parallel via the httpx library.

```codeBlockLines_RjmQ
import dlt
import httpx

@dlt.transformer
async def pokemon(id):
    async with httpx.AsyncClient() as client:
        r = await client.get(f"https://pokeapi.co/api/v2/pokemon/{id}")
        return r.json()

# Get Bulbasaur and Ivysaur (you need dlt 0.4.6 for the pipe operator working with lists).
print(list([1,2] | pokemon()))

```

### Declare a standalone resource [​](https://dlthub.com/docs/general-usage/resource\#declare-a-standalone-resource "Direct link to Declare a standalone resource")

A standalone resource is defined on a function that is top-level in a module (not an inner function) that accepts config and secrets values. Additionally, if the `standalone` flag is specified, the decorated function signature and docstring will be preserved. `dlt.resource` will just wrap the decorated function, and the user must call the wrapper to get the actual resource. Below we declare a `filesystem` resource that must be called before use.

```codeBlockLines_RjmQ
@dlt.resource(standalone=True)
def fs_resource(bucket_url=dlt.config.value):
  """List and yield files in `bucket_url`."""
  ...

# `filesystem` must be called before it is extracted or used in any other way.
pipeline.run(fs_resource("s3://my-bucket/reports"), table_name="reports")

```

Standalone may have a dynamic name that depends on the arguments passed to the decorated function. For example:

```codeBlockLines_RjmQ
@dlt.resource(standalone=True, name=lambda args: args["stream_name"])
def kinesis(stream_name: str):
    ...

kinesis_stream = kinesis("telemetry_stream")

```

`kinesis_stream` resource has a name **telemetry\_stream**.

### Declare parallel and async resources [​](https://dlthub.com/docs/general-usage/resource\#declare-parallel-and-async-resources "Direct link to Declare parallel and async resources")

You can extract multiple resources in parallel threads or with async IO.
To enable this for a sync resource, you can set the `parallelized` flag to `True` in the resource decorator:

```codeBlockLines_RjmQ
@dlt.resource(parallelized=True)
def get_users():
    for u in _get_users():
        yield u

@dlt.resource(parallelized=True)
def get_orders():
    for o in _get_orders():
        yield o

# users and orders will be iterated in parallel in two separate threads
pipeline.run([get_users(), get_orders()])

```

Async generators are automatically extracted concurrently with other resources:

```codeBlockLines_RjmQ
@dlt.resource
async def get_users():
    async for u in _get_users():  # Assuming _get_users is an async generator
        yield u

```

Please find more details in [extract performance](https://dlthub.com/docs/reference/performance#extract)

## Customize resources [​](https://dlthub.com/docs/general-usage/resource\#customize-resources "Direct link to Customize resources")

### Filter, transform, and pivot data [​](https://dlthub.com/docs/general-usage/resource\#filter-transform-and-pivot-data "Direct link to Filter, transform, and pivot data")

You can attach any number of transformations that are evaluated on an item-per-item basis to your
resource. The available transformation types:

- **map** \- transform the data item ( `resource.add_map`).
- **filter** \- filter the data item ( `resource.add_filter`).
- **yield map** \- a map that returns an iterator (so a single row may generate many rows -
`resource.add_yield_map`).

Example: We have a resource that loads a list of users from an API endpoint. We want to customize it
so:

1. We remove users with `user_id == "me"`.
2. We anonymize user data.

Here's our resource:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(write_disposition="replace")
def users():
    ...
    users = requests.get(RESOURCE_URL)
    ...
    yield users

```

Here's our script that defines transformations and loads the data:

```codeBlockLines_RjmQ
from pipedrive import users

def anonymize_user(user_data):
    user_data["user_id"] = _hash_str(user_data["user_id"])
    user_data["user_email"] = _hash_str(user_data["user_email"])
    return user_data

# add the filter and anonymize function to users resource and enumerate
for user in users().add_filter(lambda user: user["user_id"] != "me").add_map(anonymize_user):
    print(user)

```

### Reduce the nesting level of generated tables [​](https://dlthub.com/docs/general-usage/resource\#reduce-the-nesting-level-of-generated-tables "Direct link to Reduce the nesting level of generated tables")

You can limit how deep `dlt` goes when generating nested tables and flattening dicts into columns. By default, the library will descend
and generate nested tables for all nested lists, without limit.

note

`max_table_nesting` is optional so you can skip it, in this case, dlt will
use it from the source if it is specified there or fallback to the default
value which has 1000 as the maximum nesting level.

```codeBlockLines_RjmQ
import dlt

@dlt.resource(max_table_nesting=1)
def my_resource():
    yield {
        "id": 1,
        "name": "random name",
        "properties": [\
            {\
                "name": "customer_age",\
                "type": "int",\
                "label": "Age",\
                "notes": [\
                    {\
                        "text": "string",\
                        "author": "string",\
                    }\
                ]\
            }\
        ]
    }

```

In the example above, we want only 1 level of nested tables to be generated (so there are no nested
tables of a nested table). Typical settings:

- `max_table_nesting=0` will not generate nested tables and will not flatten dicts into columns at all. All nested data will be
represented as JSON.
- `max_table_nesting=1` will generate nested tables of root tables and nothing more. All nested
data in nested tables will be represented as JSON.

You can achieve the same effect after the resource instance is created:

```codeBlockLines_RjmQ
resource = my_resource()
resource.max_table_nesting = 0

```

Several data sources are prone to contain semi-structured documents with very deep nesting, i.e.,
MongoDB databases. Our practical experience is that setting the `max_nesting_level` to 2 or 3
produces the clearest and human-readable schemas.

### Sample from large data [​](https://dlthub.com/docs/general-usage/resource\#sample-from-large-data "Direct link to Sample from large data")

If your resource loads thousands of pages of data from a REST API or millions of rows from a database table, you may want to sample just a fragment of it in order to quickly see the dataset with example data and test your transformations, etc. To do this, you limit how many items will be yielded by a resource (or source) by calling the `add_limit` method. This method will close the generator that produces the data after the limit is reached.

In the example below, we load just the first 10 items from an infinite counter - that would otherwise never end.

```codeBlockLines_RjmQ
r = dlt.resource(itertools.count(), name="infinity").add_limit(10)
assert list(r) == list(range(10))

```

note

Note that `add_limit` **does not limit the number of records** but rather the "number of yields". Depending on how your resource is set up, the number of extracted rows may vary. For example, consider this resource:

```codeBlockLines_RjmQ
@dlt.resource
def my_resource():
    for i in range(100):
        yield [{"record_id": j} for j in range(15)]

dlt.pipeline(destination="duckdb").run(my_resource().add_limit(10))

```

The code above will extract `15*10=150` records. This is happening because in each iteration, 15 records are yielded, and we're limiting the number of iterations to 10.

Altenatively you can also apply a time limit to the resource. The code below will run the extraction for 10 seconds and extract how ever many items are yielded in that time. In combination with incrementals, this can be useful for batched loading or for loading on machines that have a run time limit.

```codeBlockLines_RjmQ
dlt.pipeline(destination="duckdb").run(my_resource().add_limit(max_time=10))

```

You can also apply a combination of both limits. In this case the extraction will stop as soon as either limit is reached.

```codeBlockLines_RjmQ
dlt.pipeline(destination="duckdb").run(my_resource().add_limit(max_items=10, max_time=10))

```

Some notes about the `add_limit`:

1. `add_limit` does not skip any items. It closes the iterator/generator that produces data after the limit is reached.
2. You cannot limit transformers. They should process all the data they receive fully to avoid inconsistencies in generated datasets.
3. Async resources with a limit added may occasionally produce one item more than the limit on some runs. This behavior is not deterministic.
4. Calling add limit on a resource will replace any previously set limits settings.
5. For time-limited resources, the timer starts when the first item is processed. When resources are processed sequentially (FIFO mode), each resource's time limit applies also sequentially. In the default round robin mode, the time limits will usually run concurrently.

tip

If you are parameterizing the value of `add_limit` and sometimes need it to be disabled, you can set `None` or `-1` to disable the limiting.
You can also set the limit to `0` for the resource to not yield any items.

### Set table name and adjust schema [​](https://dlthub.com/docs/general-usage/resource\#set-table-name-and-adjust-schema "Direct link to Set table name and adjust schema")

You can change the schema of a resource, whether it is standalone or part of a source. Look for a method named `apply_hints` which takes the same arguments as the resource decorator. Obviously, you should call this method before data is extracted from the resource. The example below converts an `append` resource loading the `users` table into a [merge](https://dlthub.com/docs/general-usage/merge-loading) resource that will keep just one updated record per `user_id`. It also adds ["last value" incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor) on the `created_at` column to prevent requesting again the already loaded records:

```codeBlockLines_RjmQ
tables = sql_database()
tables.users.apply_hints(
    write_disposition="merge",
    primary_key="user_id",
    incremental=dlt.sources.incremental("updated_at")
)
pipeline.run(tables)

```

To change the name of a table to which the resource will load data, do the following:

```codeBlockLines_RjmQ
tables = sql_database()
tables.users.table_name = "other_users"

```

### Adjust schema when you yield data [​](https://dlthub.com/docs/general-usage/resource\#adjust-schema-when-you-yield-data "Direct link to Adjust schema when you yield data")

You can set or update the table name, columns, and other schema elements when your resource is executed, and you already yield data. Such changes will be merged with the existing schema in the same way the `apply_hints` method above works. There are many reasons to adjust the schema at runtime. For example, when using Airflow, you should avoid lengthy operations (i.e., reflecting database tables) during the creation of the DAG, so it is better to do it when the DAG executes. You may also emit partial hints (i.e., precision and scale for decimal types) for columns to help `dlt` type inference.

```codeBlockLines_RjmQ
@dlt.resource
def sql_table(credentials, schema, table):
    # Create a SQL Alchemy engine
    engine = engine_from_credentials(credentials)
    engine.execution_options(stream_results=True)
    metadata = MetaData(schema=schema)
    # Reflect the table schema
    table_obj = Table(table, metadata, autoload_with=engine)

    for idx, batch in enumerate(table_rows(engine, table_obj)):
      if idx == 0:
        # Emit the first row with hints, table_to_columns and _get_primary_key are helpers that extract dlt schema from
        # SqlAlchemy model
        yield dlt.mark.with_hints(
            batch,
            dlt.mark.make_hints(columns=table_to_columns(table_obj), primary_key=_get_primary_key(table_obj)),
        )
      else:
        # Just yield all the other rows
        yield batch

```

In the example above, we use `dlt.mark.with_hints` and `dlt.mark.make_hints` to emit columns and primary key with the first extracted item. The table schema will be adjusted after the `batch` is processed in the extract pipeline but before any schema contracts are applied, and data is persisted in the load package.

tip

You can emit columns as a Pydantic model and use dynamic hints (i.e., lambda for table name) as well. You should avoid redefining `Incremental` this way.

### Import external files [​](https://dlthub.com/docs/general-usage/resource\#import-external-files "Direct link to Import external files")

You can import external files, i.e., CSV, Parquet, and JSONL, by yielding items marked with `with_file_import`, optionally passing a table schema corresponding to the imported file. dlt will not read, parse, or normalize any names (i.e., CSV or Arrow headers) and will attempt to copy the file into the destination as is.

```codeBlockLines_RjmQ
import os
import dlt
from dlt.sources.filesystem import filesystem

columns: List[TColumnSchema] = [\
    {"name": "id", "data_type": "bigint"},\
    {"name": "name", "data_type": "text"},\
    {"name": "description", "data_type": "text"},\
    {"name": "ordered_at", "data_type": "date"},\
    {"name": "price", "data_type": "decimal"},\
]

import_folder = "/tmp/import"

@dlt.transformer(columns=columns)
def orders(items: Iterator[FileItemDict]):
  for item in items:
    # copy the file locally
      dest_file = os.path.join(import_folder, item["file_name"])
      # download the file
      item.fsspec.download(item["file_url"], dest_file)
      # tell dlt to import the dest_file as `csv`
      yield dlt.mark.with_file_import(dest_file, "csv")

# use the filesystem core source to glob a bucket

downloader = filesystem(
  bucket_url="s3://my_bucket/csv",
  file_glob="today/*.csv.gz") | orders

info = pipeline.run(orders, destination="snowflake")

```

In the example above, we glob all zipped csv files present on **my\_bucket/csv/today** (using the `filesystem` verified source) and send file descriptors to the `orders` transformer. The transformer downloads and imports the files into the extract package. At the end, `dlt` sends them to Snowflake (the table will be created because we use `column` hints to define the schema).

If imported `csv` files are not in `dlt` [default format](https://dlthub.com/docs/dlt-ecosystem/file-formats/csv#default-settings), you may need to pass additional configuration.

```codeBlockLines_RjmQ
[destination.snowflake.csv_format]
delimiter="|"
include_header=false
on_error_continue=true

```

You can sniff the schema from the data, i.e., using DuckDB to infer the table schema from a CSV file. `dlt.mark.with_file_import` accepts additional arguments that you can use to pass hints at runtime.

note

- If you do not define any columns, the table will not be created in the destination. `dlt` will still attempt to load data into it, so if you create a fitting table upfront, the load process will succeed.
- Files are imported using hard links if possible to avoid copying and duplicating the storage space needed.

### Duplicate and rename resources [​](https://dlthub.com/docs/general-usage/resource\#duplicate-and-rename-resources "Direct link to Duplicate and rename resources")

There are cases when your resources are generic (i.e., bucket filesystem) and you want to load several instances of it (i.e., files from different folders) into separate tables. In the example below, we use the `filesystem` source to load csvs from two different folders into separate tables:

```codeBlockLines_RjmQ
@dlt.resource(standalone=True)
def fs_resource(bucket_url):
  # list and yield files in bucket_url
  ...

@dlt.transformer
def csv_reader(file_item):
  # load csv, parse, and yield rows in file_item
  ...

# create two extract pipes that list files from the bucket and send them to the reader.
# by default, both pipes will load data to the same table (csv_reader)
reports_pipe = fs_resource("s3://my-bucket/reports") | csv_reader()
transactions_pipe = fs_resource("s3://my-bucket/transactions") | csv_reader()

# so we rename resources to load to "reports" and "transactions" tables
pipeline.run(
  [reports_pipe.with_name("reports"), transactions_pipe.with_name("transactions")]
)

```

The `with_name` method returns a deep copy of the original resource, its data pipe, and the data pipes of a parent resource. A renamed clone is fully separated from the original resource (and other clones) when loading: it maintains a separate [resource state](https://dlthub.com/docs/general-usage/state#read-and-write-pipeline-state-in-a-resource) and will load to a table.

## Load resources [​](https://dlthub.com/docs/general-usage/resource\#load-resources "Direct link to Load resources")

You can pass individual resources or a list of resources to the `dlt.pipeline` object. The resources loaded outside the source context will be added to the [default schema](https://dlthub.com/docs/general-usage/schema) of the pipeline.

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

pipeline = dlt.pipeline(
    pipeline_name="rows_pipeline",
    destination="duckdb",
    dataset_name="rows_data"
)
# load an individual resource
pipeline.run(generate_rows(10))
# load a list of resources
pipeline.run([generate_rows(10), generate_rows(20)])

```

### Pick loader file format for a particular resource [​](https://dlthub.com/docs/general-usage/resource\#pick-loader-file-format-for-a-particular-resource "Direct link to Pick loader file format for a particular resource")

You can request a particular loader file format to be used for a resource.

```codeBlockLines_RjmQ
@dlt.resource(file_format="parquet")
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

```

The resource above will be saved and loaded from a Parquet file (if the destination supports it).

note

A special `file_format`: **preferred** will load the resource using a format that is preferred by a destination. This setting supersedes the `loader_file_format` passed to the `run` method.

### Do a full refresh [​](https://dlthub.com/docs/general-usage/resource\#do-a-full-refresh "Direct link to Do a full refresh")

To do a full refresh of an `append` or `merge` resource, you set the `refresh` argument on the `run` method to `drop_data`. This will truncate the tables without dropping them.

```codeBlockLines_RjmQ
p.run(merge_source(), refresh="drop_data")

```

You can also [fully drop the tables](https://dlthub.com/docs/general-usage/pipeline#refresh-pipeline-data-and-state) in the `merge_source`:

```codeBlockLines_RjmQ
p.run(merge_source(), refresh="drop_sources")

```

- [Declare a resource](https://dlthub.com/docs/general-usage/resource#declare-a-resource)
  - [Define schema](https://dlthub.com/docs/general-usage/resource#define-schema)
  - [Put a contract on tables, columns, and data](https://dlthub.com/docs/general-usage/resource#put-a-contract-on-tables-columns-and-data)
  - [Define schema of nested tables](https://dlthub.com/docs/general-usage/resource#define-schema-of-nested-tables)
  - [Define a schema with Pydantic](https://dlthub.com/docs/general-usage/resource#define-a-schema-with-pydantic)
  - [Dispatch data to many tables](https://dlthub.com/docs/general-usage/resource#dispatch-data-to-many-tables)
  - [Parametrize a resource](https://dlthub.com/docs/general-usage/resource#parametrize-a-resource)
  - [Process resources with `dlt.transformer`](https://dlthub.com/docs/general-usage/resource#process-resources-with-dlttransformer)
  - [Declare a standalone resource](https://dlthub.com/docs/general-usage/resource#declare-a-standalone-resource)
  - [Declare parallel and async resources](https://dlthub.com/docs/general-usage/resource#declare-parallel-and-async-resources)
- [Customize resources](https://dlthub.com/docs/general-usage/resource#customize-resources)
  - [Filter, transform, and pivot data](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data)
  - [Reduce the nesting level of generated tables](https://dlthub.com/docs/general-usage/resource#reduce-the-nesting-level-of-generated-tables)
  - [Sample from large data](https://dlthub.com/docs/general-usage/resource#sample-from-large-data)
  - [Set table name and adjust schema](https://dlthub.com/docs/general-usage/resource#set-table-name-and-adjust-schema)
  - [Adjust schema when you yield data](https://dlthub.com/docs/general-usage/resource#adjust-schema-when-you-yield-data)
  - [Import external files](https://dlthub.com/docs/general-usage/resource#import-external-files)
  - [Duplicate and rename resources](https://dlthub.com/docs/general-usage/resource#duplicate-and-rename-resources)
- [Load resources](https://dlthub.com/docs/general-usage/resource#load-resources)
  - [Pick loader file format for a particular resource](https://dlthub.com/docs/general-usage/resource#pick-loader-file-format-for-a-particular-resource)
  - [Do a full refresh](https://dlthub.com/docs/general-usage/resource#do-a-full-refresh)

----- https://dlthub.com/docs/general-usage/resource#process-resources-with-dlttransformer -----

Version: 1.11.0 (latest)

On this page

## Declare a resource [​](https://dlthub.com/docs/general-usage/resource\#declare-a-resource "Direct link to Declare a resource")

A [resource](https://dlthub.com/docs/general-usage/glossary#resource) is an ( [optionally async](https://dlthub.com/docs/reference/performance#parallelism-within-a-pipeline)) function that yields data. To create a resource, we add the `@dlt.resource` decorator to that function.

Commonly used arguments:

- `name`: The name of the table generated by this resource. Defaults to the decorated function name.
- `write_disposition`: How should the data be loaded at the destination? Currently supported: `append`,
`replace`, and `merge`. Defaults to `append.`

Example:

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows():
	for i in range(10):
		yield {'id': i, 'example_string': 'abc'}

@dlt.source
def source_name():
    return generate_rows

```

To get the data of a resource, we could do:

```codeBlockLines_RjmQ
for row in generate_rows():
    print(row)

for row in source_name().resources.get('table_name'):
    print(row)

```

Typically, resources are declared and grouped with related resources within a [source](https://dlthub.com/docs/general-usage/source) function.

### Define schema [​](https://dlthub.com/docs/general-usage/resource\#define-schema "Direct link to Define schema")

`dlt` will infer the [schema](https://dlthub.com/docs/general-usage/schema) for tables associated with resources from the resource's data.
You can modify the generation process by using the table and column hints. The resource decorator accepts the following arguments:

1. `table_name`: the name of the table, if different from the resource name.
2. `primary_key` and `merge_key`: define the name of the columns (compound keys are allowed) that will receive those hints. Used in [incremental loading](https://dlthub.com/docs/general-usage/incremental-loading) and [merge loading](https://dlthub.com/docs/general-usage/merge-loading).
3. `columns`: lets you define one or more columns, including the data types, nullability, and other hints. The column definition is a `TypedDict`: `TTableSchemaColumns`. In the example below, we tell `dlt` that the column `tags` (containing a list of tags) in the `user` table should have type `json`, which means that it will be loaded as JSON/struct and not as a separate nested table.

```codeBlockLines_RjmQ
@dlt.resource(name="user", columns={"tags": {"data_type": "json"}})
def get_users():
  ...

# the `table_schema` method gets the table schema generated by a resource
print(get_users().compute_table_schema())

```

note

You can pass dynamic hints which are functions that take the data item as input and return a hint value. This lets you create table and column schemas depending on the data. See an [example below](https://dlthub.com/docs/general-usage/resource#adjust-schema-when-you-yield-data).

### Put a contract on tables, columns, and data [​](https://dlthub.com/docs/general-usage/resource\#put-a-contract-on-tables-columns-and-data "Direct link to Put a contract on tables, columns, and data")

Use the `schema_contract` argument to tell dlt how to [deal with new tables, data types, and bad data types](https://dlthub.com/docs/general-usage/schema-contracts). For example, if you set it to **freeze**, `dlt` will not allow for any new tables, columns, or data types to be introduced to the schema - it will raise an exception. Learn more about available contract modes [here](https://dlthub.com/docs/general-usage/schema-contracts#setting-up-the-contract).

### Define schema of nested tables [​](https://dlthub.com/docs/general-usage/resource\#define-schema-of-nested-tables "Direct link to Define schema of nested tables")

`dlt` creates [nested tables](https://dlthub.com/docs/general-usage/schema#nested-references-root-and-nested-tables) to store [list of objects](https://dlthub.com/docs/general-usage/destination-tables#nested-tables) if present in your data.
You can define the schema of such tables with `nested_hints` argument to `@dlt.resource`:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(
    nested_hints={
        "purchases": dlt.mark.make_nested_hints(
            columns=[{"name": "price", "data_type": "decimal"}],
            schema_contract={"columns": "freeze"},
        )
    },
)
def customers():
    """Load customer data from a simple python list."""
    yield [\
        {\
            "id": 1,\
            "name": "simon",\
            "city": "berlin",\
            "purchases": [{"id": 1, "name": "apple", "price": "1.50"}],\
        },\
    ]

```

Here we convert the `price` field in list of `purchases` to decimal type and set the schema contract to lock the list
of columns in it. We use convenience function `dlt.mark.make_nested_hints` to generate nested hints dictionary. You are
free to use it directly.

Mind that `purchases` list will be stored as table with name `customers__purchases`. When declaring nested hints you just need
to specify nested field(s) name(s). In case of deeper nesting ie. let's say each `purchase` has a list of `coupons` applied,
you can apply hints to coupons and define `customers__purchases__coupons` table schema:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(
    nested_hints={
        "purchases": {},
        ("purchases", "coupons"): {
            "columns": {"registered_at": {"data_type": "timestamp"}}
        }
    },
)
def customers():
    ...

```

Here we use `("purchases", "coupons")` to locate list at the depth of 2 and set the data type on `registered_at` column
to `timestamp`. We do that by directly using nested hints dict.
Note that we specified `purchases` with an empty list of hints. **You are required to specify all parent hints, even if they**
**are empty. Currently we are not adding missing path elements automatically**.

You can use `nested_hints` primarily to set column hints and schema contract, those work exactly as in case of root tables.

- `file_format` has no effect (not implemented yet)
- `write_disposition` works as expected but leads to unintended consequences (ie. you can set nested table to `replace`) while root table is `append`.
- `references` will create [table references](https://dlthub.com/docs/general-usage/schema#table-references-1) (annotations) as expected.
- `primary_key` and `merge_key`: **setting those will convert nested table into a regular table, with a separate write disposition, file format etc.** [It allows you to create custom table relationships ie. using natural primary and foreign keys present in the data.](https://dlthub.com/docs/general-usage/schema#generate-custom-linking-for-nested-tables)

tip

[REST API Source](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic) accepts `nested_hints` argument as well.

You can apply nested hints after the resource was created by using [apply\_hints](https://dlthub.com/docs/general-usage/resource#set-table-name-and-adjust-schema).

### Define a schema with Pydantic [​](https://dlthub.com/docs/general-usage/resource\#define-a-schema-with-pydantic "Direct link to Define a schema with Pydantic")

You can alternatively use a [Pydantic](https://pydantic-docs.helpmanual.io/) model to define the schema.
For example:

```codeBlockLines_RjmQ
from pydantic import BaseModel
from typing import List, Optional, Union

class Address(BaseModel):
    street: str
    city: str
    postal_code: str

class User(BaseModel):
    id: int
    name: str
    tags: List[str]
    email: Optional[str]
    address: Address
    status: Union[int, str]

@dlt.resource(name="user", columns=User)
def get_users():
    ...

```

The data types of the table columns are inferred from the types of the Pydantic fields. These use the same type conversions
as when the schema is automatically generated from the data.

Pydantic models integrate well with [schema contracts](https://dlthub.com/docs/general-usage/schema-contracts) as data validators.

Things to note:

- Fields with an `Optional` type are marked as `nullable`.
- Fields with a `Union` type are converted to the first (not `None`) type listed in the union. For example, `status: Union[int, str]` results in a `bigint` column.
- `list`, `dict`, and nested Pydantic model fields will use the `json` type, which means they'll be stored as a JSON object in the database instead of creating nested tables.

You can override this by configuring the Pydantic model:

```codeBlockLines_RjmQ
from typing import ClassVar
from dlt.common.libs.pydantic import DltConfig

class UserWithNesting(User):
  dlt_config: ClassVar[DltConfig] = {"skip_nested_types": True}

@dlt.resource(name="user", columns=UserWithNesting)
def get_users():
    ...

```

`"skip_nested_types"` omits any `dict`/ `list`/ `BaseModel` type fields from the schema, so dlt will fall back on the default
behavior of creating nested tables for these fields.

We do not support `RootModel` that validate simple types. You can add such a validator yourself, see [data filtering section](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data).

### Dispatch data to many tables [​](https://dlthub.com/docs/general-usage/resource\#dispatch-data-to-many-tables "Direct link to Dispatch data to many tables")

You can load data to many tables from a single resource. The most common case is a stream of events
of different types, each with a different data schema. To deal with this, you can use the `table_name`
argument on `dlt.resource`. You could pass the table name as a function with the data item as an
argument and the `table_name` string as a return value.

For example, a resource that loads GitHub repository events wants to send `issue`, `pull request`,
and `comment` events to separate tables. The type of the event is in the "type" field.

```codeBlockLines_RjmQ
# send item to a table with name item["type"]
@dlt.resource(table_name=lambda event: event['type'])
def repo_events() -> Iterator[TDataItems]:
    yield item

# the `table_schema` method gets the table schema generated by a resource and takes an optional
# data item to evaluate dynamic hints
print(repo_events().compute_table_schema({"type": "WatchEvent", "id": ...}))

```

In more advanced cases, you can dispatch data to different tables directly in the code of the
resource function:

```codeBlockLines_RjmQ
@dlt.resource
def repo_events() -> Iterator[TDataItems]:
    # mark the "item" to be sent to the table with the name item["type"]
    yield dlt.mark.with_table_name(item, item["type"])

```

### Parametrize a resource [​](https://dlthub.com/docs/general-usage/resource\#parametrize-a-resource "Direct link to Parametrize a resource")

You can add arguments to your resource functions like to any other. Below we parametrize our
`generate_rows` resource to generate the number of rows we request:

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

for row in generate_rows(10):
    print(row)

for row in generate_rows(20):
    print(row)

```

tip

You can mark some resource arguments as [configuration and credentials](https://dlthub.com/docs/general-usage/credentials) values so `dlt` can pass them automatically to your functions.

### Process resources with `dlt.transformer` [​](https://dlthub.com/docs/general-usage/resource\#process-resources-with-dlttransformer "Direct link to process-resources-with-dlttransformer")

You can feed data from one resource into another. The most common case is when you have an API that returns a list of objects (i.e., users) in one endpoint and user details in another. You can deal with this by declaring a resource that obtains a list of users and another resource that receives items from the list and downloads the profiles.

```codeBlockLines_RjmQ
@dlt.resource(write_disposition="replace")
def users(limit=None):
    for u in _get_users(limit):
        yield u

# Feed data from users as user_item below,
# all transformers must have at least one
# argument that will receive data from the parent resource
@dlt.transformer(data_from=users)
def users_details(user_item):
    for detail in _get_details(user_item["user_id"]):
        yield detail

# Just load the users_details.
# dlt figures out dependencies for you.
pipeline.run(users_details)

```

In the example above, `users_details` will receive data from the default instance of the `users` resource (with `limit` set to `None`). You can also use the **pipe \|** operator to bind resources dynamically.

```codeBlockLines_RjmQ
# You can be more explicit and use a pipe operator.
# With it, you can create dynamic pipelines where the dependencies
# are set at run time and resources are parametrized, i.e.,
# below we want to load only 100 users from the `users` endpoint.
pipeline.run(users(limit=100) | users_details)

```

tip

Transformers are allowed not only to **yield** but also to **return** values and can decorate **async** functions and [**async generators**](https://dlthub.com/docs/reference/performance#extract). Below we decorate an async function and request details on two pokemons. HTTP calls are made in parallel via the httpx library.

```codeBlockLines_RjmQ
import dlt
import httpx

@dlt.transformer
async def pokemon(id):
    async with httpx.AsyncClient() as client:
        r = await client.get(f"https://pokeapi.co/api/v2/pokemon/{id}")
        return r.json()

# Get Bulbasaur and Ivysaur (you need dlt 0.4.6 for the pipe operator working with lists).
print(list([1,2] | pokemon()))

```

### Declare a standalone resource [​](https://dlthub.com/docs/general-usage/resource\#declare-a-standalone-resource "Direct link to Declare a standalone resource")

A standalone resource is defined on a function that is top-level in a module (not an inner function) that accepts config and secrets values. Additionally, if the `standalone` flag is specified, the decorated function signature and docstring will be preserved. `dlt.resource` will just wrap the decorated function, and the user must call the wrapper to get the actual resource. Below we declare a `filesystem` resource that must be called before use.

```codeBlockLines_RjmQ
@dlt.resource(standalone=True)
def fs_resource(bucket_url=dlt.config.value):
  """List and yield files in `bucket_url`."""
  ...

# `filesystem` must be called before it is extracted or used in any other way.
pipeline.run(fs_resource("s3://my-bucket/reports"), table_name="reports")

```

Standalone may have a dynamic name that depends on the arguments passed to the decorated function. For example:

```codeBlockLines_RjmQ
@dlt.resource(standalone=True, name=lambda args: args["stream_name"])
def kinesis(stream_name: str):
    ...

kinesis_stream = kinesis("telemetry_stream")

```

`kinesis_stream` resource has a name **telemetry\_stream**.

### Declare parallel and async resources [​](https://dlthub.com/docs/general-usage/resource\#declare-parallel-and-async-resources "Direct link to Declare parallel and async resources")

You can extract multiple resources in parallel threads or with async IO.
To enable this for a sync resource, you can set the `parallelized` flag to `True` in the resource decorator:

```codeBlockLines_RjmQ
@dlt.resource(parallelized=True)
def get_users():
    for u in _get_users():
        yield u

@dlt.resource(parallelized=True)
def get_orders():
    for o in _get_orders():
        yield o

# users and orders will be iterated in parallel in two separate threads
pipeline.run([get_users(), get_orders()])

```

Async generators are automatically extracted concurrently with other resources:

```codeBlockLines_RjmQ
@dlt.resource
async def get_users():
    async for u in _get_users():  # Assuming _get_users is an async generator
        yield u

```

Please find more details in [extract performance](https://dlthub.com/docs/reference/performance#extract)

## Customize resources [​](https://dlthub.com/docs/general-usage/resource\#customize-resources "Direct link to Customize resources")

### Filter, transform, and pivot data [​](https://dlthub.com/docs/general-usage/resource\#filter-transform-and-pivot-data "Direct link to Filter, transform, and pivot data")

You can attach any number of transformations that are evaluated on an item-per-item basis to your
resource. The available transformation types:

- **map** \- transform the data item ( `resource.add_map`).
- **filter** \- filter the data item ( `resource.add_filter`).
- **yield map** \- a map that returns an iterator (so a single row may generate many rows -
`resource.add_yield_map`).

Example: We have a resource that loads a list of users from an API endpoint. We want to customize it
so:

1. We remove users with `user_id == "me"`.
2. We anonymize user data.

Here's our resource:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(write_disposition="replace")
def users():
    ...
    users = requests.get(RESOURCE_URL)
    ...
    yield users

```

Here's our script that defines transformations and loads the data:

```codeBlockLines_RjmQ
from pipedrive import users

def anonymize_user(user_data):
    user_data["user_id"] = _hash_str(user_data["user_id"])
    user_data["user_email"] = _hash_str(user_data["user_email"])
    return user_data

# add the filter and anonymize function to users resource and enumerate
for user in users().add_filter(lambda user: user["user_id"] != "me").add_map(anonymize_user):
    print(user)

```

### Reduce the nesting level of generated tables [​](https://dlthub.com/docs/general-usage/resource\#reduce-the-nesting-level-of-generated-tables "Direct link to Reduce the nesting level of generated tables")

You can limit how deep `dlt` goes when generating nested tables and flattening dicts into columns. By default, the library will descend
and generate nested tables for all nested lists, without limit.

note

`max_table_nesting` is optional so you can skip it, in this case, dlt will
use it from the source if it is specified there or fallback to the default
value which has 1000 as the maximum nesting level.

```codeBlockLines_RjmQ
import dlt

@dlt.resource(max_table_nesting=1)
def my_resource():
    yield {
        "id": 1,
        "name": "random name",
        "properties": [\
            {\
                "name": "customer_age",\
                "type": "int",\
                "label": "Age",\
                "notes": [\
                    {\
                        "text": "string",\
                        "author": "string",\
                    }\
                ]\
            }\
        ]
    }

```

In the example above, we want only 1 level of nested tables to be generated (so there are no nested
tables of a nested table). Typical settings:

- `max_table_nesting=0` will not generate nested tables and will not flatten dicts into columns at all. All nested data will be
represented as JSON.
- `max_table_nesting=1` will generate nested tables of root tables and nothing more. All nested
data in nested tables will be represented as JSON.

You can achieve the same effect after the resource instance is created:

```codeBlockLines_RjmQ
resource = my_resource()
resource.max_table_nesting = 0

```

Several data sources are prone to contain semi-structured documents with very deep nesting, i.e.,
MongoDB databases. Our practical experience is that setting the `max_nesting_level` to 2 or 3
produces the clearest and human-readable schemas.

### Sample from large data [​](https://dlthub.com/docs/general-usage/resource\#sample-from-large-data "Direct link to Sample from large data")

If your resource loads thousands of pages of data from a REST API or millions of rows from a database table, you may want to sample just a fragment of it in order to quickly see the dataset with example data and test your transformations, etc. To do this, you limit how many items will be yielded by a resource (or source) by calling the `add_limit` method. This method will close the generator that produces the data after the limit is reached.

In the example below, we load just the first 10 items from an infinite counter - that would otherwise never end.

```codeBlockLines_RjmQ
r = dlt.resource(itertools.count(), name="infinity").add_limit(10)
assert list(r) == list(range(10))

```

note

Note that `add_limit` **does not limit the number of records** but rather the "number of yields". Depending on how your resource is set up, the number of extracted rows may vary. For example, consider this resource:

```codeBlockLines_RjmQ
@dlt.resource
def my_resource():
    for i in range(100):
        yield [{"record_id": j} for j in range(15)]

dlt.pipeline(destination="duckdb").run(my_resource().add_limit(10))

```

The code above will extract `15*10=150` records. This is happening because in each iteration, 15 records are yielded, and we're limiting the number of iterations to 10.

Altenatively you can also apply a time limit to the resource. The code below will run the extraction for 10 seconds and extract how ever many items are yielded in that time. In combination with incrementals, this can be useful for batched loading or for loading on machines that have a run time limit.

```codeBlockLines_RjmQ
dlt.pipeline(destination="duckdb").run(my_resource().add_limit(max_time=10))

```

You can also apply a combination of both limits. In this case the extraction will stop as soon as either limit is reached.

```codeBlockLines_RjmQ
dlt.pipeline(destination="duckdb").run(my_resource().add_limit(max_items=10, max_time=10))

```

Some notes about the `add_limit`:

1. `add_limit` does not skip any items. It closes the iterator/generator that produces data after the limit is reached.
2. You cannot limit transformers. They should process all the data they receive fully to avoid inconsistencies in generated datasets.
3. Async resources with a limit added may occasionally produce one item more than the limit on some runs. This behavior is not deterministic.
4. Calling add limit on a resource will replace any previously set limits settings.
5. For time-limited resources, the timer starts when the first item is processed. When resources are processed sequentially (FIFO mode), each resource's time limit applies also sequentially. In the default round robin mode, the time limits will usually run concurrently.

tip

If you are parameterizing the value of `add_limit` and sometimes need it to be disabled, you can set `None` or `-1` to disable the limiting.
You can also set the limit to `0` for the resource to not yield any items.

### Set table name and adjust schema [​](https://dlthub.com/docs/general-usage/resource\#set-table-name-and-adjust-schema "Direct link to Set table name and adjust schema")

You can change the schema of a resource, whether it is standalone or part of a source. Look for a method named `apply_hints` which takes the same arguments as the resource decorator. Obviously, you should call this method before data is extracted from the resource. The example below converts an `append` resource loading the `users` table into a [merge](https://dlthub.com/docs/general-usage/merge-loading) resource that will keep just one updated record per `user_id`. It also adds ["last value" incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor) on the `created_at` column to prevent requesting again the already loaded records:

```codeBlockLines_RjmQ
tables = sql_database()
tables.users.apply_hints(
    write_disposition="merge",
    primary_key="user_id",
    incremental=dlt.sources.incremental("updated_at")
)
pipeline.run(tables)

```

To change the name of a table to which the resource will load data, do the following:

```codeBlockLines_RjmQ
tables = sql_database()
tables.users.table_name = "other_users"

```

### Adjust schema when you yield data [​](https://dlthub.com/docs/general-usage/resource\#adjust-schema-when-you-yield-data "Direct link to Adjust schema when you yield data")

You can set or update the table name, columns, and other schema elements when your resource is executed, and you already yield data. Such changes will be merged with the existing schema in the same way the `apply_hints` method above works. There are many reasons to adjust the schema at runtime. For example, when using Airflow, you should avoid lengthy operations (i.e., reflecting database tables) during the creation of the DAG, so it is better to do it when the DAG executes. You may also emit partial hints (i.e., precision and scale for decimal types) for columns to help `dlt` type inference.

```codeBlockLines_RjmQ
@dlt.resource
def sql_table(credentials, schema, table):
    # Create a SQL Alchemy engine
    engine = engine_from_credentials(credentials)
    engine.execution_options(stream_results=True)
    metadata = MetaData(schema=schema)
    # Reflect the table schema
    table_obj = Table(table, metadata, autoload_with=engine)

    for idx, batch in enumerate(table_rows(engine, table_obj)):
      if idx == 0:
        # Emit the first row with hints, table_to_columns and _get_primary_key are helpers that extract dlt schema from
        # SqlAlchemy model
        yield dlt.mark.with_hints(
            batch,
            dlt.mark.make_hints(columns=table_to_columns(table_obj), primary_key=_get_primary_key(table_obj)),
        )
      else:
        # Just yield all the other rows
        yield batch

```

In the example above, we use `dlt.mark.with_hints` and `dlt.mark.make_hints` to emit columns and primary key with the first extracted item. The table schema will be adjusted after the `batch` is processed in the extract pipeline but before any schema contracts are applied, and data is persisted in the load package.

tip

You can emit columns as a Pydantic model and use dynamic hints (i.e., lambda for table name) as well. You should avoid redefining `Incremental` this way.

### Import external files [​](https://dlthub.com/docs/general-usage/resource\#import-external-files "Direct link to Import external files")

You can import external files, i.e., CSV, Parquet, and JSONL, by yielding items marked with `with_file_import`, optionally passing a table schema corresponding to the imported file. dlt will not read, parse, or normalize any names (i.e., CSV or Arrow headers) and will attempt to copy the file into the destination as is.

```codeBlockLines_RjmQ
import os
import dlt
from dlt.sources.filesystem import filesystem

columns: List[TColumnSchema] = [\
    {"name": "id", "data_type": "bigint"},\
    {"name": "name", "data_type": "text"},\
    {"name": "description", "data_type": "text"},\
    {"name": "ordered_at", "data_type": "date"},\
    {"name": "price", "data_type": "decimal"},\
]

import_folder = "/tmp/import"

@dlt.transformer(columns=columns)
def orders(items: Iterator[FileItemDict]):
  for item in items:
    # copy the file locally
      dest_file = os.path.join(import_folder, item["file_name"])
      # download the file
      item.fsspec.download(item["file_url"], dest_file)
      # tell dlt to import the dest_file as `csv`
      yield dlt.mark.with_file_import(dest_file, "csv")

# use the filesystem core source to glob a bucket

downloader = filesystem(
  bucket_url="s3://my_bucket/csv",
  file_glob="today/*.csv.gz") | orders

info = pipeline.run(orders, destination="snowflake")

```

In the example above, we glob all zipped csv files present on **my\_bucket/csv/today** (using the `filesystem` verified source) and send file descriptors to the `orders` transformer. The transformer downloads and imports the files into the extract package. At the end, `dlt` sends them to Snowflake (the table will be created because we use `column` hints to define the schema).

If imported `csv` files are not in `dlt` [default format](https://dlthub.com/docs/dlt-ecosystem/file-formats/csv#default-settings), you may need to pass additional configuration.

```codeBlockLines_RjmQ
[destination.snowflake.csv_format]
delimiter="|"
include_header=false
on_error_continue=true

```

You can sniff the schema from the data, i.e., using DuckDB to infer the table schema from a CSV file. `dlt.mark.with_file_import` accepts additional arguments that you can use to pass hints at runtime.

note

- If you do not define any columns, the table will not be created in the destination. `dlt` will still attempt to load data into it, so if you create a fitting table upfront, the load process will succeed.
- Files are imported using hard links if possible to avoid copying and duplicating the storage space needed.

### Duplicate and rename resources [​](https://dlthub.com/docs/general-usage/resource\#duplicate-and-rename-resources "Direct link to Duplicate and rename resources")

There are cases when your resources are generic (i.e., bucket filesystem) and you want to load several instances of it (i.e., files from different folders) into separate tables. In the example below, we use the `filesystem` source to load csvs from two different folders into separate tables:

```codeBlockLines_RjmQ
@dlt.resource(standalone=True)
def fs_resource(bucket_url):
  # list and yield files in bucket_url
  ...

@dlt.transformer
def csv_reader(file_item):
  # load csv, parse, and yield rows in file_item
  ...

# create two extract pipes that list files from the bucket and send them to the reader.
# by default, both pipes will load data to the same table (csv_reader)
reports_pipe = fs_resource("s3://my-bucket/reports") | csv_reader()
transactions_pipe = fs_resource("s3://my-bucket/transactions") | csv_reader()

# so we rename resources to load to "reports" and "transactions" tables
pipeline.run(
  [reports_pipe.with_name("reports"), transactions_pipe.with_name("transactions")]
)

```

The `with_name` method returns a deep copy of the original resource, its data pipe, and the data pipes of a parent resource. A renamed clone is fully separated from the original resource (and other clones) when loading: it maintains a separate [resource state](https://dlthub.com/docs/general-usage/state#read-and-write-pipeline-state-in-a-resource) and will load to a table.

## Load resources [​](https://dlthub.com/docs/general-usage/resource\#load-resources "Direct link to Load resources")

You can pass individual resources or a list of resources to the `dlt.pipeline` object. The resources loaded outside the source context will be added to the [default schema](https://dlthub.com/docs/general-usage/schema) of the pipeline.

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

pipeline = dlt.pipeline(
    pipeline_name="rows_pipeline",
    destination="duckdb",
    dataset_name="rows_data"
)
# load an individual resource
pipeline.run(generate_rows(10))
# load a list of resources
pipeline.run([generate_rows(10), generate_rows(20)])

```

### Pick loader file format for a particular resource [​](https://dlthub.com/docs/general-usage/resource\#pick-loader-file-format-for-a-particular-resource "Direct link to Pick loader file format for a particular resource")

You can request a particular loader file format to be used for a resource.

```codeBlockLines_RjmQ
@dlt.resource(file_format="parquet")
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

```

The resource above will be saved and loaded from a Parquet file (if the destination supports it).

note

A special `file_format`: **preferred** will load the resource using a format that is preferred by a destination. This setting supersedes the `loader_file_format` passed to the `run` method.

### Do a full refresh [​](https://dlthub.com/docs/general-usage/resource\#do-a-full-refresh "Direct link to Do a full refresh")

To do a full refresh of an `append` or `merge` resource, you set the `refresh` argument on the `run` method to `drop_data`. This will truncate the tables without dropping them.

```codeBlockLines_RjmQ
p.run(merge_source(), refresh="drop_data")

```

You can also [fully drop the tables](https://dlthub.com/docs/general-usage/pipeline#refresh-pipeline-data-and-state) in the `merge_source`:

```codeBlockLines_RjmQ
p.run(merge_source(), refresh="drop_sources")

```

- [Declare a resource](https://dlthub.com/docs/general-usage/resource#declare-a-resource)
  - [Define schema](https://dlthub.com/docs/general-usage/resource#define-schema)
  - [Put a contract on tables, columns, and data](https://dlthub.com/docs/general-usage/resource#put-a-contract-on-tables-columns-and-data)
  - [Define schema of nested tables](https://dlthub.com/docs/general-usage/resource#define-schema-of-nested-tables)
  - [Define a schema with Pydantic](https://dlthub.com/docs/general-usage/resource#define-a-schema-with-pydantic)
  - [Dispatch data to many tables](https://dlthub.com/docs/general-usage/resource#dispatch-data-to-many-tables)
  - [Parametrize a resource](https://dlthub.com/docs/general-usage/resource#parametrize-a-resource)
  - [Process resources with `dlt.transformer`](https://dlthub.com/docs/general-usage/resource#process-resources-with-dlttransformer)
  - [Declare a standalone resource](https://dlthub.com/docs/general-usage/resource#declare-a-standalone-resource)
  - [Declare parallel and async resources](https://dlthub.com/docs/general-usage/resource#declare-parallel-and-async-resources)
- [Customize resources](https://dlthub.com/docs/general-usage/resource#customize-resources)
  - [Filter, transform, and pivot data](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data)
  - [Reduce the nesting level of generated tables](https://dlthub.com/docs/general-usage/resource#reduce-the-nesting-level-of-generated-tables)
  - [Sample from large data](https://dlthub.com/docs/general-usage/resource#sample-from-large-data)
  - [Set table name and adjust schema](https://dlthub.com/docs/general-usage/resource#set-table-name-and-adjust-schema)
  - [Adjust schema when you yield data](https://dlthub.com/docs/general-usage/resource#adjust-schema-when-you-yield-data)
  - [Import external files](https://dlthub.com/docs/general-usage/resource#import-external-files)
  - [Duplicate and rename resources](https://dlthub.com/docs/general-usage/resource#duplicate-and-rename-resources)
- [Load resources](https://dlthub.com/docs/general-usage/resource#load-resources)
  - [Pick loader file format for a particular resource](https://dlthub.com/docs/general-usage/resource#pick-loader-file-format-for-a-particular-resource)
  - [Do a full refresh](https://dlthub.com/docs/general-usage/resource#do-a-full-refresh)

----- https://dlthub.com/docs/general-usage/resource#set-table-name-and-adjust-schema -----

Version: 1.11.0 (latest)

On this page

## Declare a resource [​](https://dlthub.com/docs/general-usage/resource\#declare-a-resource "Direct link to Declare a resource")

A [resource](https://dlthub.com/docs/general-usage/glossary#resource) is an ( [optionally async](https://dlthub.com/docs/reference/performance#parallelism-within-a-pipeline)) function that yields data. To create a resource, we add the `@dlt.resource` decorator to that function.

Commonly used arguments:

- `name`: The name of the table generated by this resource. Defaults to the decorated function name.
- `write_disposition`: How should the data be loaded at the destination? Currently supported: `append`,
`replace`, and `merge`. Defaults to `append.`

Example:

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows():
	for i in range(10):
		yield {'id': i, 'example_string': 'abc'}

@dlt.source
def source_name():
    return generate_rows

```

To get the data of a resource, we could do:

```codeBlockLines_RjmQ
for row in generate_rows():
    print(row)

for row in source_name().resources.get('table_name'):
    print(row)

```

Typically, resources are declared and grouped with related resources within a [source](https://dlthub.com/docs/general-usage/source) function.

### Define schema [​](https://dlthub.com/docs/general-usage/resource\#define-schema "Direct link to Define schema")

`dlt` will infer the [schema](https://dlthub.com/docs/general-usage/schema) for tables associated with resources from the resource's data.
You can modify the generation process by using the table and column hints. The resource decorator accepts the following arguments:

1. `table_name`: the name of the table, if different from the resource name.
2. `primary_key` and `merge_key`: define the name of the columns (compound keys are allowed) that will receive those hints. Used in [incremental loading](https://dlthub.com/docs/general-usage/incremental-loading) and [merge loading](https://dlthub.com/docs/general-usage/merge-loading).
3. `columns`: lets you define one or more columns, including the data types, nullability, and other hints. The column definition is a `TypedDict`: `TTableSchemaColumns`. In the example below, we tell `dlt` that the column `tags` (containing a list of tags) in the `user` table should have type `json`, which means that it will be loaded as JSON/struct and not as a separate nested table.

```codeBlockLines_RjmQ
@dlt.resource(name="user", columns={"tags": {"data_type": "json"}})
def get_users():
  ...

# the `table_schema` method gets the table schema generated by a resource
print(get_users().compute_table_schema())

```

note

You can pass dynamic hints which are functions that take the data item as input and return a hint value. This lets you create table and column schemas depending on the data. See an [example below](https://dlthub.com/docs/general-usage/resource#adjust-schema-when-you-yield-data).

### Put a contract on tables, columns, and data [​](https://dlthub.com/docs/general-usage/resource\#put-a-contract-on-tables-columns-and-data "Direct link to Put a contract on tables, columns, and data")

Use the `schema_contract` argument to tell dlt how to [deal with new tables, data types, and bad data types](https://dlthub.com/docs/general-usage/schema-contracts). For example, if you set it to **freeze**, `dlt` will not allow for any new tables, columns, or data types to be introduced to the schema - it will raise an exception. Learn more about available contract modes [here](https://dlthub.com/docs/general-usage/schema-contracts#setting-up-the-contract).

### Define schema of nested tables [​](https://dlthub.com/docs/general-usage/resource\#define-schema-of-nested-tables "Direct link to Define schema of nested tables")

`dlt` creates [nested tables](https://dlthub.com/docs/general-usage/schema#nested-references-root-and-nested-tables) to store [list of objects](https://dlthub.com/docs/general-usage/destination-tables#nested-tables) if present in your data.
You can define the schema of such tables with `nested_hints` argument to `@dlt.resource`:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(
    nested_hints={
        "purchases": dlt.mark.make_nested_hints(
            columns=[{"name": "price", "data_type": "decimal"}],
            schema_contract={"columns": "freeze"},
        )
    },
)
def customers():
    """Load customer data from a simple python list."""
    yield [\
        {\
            "id": 1,\
            "name": "simon",\
            "city": "berlin",\
            "purchases": [{"id": 1, "name": "apple", "price": "1.50"}],\
        },\
    ]

```

Here we convert the `price` field in list of `purchases` to decimal type and set the schema contract to lock the list
of columns in it. We use convenience function `dlt.mark.make_nested_hints` to generate nested hints dictionary. You are
free to use it directly.

Mind that `purchases` list will be stored as table with name `customers__purchases`. When declaring nested hints you just need
to specify nested field(s) name(s). In case of deeper nesting ie. let's say each `purchase` has a list of `coupons` applied,
you can apply hints to coupons and define `customers__purchases__coupons` table schema:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(
    nested_hints={
        "purchases": {},
        ("purchases", "coupons"): {
            "columns": {"registered_at": {"data_type": "timestamp"}}
        }
    },
)
def customers():
    ...

```

Here we use `("purchases", "coupons")` to locate list at the depth of 2 and set the data type on `registered_at` column
to `timestamp`. We do that by directly using nested hints dict.
Note that we specified `purchases` with an empty list of hints. **You are required to specify all parent hints, even if they**
**are empty. Currently we are not adding missing path elements automatically**.

You can use `nested_hints` primarily to set column hints and schema contract, those work exactly as in case of root tables.

- `file_format` has no effect (not implemented yet)
- `write_disposition` works as expected but leads to unintended consequences (ie. you can set nested table to `replace`) while root table is `append`.
- `references` will create [table references](https://dlthub.com/docs/general-usage/schema#table-references-1) (annotations) as expected.
- `primary_key` and `merge_key`: **setting those will convert nested table into a regular table, with a separate write disposition, file format etc.** [It allows you to create custom table relationships ie. using natural primary and foreign keys present in the data.](https://dlthub.com/docs/general-usage/schema#generate-custom-linking-for-nested-tables)

tip

[REST API Source](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic) accepts `nested_hints` argument as well.

You can apply nested hints after the resource was created by using [apply\_hints](https://dlthub.com/docs/general-usage/resource#set-table-name-and-adjust-schema).

### Define a schema with Pydantic [​](https://dlthub.com/docs/general-usage/resource\#define-a-schema-with-pydantic "Direct link to Define a schema with Pydantic")

You can alternatively use a [Pydantic](https://pydantic-docs.helpmanual.io/) model to define the schema.
For example:

```codeBlockLines_RjmQ
from pydantic import BaseModel
from typing import List, Optional, Union

class Address(BaseModel):
    street: str
    city: str
    postal_code: str

class User(BaseModel):
    id: int
    name: str
    tags: List[str]
    email: Optional[str]
    address: Address
    status: Union[int, str]

@dlt.resource(name="user", columns=User)
def get_users():
    ...

```

The data types of the table columns are inferred from the types of the Pydantic fields. These use the same type conversions
as when the schema is automatically generated from the data.

Pydantic models integrate well with [schema contracts](https://dlthub.com/docs/general-usage/schema-contracts) as data validators.

Things to note:

- Fields with an `Optional` type are marked as `nullable`.
- Fields with a `Union` type are converted to the first (not `None`) type listed in the union. For example, `status: Union[int, str]` results in a `bigint` column.
- `list`, `dict`, and nested Pydantic model fields will use the `json` type, which means they'll be stored as a JSON object in the database instead of creating nested tables.

You can override this by configuring the Pydantic model:

```codeBlockLines_RjmQ
from typing import ClassVar
from dlt.common.libs.pydantic import DltConfig

class UserWithNesting(User):
  dlt_config: ClassVar[DltConfig] = {"skip_nested_types": True}

@dlt.resource(name="user", columns=UserWithNesting)
def get_users():
    ...

```

`"skip_nested_types"` omits any `dict`/ `list`/ `BaseModel` type fields from the schema, so dlt will fall back on the default
behavior of creating nested tables for these fields.

We do not support `RootModel` that validate simple types. You can add such a validator yourself, see [data filtering section](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data).

### Dispatch data to many tables [​](https://dlthub.com/docs/general-usage/resource\#dispatch-data-to-many-tables "Direct link to Dispatch data to many tables")

You can load data to many tables from a single resource. The most common case is a stream of events
of different types, each with a different data schema. To deal with this, you can use the `table_name`
argument on `dlt.resource`. You could pass the table name as a function with the data item as an
argument and the `table_name` string as a return value.

For example, a resource that loads GitHub repository events wants to send `issue`, `pull request`,
and `comment` events to separate tables. The type of the event is in the "type" field.

```codeBlockLines_RjmQ
# send item to a table with name item["type"]
@dlt.resource(table_name=lambda event: event['type'])
def repo_events() -> Iterator[TDataItems]:
    yield item

# the `table_schema` method gets the table schema generated by a resource and takes an optional
# data item to evaluate dynamic hints
print(repo_events().compute_table_schema({"type": "WatchEvent", "id": ...}))

```

In more advanced cases, you can dispatch data to different tables directly in the code of the
resource function:

```codeBlockLines_RjmQ
@dlt.resource
def repo_events() -> Iterator[TDataItems]:
    # mark the "item" to be sent to the table with the name item["type"]
    yield dlt.mark.with_table_name(item, item["type"])

```

### Parametrize a resource [​](https://dlthub.com/docs/general-usage/resource\#parametrize-a-resource "Direct link to Parametrize a resource")

You can add arguments to your resource functions like to any other. Below we parametrize our
`generate_rows` resource to generate the number of rows we request:

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

for row in generate_rows(10):
    print(row)

for row in generate_rows(20):
    print(row)

```

tip

You can mark some resource arguments as [configuration and credentials](https://dlthub.com/docs/general-usage/credentials) values so `dlt` can pass them automatically to your functions.

### Process resources with `dlt.transformer` [​](https://dlthub.com/docs/general-usage/resource\#process-resources-with-dlttransformer "Direct link to process-resources-with-dlttransformer")

You can feed data from one resource into another. The most common case is when you have an API that returns a list of objects (i.e., users) in one endpoint and user details in another. You can deal with this by declaring a resource that obtains a list of users and another resource that receives items from the list and downloads the profiles.

```codeBlockLines_RjmQ
@dlt.resource(write_disposition="replace")
def users(limit=None):
    for u in _get_users(limit):
        yield u

# Feed data from users as user_item below,
# all transformers must have at least one
# argument that will receive data from the parent resource
@dlt.transformer(data_from=users)
def users_details(user_item):
    for detail in _get_details(user_item["user_id"]):
        yield detail

# Just load the users_details.
# dlt figures out dependencies for you.
pipeline.run(users_details)

```

In the example above, `users_details` will receive data from the default instance of the `users` resource (with `limit` set to `None`). You can also use the **pipe \|** operator to bind resources dynamically.

```codeBlockLines_RjmQ
# You can be more explicit and use a pipe operator.
# With it, you can create dynamic pipelines where the dependencies
# are set at run time and resources are parametrized, i.e.,
# below we want to load only 100 users from the `users` endpoint.
pipeline.run(users(limit=100) | users_details)

```

tip

Transformers are allowed not only to **yield** but also to **return** values and can decorate **async** functions and [**async generators**](https://dlthub.com/docs/reference/performance#extract). Below we decorate an async function and request details on two pokemons. HTTP calls are made in parallel via the httpx library.

```codeBlockLines_RjmQ
import dlt
import httpx

@dlt.transformer
async def pokemon(id):
    async with httpx.AsyncClient() as client:
        r = await client.get(f"https://pokeapi.co/api/v2/pokemon/{id}")
        return r.json()

# Get Bulbasaur and Ivysaur (you need dlt 0.4.6 for the pipe operator working with lists).
print(list([1,2] | pokemon()))

```

### Declare a standalone resource [​](https://dlthub.com/docs/general-usage/resource\#declare-a-standalone-resource "Direct link to Declare a standalone resource")

A standalone resource is defined on a function that is top-level in a module (not an inner function) that accepts config and secrets values. Additionally, if the `standalone` flag is specified, the decorated function signature and docstring will be preserved. `dlt.resource` will just wrap the decorated function, and the user must call the wrapper to get the actual resource. Below we declare a `filesystem` resource that must be called before use.

```codeBlockLines_RjmQ
@dlt.resource(standalone=True)
def fs_resource(bucket_url=dlt.config.value):
  """List and yield files in `bucket_url`."""
  ...

# `filesystem` must be called before it is extracted or used in any other way.
pipeline.run(fs_resource("s3://my-bucket/reports"), table_name="reports")

```

Standalone may have a dynamic name that depends on the arguments passed to the decorated function. For example:

```codeBlockLines_RjmQ
@dlt.resource(standalone=True, name=lambda args: args["stream_name"])
def kinesis(stream_name: str):
    ...

kinesis_stream = kinesis("telemetry_stream")

```

`kinesis_stream` resource has a name **telemetry\_stream**.

### Declare parallel and async resources [​](https://dlthub.com/docs/general-usage/resource\#declare-parallel-and-async-resources "Direct link to Declare parallel and async resources")

You can extract multiple resources in parallel threads or with async IO.
To enable this for a sync resource, you can set the `parallelized` flag to `True` in the resource decorator:

```codeBlockLines_RjmQ
@dlt.resource(parallelized=True)
def get_users():
    for u in _get_users():
        yield u

@dlt.resource(parallelized=True)
def get_orders():
    for o in _get_orders():
        yield o

# users and orders will be iterated in parallel in two separate threads
pipeline.run([get_users(), get_orders()])

```

Async generators are automatically extracted concurrently with other resources:

```codeBlockLines_RjmQ
@dlt.resource
async def get_users():
    async for u in _get_users():  # Assuming _get_users is an async generator
        yield u

```

Please find more details in [extract performance](https://dlthub.com/docs/reference/performance#extract)

## Customize resources [​](https://dlthub.com/docs/general-usage/resource\#customize-resources "Direct link to Customize resources")

### Filter, transform, and pivot data [​](https://dlthub.com/docs/general-usage/resource\#filter-transform-and-pivot-data "Direct link to Filter, transform, and pivot data")

You can attach any number of transformations that are evaluated on an item-per-item basis to your
resource. The available transformation types:

- **map** \- transform the data item ( `resource.add_map`).
- **filter** \- filter the data item ( `resource.add_filter`).
- **yield map** \- a map that returns an iterator (so a single row may generate many rows -
`resource.add_yield_map`).

Example: We have a resource that loads a list of users from an API endpoint. We want to customize it
so:

1. We remove users with `user_id == "me"`.
2. We anonymize user data.

Here's our resource:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(write_disposition="replace")
def users():
    ...
    users = requests.get(RESOURCE_URL)
    ...
    yield users

```

Here's our script that defines transformations and loads the data:

```codeBlockLines_RjmQ
from pipedrive import users

def anonymize_user(user_data):
    user_data["user_id"] = _hash_str(user_data["user_id"])
    user_data["user_email"] = _hash_str(user_data["user_email"])
    return user_data

# add the filter and anonymize function to users resource and enumerate
for user in users().add_filter(lambda user: user["user_id"] != "me").add_map(anonymize_user):
    print(user)

```

### Reduce the nesting level of generated tables [​](https://dlthub.com/docs/general-usage/resource\#reduce-the-nesting-level-of-generated-tables "Direct link to Reduce the nesting level of generated tables")

You can limit how deep `dlt` goes when generating nested tables and flattening dicts into columns. By default, the library will descend
and generate nested tables for all nested lists, without limit.

note

`max_table_nesting` is optional so you can skip it, in this case, dlt will
use it from the source if it is specified there or fallback to the default
value which has 1000 as the maximum nesting level.

```codeBlockLines_RjmQ
import dlt

@dlt.resource(max_table_nesting=1)
def my_resource():
    yield {
        "id": 1,
        "name": "random name",
        "properties": [\
            {\
                "name": "customer_age",\
                "type": "int",\
                "label": "Age",\
                "notes": [\
                    {\
                        "text": "string",\
                        "author": "string",\
                    }\
                ]\
            }\
        ]
    }

```

In the example above, we want only 1 level of nested tables to be generated (so there are no nested
tables of a nested table). Typical settings:

- `max_table_nesting=0` will not generate nested tables and will not flatten dicts into columns at all. All nested data will be
represented as JSON.
- `max_table_nesting=1` will generate nested tables of root tables and nothing more. All nested
data in nested tables will be represented as JSON.

You can achieve the same effect after the resource instance is created:

```codeBlockLines_RjmQ
resource = my_resource()
resource.max_table_nesting = 0

```

Several data sources are prone to contain semi-structured documents with very deep nesting, i.e.,
MongoDB databases. Our practical experience is that setting the `max_nesting_level` to 2 or 3
produces the clearest and human-readable schemas.

### Sample from large data [​](https://dlthub.com/docs/general-usage/resource\#sample-from-large-data "Direct link to Sample from large data")

If your resource loads thousands of pages of data from a REST API or millions of rows from a database table, you may want to sample just a fragment of it in order to quickly see the dataset with example data and test your transformations, etc. To do this, you limit how many items will be yielded by a resource (or source) by calling the `add_limit` method. This method will close the generator that produces the data after the limit is reached.

In the example below, we load just the first 10 items from an infinite counter - that would otherwise never end.

```codeBlockLines_RjmQ
r = dlt.resource(itertools.count(), name="infinity").add_limit(10)
assert list(r) == list(range(10))

```

note

Note that `add_limit` **does not limit the number of records** but rather the "number of yields". Depending on how your resource is set up, the number of extracted rows may vary. For example, consider this resource:

```codeBlockLines_RjmQ
@dlt.resource
def my_resource():
    for i in range(100):
        yield [{"record_id": j} for j in range(15)]

dlt.pipeline(destination="duckdb").run(my_resource().add_limit(10))

```

The code above will extract `15*10=150` records. This is happening because in each iteration, 15 records are yielded, and we're limiting the number of iterations to 10.

Altenatively you can also apply a time limit to the resource. The code below will run the extraction for 10 seconds and extract how ever many items are yielded in that time. In combination with incrementals, this can be useful for batched loading or for loading on machines that have a run time limit.

```codeBlockLines_RjmQ
dlt.pipeline(destination="duckdb").run(my_resource().add_limit(max_time=10))

```

You can also apply a combination of both limits. In this case the extraction will stop as soon as either limit is reached.

```codeBlockLines_RjmQ
dlt.pipeline(destination="duckdb").run(my_resource().add_limit(max_items=10, max_time=10))

```

Some notes about the `add_limit`:

1. `add_limit` does not skip any items. It closes the iterator/generator that produces data after the limit is reached.
2. You cannot limit transformers. They should process all the data they receive fully to avoid inconsistencies in generated datasets.
3. Async resources with a limit added may occasionally produce one item more than the limit on some runs. This behavior is not deterministic.
4. Calling add limit on a resource will replace any previously set limits settings.
5. For time-limited resources, the timer starts when the first item is processed. When resources are processed sequentially (FIFO mode), each resource's time limit applies also sequentially. In the default round robin mode, the time limits will usually run concurrently.

tip

If you are parameterizing the value of `add_limit` and sometimes need it to be disabled, you can set `None` or `-1` to disable the limiting.
You can also set the limit to `0` for the resource to not yield any items.

### Set table name and adjust schema [​](https://dlthub.com/docs/general-usage/resource\#set-table-name-and-adjust-schema "Direct link to Set table name and adjust schema")

You can change the schema of a resource, whether it is standalone or part of a source. Look for a method named `apply_hints` which takes the same arguments as the resource decorator. Obviously, you should call this method before data is extracted from the resource. The example below converts an `append` resource loading the `users` table into a [merge](https://dlthub.com/docs/general-usage/merge-loading) resource that will keep just one updated record per `user_id`. It also adds ["last value" incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor) on the `created_at` column to prevent requesting again the already loaded records:

```codeBlockLines_RjmQ
tables = sql_database()
tables.users.apply_hints(
    write_disposition="merge",
    primary_key="user_id",
    incremental=dlt.sources.incremental("updated_at")
)
pipeline.run(tables)

```

To change the name of a table to which the resource will load data, do the following:

```codeBlockLines_RjmQ
tables = sql_database()
tables.users.table_name = "other_users"

```

### Adjust schema when you yield data [​](https://dlthub.com/docs/general-usage/resource\#adjust-schema-when-you-yield-data "Direct link to Adjust schema when you yield data")

You can set or update the table name, columns, and other schema elements when your resource is executed, and you already yield data. Such changes will be merged with the existing schema in the same way the `apply_hints` method above works. There are many reasons to adjust the schema at runtime. For example, when using Airflow, you should avoid lengthy operations (i.e., reflecting database tables) during the creation of the DAG, so it is better to do it when the DAG executes. You may also emit partial hints (i.e., precision and scale for decimal types) for columns to help `dlt` type inference.

```codeBlockLines_RjmQ
@dlt.resource
def sql_table(credentials, schema, table):
    # Create a SQL Alchemy engine
    engine = engine_from_credentials(credentials)
    engine.execution_options(stream_results=True)
    metadata = MetaData(schema=schema)
    # Reflect the table schema
    table_obj = Table(table, metadata, autoload_with=engine)

    for idx, batch in enumerate(table_rows(engine, table_obj)):
      if idx == 0:
        # Emit the first row with hints, table_to_columns and _get_primary_key are helpers that extract dlt schema from
        # SqlAlchemy model
        yield dlt.mark.with_hints(
            batch,
            dlt.mark.make_hints(columns=table_to_columns(table_obj), primary_key=_get_primary_key(table_obj)),
        )
      else:
        # Just yield all the other rows
        yield batch

```

In the example above, we use `dlt.mark.with_hints` and `dlt.mark.make_hints` to emit columns and primary key with the first extracted item. The table schema will be adjusted after the `batch` is processed in the extract pipeline but before any schema contracts are applied, and data is persisted in the load package.

tip

You can emit columns as a Pydantic model and use dynamic hints (i.e., lambda for table name) as well. You should avoid redefining `Incremental` this way.

### Import external files [​](https://dlthub.com/docs/general-usage/resource\#import-external-files "Direct link to Import external files")

You can import external files, i.e., CSV, Parquet, and JSONL, by yielding items marked with `with_file_import`, optionally passing a table schema corresponding to the imported file. dlt will not read, parse, or normalize any names (i.e., CSV or Arrow headers) and will attempt to copy the file into the destination as is.

```codeBlockLines_RjmQ
import os
import dlt
from dlt.sources.filesystem import filesystem

columns: List[TColumnSchema] = [\
    {"name": "id", "data_type": "bigint"},\
    {"name": "name", "data_type": "text"},\
    {"name": "description", "data_type": "text"},\
    {"name": "ordered_at", "data_type": "date"},\
    {"name": "price", "data_type": "decimal"},\
]

import_folder = "/tmp/import"

@dlt.transformer(columns=columns)
def orders(items: Iterator[FileItemDict]):
  for item in items:
    # copy the file locally
      dest_file = os.path.join(import_folder, item["file_name"])
      # download the file
      item.fsspec.download(item["file_url"], dest_file)
      # tell dlt to import the dest_file as `csv`
      yield dlt.mark.with_file_import(dest_file, "csv")

# use the filesystem core source to glob a bucket

downloader = filesystem(
  bucket_url="s3://my_bucket/csv",
  file_glob="today/*.csv.gz") | orders

info = pipeline.run(orders, destination="snowflake")

```

In the example above, we glob all zipped csv files present on **my\_bucket/csv/today** (using the `filesystem` verified source) and send file descriptors to the `orders` transformer. The transformer downloads and imports the files into the extract package. At the end, `dlt` sends them to Snowflake (the table will be created because we use `column` hints to define the schema).

If imported `csv` files are not in `dlt` [default format](https://dlthub.com/docs/dlt-ecosystem/file-formats/csv#default-settings), you may need to pass additional configuration.

```codeBlockLines_RjmQ
[destination.snowflake.csv_format]
delimiter="|"
include_header=false
on_error_continue=true

```

You can sniff the schema from the data, i.e., using DuckDB to infer the table schema from a CSV file. `dlt.mark.with_file_import` accepts additional arguments that you can use to pass hints at runtime.

note

- If you do not define any columns, the table will not be created in the destination. `dlt` will still attempt to load data into it, so if you create a fitting table upfront, the load process will succeed.
- Files are imported using hard links if possible to avoid copying and duplicating the storage space needed.

### Duplicate and rename resources [​](https://dlthub.com/docs/general-usage/resource\#duplicate-and-rename-resources "Direct link to Duplicate and rename resources")

There are cases when your resources are generic (i.e., bucket filesystem) and you want to load several instances of it (i.e., files from different folders) into separate tables. In the example below, we use the `filesystem` source to load csvs from two different folders into separate tables:

```codeBlockLines_RjmQ
@dlt.resource(standalone=True)
def fs_resource(bucket_url):
  # list and yield files in bucket_url
  ...

@dlt.transformer
def csv_reader(file_item):
  # load csv, parse, and yield rows in file_item
  ...

# create two extract pipes that list files from the bucket and send them to the reader.
# by default, both pipes will load data to the same table (csv_reader)
reports_pipe = fs_resource("s3://my-bucket/reports") | csv_reader()
transactions_pipe = fs_resource("s3://my-bucket/transactions") | csv_reader()

# so we rename resources to load to "reports" and "transactions" tables
pipeline.run(
  [reports_pipe.with_name("reports"), transactions_pipe.with_name("transactions")]
)

```

The `with_name` method returns a deep copy of the original resource, its data pipe, and the data pipes of a parent resource. A renamed clone is fully separated from the original resource (and other clones) when loading: it maintains a separate [resource state](https://dlthub.com/docs/general-usage/state#read-and-write-pipeline-state-in-a-resource) and will load to a table.

## Load resources [​](https://dlthub.com/docs/general-usage/resource\#load-resources "Direct link to Load resources")

You can pass individual resources or a list of resources to the `dlt.pipeline` object. The resources loaded outside the source context will be added to the [default schema](https://dlthub.com/docs/general-usage/schema) of the pipeline.

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

pipeline = dlt.pipeline(
    pipeline_name="rows_pipeline",
    destination="duckdb",
    dataset_name="rows_data"
)
# load an individual resource
pipeline.run(generate_rows(10))
# load a list of resources
pipeline.run([generate_rows(10), generate_rows(20)])

```

### Pick loader file format for a particular resource [​](https://dlthub.com/docs/general-usage/resource\#pick-loader-file-format-for-a-particular-resource "Direct link to Pick loader file format for a particular resource")

You can request a particular loader file format to be used for a resource.

```codeBlockLines_RjmQ
@dlt.resource(file_format="parquet")
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

```

The resource above will be saved and loaded from a Parquet file (if the destination supports it).

note

A special `file_format`: **preferred** will load the resource using a format that is preferred by a destination. This setting supersedes the `loader_file_format` passed to the `run` method.

### Do a full refresh [​](https://dlthub.com/docs/general-usage/resource\#do-a-full-refresh "Direct link to Do a full refresh")

To do a full refresh of an `append` or `merge` resource, you set the `refresh` argument on the `run` method to `drop_data`. This will truncate the tables without dropping them.

```codeBlockLines_RjmQ
p.run(merge_source(), refresh="drop_data")

```

You can also [fully drop the tables](https://dlthub.com/docs/general-usage/pipeline#refresh-pipeline-data-and-state) in the `merge_source`:

```codeBlockLines_RjmQ
p.run(merge_source(), refresh="drop_sources")

```

- [Declare a resource](https://dlthub.com/docs/general-usage/resource#declare-a-resource)
  - [Define schema](https://dlthub.com/docs/general-usage/resource#define-schema)
  - [Put a contract on tables, columns, and data](https://dlthub.com/docs/general-usage/resource#put-a-contract-on-tables-columns-and-data)
  - [Define schema of nested tables](https://dlthub.com/docs/general-usage/resource#define-schema-of-nested-tables)
  - [Define a schema with Pydantic](https://dlthub.com/docs/general-usage/resource#define-a-schema-with-pydantic)
  - [Dispatch data to many tables](https://dlthub.com/docs/general-usage/resource#dispatch-data-to-many-tables)
  - [Parametrize a resource](https://dlthub.com/docs/general-usage/resource#parametrize-a-resource)
  - [Process resources with `dlt.transformer`](https://dlthub.com/docs/general-usage/resource#process-resources-with-dlttransformer)
  - [Declare a standalone resource](https://dlthub.com/docs/general-usage/resource#declare-a-standalone-resource)
  - [Declare parallel and async resources](https://dlthub.com/docs/general-usage/resource#declare-parallel-and-async-resources)
- [Customize resources](https://dlthub.com/docs/general-usage/resource#customize-resources)
  - [Filter, transform, and pivot data](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data)
  - [Reduce the nesting level of generated tables](https://dlthub.com/docs/general-usage/resource#reduce-the-nesting-level-of-generated-tables)
  - [Sample from large data](https://dlthub.com/docs/general-usage/resource#sample-from-large-data)
  - [Set table name and adjust schema](https://dlthub.com/docs/general-usage/resource#set-table-name-and-adjust-schema)
  - [Adjust schema when you yield data](https://dlthub.com/docs/general-usage/resource#adjust-schema-when-you-yield-data)
  - [Import external files](https://dlthub.com/docs/general-usage/resource#import-external-files)
  - [Duplicate and rename resources](https://dlthub.com/docs/general-usage/resource#duplicate-and-rename-resources)
- [Load resources](https://dlthub.com/docs/general-usage/resource#load-resources)
  - [Pick loader file format for a particular resource](https://dlthub.com/docs/general-usage/resource#pick-loader-file-format-for-a-particular-resource)
  - [Do a full refresh](https://dlthub.com/docs/general-usage/resource#do-a-full-refresh)

----- https://dlthub.com/docs/general-usage/incremental-loading#the-3-write-dispositions -----

Version: 1.11.0 (latest)

On this page

Incremental loading is the act of loading only new or changed data and not old records that we have already loaded. It enables low-latency and low-cost data transfer.

The challenge of incremental pipelines is that if we do not keep track of the state of the load (i.e., which increments were loaded and which are to be loaded), we may encounter issues. Read more about state [here](https://dlthub.com/docs/general-usage/state).

## Choosing a write disposition [​](https://dlthub.com/docs/general-usage/incremental-loading\#choosing-a-write-disposition "Direct link to Choosing a write disposition")

### The 3 write dispositions: [​](https://dlthub.com/docs/general-usage/incremental-loading\#the-3-write-dispositions "Direct link to The 3 write dispositions:")

- **Full load**: replaces the destination dataset with whatever the source produced on this run. To achieve this, use `write_disposition='replace'` in your resources. Learn more in the [full loading docs](https://dlthub.com/docs/general-usage/full-loading).

- **Append**: appends the new data to the destination. Use `write_disposition='append'`.

- **Merge**: Merges new data into the destination using `merge_key` and/or deduplicates/upserts new data using `primary_key`. Use `write_disposition='merge'`.

### How to choose the right write disposition [​](https://dlthub.com/docs/general-usage/incremental-loading\#how-to-choose-the-right-write-disposition "Direct link to How to choose the right write disposition")

![write disposition flowchart](https://storage.googleapis.com/dlt-blog-images/flowchart_for_scd2.png)

The "write disposition" you choose depends on the dataset and how you can extract it.

To find the "write disposition" you should use, the first question you should ask yourself is "Is my data stateful or stateless"? Stateful data has a state that is subject to change - for example, a user's profile. Stateless data cannot change - for example, a recorded event, such as a page view.

Because stateless data does not need to be updated, we can just append it.

For stateful data, comes a second question - Can I extract it incrementally from the source? If yes, you should use [slowly changing dimensions (Type-2)](https://dlthub.com/docs/general-usage/merge-loading#scd2-strategy), which allow you to maintain historical records of data changes over time.

If not, then we need to replace the entire dataset. However, if we can request the data incrementally, such as "all users added or modified since yesterday," then we can simply apply changes to our existing dataset with the merge write disposition.

## Incremental loading strategies [​](https://dlthub.com/docs/general-usage/incremental-loading\#incremental-loading-strategies "Direct link to Incremental loading strategies")

dlt provides several approaches to incremental loading:

1. [Merge strategies](https://dlthub.com/docs/general-usage/merge-loading#merge-strategies) \- Choose between delete-insert, SCD2, and upsert approaches to incrementally update your data
2. [Cursor-based incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor) \- Track changes using a cursor field (like timestamp or ID)
3. [Lag / Attribution window](https://dlthub.com/docs/general-usage/incremental/lag) \- Refresh data within a specific time window
4. [Advanced state management](https://dlthub.com/docs/general-usage/incremental/advanced-state) \- Custom state tracking

## Doing a full refresh [​](https://dlthub.com/docs/general-usage/incremental-loading\#doing-a-full-refresh "Direct link to Doing a full refresh")

You may force a full refresh of `merge` and `append` pipelines:

1. In the case of a `merge`, the data in the destination is deleted and loaded fresh. Currently, we do not deduplicate data during the full refresh.
2. In the case of `dlt.sources.incremental`, the data is deleted and loaded from scratch. The state of the incremental is reset to the initial value.

Example:

```codeBlockLines_RjmQ
p = dlt.pipeline(destination="bigquery", dataset_name="dataset_name")
# Do a full refresh
p.run(merge_source(), write_disposition="replace")
# Do a full refresh of just one table
p.run(merge_source().with_resources("merge_table"), write_disposition="replace")
# Run a normal merge
p.run(merge_source())

```

Passing write disposition to `replace` will change the write disposition on all the resources in
`repo_events` during the run of the pipeline.

## Next steps [​](https://dlthub.com/docs/general-usage/incremental-loading\#next-steps "Direct link to Next steps")

- [Cursor-based incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor) \- Use timestamps or IDs to track changes
- [Advanced state management](https://dlthub.com/docs/general-usage/incremental/advanced-state) \- Advanced techniques for state tracking
- [Walkthroughs: Add incremental configuration to SQL resources](https://dlthub.com/docs/walkthroughs/sql-incremental-configuration) \- Step-by-step examples
- [Troubleshooting incremental loading](https://dlthub.com/docs/general-usage/incremental/troubleshooting) \- Common issues and how to fix them

- [Choosing a write disposition](https://dlthub.com/docs/general-usage/incremental-loading#choosing-a-write-disposition)
  - [The 3 write dispositions:](https://dlthub.com/docs/general-usage/incremental-loading#the-3-write-dispositions)
  - [How to choose the right write disposition](https://dlthub.com/docs/general-usage/incremental-loading#how-to-choose-the-right-write-disposition)
- [Incremental loading strategies](https://dlthub.com/docs/general-usage/incremental-loading#incremental-loading-strategies)
- [Doing a full refresh](https://dlthub.com/docs/general-usage/incremental-loading#doing-a-full-refresh)
- [Next steps](https://dlthub.com/docs/general-usage/incremental-loading#next-steps)

----- https://dlthub.com/docs/general-usage/incremental/cursor -----

Version: 1.11.0 (latest)

On this page

In most REST APIs (and other data sources, i.e., database tables), you can request new or updated data by passing a timestamp or ID of the "last" record to a query. The API/database returns just the new/updated records from which you take the maximum/minimum timestamp/ID for the next load.

To do incremental loading this way, we need to:

- Figure out which field is used to track changes (the so-called **cursor field**) (e.g., "inserted\_at", "updated\_at", etc.);
- Determine how to pass the "last" (maximum/minimum) value of the cursor field to an API to get just new or modified data (how we do this depends on the source API).

Once you've figured that out, `dlt` takes care of finding maximum/minimum cursor field values, removing duplicates, and managing the state with the last values of the cursor. Take a look at the GitHub example below, where we request recently created issues.

```codeBlockLines_RjmQ
@dlt.resource(primary_key="id")
def repo_issues(
    access_token,
    repository,
    updated_at = dlt.sources.incremental("updated_at", initial_value="1970-01-01T00:00:00Z")
):
    # Get issues since "updated_at" stored in state on previous run (or initial_value on first run)
    for page in _get_issues_page(access_token, repository, since=updated_at.start_value):
        yield page
        # Last_value is updated after every page
        print(updated_at.last_value)

```

Here we add an `updated_at` argument that will receive incremental state, initialized to `1970-01-01T00:00:00Z`. It is configured to track the `updated_at` field in issues yielded by the `repo_issues` resource. It will store the newest `updated_at` value in `dlt` [state](https://dlthub.com/docs/general-usage/state) and make it available in `updated_at.start_value` on the next pipeline run. This value is inserted in the `_get_issues_page` function into the request query param **since** to the [GitHub API](https://docs.github.com/en/rest/issues/issues?#list-repository-issues).

In essence, the `dlt.sources.incremental` instance above:

- **updated\_at.initial\_value** which is always equal to "1970-01-01T00:00:00Z" passed in the constructor
- **updated\_at.start\_value** a maximum `updated_at` value from the previous run or the **initial\_value** on the first run
- **updated\_at.last\_value** a "real-time" `updated_at` value updated with each yielded item or page. Before the first yield, it equals **start\_value**
- **updated\_at.end\_value** (here not used) [marking the end of the backfill range](https://dlthub.com/docs/general-usage/incremental/cursor#using-end_value-for-backfill)

When paginating, you probably need the **start\_value** which does not change during the execution of the resource, however, most paginators will return a **next page** link which you should use.

Behind the scenes, dlt will deduplicate the results, i.e., in case the last issue is returned again ( `updated_at` filter is inclusive) and skip already loaded ones.

In the example below, we incrementally load the GitHub events, where the API does not let us filter for the newest events - it always returns all of them. Nevertheless, `dlt` will load only the new items, filtering out all the duplicates and past issues.

```codeBlockLines_RjmQ
# Use naming function in table name to generate separate tables for each event
@dlt.resource(primary_key="id", table_name=lambda i: i['type'])  # type: ignore
def repo_events(
    last_created_at = dlt.sources.incremental("created_at", initial_value="1970-01-01T00:00:00Z", last_value_func=max), row_order="desc"
) -> Iterator[TDataItems]:
    repos_path = "/repos/%s/%s/events" % (urllib.parse.quote(owner), urllib.parse.quote(name))
    for page in _get_rest_pages(access_token, repos_path + "?per_page=100"):
        yield page

```

We just yield all the events and `dlt` does the filtering (using the `id` column declared as `primary_key`).

GitHub returns events ordered from newest to oldest. So we declare the `rows_order` as **descending** to [stop requesting more pages once the incremental value is out of range](https://dlthub.com/docs/general-usage/incremental/cursor#declare-row-order-to-not-request-unnecessary-data). We stop requesting more data from the API after finding the first event with `created_at` earlier than `initial_value`.

note

`dlt.sources.incremental` is implemented as a [filter function](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data) that is executed **after** all other transforms you add with `add_map` or `add_filter`. This means that you can manipulate the data item before the incremental filter sees it. For example:

- You can create a surrogate primary key from other columns
- You can modify the cursor value or create a new field composed of other fields
- Dump Pydantic models to Python dicts to allow incremental to find custom values

[Data validation with Pydantic](https://dlthub.com/docs/general-usage/schema-contracts#use-pydantic-models-for-data-validation) happens **before** incremental filtering.

## Max, min, or custom `last_value_func` [​](https://dlthub.com/docs/general-usage/incremental/cursor\#max-min-or-custom-last_value_func "Direct link to max-min-or-custom-last_value_func")

`dlt.sources.incremental` allows you to choose a function that orders (compares) cursor values to the current `last_value`.

- The default function is the built-in `max`, which returns the larger value of the two.
- Another built-in, `min`, returns the smaller value.

You can also pass your custom function. This lets you define
`last_value` on nested types, i.e., dictionaries, and store indexes of last values, not just simple
types. The `last_value` argument is a [JSON Path](https://github.com/json-path/JsonPath#operators)
and lets you select nested data (including the whole data item when `$` is used).
The example below creates a last value which is a dictionary holding a max `created_at` value for each
created table name:

```codeBlockLines_RjmQ
def by_event_type(event):
    last_value = None
    if len(event) == 1:
        item, = event
    else:
        item, last_value = event

    if last_value is None:
        last_value = {}
    else:
        last_value = dict(last_value)
    item_type = item["type"]
    last_value[item_type] = max(item["created_at"], last_value.get(item_type, "1970-01-01T00:00:00Z"))
    return last_value

@dlt.resource(primary_key="id", table_name=lambda i: i['type'])
def get_events(last_created_at = dlt.sources.incremental("$", last_value_func=by_event_type)):
    with open("tests/normalize/cases/github.events.load_page_1_duck.json", "r", encoding="utf-8") as f:
        yield json.load(f)

```

## Using `end_value` for backfill [​](https://dlthub.com/docs/general-usage/incremental/cursor\#using-end_value-for-backfill "Direct link to using-end_value-for-backfill")

You can specify both initial and end dates when defining incremental loading. Let's go back to our Github example:

```codeBlockLines_RjmQ
@dlt.resource(primary_key="id")
def repo_issues(
    access_token,
    repository,
    created_at=dlt.sources.incremental("created_at", initial_value="1970-01-01T00:00:00Z", end_value="2022-07-01T00:00:00Z")
):
    # get issues created from the last "created_at" value
    for page in _get_issues_page(access_token, repository, since=created_at.start_value, until=created_at.end_value):
        yield page

```

Above, we use the `initial_value` and `end_value` arguments of the `incremental` to define the range of issues that we want to retrieve
and pass this range to the Github API ( `since` and `until`). As in the examples above, `dlt` will make sure that only the issues from
the defined range are returned.

Please note that when `end_date` is specified, `dlt` **will not modify the existing incremental state**. The backfill is **stateless** and:

1. You can run backfill and incremental load in parallel (i.e., in an Airflow DAG) in a single pipeline.
2. You can partition your backfill into several smaller chunks and run them in parallel as well.

To define specific ranges to load, you can simply override the incremental argument in the resource, for example:

```codeBlockLines_RjmQ
july_issues = repo_issues(
    created_at=dlt.sources.incremental(
        initial_value='2022-07-01T00:00:00Z', end_value='2022-08-01T00:00:00Z'
    )
)
august_issues = repo_issues(
    created_at=dlt.sources.incremental(
        initial_value='2022-08-01T00:00:00Z', end_value='2022-09-01T00:00:00Z'
    )
)
...

```

Note that dlt's incremental filtering considers the ranges half-closed. `initial_value` is inclusive, `end_value` is exclusive, so chaining ranges like above works without overlaps. This behaviour can be changed with the `range_start` (default `"closed"`) and `range_end` (default `"open"`) arguments.

## Declare row order to not request unnecessary data [​](https://dlthub.com/docs/general-usage/incremental/cursor\#declare-row-order-to-not-request-unnecessary-data "Direct link to Declare row order to not request unnecessary data")

With the `row_order` argument set, dlt will stop retrieving data from the data source (e.g., GitHub API) if it detects that the values of the cursor field are out of the range of **start** and **end** values.

In particular:

- dlt stops processing when the resource yields any item with a cursor value _equal to or greater than_ the `end_value` and `row_order` is set to **asc**. ( `end_value` is not included)
- dlt stops processing when the resource yields any item with a cursor value _lower_ than the `last_value` and `row_order` is set to **desc**. ( `last_value` is included)

note

"higher" and "lower" here refer to when the default `last_value_func` is used ( `max()`),
when using `min()` "higher" and "lower" are inverted.

caution

If you use `row_order`, **make sure that the data source returns ordered records** (ascending / descending) on the cursor field,
e.g., if an API returns results both higher and lower
than the given `end_value` in no particular order, data reading stops and you'll miss the data items that were out of order.

Row order is most useful when:

1. The data source does **not** offer start/end filtering of results (e.g., there is no `start_time/end_time` query parameter or similar).
2. The source returns results **ordered by the cursor field**.

The GitHub events example is exactly such a case. The results are ordered on cursor value descending, but there's no way to tell the API to limit returned items to those created before a certain date. Without the `row_order` setting, we'd be getting all events, each time we extract the `github_events` resource.

In the same fashion, the `row_order` can be used to **optimize backfill** so we don't continue
making unnecessary API requests after the end of the range is reached. For example:

```codeBlockLines_RjmQ
@dlt.resource(primary_key="id")
def tickets(
    zendesk_client,
    updated_at=dlt.sources.incremental(
        "updated_at",
        initial_value="2023-01-01T00:00:00Z",
        end_value="2023-02-01T00:00:00Z",
        row_order="asc"
    ),
):
    for page in zendesk_client.get_pages(
        "/api/v2/incremental/tickets", "tickets", start_time=updated_at.start_value
    ):
        yield page

```

In this example, we're loading tickets from Zendesk. The Zendesk API yields items paginated and ordered from oldest to newest,
but only offers a `start_time` parameter for filtering, so we cannot tell it to
stop retrieving data at `end_value`. Instead, we set `row_order` to `asc` and `dlt` will stop
getting more pages from the API after the first page with a cursor value `updated_at` is found older
than `end_value`.

caution

In rare cases when you use Incremental with a transformer, `dlt` will not be able to automatically close
the generator associated with a row that is out of range. You can still call the `can_close()` method on
incremental and exit the yield loop when true.

tip

The `dlt.sources.incremental` instance provides `start_out_of_range` and `end_out_of_range`
attributes which are set when the resource yields an element with a higher/lower cursor value than the
initial or end values. If you do not want `dlt` to stop processing automatically and instead want to handle such events yourself, do not specify `row_order`:

```codeBlockLines_RjmQ
@dlt.transformer(primary_key="id")
def tickets(
    zendesk_client,
    updated_at=dlt.sources.incremental(
        "updated_at",
        initial_value="2023-01-01T00:00:00Z",
        end_value="2023-02-01T00:00:00Z",
        row_order="asc"
    ),
):
    for page in zendesk_client.get_pages(
        "/api/v2/incremental/tickets", "tickets", start_time=updated_at.start_value
    ):
        yield page
        # Stop loading when we reach the end value
        if updated_at.end_out_of_range:
            return

```

## Deduplicate overlapping ranges with primary key [​](https://dlthub.com/docs/general-usage/incremental/cursor\#deduplicate-overlapping-ranges-with-primary-key "Direct link to Deduplicate overlapping ranges with primary key")

`Incremental` **does not** deduplicate datasets like the **merge** write disposition does. However, it ensures that when another portion of data is extracted, records that were previously loaded won't be included again. `dlt` assumes that you load a range of data, where the lower bound is inclusive (i.e., greater than or equal). This ensures that you never lose any data but will also re-acquire some rows. For example, if you have a database table with a cursor field on `updated_at` which has a day resolution, then there's a high chance that after you extract data on a given day, more records will still be added. When you extract on the next day, you should reacquire data from the last day to ensure all records are present; however, this will create overlap with data from the previous extract.

By default, a content hash (a hash of the JSON representation of a row) will be used to deduplicate. This may be slow, so `dlt.sources.incremental` will inherit the primary key that is set on the resource. You can optionally set a `primary_key` that is used exclusively to deduplicate and which does not become a table hint. The same setting lets you disable the deduplication altogether when an empty tuple is passed. Below, we pass `primary_key` directly to `incremental` to disable deduplication. That overrides the `delta` primary\_key set in the resource:

```codeBlockLines_RjmQ
@dlt.resource(primary_key="delta")
# disable the unique value check by passing () as primary key to incremental
def some_data(last_timestamp=dlt.sources.incremental("item.ts", primary_key=())):
    for i in range(-10, 10):
        yield {"delta": i, "item": {"ts": pendulum.now().timestamp()}}

```

This deduplication process is always enabled when `range_start` is set to `"closed"` (default).
When you pass `range_start="open"` no deduplication is done as it is not needed as rows with the previous cursor value are excluded. This can be a useful optimization to avoid the performance overhead of deduplication if the cursor field is guaranteed to be unique.

## Using `dlt.sources.incremental` with dynamically created resources [​](https://dlthub.com/docs/general-usage/incremental/cursor\#using-dltsourcesincremental-with-dynamically-created-resources "Direct link to using-dltsourcesincremental-with-dynamically-created-resources")

When resources are [created dynamically](https://dlthub.com/docs/general-usage/source#create-resources-dynamically), it is possible to use the `dlt.sources.incremental` definition as well.

```codeBlockLines_RjmQ
@dlt.source
def stripe():
    # declare a generator function
    def get_resource(
        endpoints: List[str] = ENDPOINTS,
        created: dlt.sources.incremental=dlt.sources.incremental("created")
    ):
        ...

    # create resources for several endpoints on a single decorator function
    for endpoint in endpoints:
        yield dlt.resource(
            get_resource,
            name=endpoint.value,
            write_disposition="merge",
            primary_key="id"
        )(endpoint)

```

Please note that in the example above, `get_resource` is passed as a function to `dlt.resource` to which we bind the endpoint: **dlt.resource(...)(endpoint)**.

caution

The typical mistake is to pass a generator (not a function) as below:

`yield dlt.resource(get_resource(endpoint), name=endpoint.value, write_disposition="merge", primary_key="id")`.

Here we call **get\_resource(endpoint)** and that creates an un-evaluated generator on which the resource is created. That prevents `dlt` from controlling the **created** argument during runtime and will result in an `IncrementalUnboundError` exception.

## Using Airflow schedule for backfill and incremental loading [​](https://dlthub.com/docs/general-usage/incremental/cursor\#using-airflow-schedule-for-backfill-and-incremental-loading "Direct link to Using Airflow schedule for backfill and incremental loading")

When [running an Airflow task](https://dlthub.com/docs/walkthroughs/deploy-a-pipeline/deploy-with-airflow-composer#2-modify-dag-file), you can opt-in your resource to get the `initial_value`/ `start_value` and `end_value` from the Airflow schedule associated with your DAG. Let's assume that the **Zendesk tickets** resource contains a year of data with thousands of tickets. We want to backfill the last year of data week by week and then continue with incremental loading daily.

```codeBlockLines_RjmQ
@dlt.resource(primary_key="id")
def tickets(
    zendesk_client,
    updated_at=dlt.sources.incremental[int](
        "updated_at",
        allow_external_schedulers=True
    ),
):
    for page in zendesk_client.get_pages(
        "/api/v2/incremental/tickets", "tickets", start_time=updated_at.start_value
    ):
        yield page

```

We opt-in to the Airflow scheduler by setting `allow_external_schedulers` to `True`:

1. When running on Airflow, the start and end values are controlled by Airflow and the dlt [state](https://dlthub.com/docs/general-usage/state) is not used.
2. In all other environments, the `incremental` behaves as usual, maintaining the dlt state.

Let's generate a deployment with `dlt deploy zendesk_pipeline.py airflow-composer` and customize the DAG:

```codeBlockLines_RjmQ
from dlt.helpers.airflow_helper import PipelineTasksGroup

@dag(
    schedule_interval='@weekly',
    start_date=pendulum.DateTime(2023, 2, 1),
    end_date=pendulum.DateTime(2023, 8, 1),
    catchup=True,
    max_active_runs=1,
    default_args=default_task_args
)
def zendesk_backfill_bigquery():
    tasks = PipelineTasksGroup("zendesk_support_backfill", use_data_folder=False, wipe_local_data=True)

    # import zendesk like in the demo script
    from zendesk import zendesk_support

    pipeline = dlt.pipeline(
        pipeline_name="zendesk_support_backfill",
        dataset_name="zendesk_support_data",
        destination='bigquery',
    )
    # select only incremental endpoints in support api
    data = zendesk_support().with_resources("tickets", "ticket_events", "ticket_metric_events")
    # create the source, the "serialize" decompose option will convert dlt resources into Airflow tasks. use "none" to disable it
    tasks.add_run(pipeline, data, decompose="serialize", trigger_rule="all_done", retries=0, provide_context=True)

zendesk_backfill_bigquery()

```

What got customized:

1. We use a weekly schedule and want to get the data from February 2023 ( `start_date`) until the end of July ( `end_date`).
2. We make Airflow generate all weekly runs ( `catchup` is True).
3. We create `zendesk_support` resources where we select only the incremental resources we want to backfill.

When you enable the DAG in Airflow, it will generate several runs and start executing them, starting in February and ending in August. Your resource will receive subsequent weekly intervals starting with `2023-02-12, 00:00:00 UTC` to `2023-02-19, 00:00:00 UTC`.

You can repurpose the DAG above to start loading new data incrementally after (or during) the backfill:

```codeBlockLines_RjmQ
@dag(
    schedule_interval='@daily',
    start_date=pendulum.DateTime(2023, 2, 1),
    catchup=False,
    max_active_runs=1,
    default_args=default_task_args
)
def zendesk_new_bigquery():
    tasks = PipelineTasksGroup("zendesk_support_new", use_data_folder=False, wipe_local_data=True)

    # import your source from pipeline script
    from zendesk import zendesk_support

    pipeline = dlt.pipeline(
        pipeline_name="zendesk_support_new",
        dataset_name="zendesk_support_data",
        destination='bigquery',
    )
    tasks.add_run(pipeline, zendesk_support(), decompose="serialize", trigger_rule="all_done", retries=0, provide_context=True)

```

Above, we switch to a daily schedule and disable catchup and end date. We also load all the support resources to the same dataset as backfill ( `zendesk_support_data`).
If you want to run this DAG parallel with the backfill DAG, change the pipeline name, for example, to `zendesk_support_new` as above.

**Under the hood**

Before `dlt` starts executing incremental resources, it looks for `data_interval_start` and `data_interval_end` Airflow task context variables. These are mapped to `initial_value` and `end_value` of the `Incremental` class:

1. `dlt` is smart enough to convert Airflow datetime to ISO strings or Unix timestamps if your resource is using them. In our example, we instantiate `updated_at=dlt.sources.incremental[int]`, where we declare the last value type to be **int**. `dlt` can also infer the type if you provide the `initial_value` argument.
2. If `data_interval_end` is in the future or is None, `dlt` sets the `end_value` to **now**.
3. If `data_interval_start` == `data_interval_end`, we have a manually triggered DAG run. In that case, `data_interval_end` will also be set to **now**.

**Manual runs**

You can run DAGs manually, but you must remember to specify the Airflow logical date of the run in the past (use the Run with config option). For such a run, `dlt` will load all data from that past date until now.
If you do not specify the past date, a run with a range (now, now) will happen, yielding no data.

## Reading incremental loading parameters from configuration [​](https://dlthub.com/docs/general-usage/incremental/cursor\#reading-incremental-loading-parameters-from-configuration "Direct link to Reading incremental loading parameters from configuration")

Consider the example below for reading incremental loading parameters from "config.toml". We create a `generate_incremental_records` resource that yields "id", "idAfter", and "name". This resource retrieves `cursor_path` and `initial_value` from "config.toml".

1. In "config.toml", define the `cursor_path` and `initial_value` as:

```codeBlockLines_RjmQ
# Configuration snippet for an incremental resource
[pipeline_with_incremental.sources.id_after]
cursor_path = "idAfter"
initial_value = 10

```

`cursor_path` is assigned the value "idAfter" with an initial value of 10.

2. Here's how the `generate_incremental_records` resource uses the `cursor_path` defined in "config.toml":

```codeBlockLines_RjmQ
@dlt.resource(table_name="incremental_records")
def generate_incremental_records(id_after: dlt.sources.incremental = dlt.config.value):
       for i in range(150):
           yield {"id": i, "idAfter": i, "name": "name-" + str(i)}

pipeline = dlt.pipeline(
       pipeline_name="pipeline_with_incremental",
       destination="duckdb",
)

pipeline.run(generate_incremental_records)

```

`id_after` incrementally stores the latest `cursor_path` value for future pipeline runs.

## Loading when incremental cursor path is missing or value is None/NULL [​](https://dlthub.com/docs/general-usage/incremental/cursor\#loading-when-incremental-cursor-path-is-missing-or-value-is-nonenull "Direct link to Loading when incremental cursor path is missing or value is None/NULL")

You can customize the incremental processing of dlt by setting the parameter `on_cursor_value_missing`.

When loading incrementally with the default settings, there are two assumptions:

1. Each row contains the cursor path.
2. Each row is expected to contain a value at the cursor path that is not `None`.

For example, the two following source data will raise an error:

```codeBlockLines_RjmQ
@dlt.resource
def some_data_without_cursor_path(updated_at=dlt.sources.incremental("updated_at")):
    yield [\
        {"id": 1, "created_at": 1, "updated_at": 1},\
        {"id": 2, "created_at": 2},  # cursor field is missing\
    ]

list(some_data_without_cursor_path())

@dlt.resource
def some_data_without_cursor_value(updated_at=dlt.sources.incremental("updated_at")):
    yield [\
        {"id": 1, "created_at": 1, "updated_at": 1},\
        {"id": 3, "created_at": 4, "updated_at": None},  # value at cursor field is None\
    ]

list(some_data_without_cursor_value())

```

To process a data set where some records do not include the incremental cursor path or where the values at the cursor path are `None`, there are the following four options:

1. Configure the incremental load to raise an exception in case there is a row where the cursor path is missing or has the value `None` using `incremental(..., on_cursor_value_missing="raise")`. This is the default behavior.
2. Configure the incremental load to tolerate the missing cursor path and `None` values using `incremental(..., on_cursor_value_missing="include")`.
3. Configure the incremental load to exclude the missing cursor path and `None` values using `incremental(..., on_cursor_value_missing="exclude")`.
4. Before the incremental processing begins: Ensure that the incremental field is present and transform the values at the incremental cursor to a value different from `None`. [See docs below](https://dlthub.com/docs/general-usage/incremental/cursor#transform-records-before-incremental-processing)

Here is an example of including rows where the incremental cursor value is missing or `None`:

```codeBlockLines_RjmQ
@dlt.resource
def some_data(updated_at=dlt.sources.incremental("updated_at", on_cursor_value_missing="include")):
    yield [\
        {"id": 1, "created_at": 1, "updated_at": 1},\
        {"id": 2, "created_at": 2},\
        {"id": 3, "created_at": 4, "updated_at": None},\
    ]

result = list(some_data())
assert len(result) == 3
assert result[1] == {"id": 2, "created_at": 2}
assert result[2] == {"id": 3, "created_at": 4, "updated_at": None}

```

If you do not want to import records without the cursor path or where the value at the cursor path is `None`, use the following incremental configuration:

```codeBlockLines_RjmQ
@dlt.resource
def some_data(updated_at=dlt.sources.incremental("updated_at", on_cursor_value_missing="exclude")):
    yield [\
        {"id": 1, "created_at": 1, "updated_at": 1},\
        {"id": 2, "created_at": 2},\
        {"id": 3, "created_at": 4, "updated_at": None},\
    ]

result = list(some_data())
assert len(result) == 1

```

## Transform records before incremental processing [​](https://dlthub.com/docs/general-usage/incremental/cursor\#transform-records-before-incremental-processing "Direct link to Transform records before incremental processing")

If you want to load data that includes `None` values, you can transform the records before the incremental processing.
You can add steps to the pipeline that [filter, transform, or pivot your data](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data).

caution

It is important to set the `insert_at` parameter of the `add_map` function to control the order of execution and ensure that your custom steps are executed before the incremental processing starts.
In the following example, the step of data yielding is at `index = 0`, the custom transformation at `index = 1`, and the incremental processing at `index = 2`.

See below how you can modify rows before the incremental processing using `add_map()` and filter rows using `add_filter()`.

```codeBlockLines_RjmQ
@dlt.resource
def some_data(updated_at=dlt.sources.incremental("updated_at")):
    yield [\
        {"id": 1, "created_at": 1, "updated_at": 1},\
        {"id": 2, "created_at": 2, "updated_at": 2},\
        {"id": 3, "created_at": 4, "updated_at": None},\
    ]

def set_default_updated_at(record):
    if record.get("updated_at") is None:
        record["updated_at"] = record.get("created_at")
    return record

# Modifies records before the incremental processing
with_default_values = some_data().add_map(set_default_updated_at, insert_at=1)
result = list(with_default_values)
assert len(result) == 3
assert result[2]["updated_at"] == 4

# Removes records before the incremental processing
without_none = some_data().add_filter(lambda r: r.get("updated_at") is not None, insert_at=1)
result_filtered = list(without_none)
assert len(result_filtered) == 2

```

- [Max, min, or custom `last_value_func`](https://dlthub.com/docs/general-usage/incremental/cursor#max-min-or-custom-last_value_func)
- [Using `end_value` for backfill](https://dlthub.com/docs/general-usage/incremental/cursor#using-end_value-for-backfill)
- [Declare row order to not request unnecessary data](https://dlthub.com/docs/general-usage/incremental/cursor#declare-row-order-to-not-request-unnecessary-data)
- [Deduplicate overlapping ranges with primary key](https://dlthub.com/docs/general-usage/incremental/cursor#deduplicate-overlapping-ranges-with-primary-key)
- [Using `dlt.sources.incremental` with dynamically created resources](https://dlthub.com/docs/general-usage/incremental/cursor#using-dltsourcesincremental-with-dynamically-created-resources)
- [Using Airflow schedule for backfill and incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor#using-airflow-schedule-for-backfill-and-incremental-loading)
- [Reading incremental loading parameters from configuration](https://dlthub.com/docs/general-usage/incremental/cursor#reading-incremental-loading-parameters-from-configuration)
- [Loading when incremental cursor path is missing or value is None/NULL](https://dlthub.com/docs/general-usage/incremental/cursor#loading-when-incremental-cursor-path-is-missing-or-value-is-nonenull)
- [Transform records before incremental processing](https://dlthub.com/docs/general-usage/incremental/cursor#transform-records-before-incremental-processing)

----- https://dlthub.com/docs/general-usage/resource#define-schema -----

PreferencesDeclineAccept

Version: 1.11.0 (latest)

On this page

## Declare a resource [​](https://dlthub.com/docs/general-usage/resource\#declare-a-resource "Direct link to Declare a resource")

A [resource](https://dlthub.com/docs/general-usage/glossary#resource) is an ( [optionally async](https://dlthub.com/docs/reference/performance#parallelism-within-a-pipeline)) function that yields data. To create a resource, we add the `@dlt.resource` decorator to that function.

Commonly used arguments:

- `name`: The name of the table generated by this resource. Defaults to the decorated function name.
- `write_disposition`: How should the data be loaded at the destination? Currently supported: `append`,
`replace`, and `merge`. Defaults to `append.`

Example:

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows():
	for i in range(10):
		yield {'id': i, 'example_string': 'abc'}

@dlt.source
def source_name():
    return generate_rows

```

To get the data of a resource, we could do:

```codeBlockLines_RjmQ
for row in generate_rows():
    print(row)

for row in source_name().resources.get('table_name'):
    print(row)

```

Typically, resources are declared and grouped with related resources within a [source](https://dlthub.com/docs/general-usage/source) function.

### Define schema [​](https://dlthub.com/docs/general-usage/resource\#define-schema "Direct link to Define schema")

`dlt` will infer the [schema](https://dlthub.com/docs/general-usage/schema) for tables associated with resources from the resource's data.
You can modify the generation process by using the table and column hints. The resource decorator accepts the following arguments:

1. `table_name`: the name of the table, if different from the resource name.
2. `primary_key` and `merge_key`: define the name of the columns (compound keys are allowed) that will receive those hints. Used in [incremental loading](https://dlthub.com/docs/general-usage/incremental-loading) and [merge loading](https://dlthub.com/docs/general-usage/merge-loading).
3. `columns`: lets you define one or more columns, including the data types, nullability, and other hints. The column definition is a `TypedDict`: `TTableSchemaColumns`. In the example below, we tell `dlt` that the column `tags` (containing a list of tags) in the `user` table should have type `json`, which means that it will be loaded as JSON/struct and not as a separate nested table.

```codeBlockLines_RjmQ
@dlt.resource(name="user", columns={"tags": {"data_type": "json"}})
def get_users():
  ...

# the `table_schema` method gets the table schema generated by a resource
print(get_users().compute_table_schema())

```

note

You can pass dynamic hints which are functions that take the data item as input and return a hint value. This lets you create table and column schemas depending on the data. See an [example below](https://dlthub.com/docs/general-usage/resource#adjust-schema-when-you-yield-data).

### Put a contract on tables, columns, and data [​](https://dlthub.com/docs/general-usage/resource\#put-a-contract-on-tables-columns-and-data "Direct link to Put a contract on tables, columns, and data")

Use the `schema_contract` argument to tell dlt how to [deal with new tables, data types, and bad data types](https://dlthub.com/docs/general-usage/schema-contracts). For example, if you set it to **freeze**, `dlt` will not allow for any new tables, columns, or data types to be introduced to the schema - it will raise an exception. Learn more about available contract modes [here](https://dlthub.com/docs/general-usage/schema-contracts#setting-up-the-contract).

### Define schema of nested tables [​](https://dlthub.com/docs/general-usage/resource\#define-schema-of-nested-tables "Direct link to Define schema of nested tables")

`dlt` creates [nested tables](https://dlthub.com/docs/general-usage/schema#nested-references-root-and-nested-tables) to store [list of objects](https://dlthub.com/docs/general-usage/destination-tables#nested-tables) if present in your data.
You can define the schema of such tables with `nested_hints` argument to `@dlt.resource`:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(
    nested_hints={
        "purchases": dlt.mark.make_nested_hints(
            columns=[{"name": "price", "data_type": "decimal"}],
            schema_contract={"columns": "freeze"},
        )
    },
)
def customers():
    """Load customer data from a simple python list."""
    yield [\
        {\
            "id": 1,\
            "name": "simon",\
            "city": "berlin",\
            "purchases": [{"id": 1, "name": "apple", "price": "1.50"}],\
        },\
    ]

```

Here we convert the `price` field in list of `purchases` to decimal type and set the schema contract to lock the list
of columns in it. We use convenience function `dlt.mark.make_nested_hints` to generate nested hints dictionary. You are
free to use it directly.

Mind that `purchases` list will be stored as table with name `customers__purchases`. When declaring nested hints you just need
to specify nested field(s) name(s). In case of deeper nesting ie. let's say each `purchase` has a list of `coupons` applied,
you can apply hints to coupons and define `customers__purchases__coupons` table schema:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(
    nested_hints={
        "purchases": {},
        ("purchases", "coupons"): {
            "columns": {"registered_at": {"data_type": "timestamp"}}
        }
    },
)
def customers():
    ...

```

Here we use `("purchases", "coupons")` to locate list at the depth of 2 and set the data type on `registered_at` column
to `timestamp`. We do that by directly using nested hints dict.
Note that we specified `purchases` with an empty list of hints. **You are required to specify all parent hints, even if they**
**are empty. Currently we are not adding missing path elements automatically**.

You can use `nested_hints` primarily to set column hints and schema contract, those work exactly as in case of root tables.

- `file_format` has no effect (not implemented yet)
- `write_disposition` works as expected but leads to unintended consequences (ie. you can set nested table to `replace`) while root table is `append`.
- `references` will create [table references](https://dlthub.com/docs/general-usage/schema#table-references-1) (annotations) as expected.
- `primary_key` and `merge_key`: **setting those will convert nested table into a regular table, with a separate write disposition, file format etc.** [It allows you to create custom table relationships ie. using natural primary and foreign keys present in the data.](https://dlthub.com/docs/general-usage/schema#generate-custom-linking-for-nested-tables)

tip

[REST API Source](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic) accepts `nested_hints` argument as well.

You can apply nested hints after the resource was created by using [apply\_hints](https://dlthub.com/docs/general-usage/resource#set-table-name-and-adjust-schema).

### Define a schema with Pydantic [​](https://dlthub.com/docs/general-usage/resource\#define-a-schema-with-pydantic "Direct link to Define a schema with Pydantic")

You can alternatively use a [Pydantic](https://pydantic-docs.helpmanual.io/) model to define the schema.
For example:

```codeBlockLines_RjmQ
from pydantic import BaseModel
from typing import List, Optional, Union

class Address(BaseModel):
    street: str
    city: str
    postal_code: str

class User(BaseModel):
    id: int
    name: str
    tags: List[str]
    email: Optional[str]
    address: Address
    status: Union[int, str]

@dlt.resource(name="user", columns=User)
def get_users():
    ...

```

The data types of the table columns are inferred from the types of the Pydantic fields. These use the same type conversions
as when the schema is automatically generated from the data.

Pydantic models integrate well with [schema contracts](https://dlthub.com/docs/general-usage/schema-contracts) as data validators.

Things to note:

- Fields with an `Optional` type are marked as `nullable`.
- Fields with a `Union` type are converted to the first (not `None`) type listed in the union. For example, `status: Union[int, str]` results in a `bigint` column.
- `list`, `dict`, and nested Pydantic model fields will use the `json` type, which means they'll be stored as a JSON object in the database instead of creating nested tables.

You can override this by configuring the Pydantic model:

```codeBlockLines_RjmQ
from typing import ClassVar
from dlt.common.libs.pydantic import DltConfig

class UserWithNesting(User):
  dlt_config: ClassVar[DltConfig] = {"skip_nested_types": True}

@dlt.resource(name="user", columns=UserWithNesting)
def get_users():
    ...

```

`"skip_nested_types"` omits any `dict`/ `list`/ `BaseModel` type fields from the schema, so dlt will fall back on the default
behavior of creating nested tables for these fields.

We do not support `RootModel` that validate simple types. You can add such a validator yourself, see [data filtering section](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data).

### Dispatch data to many tables [​](https://dlthub.com/docs/general-usage/resource\#dispatch-data-to-many-tables "Direct link to Dispatch data to many tables")

You can load data to many tables from a single resource. The most common case is a stream of events
of different types, each with a different data schema. To deal with this, you can use the `table_name`
argument on `dlt.resource`. You could pass the table name as a function with the data item as an
argument and the `table_name` string as a return value.

For example, a resource that loads GitHub repository events wants to send `issue`, `pull request`,
and `comment` events to separate tables. The type of the event is in the "type" field.

```codeBlockLines_RjmQ
# send item to a table with name item["type"]
@dlt.resource(table_name=lambda event: event['type'])
def repo_events() -> Iterator[TDataItems]:
    yield item

# the `table_schema` method gets the table schema generated by a resource and takes an optional
# data item to evaluate dynamic hints
print(repo_events().compute_table_schema({"type": "WatchEvent", "id": ...}))

```

In more advanced cases, you can dispatch data to different tables directly in the code of the
resource function:

```codeBlockLines_RjmQ
@dlt.resource
def repo_events() -> Iterator[TDataItems]:
    # mark the "item" to be sent to the table with the name item["type"]
    yield dlt.mark.with_table_name(item, item["type"])

```

### Parametrize a resource [​](https://dlthub.com/docs/general-usage/resource\#parametrize-a-resource "Direct link to Parametrize a resource")

You can add arguments to your resource functions like to any other. Below we parametrize our
`generate_rows` resource to generate the number of rows we request:

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

for row in generate_rows(10):
    print(row)

for row in generate_rows(20):
    print(row)

```

tip

You can mark some resource arguments as [configuration and credentials](https://dlthub.com/docs/general-usage/credentials) values so `dlt` can pass them automatically to your functions.

### Process resources with `dlt.transformer` [​](https://dlthub.com/docs/general-usage/resource\#process-resources-with-dlttransformer "Direct link to process-resources-with-dlttransformer")

You can feed data from one resource into another. The most common case is when you have an API that returns a list of objects (i.e., users) in one endpoint and user details in another. You can deal with this by declaring a resource that obtains a list of users and another resource that receives items from the list and downloads the profiles.

```codeBlockLines_RjmQ
@dlt.resource(write_disposition="replace")
def users(limit=None):
    for u in _get_users(limit):
        yield u

# Feed data from users as user_item below,
# all transformers must have at least one
# argument that will receive data from the parent resource
@dlt.transformer(data_from=users)
def users_details(user_item):
    for detail in _get_details(user_item["user_id"]):
        yield detail

# Just load the users_details.
# dlt figures out dependencies for you.
pipeline.run(users_details)

```

In the example above, `users_details` will receive data from the default instance of the `users` resource (with `limit` set to `None`). You can also use the **pipe \|** operator to bind resources dynamically.

```codeBlockLines_RjmQ
# You can be more explicit and use a pipe operator.
# With it, you can create dynamic pipelines where the dependencies
# are set at run time and resources are parametrized, i.e.,
# below we want to load only 100 users from the `users` endpoint.
pipeline.run(users(limit=100) | users_details)

```

tip

Transformers are allowed not only to **yield** but also to **return** values and can decorate **async** functions and [**async generators**](https://dlthub.com/docs/reference/performance#extract). Below we decorate an async function and request details on two pokemons. HTTP calls are made in parallel via the httpx library.

```codeBlockLines_RjmQ
import dlt
import httpx

@dlt.transformer
async def pokemon(id):
    async with httpx.AsyncClient() as client:
        r = await client.get(f"https://pokeapi.co/api/v2/pokemon/{id}")
        return r.json()

# Get Bulbasaur and Ivysaur (you need dlt 0.4.6 for the pipe operator working with lists).
print(list([1,2] | pokemon()))

```

### Declare a standalone resource [​](https://dlthub.com/docs/general-usage/resource\#declare-a-standalone-resource "Direct link to Declare a standalone resource")

A standalone resource is defined on a function that is top-level in a module (not an inner function) that accepts config and secrets values. Additionally, if the `standalone` flag is specified, the decorated function signature and docstring will be preserved. `dlt.resource` will just wrap the decorated function, and the user must call the wrapper to get the actual resource. Below we declare a `filesystem` resource that must be called before use.

```codeBlockLines_RjmQ
@dlt.resource(standalone=True)
def fs_resource(bucket_url=dlt.config.value):
  """List and yield files in `bucket_url`."""
  ...

# `filesystem` must be called before it is extracted or used in any other way.
pipeline.run(fs_resource("s3://my-bucket/reports"), table_name="reports")

```

Standalone may have a dynamic name that depends on the arguments passed to the decorated function. For example:

```codeBlockLines_RjmQ
@dlt.resource(standalone=True, name=lambda args: args["stream_name"])
def kinesis(stream_name: str):
    ...

kinesis_stream = kinesis("telemetry_stream")

```

`kinesis_stream` resource has a name **telemetry\_stream**.

### Declare parallel and async resources [​](https://dlthub.com/docs/general-usage/resource\#declare-parallel-and-async-resources "Direct link to Declare parallel and async resources")

You can extract multiple resources in parallel threads or with async IO.
To enable this for a sync resource, you can set the `parallelized` flag to `True` in the resource decorator:

```codeBlockLines_RjmQ
@dlt.resource(parallelized=True)
def get_users():
    for u in _get_users():
        yield u

@dlt.resource(parallelized=True)
def get_orders():
    for o in _get_orders():
        yield o

# users and orders will be iterated in parallel in two separate threads
pipeline.run([get_users(), get_orders()])

```

Async generators are automatically extracted concurrently with other resources:

```codeBlockLines_RjmQ
@dlt.resource
async def get_users():
    async for u in _get_users():  # Assuming _get_users is an async generator
        yield u

```

Please find more details in [extract performance](https://dlthub.com/docs/reference/performance#extract)

## Customize resources [​](https://dlthub.com/docs/general-usage/resource\#customize-resources "Direct link to Customize resources")

### Filter, transform, and pivot data [​](https://dlthub.com/docs/general-usage/resource\#filter-transform-and-pivot-data "Direct link to Filter, transform, and pivot data")

You can attach any number of transformations that are evaluated on an item-per-item basis to your
resource. The available transformation types:

- **map** \- transform the data item ( `resource.add_map`).
- **filter** \- filter the data item ( `resource.add_filter`).
- **yield map** \- a map that returns an iterator (so a single row may generate many rows -
`resource.add_yield_map`).

Example: We have a resource that loads a list of users from an API endpoint. We want to customize it
so:

1. We remove users with `user_id == "me"`.
2. We anonymize user data.

Here's our resource:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(write_disposition="replace")
def users():
    ...
    users = requests.get(RESOURCE_URL)
    ...
    yield users

```

Here's our script that defines transformations and loads the data:

```codeBlockLines_RjmQ
from pipedrive import users

def anonymize_user(user_data):
    user_data["user_id"] = _hash_str(user_data["user_id"])
    user_data["user_email"] = _hash_str(user_data["user_email"])
    return user_data

# add the filter and anonymize function to users resource and enumerate
for user in users().add_filter(lambda user: user["user_id"] != "me").add_map(anonymize_user):
    print(user)

```

### Reduce the nesting level of generated tables [​](https://dlthub.com/docs/general-usage/resource\#reduce-the-nesting-level-of-generated-tables "Direct link to Reduce the nesting level of generated tables")

You can limit how deep `dlt` goes when generating nested tables and flattening dicts into columns. By default, the library will descend
and generate nested tables for all nested lists, without limit.

note

`max_table_nesting` is optional so you can skip it, in this case, dlt will
use it from the source if it is specified there or fallback to the default
value which has 1000 as the maximum nesting level.

```codeBlockLines_RjmQ
import dlt

@dlt.resource(max_table_nesting=1)
def my_resource():
    yield {
        "id": 1,
        "name": "random name",
        "properties": [\
            {\
                "name": "customer_age",\
                "type": "int",\
                "label": "Age",\
                "notes": [\
                    {\
                        "text": "string",\
                        "author": "string",\
                    }\
                ]\
            }\
        ]
    }

```

In the example above, we want only 1 level of nested tables to be generated (so there are no nested
tables of a nested table). Typical settings:

- `max_table_nesting=0` will not generate nested tables and will not flatten dicts into columns at all. All nested data will be
represented as JSON.
- `max_table_nesting=1` will generate nested tables of root tables and nothing more. All nested
data in nested tables will be represented as JSON.

You can achieve the same effect after the resource instance is created:

```codeBlockLines_RjmQ
resource = my_resource()
resource.max_table_nesting = 0

```

Several data sources are prone to contain semi-structured documents with very deep nesting, i.e.,
MongoDB databases. Our practical experience is that setting the `max_nesting_level` to 2 or 3
produces the clearest and human-readable schemas.

### Sample from large data [​](https://dlthub.com/docs/general-usage/resource\#sample-from-large-data "Direct link to Sample from large data")

If your resource loads thousands of pages of data from a REST API or millions of rows from a database table, you may want to sample just a fragment of it in order to quickly see the dataset with example data and test your transformations, etc. To do this, you limit how many items will be yielded by a resource (or source) by calling the `add_limit` method. This method will close the generator that produces the data after the limit is reached.

In the example below, we load just the first 10 items from an infinite counter - that would otherwise never end.

```codeBlockLines_RjmQ
r = dlt.resource(itertools.count(), name="infinity").add_limit(10)
assert list(r) == list(range(10))

```

note

Note that `add_limit` **does not limit the number of records** but rather the "number of yields". Depending on how your resource is set up, the number of extracted rows may vary. For example, consider this resource:

```codeBlockLines_RjmQ
@dlt.resource
def my_resource():
    for i in range(100):
        yield [{"record_id": j} for j in range(15)]

dlt.pipeline(destination="duckdb").run(my_resource().add_limit(10))

```

The code above will extract `15*10=150` records. This is happening because in each iteration, 15 records are yielded, and we're limiting the number of iterations to 10.

Altenatively you can also apply a time limit to the resource. The code below will run the extraction for 10 seconds and extract how ever many items are yielded in that time. In combination with incrementals, this can be useful for batched loading or for loading on machines that have a run time limit.

```codeBlockLines_RjmQ
dlt.pipeline(destination="duckdb").run(my_resource().add_limit(max_time=10))

```

You can also apply a combination of both limits. In this case the extraction will stop as soon as either limit is reached.

```codeBlockLines_RjmQ
dlt.pipeline(destination="duckdb").run(my_resource().add_limit(max_items=10, max_time=10))

```

Some notes about the `add_limit`:

1. `add_limit` does not skip any items. It closes the iterator/generator that produces data after the limit is reached.
2. You cannot limit transformers. They should process all the data they receive fully to avoid inconsistencies in generated datasets.
3. Async resources with a limit added may occasionally produce one item more than the limit on some runs. This behavior is not deterministic.
4. Calling add limit on a resource will replace any previously set limits settings.
5. For time-limited resources, the timer starts when the first item is processed. When resources are processed sequentially (FIFO mode), each resource's time limit applies also sequentially. In the default round robin mode, the time limits will usually run concurrently.

tip

If you are parameterizing the value of `add_limit` and sometimes need it to be disabled, you can set `None` or `-1` to disable the limiting.
You can also set the limit to `0` for the resource to not yield any items.

### Set table name and adjust schema [​](https://dlthub.com/docs/general-usage/resource\#set-table-name-and-adjust-schema "Direct link to Set table name and adjust schema")

You can change the schema of a resource, whether it is standalone or part of a source. Look for a method named `apply_hints` which takes the same arguments as the resource decorator. Obviously, you should call this method before data is extracted from the resource. The example below converts an `append` resource loading the `users` table into a [merge](https://dlthub.com/docs/general-usage/merge-loading) resource that will keep just one updated record per `user_id`. It also adds ["last value" incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor) on the `created_at` column to prevent requesting again the already loaded records:

```codeBlockLines_RjmQ
tables = sql_database()
tables.users.apply_hints(
    write_disposition="merge",
    primary_key="user_id",
    incremental=dlt.sources.incremental("updated_at")
)
pipeline.run(tables)

```

To change the name of a table to which the resource will load data, do the following:

```codeBlockLines_RjmQ
tables = sql_database()
tables.users.table_name = "other_users"

```

### Adjust schema when you yield data [​](https://dlthub.com/docs/general-usage/resource\#adjust-schema-when-you-yield-data "Direct link to Adjust schema when you yield data")

You can set or update the table name, columns, and other schema elements when your resource is executed, and you already yield data. Such changes will be merged with the existing schema in the same way the `apply_hints` method above works. There are many reasons to adjust the schema at runtime. For example, when using Airflow, you should avoid lengthy operations (i.e., reflecting database tables) during the creation of the DAG, so it is better to do it when the DAG executes. You may also emit partial hints (i.e., precision and scale for decimal types) for columns to help `dlt` type inference.

```codeBlockLines_RjmQ
@dlt.resource
def sql_table(credentials, schema, table):
    # Create a SQL Alchemy engine
    engine = engine_from_credentials(credentials)
    engine.execution_options(stream_results=True)
    metadata = MetaData(schema=schema)
    # Reflect the table schema
    table_obj = Table(table, metadata, autoload_with=engine)

    for idx, batch in enumerate(table_rows(engine, table_obj)):
      if idx == 0:
        # Emit the first row with hints, table_to_columns and _get_primary_key are helpers that extract dlt schema from
        # SqlAlchemy model
        yield dlt.mark.with_hints(
            batch,
            dlt.mark.make_hints(columns=table_to_columns(table_obj), primary_key=_get_primary_key(table_obj)),
        )
      else:
        # Just yield all the other rows
        yield batch

```

In the example above, we use `dlt.mark.with_hints` and `dlt.mark.make_hints` to emit columns and primary key with the first extracted item. The table schema will be adjusted after the `batch` is processed in the extract pipeline but before any schema contracts are applied, and data is persisted in the load package.

tip

You can emit columns as a Pydantic model and use dynamic hints (i.e., lambda for table name) as well. You should avoid redefining `Incremental` this way.

### Import external files [​](https://dlthub.com/docs/general-usage/resource\#import-external-files "Direct link to Import external files")

You can import external files, i.e., CSV, Parquet, and JSONL, by yielding items marked with `with_file_import`, optionally passing a table schema corresponding to the imported file. dlt will not read, parse, or normalize any names (i.e., CSV or Arrow headers) and will attempt to copy the file into the destination as is.

```codeBlockLines_RjmQ
import os
import dlt
from dlt.sources.filesystem import filesystem

columns: List[TColumnSchema] = [\
    {"name": "id", "data_type": "bigint"},\
    {"name": "name", "data_type": "text"},\
    {"name": "description", "data_type": "text"},\
    {"name": "ordered_at", "data_type": "date"},\
    {"name": "price", "data_type": "decimal"},\
]

import_folder = "/tmp/import"

@dlt.transformer(columns=columns)
def orders(items: Iterator[FileItemDict]):
  for item in items:
    # copy the file locally
      dest_file = os.path.join(import_folder, item["file_name"])
      # download the file
      item.fsspec.download(item["file_url"], dest_file)
      # tell dlt to import the dest_file as `csv`
      yield dlt.mark.with_file_import(dest_file, "csv")

# use the filesystem core source to glob a bucket

downloader = filesystem(
  bucket_url="s3://my_bucket/csv",
  file_glob="today/*.csv.gz") | orders

info = pipeline.run(orders, destination="snowflake")

```

In the example above, we glob all zipped csv files present on **my\_bucket/csv/today** (using the `filesystem` verified source) and send file descriptors to the `orders` transformer. The transformer downloads and imports the files into the extract package. At the end, `dlt` sends them to Snowflake (the table will be created because we use `column` hints to define the schema).

If imported `csv` files are not in `dlt` [default format](https://dlthub.com/docs/dlt-ecosystem/file-formats/csv#default-settings), you may need to pass additional configuration.

```codeBlockLines_RjmQ
[destination.snowflake.csv_format]
delimiter="|"
include_header=false
on_error_continue=true

```

You can sniff the schema from the data, i.e., using DuckDB to infer the table schema from a CSV file. `dlt.mark.with_file_import` accepts additional arguments that you can use to pass hints at runtime.

note

- If you do not define any columns, the table will not be created in the destination. `dlt` will still attempt to load data into it, so if you create a fitting table upfront, the load process will succeed.
- Files are imported using hard links if possible to avoid copying and duplicating the storage space needed.

### Duplicate and rename resources [​](https://dlthub.com/docs/general-usage/resource\#duplicate-and-rename-resources "Direct link to Duplicate and rename resources")

There are cases when your resources are generic (i.e., bucket filesystem) and you want to load several instances of it (i.e., files from different folders) into separate tables. In the example below, we use the `filesystem` source to load csvs from two different folders into separate tables:

```codeBlockLines_RjmQ
@dlt.resource(standalone=True)
def fs_resource(bucket_url):
  # list and yield files in bucket_url
  ...

@dlt.transformer
def csv_reader(file_item):
  # load csv, parse, and yield rows in file_item
  ...

# create two extract pipes that list files from the bucket and send them to the reader.
# by default, both pipes will load data to the same table (csv_reader)
reports_pipe = fs_resource("s3://my-bucket/reports") | csv_reader()
transactions_pipe = fs_resource("s3://my-bucket/transactions") | csv_reader()

# so we rename resources to load to "reports" and "transactions" tables
pipeline.run(
  [reports_pipe.with_name("reports"), transactions_pipe.with_name("transactions")]
)

```

The `with_name` method returns a deep copy of the original resource, its data pipe, and the data pipes of a parent resource. A renamed clone is fully separated from the original resource (and other clones) when loading: it maintains a separate [resource state](https://dlthub.com/docs/general-usage/state#read-and-write-pipeline-state-in-a-resource) and will load to a table.

## Load resources [​](https://dlthub.com/docs/general-usage/resource\#load-resources "Direct link to Load resources")

You can pass individual resources or a list of resources to the `dlt.pipeline` object. The resources loaded outside the source context will be added to the [default schema](https://dlthub.com/docs/general-usage/schema) of the pipeline.

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

pipeline = dlt.pipeline(
    pipeline_name="rows_pipeline",
    destination="duckdb",
    dataset_name="rows_data"
)
# load an individual resource
pipeline.run(generate_rows(10))
# load a list of resources
pipeline.run([generate_rows(10), generate_rows(20)])

```

### Pick loader file format for a particular resource [​](https://dlthub.com/docs/general-usage/resource\#pick-loader-file-format-for-a-particular-resource "Direct link to Pick loader file format for a particular resource")

You can request a particular loader file format to be used for a resource.

```codeBlockLines_RjmQ
@dlt.resource(file_format="parquet")
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

```

The resource above will be saved and loaded from a Parquet file (if the destination supports it).

note

A special `file_format`: **preferred** will load the resource using a format that is preferred by a destination. This setting supersedes the `loader_file_format` passed to the `run` method.

### Do a full refresh [​](https://dlthub.com/docs/general-usage/resource\#do-a-full-refresh "Direct link to Do a full refresh")

To do a full refresh of an `append` or `merge` resource, you set the `refresh` argument on the `run` method to `drop_data`. This will truncate the tables without dropping them.

```codeBlockLines_RjmQ
p.run(merge_source(), refresh="drop_data")

```

You can also [fully drop the tables](https://dlthub.com/docs/general-usage/pipeline#refresh-pipeline-data-and-state) in the `merge_source`:

```codeBlockLines_RjmQ
p.run(merge_source(), refresh="drop_sources")

```

- [Declare a resource](https://dlthub.com/docs/general-usage/resource#declare-a-resource)
  - [Define schema](https://dlthub.com/docs/general-usage/resource#define-schema)
  - [Put a contract on tables, columns, and data](https://dlthub.com/docs/general-usage/resource#put-a-contract-on-tables-columns-and-data)
  - [Define schema of nested tables](https://dlthub.com/docs/general-usage/resource#define-schema-of-nested-tables)
  - [Define a schema with Pydantic](https://dlthub.com/docs/general-usage/resource#define-a-schema-with-pydantic)
  - [Dispatch data to many tables](https://dlthub.com/docs/general-usage/resource#dispatch-data-to-many-tables)
  - [Parametrize a resource](https://dlthub.com/docs/general-usage/resource#parametrize-a-resource)
  - [Process resources with `dlt.transformer`](https://dlthub.com/docs/general-usage/resource#process-resources-with-dlttransformer)
  - [Declare a standalone resource](https://dlthub.com/docs/general-usage/resource#declare-a-standalone-resource)
  - [Declare parallel and async resources](https://dlthub.com/docs/general-usage/resource#declare-parallel-and-async-resources)
- [Customize resources](https://dlthub.com/docs/general-usage/resource#customize-resources)
  - [Filter, transform, and pivot data](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data)
  - [Reduce the nesting level of generated tables](https://dlthub.com/docs/general-usage/resource#reduce-the-nesting-level-of-generated-tables)
  - [Sample from large data](https://dlthub.com/docs/general-usage/resource#sample-from-large-data)
  - [Set table name and adjust schema](https://dlthub.com/docs/general-usage/resource#set-table-name-and-adjust-schema)
  - [Adjust schema when you yield data](https://dlthub.com/docs/general-usage/resource#adjust-schema-when-you-yield-data)
  - [Import external files](https://dlthub.com/docs/general-usage/resource#import-external-files)
  - [Duplicate and rename resources](https://dlthub.com/docs/general-usage/resource#duplicate-and-rename-resources)
- [Load resources](https://dlthub.com/docs/general-usage/resource#load-resources)
  - [Pick loader file format for a particular resource](https://dlthub.com/docs/general-usage/resource#pick-loader-file-format-for-a-particular-resource)
  - [Do a full refresh](https://dlthub.com/docs/general-usage/resource#do-a-full-refresh)

[iframe](https://

----- https://dlthub.com/docs/general-usage/resource#customize-resources -----

Version: 1.11.0 (latest)

On this page

## Declare a resource [​](https://dlthub.com/docs/general-usage/resource\#declare-a-resource "Direct link to Declare a resource")

A [resource](https://dlthub.com/docs/general-usage/glossary#resource) is an ( [optionally async](https://dlthub.com/docs/reference/performance#parallelism-within-a-pipeline)) function that yields data. To create a resource, we add the `@dlt.resource` decorator to that function.

Commonly used arguments:

- `name`: The name of the table generated by this resource. Defaults to the decorated function name.
- `write_disposition`: How should the data be loaded at the destination? Currently supported: `append`,
`replace`, and `merge`. Defaults to `append.`

Example:

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows():
	for i in range(10):
		yield {'id': i, 'example_string': 'abc'}

@dlt.source
def source_name():
    return generate_rows

```

To get the data of a resource, we could do:

```codeBlockLines_RjmQ
for row in generate_rows():
    print(row)

for row in source_name().resources.get('table_name'):
    print(row)

```

Typically, resources are declared and grouped with related resources within a [source](https://dlthub.com/docs/general-usage/source) function.

### Define schema [​](https://dlthub.com/docs/general-usage/resource\#define-schema "Direct link to Define schema")

`dlt` will infer the [schema](https://dlthub.com/docs/general-usage/schema) for tables associated with resources from the resource's data.
You can modify the generation process by using the table and column hints. The resource decorator accepts the following arguments:

1. `table_name`: the name of the table, if different from the resource name.
2. `primary_key` and `merge_key`: define the name of the columns (compound keys are allowed) that will receive those hints. Used in [incremental loading](https://dlthub.com/docs/general-usage/incremental-loading) and [merge loading](https://dlthub.com/docs/general-usage/merge-loading).
3. `columns`: lets you define one or more columns, including the data types, nullability, and other hints. The column definition is a `TypedDict`: `TTableSchemaColumns`. In the example below, we tell `dlt` that the column `tags` (containing a list of tags) in the `user` table should have type `json`, which means that it will be loaded as JSON/struct and not as a separate nested table.

```codeBlockLines_RjmQ
@dlt.resource(name="user", columns={"tags": {"data_type": "json"}})
def get_users():
  ...

# the `table_schema` method gets the table schema generated by a resource
print(get_users().compute_table_schema())

```

note

You can pass dynamic hints which are functions that take the data item as input and return a hint value. This lets you create table and column schemas depending on the data. See an [example below](https://dlthub.com/docs/general-usage/resource#adjust-schema-when-you-yield-data).

### Put a contract on tables, columns, and data [​](https://dlthub.com/docs/general-usage/resource\#put-a-contract-on-tables-columns-and-data "Direct link to Put a contract on tables, columns, and data")

Use the `schema_contract` argument to tell dlt how to [deal with new tables, data types, and bad data types](https://dlthub.com/docs/general-usage/schema-contracts). For example, if you set it to **freeze**, `dlt` will not allow for any new tables, columns, or data types to be introduced to the schema - it will raise an exception. Learn more about available contract modes [here](https://dlthub.com/docs/general-usage/schema-contracts#setting-up-the-contract).

### Define schema of nested tables [​](https://dlthub.com/docs/general-usage/resource\#define-schema-of-nested-tables "Direct link to Define schema of nested tables")

`dlt` creates [nested tables](https://dlthub.com/docs/general-usage/schema#nested-references-root-and-nested-tables) to store [list of objects](https://dlthub.com/docs/general-usage/destination-tables#nested-tables) if present in your data.
You can define the schema of such tables with `nested_hints` argument to `@dlt.resource`:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(
    nested_hints={
        "purchases": dlt.mark.make_nested_hints(
            columns=[{"name": "price", "data_type": "decimal"}],
            schema_contract={"columns": "freeze"},
        )
    },
)
def customers():
    """Load customer data from a simple python list."""
    yield [\
        {\
            "id": 1,\
            "name": "simon",\
            "city": "berlin",\
            "purchases": [{"id": 1, "name": "apple", "price": "1.50"}],\
        },\
    ]

```

Here we convert the `price` field in list of `purchases` to decimal type and set the schema contract to lock the list
of columns in it. We use convenience function `dlt.mark.make_nested_hints` to generate nested hints dictionary. You are
free to use it directly.

Mind that `purchases` list will be stored as table with name `customers__purchases`. When declaring nested hints you just need
to specify nested field(s) name(s). In case of deeper nesting ie. let's say each `purchase` has a list of `coupons` applied,
you can apply hints to coupons and define `customers__purchases__coupons` table schema:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(
    nested_hints={
        "purchases": {},
        ("purchases", "coupons"): {
            "columns": {"registered_at": {"data_type": "timestamp"}}
        }
    },
)
def customers():
    ...

```

Here we use `("purchases", "coupons")` to locate list at the depth of 2 and set the data type on `registered_at` column
to `timestamp`. We do that by directly using nested hints dict.
Note that we specified `purchases` with an empty list of hints. **You are required to specify all parent hints, even if they**
**are empty. Currently we are not adding missing path elements automatically**.

You can use `nested_hints` primarily to set column hints and schema contract, those work exactly as in case of root tables.

- `file_format` has no effect (not implemented yet)
- `write_disposition` works as expected but leads to unintended consequences (ie. you can set nested table to `replace`) while root table is `append`.
- `references` will create [table references](https://dlthub.com/docs/general-usage/schema#table-references-1) (annotations) as expected.
- `primary_key` and `merge_key`: **setting those will convert nested table into a regular table, with a separate write disposition, file format etc.** [It allows you to create custom table relationships ie. using natural primary and foreign keys present in the data.](https://dlthub.com/docs/general-usage/schema#generate-custom-linking-for-nested-tables)

tip

[REST API Source](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic) accepts `nested_hints` argument as well.

You can apply nested hints after the resource was created by using [apply\_hints](https://dlthub.com/docs/general-usage/resource#set-table-name-and-adjust-schema).

### Define a schema with Pydantic [​](https://dlthub.com/docs/general-usage/resource\#define-a-schema-with-pydantic "Direct link to Define a schema with Pydantic")

You can alternatively use a [Pydantic](https://pydantic-docs.helpmanual.io/) model to define the schema.
For example:

```codeBlockLines_RjmQ
from pydantic import BaseModel
from typing import List, Optional, Union

class Address(BaseModel):
    street: str
    city: str
    postal_code: str

class User(BaseModel):
    id: int
    name: str
    tags: List[str]
    email: Optional[str]
    address: Address
    status: Union[int, str]

@dlt.resource(name="user", columns=User)
def get_users():
    ...

```

The data types of the table columns are inferred from the types of the Pydantic fields. These use the same type conversions
as when the schema is automatically generated from the data.

Pydantic models integrate well with [schema contracts](https://dlthub.com/docs/general-usage/schema-contracts) as data validators.

Things to note:

- Fields with an `Optional` type are marked as `nullable`.
- Fields with a `Union` type are converted to the first (not `None`) type listed in the union. For example, `status: Union[int, str]` results in a `bigint` column.
- `list`, `dict`, and nested Pydantic model fields will use the `json` type, which means they'll be stored as a JSON object in the database instead of creating nested tables.

You can override this by configuring the Pydantic model:

```codeBlockLines_RjmQ
from typing import ClassVar
from dlt.common.libs.pydantic import DltConfig

class UserWithNesting(User):
  dlt_config: ClassVar[DltConfig] = {"skip_nested_types": True}

@dlt.resource(name="user", columns=UserWithNesting)
def get_users():
    ...

```

`"skip_nested_types"` omits any `dict`/ `list`/ `BaseModel` type fields from the schema, so dlt will fall back on the default
behavior of creating nested tables for these fields.

We do not support `RootModel` that validate simple types. You can add such a validator yourself, see [data filtering section](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data).

### Dispatch data to many tables [​](https://dlthub.com/docs/general-usage/resource\#dispatch-data-to-many-tables "Direct link to Dispatch data to many tables")

You can load data to many tables from a single resource. The most common case is a stream of events
of different types, each with a different data schema. To deal with this, you can use the `table_name`
argument on `dlt.resource`. You could pass the table name as a function with the data item as an
argument and the `table_name` string as a return value.

For example, a resource that loads GitHub repository events wants to send `issue`, `pull request`,
and `comment` events to separate tables. The type of the event is in the "type" field.

```codeBlockLines_RjmQ
# send item to a table with name item["type"]
@dlt.resource(table_name=lambda event: event['type'])
def repo_events() -> Iterator[TDataItems]:
    yield item

# the `table_schema` method gets the table schema generated by a resource and takes an optional
# data item to evaluate dynamic hints
print(repo_events().compute_table_schema({"type": "WatchEvent", "id": ...}))

```

In more advanced cases, you can dispatch data to different tables directly in the code of the
resource function:

```codeBlockLines_RjmQ
@dlt.resource
def repo_events() -> Iterator[TDataItems]:
    # mark the "item" to be sent to the table with the name item["type"]
    yield dlt.mark.with_table_name(item, item["type"])

```

### Parametrize a resource [​](https://dlthub.com/docs/general-usage/resource\#parametrize-a-resource "Direct link to Parametrize a resource")

You can add arguments to your resource functions like to any other. Below we parametrize our
`generate_rows` resource to generate the number of rows we request:

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

for row in generate_rows(10):
    print(row)

for row in generate_rows(20):
    print(row)

```

tip

You can mark some resource arguments as [configuration and credentials](https://dlthub.com/docs/general-usage/credentials) values so `dlt` can pass them automatically to your functions.

### Process resources with `dlt.transformer` [​](https://dlthub.com/docs/general-usage/resource\#process-resources-with-dlttransformer "Direct link to process-resources-with-dlttransformer")

You can feed data from one resource into another. The most common case is when you have an API that returns a list of objects (i.e., users) in one endpoint and user details in another. You can deal with this by declaring a resource that obtains a list of users and another resource that receives items from the list and downloads the profiles.

```codeBlockLines_RjmQ
@dlt.resource(write_disposition="replace")
def users(limit=None):
    for u in _get_users(limit):
        yield u

# Feed data from users as user_item below,
# all transformers must have at least one
# argument that will receive data from the parent resource
@dlt.transformer(data_from=users)
def users_details(user_item):
    for detail in _get_details(user_item["user_id"]):
        yield detail

# Just load the users_details.
# dlt figures out dependencies for you.
pipeline.run(users_details)

```

In the example above, `users_details` will receive data from the default instance of the `users` resource (with `limit` set to `None`). You can also use the **pipe \|** operator to bind resources dynamically.

```codeBlockLines_RjmQ
# You can be more explicit and use a pipe operator.
# With it, you can create dynamic pipelines where the dependencies
# are set at run time and resources are parametrized, i.e.,
# below we want to load only 100 users from the `users` endpoint.
pipeline.run(users(limit=100) | users_details)

```

tip

Transformers are allowed not only to **yield** but also to **return** values and can decorate **async** functions and [**async generators**](https://dlthub.com/docs/reference/performance#extract). Below we decorate an async function and request details on two pokemons. HTTP calls are made in parallel via the httpx library.

```codeBlockLines_RjmQ
import dlt
import httpx

@dlt.transformer
async def pokemon(id):
    async with httpx.AsyncClient() as client:
        r = await client.get(f"https://pokeapi.co/api/v2/pokemon/{id}")
        return r.json()

# Get Bulbasaur and Ivysaur (you need dlt 0.4.6 for the pipe operator working with lists).
print(list([1,2] | pokemon()))

```

### Declare a standalone resource [​](https://dlthub.com/docs/general-usage/resource\#declare-a-standalone-resource "Direct link to Declare a standalone resource")

A standalone resource is defined on a function that is top-level in a module (not an inner function) that accepts config and secrets values. Additionally, if the `standalone` flag is specified, the decorated function signature and docstring will be preserved. `dlt.resource` will just wrap the decorated function, and the user must call the wrapper to get the actual resource. Below we declare a `filesystem` resource that must be called before use.

```codeBlockLines_RjmQ
@dlt.resource(standalone=True)
def fs_resource(bucket_url=dlt.config.value):
  """List and yield files in `bucket_url`."""
  ...

# `filesystem` must be called before it is extracted or used in any other way.
pipeline.run(fs_resource("s3://my-bucket/reports"), table_name="reports")

```

Standalone may have a dynamic name that depends on the arguments passed to the decorated function. For example:

```codeBlockLines_RjmQ
@dlt.resource(standalone=True, name=lambda args: args["stream_name"])
def kinesis(stream_name: str):
    ...

kinesis_stream = kinesis("telemetry_stream")

```

`kinesis_stream` resource has a name **telemetry\_stream**.

### Declare parallel and async resources [​](https://dlthub.com/docs/general-usage/resource\#declare-parallel-and-async-resources "Direct link to Declare parallel and async resources")

You can extract multiple resources in parallel threads or with async IO.
To enable this for a sync resource, you can set the `parallelized` flag to `True` in the resource decorator:

```codeBlockLines_RjmQ
@dlt.resource(parallelized=True)
def get_users():
    for u in _get_users():
        yield u

@dlt.resource(parallelized=True)
def get_orders():
    for o in _get_orders():
        yield o

# users and orders will be iterated in parallel in two separate threads
pipeline.run([get_users(), get_orders()])

```

Async generators are automatically extracted concurrently with other resources:

```codeBlockLines_RjmQ
@dlt.resource
async def get_users():
    async for u in _get_users():  # Assuming _get_users is an async generator
        yield u

```

Please find more details in [extract performance](https://dlthub.com/docs/reference/performance#extract)

## Customize resources [​](https://dlthub.com/docs/general-usage/resource\#customize-resources "Direct link to Customize resources")

### Filter, transform, and pivot data [​](https://dlthub.com/docs/general-usage/resource\#filter-transform-and-pivot-data "Direct link to Filter, transform, and pivot data")

You can attach any number of transformations that are evaluated on an item-per-item basis to your
resource. The available transformation types:

- **map** \- transform the data item ( `resource.add_map`).
- **filter** \- filter the data item ( `resource.add_filter`).
- **yield map** \- a map that returns an iterator (so a single row may generate many rows -
`resource.add_yield_map`).

Example: We have a resource that loads a list of users from an API endpoint. We want to customize it
so:

1. We remove users with `user_id == "me"`.
2. We anonymize user data.

Here's our resource:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(write_disposition="replace")
def users():
    ...
    users = requests.get(RESOURCE_URL)
    ...
    yield users

```

Here's our script that defines transformations and loads the data:

```codeBlockLines_RjmQ
from pipedrive import users

def anonymize_user(user_data):
    user_data["user_id"] = _hash_str(user_data["user_id"])
    user_data["user_email"] = _hash_str(user_data["user_email"])
    return user_data

# add the filter and anonymize function to users resource and enumerate
for user in users().add_filter(lambda user: user["user_id"] != "me").add_map(anonymize_user):
    print(user)

```

### Reduce the nesting level of generated tables [​](https://dlthub.com/docs/general-usage/resource\#reduce-the-nesting-level-of-generated-tables "Direct link to Reduce the nesting level of generated tables")

You can limit how deep `dlt` goes when generating nested tables and flattening dicts into columns. By default, the library will descend
and generate nested tables for all nested lists, without limit.

note

`max_table_nesting` is optional so you can skip it, in this case, dlt will
use it from the source if it is specified there or fallback to the default
value which has 1000 as the maximum nesting level.

```codeBlockLines_RjmQ
import dlt

@dlt.resource(max_table_nesting=1)
def my_resource():
    yield {
        "id": 1,
        "name": "random name",
        "properties": [\
            {\
                "name": "customer_age",\
                "type": "int",\
                "label": "Age",\
                "notes": [\
                    {\
                        "text": "string",\
                        "author": "string",\
                    }\
                ]\
            }\
        ]
    }

```

In the example above, we want only 1 level of nested tables to be generated (so there are no nested
tables of a nested table). Typical settings:

- `max_table_nesting=0` will not generate nested tables and will not flatten dicts into columns at all. All nested data will be
represented as JSON.
- `max_table_nesting=1` will generate nested tables of root tables and nothing more. All nested
data in nested tables will be represented as JSON.

You can achieve the same effect after the resource instance is created:

```codeBlockLines_RjmQ
resource = my_resource()
resource.max_table_nesting = 0

```

Several data sources are prone to contain semi-structured documents with very deep nesting, i.e.,
MongoDB databases. Our practical experience is that setting the `max_nesting_level` to 2 or 3
produces the clearest and human-readable schemas.

### Sample from large data [​](https://dlthub.com/docs/general-usage/resource\#sample-from-large-data "Direct link to Sample from large data")

If your resource loads thousands of pages of data from a REST API or millions of rows from a database table, you may want to sample just a fragment of it in order to quickly see the dataset with example data and test your transformations, etc. To do this, you limit how many items will be yielded by a resource (or source) by calling the `add_limit` method. This method will close the generator that produces the data after the limit is reached.

In the example below, we load just the first 10 items from an infinite counter - that would otherwise never end.

```codeBlockLines_RjmQ
r = dlt.resource(itertools.count(), name="infinity").add_limit(10)
assert list(r) == list(range(10))

```

note

Note that `add_limit` **does not limit the number of records** but rather the "number of yields". Depending on how your resource is set up, the number of extracted rows may vary. For example, consider this resource:

```codeBlockLines_RjmQ
@dlt.resource
def my_resource():
    for i in range(100):
        yield [{"record_id": j} for j in range(15)]

dlt.pipeline(destination="duckdb").run(my_resource().add_limit(10))

```

The code above will extract `15*10=150` records. This is happening because in each iteration, 15 records are yielded, and we're limiting the number of iterations to 10.

Altenatively you can also apply a time limit to the resource. The code below will run the extraction for 10 seconds and extract how ever many items are yielded in that time. In combination with incrementals, this can be useful for batched loading or for loading on machines that have a run time limit.

```codeBlockLines_RjmQ
dlt.pipeline(destination="duckdb").run(my_resource().add_limit(max_time=10))

```

You can also apply a combination of both limits. In this case the extraction will stop as soon as either limit is reached.

```codeBlockLines_RjmQ
dlt.pipeline(destination="duckdb").run(my_resource().add_limit(max_items=10, max_time=10))

```

Some notes about the `add_limit`:

1. `add_limit` does not skip any items. It closes the iterator/generator that produces data after the limit is reached.
2. You cannot limit transformers. They should process all the data they receive fully to avoid inconsistencies in generated datasets.
3. Async resources with a limit added may occasionally produce one item more than the limit on some runs. This behavior is not deterministic.
4. Calling add limit on a resource will replace any previously set limits settings.
5. For time-limited resources, the timer starts when the first item is processed. When resources are processed sequentially (FIFO mode), each resource's time limit applies also sequentially. In the default round robin mode, the time limits will usually run concurrently.

tip

If you are parameterizing the value of `add_limit` and sometimes need it to be disabled, you can set `None` or `-1` to disable the limiting.
You can also set the limit to `0` for the resource to not yield any items.

### Set table name and adjust schema [​](https://dlthub.com/docs/general-usage/resource\#set-table-name-and-adjust-schema "Direct link to Set table name and adjust schema")

You can change the schema of a resource, whether it is standalone or part of a source. Look for a method named `apply_hints` which takes the same arguments as the resource decorator. Obviously, you should call this method before data is extracted from the resource. The example below converts an `append` resource loading the `users` table into a [merge](https://dlthub.com/docs/general-usage/merge-loading) resource that will keep just one updated record per `user_id`. It also adds ["last value" incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor) on the `created_at` column to prevent requesting again the already loaded records:

```codeBlockLines_RjmQ
tables = sql_database()
tables.users.apply_hints(
    write_disposition="merge",
    primary_key="user_id",
    incremental=dlt.sources.incremental("updated_at")
)
pipeline.run(tables)

```

To change the name of a table to which the resource will load data, do the following:

```codeBlockLines_RjmQ
tables = sql_database()
tables.users.table_name = "other_users"

```

### Adjust schema when you yield data [​](https://dlthub.com/docs/general-usage/resource\#adjust-schema-when-you-yield-data "Direct link to Adjust schema when you yield data")

You can set or update the table name, columns, and other schema elements when your resource is executed, and you already yield data. Such changes will be merged with the existing schema in the same way the `apply_hints` method above works. There are many reasons to adjust the schema at runtime. For example, when using Airflow, you should avoid lengthy operations (i.e., reflecting database tables) during the creation of the DAG, so it is better to do it when the DAG executes. You may also emit partial hints (i.e., precision and scale for decimal types) for columns to help `dlt` type inference.

```codeBlockLines_RjmQ
@dlt.resource
def sql_table(credentials, schema, table):
    # Create a SQL Alchemy engine
    engine = engine_from_credentials(credentials)
    engine.execution_options(stream_results=True)
    metadata = MetaData(schema=schema)
    # Reflect the table schema
    table_obj = Table(table, metadata, autoload_with=engine)

    for idx, batch in enumerate(table_rows(engine, table_obj)):
      if idx == 0:
        # Emit the first row with hints, table_to_columns and _get_primary_key are helpers that extract dlt schema from
        # SqlAlchemy model
        yield dlt.mark.with_hints(
            batch,
            dlt.mark.make_hints(columns=table_to_columns(table_obj), primary_key=_get_primary_key(table_obj)),
        )
      else:
        # Just yield all the other rows
        yield batch

```

In the example above, we use `dlt.mark.with_hints` and `dlt.mark.make_hints` to emit columns and primary key with the first extracted item. The table schema will be adjusted after the `batch` is processed in the extract pipeline but before any schema contracts are applied, and data is persisted in the load package.

tip

You can emit columns as a Pydantic model and use dynamic hints (i.e., lambda for table name) as well. You should avoid redefining `Incremental` this way.

### Import external files [​](https://dlthub.com/docs/general-usage/resource\#import-external-files "Direct link to Import external files")

You can import external files, i.e., CSV, Parquet, and JSONL, by yielding items marked with `with_file_import`, optionally passing a table schema corresponding to the imported file. dlt will not read, parse, or normalize any names (i.e., CSV or Arrow headers) and will attempt to copy the file into the destination as is.

```codeBlockLines_RjmQ
import os
import dlt
from dlt.sources.filesystem import filesystem

columns: List[TColumnSchema] = [\
    {"name": "id", "data_type": "bigint"},\
    {"name": "name", "data_type": "text"},\
    {"name": "description", "data_type": "text"},\
    {"name": "ordered_at", "data_type": "date"},\
    {"name": "price", "data_type": "decimal"},\
]

import_folder = "/tmp/import"

@dlt.transformer(columns=columns)
def orders(items: Iterator[FileItemDict]):
  for item in items:
    # copy the file locally
      dest_file = os.path.join(import_folder, item["file_name"])
      # download the file
      item.fsspec.download(item["file_url"], dest_file)
      # tell dlt to import the dest_file as `csv`
      yield dlt.mark.with_file_import(dest_file, "csv")

# use the filesystem core source to glob a bucket

downloader = filesystem(
  bucket_url="s3://my_bucket/csv",
  file_glob="today/*.csv.gz") | orders

info = pipeline.run(orders, destination="snowflake")

```

In the example above, we glob all zipped csv files present on **my\_bucket/csv/today** (using the `filesystem` verified source) and send file descriptors to the `orders` transformer. The transformer downloads and imports the files into the extract package. At the end, `dlt` sends them to Snowflake (the table will be created because we use `column` hints to define the schema).

If imported `csv` files are not in `dlt` [default format](https://dlthub.com/docs/dlt-ecosystem/file-formats/csv#default-settings), you may need to pass additional configuration.

```codeBlockLines_RjmQ
[destination.snowflake.csv_format]
delimiter="|"
include_header=false
on_error_continue=true

```

You can sniff the schema from the data, i.e., using DuckDB to infer the table schema from a CSV file. `dlt.mark.with_file_import` accepts additional arguments that you can use to pass hints at runtime.

note

- If you do not define any columns, the table will not be created in the destination. `dlt` will still attempt to load data into it, so if you create a fitting table upfront, the load process will succeed.
- Files are imported using hard links if possible to avoid copying and duplicating the storage space needed.

### Duplicate and rename resources [​](https://dlthub.com/docs/general-usage/resource\#duplicate-and-rename-resources "Direct link to Duplicate and rename resources")

There are cases when your resources are generic (i.e., bucket filesystem) and you want to load several instances of it (i.e., files from different folders) into separate tables. In the example below, we use the `filesystem` source to load csvs from two different folders into separate tables:

```codeBlockLines_RjmQ
@dlt.resource(standalone=True)
def fs_resource(bucket_url):
  # list and yield files in bucket_url
  ...

@dlt.transformer
def csv_reader(file_item):
  # load csv, parse, and yield rows in file_item
  ...

# create two extract pipes that list files from the bucket and send them to the reader.
# by default, both pipes will load data to the same table (csv_reader)
reports_pipe = fs_resource("s3://my-bucket/reports") | csv_reader()
transactions_pipe = fs_resource("s3://my-bucket/transactions") | csv_reader()

# so we rename resources to load to "reports" and "transactions" tables
pipeline.run(
  [reports_pipe.with_name("reports"), transactions_pipe.with_name("transactions")]
)

```

The `with_name` method returns a deep copy of the original resource, its data pipe, and the data pipes of a parent resource. A renamed clone is fully separated from the original resource (and other clones) when loading: it maintains a separate [resource state](https://dlthub.com/docs/general-usage/state#read-and-write-pipeline-state-in-a-resource) and will load to a table.

## Load resources [​](https://dlthub.com/docs/general-usage/resource\#load-resources "Direct link to Load resources")

You can pass individual resources or a list of resources to the `dlt.pipeline` object. The resources loaded outside the source context will be added to the [default schema](https://dlthub.com/docs/general-usage/schema) of the pipeline.

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

pipeline = dlt.pipeline(
    pipeline_name="rows_pipeline",
    destination="duckdb",
    dataset_name="rows_data"
)
# load an individual resource
pipeline.run(generate_rows(10))
# load a list of resources
pipeline.run([generate_rows(10), generate_rows(20)])

```

### Pick loader file format for a particular resource [​](https://dlthub.com/docs/general-usage/resource\#pick-loader-file-format-for-a-particular-resource "Direct link to Pick loader file format for a particular resource")

You can request a particular loader file format to be used for a resource.

```codeBlockLines_RjmQ
@dlt.resource(file_format="parquet")
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

```

The resource above will be saved and loaded from a Parquet file (if the destination supports it).

note

A special `file_format`: **preferred** will load the resource using a format that is preferred by a destination. This setting supersedes the `loader_file_format` passed to the `run` method.

### Do a full refresh [​](https://dlthub.com/docs/general-usage/resource\#do-a-full-refresh "Direct link to Do a full refresh")

To do a full refresh of an `append` or `merge` resource, you set the `refresh` argument on the `run` method to `drop_data`. This will truncate the tables without dropping them.

```codeBlockLines_RjmQ
p.run(merge_source(), refresh="drop_data")

```

You can also [fully drop the tables](https://dlthub.com/docs/general-usage/pipeline#refresh-pipeline-data-and-state) in the `merge_source`:

```codeBlockLines_RjmQ
p.run(merge_source(), refresh="drop_sources")

```

- [Declare a resource](https://dlthub.com/docs/general-usage/resource#declare-a-resource)
  - [Define schema](https://dlthub.com/docs/general-usage/resource#define-schema)
  - [Put a contract on tables, columns, and data](https://dlthub.com/docs/general-usage/resource#put-a-contract-on-tables-columns-and-data)
  - [Define schema of nested tables](https://dlthub.com/docs/general-usage/resource#define-schema-of-nested-tables)
  - [Define a schema with Pydantic](https://dlthub.com/docs/general-usage/resource#define-a-schema-with-pydantic)
  - [Dispatch data to many tables](https://dlthub.com/docs/general-usage/resource#dispatch-data-to-many-tables)
  - [Parametrize a resource](https://dlthub.com/docs/general-usage/resource#parametrize-a-resource)
  - [Process resources with `dlt.transformer`](https://dlthub.com/docs/general-usage/resource#process-resources-with-dlttransformer)
  - [Declare a standalone resource](https://dlthub.com/docs/general-usage/resource#declare-a-standalone-resource)
  - [Declare parallel and async resources](https://dlthub.com/docs/general-usage/resource#declare-parallel-and-async-resources)
- [Customize resources](https://dlthub.com/docs/general-usage/resource#customize-resources)
  - [Filter, transform, and pivot data](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data)
  - [Reduce the nesting level of generated tables](https://dlthub.com/docs/general-usage/resource#reduce-the-nesting-level-of-generated-tables)
  - [Sample from large data](https://dlthub.com/docs/general-usage/resource#sample-from-large-data)
  - [Set table name and adjust schema](https://dlthub.com/docs/general-usage/resource#set-table-name-and-adjust-schema)
  - [Adjust schema when you yield data](https://dlthub.com/docs/general-usage/resource#adjust-schema-when-you-yield-data)
  - [Import external files](https://dlthub.com/docs/general-usage/resource#import-external-files)
  - [Duplicate and rename resources](https://dlthub.com/docs/general-usage/resource#duplicate-and-rename-resources)
- [Load resources](https://dlthub.com/docs/general-usage/resource#load-resources)
  - [Pick loader file format for a particular resource](https://dlthub.com/docs/general-usage/resource#pick-loader-file-format-for-a-particular-resource)
  - [Do a full refresh](https://dlthub.com/docs/general-usage/resource#do-a-full-refresh)

----- https://dlthub.com/docs/general-usage/resource#sample-from-large-data -----

Version: 1.11.0 (latest)

On this page

## Declare a resource [​](https://dlthub.com/docs/general-usage/resource\#declare-a-resource "Direct link to Declare a resource")

A [resource](https://dlthub.com/docs/general-usage/glossary#resource) is an ( [optionally async](https://dlthub.com/docs/reference/performance#parallelism-within-a-pipeline)) function that yields data. To create a resource, we add the `@dlt.resource` decorator to that function.

Commonly used arguments:

- `name`: The name of the table generated by this resource. Defaults to the decorated function name.
- `write_disposition`: How should the data be loaded at the destination? Currently supported: `append`,
`replace`, and `merge`. Defaults to `append.`

Example:

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows():
	for i in range(10):
		yield {'id': i, 'example_string': 'abc'}

@dlt.source
def source_name():
    return generate_rows

```

To get the data of a resource, we could do:

```codeBlockLines_RjmQ
for row in generate_rows():
    print(row)

for row in source_name().resources.get('table_name'):
    print(row)

```

Typically, resources are declared and grouped with related resources within a [source](https://dlthub.com/docs/general-usage/source) function.

### Define schema [​](https://dlthub.com/docs/general-usage/resource\#define-schema "Direct link to Define schema")

`dlt` will infer the [schema](https://dlthub.com/docs/general-usage/schema) for tables associated with resources from the resource's data.
You can modify the generation process by using the table and column hints. The resource decorator accepts the following arguments:

1. `table_name`: the name of the table, if different from the resource name.
2. `primary_key` and `merge_key`: define the name of the columns (compound keys are allowed) that will receive those hints. Used in [incremental loading](https://dlthub.com/docs/general-usage/incremental-loading) and [merge loading](https://dlthub.com/docs/general-usage/merge-loading).
3. `columns`: lets you define one or more columns, including the data types, nullability, and other hints. The column definition is a `TypedDict`: `TTableSchemaColumns`. In the example below, we tell `dlt` that the column `tags` (containing a list of tags) in the `user` table should have type `json`, which means that it will be loaded as JSON/struct and not as a separate nested table.

```codeBlockLines_RjmQ
@dlt.resource(name="user", columns={"tags": {"data_type": "json"}})
def get_users():
  ...

# the `table_schema` method gets the table schema generated by a resource
print(get_users().compute_table_schema())

```

note

You can pass dynamic hints which are functions that take the data item as input and return a hint value. This lets you create table and column schemas depending on the data. See an [example below](https://dlthub.com/docs/general-usage/resource#adjust-schema-when-you-yield-data).

### Put a contract on tables, columns, and data [​](https://dlthub.com/docs/general-usage/resource\#put-a-contract-on-tables-columns-and-data "Direct link to Put a contract on tables, columns, and data")

Use the `schema_contract` argument to tell dlt how to [deal with new tables, data types, and bad data types](https://dlthub.com/docs/general-usage/schema-contracts). For example, if you set it to **freeze**, `dlt` will not allow for any new tables, columns, or data types to be introduced to the schema - it will raise an exception. Learn more about available contract modes [here](https://dlthub.com/docs/general-usage/schema-contracts#setting-up-the-contract).

### Define schema of nested tables [​](https://dlthub.com/docs/general-usage/resource\#define-schema-of-nested-tables "Direct link to Define schema of nested tables")

`dlt` creates [nested tables](https://dlthub.com/docs/general-usage/schema#nested-references-root-and-nested-tables) to store [list of objects](https://dlthub.com/docs/general-usage/destination-tables#nested-tables) if present in your data.
You can define the schema of such tables with `nested_hints` argument to `@dlt.resource`:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(
    nested_hints={
        "purchases": dlt.mark.make_nested_hints(
            columns=[{"name": "price", "data_type": "decimal"}],
            schema_contract={"columns": "freeze"},
        )
    },
)
def customers():
    """Load customer data from a simple python list."""
    yield [\
        {\
            "id": 1,\
            "name": "simon",\
            "city": "berlin",\
            "purchases": [{"id": 1, "name": "apple", "price": "1.50"}],\
        },\
    ]

```

Here we convert the `price` field in list of `purchases` to decimal type and set the schema contract to lock the list
of columns in it. We use convenience function `dlt.mark.make_nested_hints` to generate nested hints dictionary. You are
free to use it directly.

Mind that `purchases` list will be stored as table with name `customers__purchases`. When declaring nested hints you just need
to specify nested field(s) name(s). In case of deeper nesting ie. let's say each `purchase` has a list of `coupons` applied,
you can apply hints to coupons and define `customers__purchases__coupons` table schema:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(
    nested_hints={
        "purchases": {},
        ("purchases", "coupons"): {
            "columns": {"registered_at": {"data_type": "timestamp"}}
        }
    },
)
def customers():
    ...

```

Here we use `("purchases", "coupons")` to locate list at the depth of 2 and set the data type on `registered_at` column
to `timestamp`. We do that by directly using nested hints dict.
Note that we specified `purchases` with an empty list of hints. **You are required to specify all parent hints, even if they**
**are empty. Currently we are not adding missing path elements automatically**.

You can use `nested_hints` primarily to set column hints and schema contract, those work exactly as in case of root tables.

- `file_format` has no effect (not implemented yet)
- `write_disposition` works as expected but leads to unintended consequences (ie. you can set nested table to `replace`) while root table is `append`.
- `references` will create [table references](https://dlthub.com/docs/general-usage/schema#table-references-1) (annotations) as expected.
- `primary_key` and `merge_key`: **setting those will convert nested table into a regular table, with a separate write disposition, file format etc.** [It allows you to create custom table relationships ie. using natural primary and foreign keys present in the data.](https://dlthub.com/docs/general-usage/schema#generate-custom-linking-for-nested-tables)

tip

[REST API Source](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic) accepts `nested_hints` argument as well.

You can apply nested hints after the resource was created by using [apply\_hints](https://dlthub.com/docs/general-usage/resource#set-table-name-and-adjust-schema).

### Define a schema with Pydantic [​](https://dlthub.com/docs/general-usage/resource\#define-a-schema-with-pydantic "Direct link to Define a schema with Pydantic")

You can alternatively use a [Pydantic](https://pydantic-docs.helpmanual.io/) model to define the schema.
For example:

```codeBlockLines_RjmQ
from pydantic import BaseModel
from typing import List, Optional, Union

class Address(BaseModel):
    street: str
    city: str
    postal_code: str

class User(BaseModel):
    id: int
    name: str
    tags: List[str]
    email: Optional[str]
    address: Address
    status: Union[int, str]

@dlt.resource(name="user", columns=User)
def get_users():
    ...

```

The data types of the table columns are inferred from the types of the Pydantic fields. These use the same type conversions
as when the schema is automatically generated from the data.

Pydantic models integrate well with [schema contracts](https://dlthub.com/docs/general-usage/schema-contracts) as data validators.

Things to note:

- Fields with an `Optional` type are marked as `nullable`.
- Fields with a `Union` type are converted to the first (not `None`) type listed in the union. For example, `status: Union[int, str]` results in a `bigint` column.
- `list`, `dict`, and nested Pydantic model fields will use the `json` type, which means they'll be stored as a JSON object in the database instead of creating nested tables.

You can override this by configuring the Pydantic model:

```codeBlockLines_RjmQ
from typing import ClassVar
from dlt.common.libs.pydantic import DltConfig

class UserWithNesting(User):
  dlt_config: ClassVar[DltConfig] = {"skip_nested_types": True}

@dlt.resource(name="user", columns=UserWithNesting)
def get_users():
    ...

```

`"skip_nested_types"` omits any `dict`/ `list`/ `BaseModel` type fields from the schema, so dlt will fall back on the default
behavior of creating nested tables for these fields.

We do not support `RootModel` that validate simple types. You can add such a validator yourself, see [data filtering section](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data).

### Dispatch data to many tables [​](https://dlthub.com/docs/general-usage/resource\#dispatch-data-to-many-tables "Direct link to Dispatch data to many tables")

You can load data to many tables from a single resource. The most common case is a stream of events
of different types, each with a different data schema. To deal with this, you can use the `table_name`
argument on `dlt.resource`. You could pass the table name as a function with the data item as an
argument and the `table_name` string as a return value.

For example, a resource that loads GitHub repository events wants to send `issue`, `pull request`,
and `comment` events to separate tables. The type of the event is in the "type" field.

```codeBlockLines_RjmQ
# send item to a table with name item["type"]
@dlt.resource(table_name=lambda event: event['type'])
def repo_events() -> Iterator[TDataItems]:
    yield item

# the `table_schema` method gets the table schema generated by a resource and takes an optional
# data item to evaluate dynamic hints
print(repo_events().compute_table_schema({"type": "WatchEvent", "id": ...}))

```

In more advanced cases, you can dispatch data to different tables directly in the code of the
resource function:

```codeBlockLines_RjmQ
@dlt.resource
def repo_events() -> Iterator[TDataItems]:
    # mark the "item" to be sent to the table with the name item["type"]
    yield dlt.mark.with_table_name(item, item["type"])

```

### Parametrize a resource [​](https://dlthub.com/docs/general-usage/resource\#parametrize-a-resource "Direct link to Parametrize a resource")

You can add arguments to your resource functions like to any other. Below we parametrize our
`generate_rows` resource to generate the number of rows we request:

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

for row in generate_rows(10):
    print(row)

for row in generate_rows(20):
    print(row)

```

tip

You can mark some resource arguments as [configuration and credentials](https://dlthub.com/docs/general-usage/credentials) values so `dlt` can pass them automatically to your functions.

### Process resources with `dlt.transformer` [​](https://dlthub.com/docs/general-usage/resource\#process-resources-with-dlttransformer "Direct link to process-resources-with-dlttransformer")

You can feed data from one resource into another. The most common case is when you have an API that returns a list of objects (i.e., users) in one endpoint and user details in another. You can deal with this by declaring a resource that obtains a list of users and another resource that receives items from the list and downloads the profiles.

```codeBlockLines_RjmQ
@dlt.resource(write_disposition="replace")
def users(limit=None):
    for u in _get_users(limit):
        yield u

# Feed data from users as user_item below,
# all transformers must have at least one
# argument that will receive data from the parent resource
@dlt.transformer(data_from=users)
def users_details(user_item):
    for detail in _get_details(user_item["user_id"]):
        yield detail

# Just load the users_details.
# dlt figures out dependencies for you.
pipeline.run(users_details)

```

In the example above, `users_details` will receive data from the default instance of the `users` resource (with `limit` set to `None`). You can also use the **pipe \|** operator to bind resources dynamically.

```codeBlockLines_RjmQ
# You can be more explicit and use a pipe operator.
# With it, you can create dynamic pipelines where the dependencies
# are set at run time and resources are parametrized, i.e.,
# below we want to load only 100 users from the `users` endpoint.
pipeline.run(users(limit=100) | users_details)

```

tip

Transformers are allowed not only to **yield** but also to **return** values and can decorate **async** functions and [**async generators**](https://dlthub.com/docs/reference/performance#extract). Below we decorate an async function and request details on two pokemons. HTTP calls are made in parallel via the httpx library.

```codeBlockLines_RjmQ
import dlt
import httpx

@dlt.transformer
async def pokemon(id):
    async with httpx.AsyncClient() as client:
        r = await client.get(f"https://pokeapi.co/api/v2/pokemon/{id}")
        return r.json()

# Get Bulbasaur and Ivysaur (you need dlt 0.4.6 for the pipe operator working with lists).
print(list([1,2] | pokemon()))

```

### Declare a standalone resource [​](https://dlthub.com/docs/general-usage/resource\#declare-a-standalone-resource "Direct link to Declare a standalone resource")

A standalone resource is defined on a function that is top-level in a module (not an inner function) that accepts config and secrets values. Additionally, if the `standalone` flag is specified, the decorated function signature and docstring will be preserved. `dlt.resource` will just wrap the decorated function, and the user must call the wrapper to get the actual resource. Below we declare a `filesystem` resource that must be called before use.

```codeBlockLines_RjmQ
@dlt.resource(standalone=True)
def fs_resource(bucket_url=dlt.config.value):
  """List and yield files in `bucket_url`."""
  ...

# `filesystem` must be called before it is extracted or used in any other way.
pipeline.run(fs_resource("s3://my-bucket/reports"), table_name="reports")

```

Standalone may have a dynamic name that depends on the arguments passed to the decorated function. For example:

```codeBlockLines_RjmQ
@dlt.resource(standalone=True, name=lambda args: args["stream_name"])
def kinesis(stream_name: str):
    ...

kinesis_stream = kinesis("telemetry_stream")

```

`kinesis_stream` resource has a name **telemetry\_stream**.

### Declare parallel and async resources [​](https://dlthub.com/docs/general-usage/resource\#declare-parallel-and-async-resources "Direct link to Declare parallel and async resources")

You can extract multiple resources in parallel threads or with async IO.
To enable this for a sync resource, you can set the `parallelized` flag to `True` in the resource decorator:

```codeBlockLines_RjmQ
@dlt.resource(parallelized=True)
def get_users():
    for u in _get_users():
        yield u

@dlt.resource(parallelized=True)
def get_orders():
    for o in _get_orders():
        yield o

# users and orders will be iterated in parallel in two separate threads
pipeline.run([get_users(), get_orders()])

```

Async generators are automatically extracted concurrently with other resources:

```codeBlockLines_RjmQ
@dlt.resource
async def get_users():
    async for u in _get_users():  # Assuming _get_users is an async generator
        yield u

```

Please find more details in [extract performance](https://dlthub.com/docs/reference/performance#extract)

## Customize resources [​](https://dlthub.com/docs/general-usage/resource\#customize-resources "Direct link to Customize resources")

### Filter, transform, and pivot data [​](https://dlthub.com/docs/general-usage/resource\#filter-transform-and-pivot-data "Direct link to Filter, transform, and pivot data")

You can attach any number of transformations that are evaluated on an item-per-item basis to your
resource. The available transformation types:

- **map** \- transform the data item ( `resource.add_map`).
- **filter** \- filter the data item ( `resource.add_filter`).
- **yield map** \- a map that returns an iterator (so a single row may generate many rows -
`resource.add_yield_map`).

Example: We have a resource that loads a list of users from an API endpoint. We want to customize it
so:

1. We remove users with `user_id == "me"`.
2. We anonymize user data.

Here's our resource:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(write_disposition="replace")
def users():
    ...
    users = requests.get(RESOURCE_URL)
    ...
    yield users

```

Here's our script that defines transformations and loads the data:

```codeBlockLines_RjmQ
from pipedrive import users

def anonymize_user(user_data):
    user_data["user_id"] = _hash_str(user_data["user_id"])
    user_data["user_email"] = _hash_str(user_data["user_email"])
    return user_data

# add the filter and anonymize function to users resource and enumerate
for user in users().add_filter(lambda user: user["user_id"] != "me").add_map(anonymize_user):
    print(user)

```

### Reduce the nesting level of generated tables [​](https://dlthub.com/docs/general-usage/resource\#reduce-the-nesting-level-of-generated-tables "Direct link to Reduce the nesting level of generated tables")

You can limit how deep `dlt` goes when generating nested tables and flattening dicts into columns. By default, the library will descend
and generate nested tables for all nested lists, without limit.

note

`max_table_nesting` is optional so you can skip it, in this case, dlt will
use it from the source if it is specified there or fallback to the default
value which has 1000 as the maximum nesting level.

```codeBlockLines_RjmQ
import dlt

@dlt.resource(max_table_nesting=1)
def my_resource():
    yield {
        "id": 1,
        "name": "random name",
        "properties": [\
            {\
                "name": "customer_age",\
                "type": "int",\
                "label": "Age",\
                "notes": [\
                    {\
                        "text": "string",\
                        "author": "string",\
                    }\
                ]\
            }\
        ]
    }

```

In the example above, we want only 1 level of nested tables to be generated (so there are no nested
tables of a nested table). Typical settings:

- `max_table_nesting=0` will not generate nested tables and will not flatten dicts into columns at all. All nested data will be
represented as JSON.
- `max_table_nesting=1` will generate nested tables of root tables and nothing more. All nested
data in nested tables will be represented as JSON.

You can achieve the same effect after the resource instance is created:

```codeBlockLines_RjmQ
resource = my_resource()
resource.max_table_nesting = 0

```

Several data sources are prone to contain semi-structured documents with very deep nesting, i.e.,
MongoDB databases. Our practical experience is that setting the `max_nesting_level` to 2 or 3
produces the clearest and human-readable schemas.

### Sample from large data [​](https://dlthub.com/docs/general-usage/resource\#sample-from-large-data "Direct link to Sample from large data")

If your resource loads thousands of pages of data from a REST API or millions of rows from a database table, you may want to sample just a fragment of it in order to quickly see the dataset with example data and test your transformations, etc. To do this, you limit how many items will be yielded by a resource (or source) by calling the `add_limit` method. This method will close the generator that produces the data after the limit is reached.

In the example below, we load just the first 10 items from an infinite counter - that would otherwise never end.

```codeBlockLines_RjmQ
r = dlt.resource(itertools.count(), name="infinity").add_limit(10)
assert list(r) == list(range(10))

```

note

Note that `add_limit` **does not limit the number of records** but rather the "number of yields". Depending on how your resource is set up, the number of extracted rows may vary. For example, consider this resource:

```codeBlockLines_RjmQ
@dlt.resource
def my_resource():
    for i in range(100):
        yield [{"record_id": j} for j in range(15)]

dlt.pipeline(destination="duckdb").run(my_resource().add_limit(10))

```

The code above will extract `15*10=150` records. This is happening because in each iteration, 15 records are yielded, and we're limiting the number of iterations to 10.

Altenatively you can also apply a time limit to the resource. The code below will run the extraction for 10 seconds and extract how ever many items are yielded in that time. In combination with incrementals, this can be useful for batched loading or for loading on machines that have a run time limit.

```codeBlockLines_RjmQ
dlt.pipeline(destination="duckdb").run(my_resource().add_limit(max_time=10))

```

You can also apply a combination of both limits. In this case the extraction will stop as soon as either limit is reached.

```codeBlockLines_RjmQ
dlt.pipeline(destination="duckdb").run(my_resource().add_limit(max_items=10, max_time=10))

```

Some notes about the `add_limit`:

1. `add_limit` does not skip any items. It closes the iterator/generator that produces data after the limit is reached.
2. You cannot limit transformers. They should process all the data they receive fully to avoid inconsistencies in generated datasets.
3. Async resources with a limit added may occasionally produce one item more than the limit on some runs. This behavior is not deterministic.
4. Calling add limit on a resource will replace any previously set limits settings.
5. For time-limited resources, the timer starts when the first item is processed. When resources are processed sequentially (FIFO mode), each resource's time limit applies also sequentially. In the default round robin mode, the time limits will usually run concurrently.

tip

If you are parameterizing the value of `add_limit` and sometimes need it to be disabled, you can set `None` or `-1` to disable the limiting.
You can also set the limit to `0` for the resource to not yield any items.

### Set table name and adjust schema [​](https://dlthub.com/docs/general-usage/resource\#set-table-name-and-adjust-schema "Direct link to Set table name and adjust schema")

You can change the schema of a resource, whether it is standalone or part of a source. Look for a method named `apply_hints` which takes the same arguments as the resource decorator. Obviously, you should call this method before data is extracted from the resource. The example below converts an `append` resource loading the `users` table into a [merge](https://dlthub.com/docs/general-usage/merge-loading) resource that will keep just one updated record per `user_id`. It also adds ["last value" incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor) on the `created_at` column to prevent requesting again the already loaded records:

```codeBlockLines_RjmQ
tables = sql_database()
tables.users.apply_hints(
    write_disposition="merge",
    primary_key="user_id",
    incremental=dlt.sources.incremental("updated_at")
)
pipeline.run(tables)

```

To change the name of a table to which the resource will load data, do the following:

```codeBlockLines_RjmQ
tables = sql_database()
tables.users.table_name = "other_users"

```

### Adjust schema when you yield data [​](https://dlthub.com/docs/general-usage/resource\#adjust-schema-when-you-yield-data "Direct link to Adjust schema when you yield data")

You can set or update the table name, columns, and other schema elements when your resource is executed, and you already yield data. Such changes will be merged with the existing schema in the same way the `apply_hints` method above works. There are many reasons to adjust the schema at runtime. For example, when using Airflow, you should avoid lengthy operations (i.e., reflecting database tables) during the creation of the DAG, so it is better to do it when the DAG executes. You may also emit partial hints (i.e., precision and scale for decimal types) for columns to help `dlt` type inference.

```codeBlockLines_RjmQ
@dlt.resource
def sql_table(credentials, schema, table):
    # Create a SQL Alchemy engine
    engine = engine_from_credentials(credentials)
    engine.execution_options(stream_results=True)
    metadata = MetaData(schema=schema)
    # Reflect the table schema
    table_obj = Table(table, metadata, autoload_with=engine)

    for idx, batch in enumerate(table_rows(engine, table_obj)):
      if idx == 0:
        # Emit the first row with hints, table_to_columns and _get_primary_key are helpers that extract dlt schema from
        # SqlAlchemy model
        yield dlt.mark.with_hints(
            batch,
            dlt.mark.make_hints(columns=table_to_columns(table_obj), primary_key=_get_primary_key(table_obj)),
        )
      else:
        # Just yield all the other rows
        yield batch

```

In the example above, we use `dlt.mark.with_hints` and `dlt.mark.make_hints` to emit columns and primary key with the first extracted item. The table schema will be adjusted after the `batch` is processed in the extract pipeline but before any schema contracts are applied, and data is persisted in the load package.

tip

You can emit columns as a Pydantic model and use dynamic hints (i.e., lambda for table name) as well. You should avoid redefining `Incremental` this way.

### Import external files [​](https://dlthub.com/docs/general-usage/resource\#import-external-files "Direct link to Import external files")

You can import external files, i.e., CSV, Parquet, and JSONL, by yielding items marked with `with_file_import`, optionally passing a table schema corresponding to the imported file. dlt will not read, parse, or normalize any names (i.e., CSV or Arrow headers) and will attempt to copy the file into the destination as is.

```codeBlockLines_RjmQ
import os
import dlt
from dlt.sources.filesystem import filesystem

columns: List[TColumnSchema] = [\
    {"name": "id", "data_type": "bigint"},\
    {"name": "name", "data_type": "text"},\
    {"name": "description", "data_type": "text"},\
    {"name": "ordered_at", "data_type": "date"},\
    {"name": "price", "data_type": "decimal"},\
]

import_folder = "/tmp/import"

@dlt.transformer(columns=columns)
def orders(items: Iterator[FileItemDict]):
  for item in items:
    # copy the file locally
      dest_file = os.path.join(import_folder, item["file_name"])
      # download the file
      item.fsspec.download(item["file_url"], dest_file)
      # tell dlt to import the dest_file as `csv`
      yield dlt.mark.with_file_import(dest_file, "csv")

# use the filesystem core source to glob a bucket

downloader = filesystem(
  bucket_url="s3://my_bucket/csv",
  file_glob="today/*.csv.gz") | orders

info = pipeline.run(orders, destination="snowflake")

```

In the example above, we glob all zipped csv files present on **my\_bucket/csv/today** (using the `filesystem` verified source) and send file descriptors to the `orders` transformer. The transformer downloads and imports the files into the extract package. At the end, `dlt` sends them to Snowflake (the table will be created because we use `column` hints to define the schema).

If imported `csv` files are not in `dlt` [default format](https://dlthub.com/docs/dlt-ecosystem/file-formats/csv#default-settings), you may need to pass additional configuration.

```codeBlockLines_RjmQ
[destination.snowflake.csv_format]
delimiter="|"
include_header=false
on_error_continue=true

```

You can sniff the schema from the data, i.e., using DuckDB to infer the table schema from a CSV file. `dlt.mark.with_file_import` accepts additional arguments that you can use to pass hints at runtime.

note

- If you do not define any columns, the table will not be created in the destination. `dlt` will still attempt to load data into it, so if you create a fitting table upfront, the load process will succeed.
- Files are imported using hard links if possible to avoid copying and duplicating the storage space needed.

### Duplicate and rename resources [​](https://dlthub.com/docs/general-usage/resource\#duplicate-and-rename-resources "Direct link to Duplicate and rename resources")

There are cases when your resources are generic (i.e., bucket filesystem) and you want to load several instances of it (i.e., files from different folders) into separate tables. In the example below, we use the `filesystem` source to load csvs from two different folders into separate tables:

```codeBlockLines_RjmQ
@dlt.resource(standalone=True)
def fs_resource(bucket_url):
  # list and yield files in bucket_url
  ...

@dlt.transformer
def csv_reader(file_item):
  # load csv, parse, and yield rows in file_item
  ...

# create two extract pipes that list files from the bucket and send them to the reader.
# by default, both pipes will load data to the same table (csv_reader)
reports_pipe = fs_resource("s3://my-bucket/reports") | csv_reader()
transactions_pipe = fs_resource("s3://my-bucket/transactions") | csv_reader()

# so we rename resources to load to "reports" and "transactions" tables
pipeline.run(
  [reports_pipe.with_name("reports"), transactions_pipe.with_name("transactions")]
)

```

The `with_name` method returns a deep copy of the original resource, its data pipe, and the data pipes of a parent resource. A renamed clone is fully separated from the original resource (and other clones) when loading: it maintains a separate [resource state](https://dlthub.com/docs/general-usage/state#read-and-write-pipeline-state-in-a-resource) and will load to a table.

## Load resources [​](https://dlthub.com/docs/general-usage/resource\#load-resources "Direct link to Load resources")

You can pass individual resources or a list of resources to the `dlt.pipeline` object. The resources loaded outside the source context will be added to the [default schema](https://dlthub.com/docs/general-usage/schema) of the pipeline.

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

pipeline = dlt.pipeline(
    pipeline_name="rows_pipeline",
    destination="duckdb",
    dataset_name="rows_data"
)
# load an individual resource
pipeline.run(generate_rows(10))
# load a list of resources
pipeline.run([generate_rows(10), generate_rows(20)])

```

### Pick loader file format for a particular resource [​](https://dlthub.com/docs/general-usage/resource\#pick-loader-file-format-for-a-particular-resource "Direct link to Pick loader file format for a particular resource")

You can request a particular loader file format to be used for a resource.

```codeBlockLines_RjmQ
@dlt.resource(file_format="parquet")
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

```

The resource above will be saved and loaded from a Parquet file (if the destination supports it).

note

A special `file_format`: **preferred** will load the resource using a format that is preferred by a destination. This setting supersedes the `loader_file_format` passed to the `run` method.

### Do a full refresh [​](https://dlthub.com/docs/general-usage/resource\#do-a-full-refresh "Direct link to Do a full refresh")

To do a full refresh of an `append` or `merge` resource, you set the `refresh` argument on the `run` method to `drop_data`. This will truncate the tables without dropping them.

```codeBlockLines_RjmQ
p.run(merge_source(), refresh="drop_data")

```

You can also [fully drop the tables](https://dlthub.com/docs/general-usage/pipeline#refresh-pipeline-data-and-state) in the `merge_source`:

```codeBlockLines_RjmQ
p.run(merge_source(), refresh="drop_sources")

```

- [Declare a resource](https://dlthub.com/docs/general-usage/resource#declare-a-resource)
  - [Define schema](https://dlthub.com/docs/general-usage/resource#define-schema)
  - [Put a contract on tables, columns, and data](https://dlthub.com/docs/general-usage/resource#put-a-contract-on-tables-columns-and-data)
  - [Define schema of nested tables](https://dlthub.com/docs/general-usage/resource#define-schema-of-nested-tables)
  - [Define a schema with Pydantic](https://dlthub.com/docs/general-usage/resource#define-a-schema-with-pydantic)
  - [Dispatch data to many tables](https://dlthub.com/docs/general-usage/resource#dispatch-data-to-many-tables)
  - [Parametrize a resource](https://dlthub.com/docs/general-usage/resource#parametrize-a-resource)
  - [Process resources with `dlt.transformer`](https://dlthub.com/docs/general-usage/resource#process-resources-with-dlttransformer)
  - [Declare a standalone resource](https://dlthub.com/docs/general-usage/resource#declare-a-standalone-resource)
  - [Declare parallel and async resources](https://dlthub.com/docs/general-usage/resource#declare-parallel-and-async-resources)
- [Customize resources](https://dlthub.com/docs/general-usage/resource#customize-resources)
  - [Filter, transform, and pivot data](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data)
  - [Reduce the nesting level of generated tables](https://dlthub.com/docs/general-usage/resource#reduce-the-nesting-level-of-generated-tables)
  - [Sample from large data](https://dlthub.com/docs/general-usage/resource#sample-from-large-data)
  - [Set table name and adjust schema](https://dlthub.com/docs/general-usage/resource#set-table-name-and-adjust-schema)
  - [Adjust schema when you yield data](https://dlthub.com/docs/general-usage/resource#adjust-schema-when-you-yield-data)
  - [Import external files](https://dlthub.com/docs/general-usage/resource#import-external-files)
  - [Duplicate and rename resources](https://dlthub.com/docs/general-usage/resource#duplicate-and-rename-resources)
- [Load resources](https://dlthub.com/docs/general-usage/resource#load-resources)
  - [Pick loader file format for a particular resource](https://dlthub.com/docs/general-usage/resource#pick-loader-file-format-for-a-particular-resource)
  - [Do a full refresh](https://dlthub.com/docs/general-usage/resource#do-a-full-refresh)

----- https://dlthub.com/docs/general-usage/source#reduce-the-nesting-level-of-generated-tables -----

Version: 1.11.0 (latest)

On this page

A [source](https://dlthub.com/docs/general-usage/glossary#source) is a logical grouping of resources, i.e., endpoints of a
single API. The most common approach is to define it in a separate Python module.

- A source is a function decorated with `@dlt.source` that returns one or more resources.
- A source can optionally define a [schema](https://dlthub.com/docs/general-usage/schema) with tables, columns, performance hints, and
more.
- The source Python module typically contains optional customizations and data transformations.
- The source Python module typically contains the authentication and pagination code for a particular
API.

## Declare sources [​](https://dlthub.com/docs/general-usage/source\#declare-sources "Direct link to Declare sources")

You declare a source by decorating an (optionally async) function that returns or yields one or more resources with `@dlt.source`. Our
[Create a pipeline](https://dlthub.com/docs/walkthroughs/create-a-pipeline) how-to guide teaches you how to do that.

### Create resources dynamically [​](https://dlthub.com/docs/general-usage/source\#create-resources-dynamically "Direct link to Create resources dynamically")

You can create resources by using `dlt.resource` as a function. In the example below, we reuse a
single generator function to create a list of resources for several Hubspot endpoints.

```codeBlockLines_RjmQ
@dlt.source
def hubspot(api_key=dlt.secrets.value):

    endpoints = ["companies", "deals", "products"]

    def get_resource(endpoint):
        yield requests.get(url + "/" + endpoint).json()

    for endpoint in endpoints:
        # calling get_resource creates a generator,
        # the actual code of the function will be executed in pipeline.run
        yield dlt.resource(get_resource(endpoint), name=endpoint)

```

### Attach and configure schemas [​](https://dlthub.com/docs/general-usage/source\#attach-and-configure-schemas "Direct link to Attach and configure schemas")

You can [create, attach, and configure schemas](https://dlthub.com/docs/general-usage/schema#attaching-schemas-to-sources) that will be
used when loading the source.

### Avoid long-lasting operations in source function [​](https://dlthub.com/docs/general-usage/source\#avoid-long-lasting-operations-in-source-function "Direct link to Avoid long-lasting operations in source function")

Do not extract data in the source function. Leave that task to your resources if possible. The source function is executed immediately when called (contrary to resources which delay execution - like Python generators). There are several benefits (error handling, execution metrics, parallelization) you get when you extract data in `pipeline.run` or `pipeline.extract`.

If this is impractical (for example, you want to reflect a database to create resources for tables), make sure you do not call the source function too often. [See this note if you plan to deploy on Airflow](https://dlthub.com/docs/walkthroughs/deploy-a-pipeline/deploy-with-airflow-composer#2-modify-dag-file)

## Customize sources [​](https://dlthub.com/docs/general-usage/source\#customize-sources "Direct link to Customize sources")

### Access and select resources to load [​](https://dlthub.com/docs/general-usage/source\#access-and-select-resources-to-load "Direct link to Access and select resources to load")

You can access resources present in a source and select which of them you want to load. In the case of
the `hubspot` resource above, we could select and load the "companies", "deals", and "products" resources:

```codeBlockLines_RjmQ
from hubspot import hubspot

source = hubspot()
# "resources" is a dictionary with all resources available, the key is the resource name
print(source.resources.keys())  # print names of all resources
# print resources that are selected to load
print(source.resources.selected.keys())
# load only "companies" and "deals" using the "with_resources" convenience method
pipeline.run(source.with_resources("companies", "deals"))

```

Resources can be individually accessed and selected:

```codeBlockLines_RjmQ
# resources are accessible as attributes of a source
for c in source.companies:  # enumerate all data in the companies resource
    print(c)

# check if deals are selected to load
print(source.deals.selected)
# deselect the deals
source.deals.selected = False

```

### Filter, transform, and pivot data [​](https://dlthub.com/docs/general-usage/source\#filter-transform-and-pivot-data "Direct link to Filter, transform, and pivot data")

You can modify and filter data in resources, for example, if we want to keep only deals after a certain
date:

```codeBlockLines_RjmQ
source.deals.add_filter(lambda deal: deal["created_at"] > yesterday)

```

Find more on transforms [here](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data).

### Load data partially [​](https://dlthub.com/docs/general-usage/source\#load-data-partially "Direct link to Load data partially")

You can limit the number of items produced by each resource by calling the `add_limit` method on a source. This is useful for testing, debugging, and generating sample datasets for experimentation. You can easily get your test dataset in a few minutes, when otherwise you'd need to wait hours for the full loading to complete. Below, we limit the `pipedrive` source to just get **10 pages** of data from each endpoint. Mind that the transformers will be evaluated fully:

```codeBlockLines_RjmQ
from pipedrive import pipedrive_source

pipeline = dlt.pipeline(pipeline_name='pipedrive', destination='duckdb', dataset_name='pipedrive_data')
load_info = pipeline.run(pipedrive_source().add_limit(10))
print(load_info)

```

You can also apply a time limit to the source:

```codeBlockLines_RjmQ
pipeline.run(pipedrive_source().add_limit(max_time=10))

```

Or limit by both, the limit that is reached first will stop the extraction:

```codeBlockLines_RjmQ
pipeline.run(pipedrive_source().add_limit(max_items=10, max_time=10))

```

note

Note that `add_limit` **does not limit the number of records** but rather the "number of yields". dlt will close the iterator/generator that produces data after the limit is reached. Please read in more detail about the `add_limit` on the resource page.

Find more on sampling data [here](https://dlthub.com/docs/general-usage/resource#sample-from-large-data).

### Rename the source [​](https://dlthub.com/docs/general-usage/source\#rename-the-source "Direct link to Rename the source")

dlt allows you to rename the source i.e. to place the source configuration into custom section or to have many instances
of the source created side by side. For example:

```codeBlockLines_RjmQ
from dlt.sources.sql_database import sql_database

my_db = sql_database.clone(name="my_db", section="my_db")(table_names=["table_1"])
print(my_db.name)

```

Here we create a renamed version of the `sql_database` and then instantiate it. Such source will read credentials from:

```codeBlockLines_RjmQ
[sources.my_db.my_db.credentials]
password="..."

```

### Add more resources to existing source [​](https://dlthub.com/docs/general-usage/source\#add-more-resources-to-existing-source "Direct link to Add more resources to existing source")

You can add a custom resource to a source after it was created. Imagine that you want to score all the deals with a keras model that will tell you if the deal is a fraud or not. In order to do that, you declare a new [transformer that takes the data from](https://dlthub.com/docs/general-usage/resource#process-resources-with-dlttransformer) `deals` resource and add it to the source.

```codeBlockLines_RjmQ
import dlt
from hubspot import hubspot

# source contains `deals` resource
source = hubspot()

@dlt.transformer
def deal_scores(deal_item):
    # obtain the score, deal_items contains data yielded by source.deals
    score = model.predict(featurize(deal_item))
    yield {"deal_id": deal_item, "score": score}

# connect the data from `deals` resource into `deal_scores` and add to the source
source.resources.add(source.deals | deal_scores)
# load the data: you'll see the new table `deal_scores` in your destination!
pipeline.run(source)

```

You can also set the resources in the source as follows:

```codeBlockLines_RjmQ
source.deal_scores = source.deals | deal_scores

```

or

```codeBlockLines_RjmQ
source.resources["deal_scores"] = source.deals | deal_scores

```

note

When adding a resource to the source, dlt clones the resource so your existing instance is not affected.

### Reduce the nesting level of generated tables [​](https://dlthub.com/docs/general-usage/source\#reduce-the-nesting-level-of-generated-tables "Direct link to Reduce the nesting level of generated tables")

You can limit how deep dlt goes when generating nested tables and flattening dicts into columns. By default, the library will descend and generate nested tables for all nested lists and columns from dicts, without limit.

```codeBlockLines_RjmQ
@dlt.source(max_table_nesting=1)
def mongo_db():
    ...

```

In the example above, we want only 1 level of nested tables to be generated (so there are no nested tables of a nested table). Typical settings:

- `max_table_nesting=0` will not generate nested tables and will not flatten dicts into columns at all. All nested data will be represented as JSON.
- `max_table_nesting=1` will generate nested tables of root tables and nothing more. All nested data in nested tables will be represented as JSON.

You can achieve the same effect after the source instance is created:

```codeBlockLines_RjmQ
from mongo_db import mongo_db

source = mongo_db()
source.max_table_nesting = 0

```

Several data sources are prone to contain semi-structured documents with very deep nesting, e.g., MongoDB databases. Our practical experience is that setting the `max_nesting_level` to 2 or 3 produces the clearest and human-readable schemas.

tip

The `max_table_nesting` parameter at the source level doesn't automatically apply to individual resources when accessed directly (e.g., using `source.resources["resource_1"]`). To make sure it works, either use `source.with_resources("resource_1")` or set the parameter directly on the resource.

You can directly configure the `max_table_nesting` parameter on the resource level as:

```codeBlockLines_RjmQ
@dlt.resource(max_table_nesting=0)
def my_resource():
    ...

```

or

```codeBlockLines_RjmQ
source.my_resource.max_table_nesting = 0

```

### Modify schema [​](https://dlthub.com/docs/general-usage/source\#modify-schema "Direct link to Modify schema")

The schema is available via the `schema` property of the source.
[You can manipulate this schema, i.e., add tables, change column definitions, etc., before the data is loaded.](https://dlthub.com/docs/general-usage/schema#schema-is-modified-in-the-source-function-body)

The source provides two other convenience properties:

1. `max_table_nesting` to set the maximum nesting level for nested tables and flattened columns.
2. `root_key` to propagate the `_dlt_id` from a root table to all nested tables.

## Load sources [​](https://dlthub.com/docs/general-usage/source\#load-sources "Direct link to Load sources")

You can pass individual sources or a list of sources to the `dlt.pipeline` object. By default, all the
sources will be loaded into a single dataset.

You are also free to decompose a single source into several ones. For example, you may want to break
down a 50-table copy job into an Airflow DAG with high parallelism to load the data faster. To do
so, you could get the list of resources as:

```codeBlockLines_RjmQ
# get a list of resources' names
resource_list = sql_source().resources.keys()

# now we are able to make a pipeline for each resource
for res in resource_list:
    pipeline.run(sql_source().with_resources(res))

```

### Do a full refresh [​](https://dlthub.com/docs/general-usage/source\#do-a-full-refresh "Direct link to Do a full refresh")

You can temporarily change the "write disposition" to `replace` on all (or selected) resources within
a source to force a full refresh:

```codeBlockLines_RjmQ
p.run(merge_source(), write_disposition="replace")

```

With selected resources:

```codeBlockLines_RjmQ
p.run(tables.with_resources("users"), write_disposition="replace")

```

- [Declare sources](https://dlthub.com/docs/general-usage/source#declare-sources)
  - [Create resources dynamically](https://dlthub.com/docs/general-usage/source#create-resources-dynamically)
  - [Attach and configure schemas](https://dlthub.com/docs/general-usage/source#attach-and-configure-schemas)
  - [Avoid long-lasting operations in source function](https://dlthub.com/docs/general-usage/source#avoid-long-lasting-operations-in-source-function)
- [Customize sources](https://dlthub.com/docs/general-usage/source#customize-sources)
  - [Access and select resources to load](https://dlthub.com/docs/general-usage/source#access-and-select-resources-to-load)
  - [Filter, transform, and pivot data](https://dlthub.com/docs/general-usage/source#filter-transform-and-pivot-data)
  - [Load data partially](https://dlthub.com/docs/general-usage/source#load-data-partially)
  - [Rename the source](https://dlthub.com/docs/general-usage/source#rename-the-source)
  - [Add more resources to existing source](https://dlthub.com/docs/general-usage/source#add-more-resources-to-existing-source)
  - [Reduce the nesting level of generated tables](https://dlthub.com/docs/general-usage/source#reduce-the-nesting-level-of-generated-tables)
  - [Modify schema](https://dlthub.com/docs/general-usage/source#modify-schema)
- [Load sources](https://dlthub.com/docs/general-usage/source#load-sources)
  - [Do a full refresh](https://dlthub.com/docs/general-usage/source#do-a-full-refresh)

----- https://dlthub.com/docs/general-usage/incremental-loading#choosing-a-write-disposition -----

Version: 1.11.0 (latest)

On this page

Incremental loading is the act of loading only new or changed data and not old records that we have already loaded. It enables low-latency and low-cost data transfer.

The challenge of incremental pipelines is that if we do not keep track of the state of the load (i.e., which increments were loaded and which are to be loaded), we may encounter issues. Read more about state [here](https://dlthub.com/docs/general-usage/state).

## Choosing a write disposition [​](https://dlthub.com/docs/general-usage/incremental-loading\#choosing-a-write-disposition "Direct link to Choosing a write disposition")

### The 3 write dispositions: [​](https://dlthub.com/docs/general-usage/incremental-loading\#the-3-write-dispositions "Direct link to The 3 write dispositions:")

- **Full load**: replaces the destination dataset with whatever the source produced on this run. To achieve this, use `write_disposition='replace'` in your resources. Learn more in the [full loading docs](https://dlthub.com/docs/general-usage/full-loading).

- **Append**: appends the new data to the destination. Use `write_disposition='append'`.

- **Merge**: Merges new data into the destination using `merge_key` and/or deduplicates/upserts new data using `primary_key`. Use `write_disposition='merge'`.

### How to choose the right write disposition [​](https://dlthub.com/docs/general-usage/incremental-loading\#how-to-choose-the-right-write-disposition "Direct link to How to choose the right write disposition")

![write disposition flowchart](https://storage.googleapis.com/dlt-blog-images/flowchart_for_scd2.png)

The "write disposition" you choose depends on the dataset and how you can extract it.

To find the "write disposition" you should use, the first question you should ask yourself is "Is my data stateful or stateless"? Stateful data has a state that is subject to change - for example, a user's profile. Stateless data cannot change - for example, a recorded event, such as a page view.

Because stateless data does not need to be updated, we can just append it.

For stateful data, comes a second question - Can I extract it incrementally from the source? If yes, you should use [slowly changing dimensions (Type-2)](https://dlthub.com/docs/general-usage/merge-loading#scd2-strategy), which allow you to maintain historical records of data changes over time.

If not, then we need to replace the entire dataset. However, if we can request the data incrementally, such as "all users added or modified since yesterday," then we can simply apply changes to our existing dataset with the merge write disposition.

## Incremental loading strategies [​](https://dlthub.com/docs/general-usage/incremental-loading\#incremental-loading-strategies "Direct link to Incremental loading strategies")

dlt provides several approaches to incremental loading:

1. [Merge strategies](https://dlthub.com/docs/general-usage/merge-loading#merge-strategies) \- Choose between delete-insert, SCD2, and upsert approaches to incrementally update your data
2. [Cursor-based incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor) \- Track changes using a cursor field (like timestamp or ID)
3. [Lag / Attribution window](https://dlthub.com/docs/general-usage/incremental/lag) \- Refresh data within a specific time window
4. [Advanced state management](https://dlthub.com/docs/general-usage/incremental/advanced-state) \- Custom state tracking

## Doing a full refresh [​](https://dlthub.com/docs/general-usage/incremental-loading\#doing-a-full-refresh "Direct link to Doing a full refresh")

You may force a full refresh of `merge` and `append` pipelines:

1. In the case of a `merge`, the data in the destination is deleted and loaded fresh. Currently, we do not deduplicate data during the full refresh.
2. In the case of `dlt.sources.incremental`, the data is deleted and loaded from scratch. The state of the incremental is reset to the initial value.

Example:

```codeBlockLines_RjmQ
p = dlt.pipeline(destination="bigquery", dataset_name="dataset_name")
# Do a full refresh
p.run(merge_source(), write_disposition="replace")
# Do a full refresh of just one table
p.run(merge_source().with_resources("merge_table"), write_disposition="replace")
# Run a normal merge
p.run(merge_source())

```

Passing write disposition to `replace` will change the write disposition on all the resources in
`repo_events` during the run of the pipeline.

## Next steps [​](https://dlthub.com/docs/general-usage/incremental-loading\#next-steps "Direct link to Next steps")

- [Cursor-based incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor) \- Use timestamps or IDs to track changes
- [Advanced state management](https://dlthub.com/docs/general-usage/incremental/advanced-state) \- Advanced techniques for state tracking
- [Walkthroughs: Add incremental configuration to SQL resources](https://dlthub.com/docs/walkthroughs/sql-incremental-configuration) \- Step-by-step examples
- [Troubleshooting incremental loading](https://dlthub.com/docs/general-usage/incremental/troubleshooting) \- Common issues and how to fix them

- [Choosing a write disposition](https://dlthub.com/docs/general-usage/incremental-loading#choosing-a-write-disposition)
  - [The 3 write dispositions:](https://dlthub.com/docs/general-usage/incremental-loading#the-3-write-dispositions)
  - [How to choose the right write disposition](https://dlthub.com/docs/general-usage/incremental-loading#how-to-choose-the-right-write-disposition)
- [Incremental loading strategies](https://dlthub.com/docs/general-usage/incremental-loading#incremental-loading-strategies)
- [Doing a full refresh](https://dlthub.com/docs/general-usage/incremental-loading#doing-a-full-refresh)
- [Next steps](https://dlthub.com/docs/general-usage/incremental-loading#next-steps)

----- https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads -----

Version: 1.11.0 (latest)

On this page

Need help deploying these sources or figuring out how to run them in your data stack?

[Join our Slack community](https://dlthub.com/community) or [Get in touch](https://dlthub.com/contact) with the dltHub Customer Success team.

Facebook Ads is the advertising platform that lets businesses and individuals create targeted ads on
Facebook and its affiliated apps like Instagram and Messenger.

This Facebook `dlt` verified source and
[pipeline example](https://github.com/dlt-hub/verified-sources/blob/master/sources/facebook_ads_pipeline.py)
loads data using the [Facebook Marketing API](https://developers.facebook.com/products/marketing-api/) to the destination of your choice.

The endpoints that this verified source supports are:

| Name | Description |
| --- | --- |
| campaigns | A structured marketing initiative that focuses on a specific objective or goal |
| ad\_sets | A subset or group of ads within a campaign |
| ads | An individual advertisement that is created and displayed within an ad set |
| creatives | Visual and textual elements that make up an advertisement |
| ad\_leads | Information collected from users who have interacted with lead generation ads |
| facebook\_insights | Data on audience demographics, post reach, and engagement metrics |

To get a complete list of sub-endpoints that can be loaded, see
[facebook\_ads/settings.py.](https://github.com/dlt-hub/verified-sources/blob/master/sources/facebook_ads/settings.py)

## Setup guide [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads\#setup-guide "Direct link to Setup guide")

### Grab credentials [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads\#grab-credentials "Direct link to Grab credentials")

#### Grab `Account ID` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads\#grab-account-id "Direct link to grab-account-id")

1. Ensure that you have Ads Manager active for your Facebook account.
2. Find your account ID, which is a long number. You can locate it by clicking on the Account
Overview dropdown in Ads Manager or by checking the link address. For example,
[https://adsmanager.facebook.com/adsmanager/manage/accounts?act=10150974068878324](https://adsmanager.facebook.com/adsmanager/manage/accounts?act=10150974068878324).
3. Note this account ID as it will further be used in configuring dlt.

#### Grab `Access_Token` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads\#grab-access_token "Direct link to grab-access_token")

1. Sign up for a developer account on
[developers.facebook.com](https://developers.facebook.com/).
2. Log in to your developer account and click on "My Apps" in the top right corner.
3. Create an app, select "Other" as the type, choose "Business" as the category, and click "Next".
4. Enter the name of your app and select the associated business manager account.
5. Go to the "Basic" settings in the left-hand side menu.
6. Copy the "App ID" and "App secret" and paste them as "client\_id" and "client\_secret" in the
secrets.toml file in the .dlt folder.
7. Next, obtain a short-lived access token at [https://developers.facebook.com/tools/explorer/](https://developers.facebook.com/tools/explorer/).
8. Select the created app, add "ads\_read" and "lead\_retrieval" permissions, and generate a
short-lived access token.
9. Copy the access token and update it in the `.dlt/secrets.toml` file.

#### Exchange short-lived token for a long-lived token [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads\#exchange-short-lived-token-for-a-long-lived-token "Direct link to Exchange short-lived token for a long-lived token")

By default, Facebook access tokens have a short lifespan of one hour. To exchange a short-lived Facebook access token for a long-lived token, update the `.dlt/secrets.toml` with client\_id and client\_secret, and execute the provided Python code.

```codeBlockLines_RjmQ
from facebook_ads import get_long_lived_token
print(get_long_lived_token("your short-lived token"))

```

Replace the `access_token` in the `.dlt/secrets.toml` file with the long-lived token obtained from the above code snippet.

To retrieve the expiry date and the associated scopes of the token, you can use the following command:

```codeBlockLines_RjmQ
from facebook_ads import debug_access_token
debug_access_token()

```

We highly recommend you add the token expiration timestamp to get notified a week before token expiration that you need to rotate it. Right now, the notifications are sent to the logger with error level. In `config.toml` / `secrets.toml`:

```codeBlockLines_RjmQ
[sources.facebook_ads]
access_token_expires_at=1688821881

```

> Note: The Facebook UI, which is described here, might change.
> The full guide is available at [this link.](https://developers.facebook.com/docs/marketing-apis/overview/authentication)

### Initialize the verified source [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads\#initialize-the-verified-source "Direct link to Initialize the verified source")

To get started with your data pipeline, follow these steps:

1. Enter the following command:

```codeBlockLines_RjmQ
dlt init facebook_ads duckdb

```

[This command](https://dlthub.com/docs/reference/command-line-interface) will initialize [the pipeline example](https://github.com/dlt-hub/verified-sources/blob/master/sources/facebook_ads_pipeline.py) with Facebook Ads as the [source](https://dlthub.com/docs/general-usage/source) and [duckdb](https://dlthub.com/docs/dlt-ecosystem/destinations/duckdb) as the [destination](https://dlthub.com/docs/dlt-ecosystem/destinations).

2. If you'd like to use a different destination, simply replace `duckdb` with the name of your preferred [destination](https://dlthub.com/docs/dlt-ecosystem/destinations).

3. After running this command, a new directory will be created with the necessary files and configuration settings to get started.

For more information, read the guide on [how to add a verified source](https://dlthub.com/docs/walkthroughs/add-a-verified-source).

### Add credential [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads\#add-credential "Direct link to Add credential")

1. Inside the `.dlt` folder, you'll find a file called `secrets.toml`, which is where you can securely store your access tokens and other sensitive information. It's important to handle this file with care and keep it safe. Here's what the file looks like:

```codeBlockLines_RjmQ
# put your secret values and credentials here
# do not share this file and do not push it to github
[sources.facebook_ads]
access_token="set me up!"

```

2. Replace the access\_token value with the [previously copied one](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads#grab-credentials) to ensure secure access to your Facebook Ads resources.

3. Next, follow the [destination documentation](https://dlthub.com/docs/dlt-ecosystem/destinations) instructions to add credentials for your chosen destination, ensuring proper routing of your data to the final destination.

4. It is strongly recommended to add the token expiration timestamp to your `config.toml` or `secrets.toml` file.

5. Next, store your pipeline configuration details in the `.dlt/config.toml`.

Here's what the `config.toml` looks like:

```codeBlockLines_RjmQ
[sources.facebook_ads]
account_id = "Please set me up!"

```

6. Replace the value of the "account id" with the one [copied above](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads#grab-account-id).

For more information, read the [General Usage: Credentials.](https://dlthub.com/docs/general-usage/credentials)

## Run the pipeline [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads\#run-the-pipeline "Direct link to Run the pipeline")

1. Before running the pipeline, ensure that you have installed all the necessary dependencies by
running the command:

```codeBlockLines_RjmQ
pip install -r requirements.txt

```

2. You're now ready to run the pipeline! To get started, run the following command:

```codeBlockLines_RjmQ
python facebook_ads_pipeline.py

```

3. Once the pipeline has finished running, you can verify that everything loaded correctly by using
the following command:

```codeBlockLines_RjmQ
dlt pipeline <pipeline_name> show

```

For example, the `pipeline_name` for the above pipeline example is `facebook_ads`. You may also
use any custom name instead.

For more information, read the guide on [how to run a pipeline](https://dlthub.com/docs/walkthroughs/run-a-pipeline).

## Sources and resources [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads\#sources-and-resources "Direct link to Sources and resources")

`dlt` works on the principle of [sources](https://dlthub.com/docs/general-usage/source) and
[resources](https://dlthub.com/docs/general-usage/resource).

### Default endpoints [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads\#default-endpoints "Direct link to Default endpoints")

You can write your own pipelines to load data to a destination using this verified source. However,
it is important to note the complete list of the default endpoints given in
[facebook\_ads/settings.py.](https://github.com/dlt-hub/verified-sources/blob/master/sources/facebook_ads/settings.py)

### Source `facebook_ads_source` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads\#source-facebook_ads_source "Direct link to source-facebook_ads_source")

This function returns a list of resources to load campaigns, ad sets, ads, creatives, and ad leads
data from the Facebook Marketing API.

```codeBlockLines_RjmQ
@dlt.source(name="facebook_ads")
def facebook_ads_source(
    account_id: str = dlt.config.value,
    access_token: str = dlt.secrets.value,
    chunk_size: int = 50,
    request_timeout: float = 300.0,
    app_api_version: str = None,
) -> Sequence[DltResource]:
   ...

```

`account_id`: Account ID associated with the ad manager, configured in "config.toml".

`access_token`: Access token associated with the Business Facebook App, configured in
"secrets.toml".

`chunk_size`: The size of the page and batch request. You may need to decrease it if you request a lot
of fields. Defaults to 50.

`request_timeout`: Connection timeout. Defaults to 300.0.

`app_api_version`: A version of the Facebook API required by the app for which the access tokens
were issued, e.g., 'v17.0'. Defaults to the _facebook\_business_ library default version.

### Resource `ads` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads\#resource-ads "Direct link to resource-ads")

The ads function fetches ad data. It retrieves ads from a specified account with specific fields and
states.

```codeBlockLines_RjmQ
@dlt.resource(primary_key="id", write_disposition="replace")
def ads(
    fields: Sequence[str] = DEFAULT_AD_FIELDS,
    states: Sequence[str] = None,
    chunk_size: int = 50
) -> Iterator[TDataItems]:

  yield _get_data_chunked(account.get_ads, fields, states, chunk_size)

```

`fields`: Retrieves fields for each ad. For example, “id”, “name”, “adset\_id”, etc.

`states`: The possible states include "Active," "Paused," "Pending Review," "Disapproved,"
"Completed," and "Archived."

### Resources for `facebook_ads_source` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads\#resources-for-facebook_ads_source "Direct link to resources-for-facebook_ads_source")

Similar to resource `ads`, the following resources have been defined in the `__init__.py` for source
`facebook_ads_source`:

| Resource | Description |
| --- | --- |
| campaigns | Fetches all `DEFAULT_CAMPAIGN_FIELDS` |
| ad\_sets | Fetches all `DEFAULT_ADSET_FIELDS` |
| leads | Fetches all `DEFAULT_LEAD_FIELDS`, uses `@dlt.transformer` decorator |
| ad\_creatives | Fetches all `DEFAULT_ADCREATIVE_FIELDS` |

The default fields are defined in
[facebook\_ads/settings.py](https://github.com/dlt-hub/verified-sources/blob/master/sources/facebook_ads/settings.py)

### Source `facebook_insights_source` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads\#source-facebook_insights_source "Direct link to source-facebook_insights_source")

This function returns a list of resources to load facebook\_insights.

```codeBlockLines_RjmQ
@dlt.source(name="facebook_ads")
def facebook_insights_source(
    account_id: str = dlt.config.value,
    access_token: str = dlt.secrets.value,
    initial_load_past_days: int = 30,
    fields: Sequence[str] = DEFAULT_INSIGHT_FIELDS,
    attribution_window_days_lag: int = 7,
    time_increment_days: int = 1,
    breakdowns: TInsightsBreakdownOptions = "ads_insights_age_and_gender",
    action_breakdowns: Sequence[str] = ALL_ACTION_BREAKDOWNS,
    level: TInsightsLevels = "ad",
    action_attribution_windows: Sequence[str] = ALL_ACTION_ATTRIBUTION_WINDOWS,
    batch_size: int = 50,
    request_timeout: int = 300,
    app_api_version: str = None,
) -> DltResource:
   ...

```

`account_id`: Account ID associated with ads manager, configured in _config.toml_.

`access_token`: Access token associated with the Business Facebook App, configured in _secrets.toml_.

`initial_load_past_days`: How many past days (starting from today) to initially load. Defaults to 30.

`fields`: A list of fields to include in each report. Note that the “breakdowns” option adds fields automatically. Defaults to DEFAULT\_INSIGHT\_FIELDS.

`attribution_window_days_lag`: Attribution window in days. The reports in the attribution window are refreshed on each run. Defaults to 7.

`time_increment_days`: The report aggregation window in days. Use 7 for weekly aggregation. Defaults to 1.

`breakdowns`: Presents with common aggregations. See [settings.py](https://github.com/dlt-hub/verified-sources/blob/master/sources/facebook_ads/settings.py) for details. Defaults to "ads\_insights\_age\_and\_gender".

`action_breakdowns`: Action aggregation types. See [settings.py](https://github.com/dlt-hub/verified-sources/blob/master/sources/facebook_ads/settings.py) for details. Defaults to ALL\_ACTION\_BREAKDOWNS.

`level`: The granularity level. Defaults to "ad".

`action_attribution_windows`: Attribution windows for actions. Defaults to ALL\_ACTION\_ATTRIBUTION\_WINDOWS.

`batch_size`: Page size when reading data from a particular report. Defaults to 50.

`request_timeout`: Connection timeout. Defaults to 300.

`app_api_version`: A version of the Facebook API required by the app for which the access tokens were issued, e.g., 'v17.0'. Defaults to the facebook\_business library default version.

### Resource `facebook_insights` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads\#resource-facebook_insights "Direct link to resource-facebook_insights")

This function fetches Facebook insights data incrementally from a specified start date until the current date, in day steps.

```codeBlockLines_RjmQ
@dlt.resource(primary_key=INSIGHTS_PRIMARY_KEY, write_disposition="merge")
def facebook_insights(
    date_start: dlt.sources.incremental[str] = dlt.sources.incremental(
        "date_start", initial_value=START_DATE_STRING
    )
) -> Iterator[TDataItems]:
   ...

```

`date_start`: Parameter sets the initial value for the "date\_start" parameter in dlt.sources.incremental. It is based on the last pipeline run or defaults to today's date minus the specified number of days in the "initial\_load\_past\_days" parameter.

## Customization [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads\#customization "Direct link to Customization")

### Create your own pipeline [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads\#create-your-own-pipeline "Direct link to Create your own pipeline")

If you wish to create your own pipelines, you can leverage source and resource methods from this verified source.

1. Configure the pipeline by specifying the pipeline name, destination, and dataset as follows:

```codeBlockLines_RjmQ
pipeline = dlt.pipeline(
       pipeline_name="facebook_ads",  # Use a custom name if desired
       destination="duckdb",  # Choose the appropriate destination (e.g., duckdb, redshift, post)
       dataset_name="facebook_ads_data"  # Use a custom name if desired
)

```

To read more about pipeline configuration, please refer to our [documentation](https://dlthub.com/docs/general-usage/pipeline).

2. To load all the data from campaigns, ad sets, ads, ad creatives, and leads:

```codeBlockLines_RjmQ
load_data = facebook_ads_source()
load_info = pipeline.run(load_data)
print(load_info)

```

3. To merge the Facebook Ads with the state "DISAPPROVED" and with ads state "PAUSED", you can do the following:

```codeBlockLines_RjmQ
load_data = facebook_ads_source()
# It is recommended to enable root key propagation on a source that is not a merge one by default. This is not required if you always use merge but below we start with replace
load_data.root_key = True

# Load only disapproved ads
load_data.ads.bind(states=("DISAPPROVED",))
load_info = pipeline.run(load_data.with_resources("ads"), write_disposition="replace")
print(load_info)

# Here we merge the paused ads but the disapproved ads stay there!
load_data = facebook_ads_source()
load_data.ads.bind(states=("PAUSED",))
load_info = pipeline.run(load_data.with_resources("ads"), write_disposition="merge")
print(load_info)

```

In the above steps, we first load the "ads" data with the "DISAPPROVED" state in _replace_ mode and then merge the ads data with the "PAUSED" state on that.

4. To load data with a custom field, for example, to load only "id" from Facebook ads, you can do the following:

```codeBlockLines_RjmQ
load_data = facebook_ads_source()
# Only loads ad ids, works the same for campaigns, leads, etc.
load_data.ads.bind(fields=("id",))
load_info = pipeline.run(load_data.with_resources("ads"))
print(load_info)

```

5. This pipeline includes an enrichment transformation called `enrich_ad_objects` that you can apply to any resource to obtain additional data per object using `object.get_api`. The following code demonstrates how to enrich objects by adding an enrichment transformation that includes additional fields.

```codeBlockLines_RjmQ
# You can reduce the chunk size for smaller requests
load_data = facebook_ads_source(chunk_size=2)

# Request only the "id" field for ad_creatives
load_data.ad_creatives.bind(fields=("id",))

# Add a transformation to the ad_creatives resource
load_data.ad_creatives.add_step(
       # Specify the AdCreative object type and request the desired fields
       enrich_ad_objects(AdCreative, DEFAULT_ADCREATIVE_FIELDS)
)

# Run the pipeline with the ad_creatives resource
load_info = pipeline.run(load_data.with_resources("ad_creatives"))

print(load_info)

```

In the above code, the "load\_data" object represents the Facebook Ads source, and we specify the desired chunk size for the requests. We then bind the "id" field for the "ad\_creatives" resource using the "bind()" method.

To enrich the ad\_creatives objects, we add a transformation using the "add\_step()" method. The "enrich\_ad\_objects" function is used to specify the AdCreative object type and request the fields defined in _DEFAULT\_ADCREATIVE\_FIELDS_.

Finally, we run the pipeline with the ad\_creatives resource and store the load information in the `load_info`.

6. You can also load insights reports incrementally with defined granularity levels, fields, breakdowns, etc., as defined in the `facebook_insights_source`. This function generates daily reports for a specified number of past days.

```codeBlockLines_RjmQ
load_data = facebook_insights_source(
       initial_load_past_days=30,
       attribution_window_days_lag=7,
       time_increment_days=1
)
load_info = pipeline.run(load_data)
print(load_info)

```

> By default, daily reports are generated from `initial_load_past_days` ago to today. On subsequent runs, only new reports are loaded, with the past `attribution_window_days_lag` days (default is 7) being refreshed to accommodate any changes. You can adjust `time_increment_days` to change report frequency (default set to one).

- [Setup guide](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads#setup-guide)
  - [Grab credentials](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads#grab-credentials)
  - [Initialize the verified source](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads#initialize-the-verified-source)
  - [Add credential](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads#add-credential)
- [Run the pipeline](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads#run-the-pipeline)
- [Sources and resources](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads#sources-and-resources)
  - [Default endpoints](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads#default-endpoints)
  - [Source `facebook_ads_source`](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads#source-facebook_ads_source)
  - [Resource `ads`](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads#resource-ads)
  - [Resources for `facebook_ads_source`](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads#resources-for-facebook_ads_source)
  - [Source `facebook_insights_source`](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads#source-facebook_insights_source)
  - [Resource `facebook_insights`](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads#resource-facebook_insights)
- [Customization](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads#customization)
  - [Create your own pipeline](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads#create-your-own-pipeline)

----- https://dlthub.com/docs/walkthroughs/create-a-pipeline -----

PreferencesDeclineAccept

Version: 1.11.0 (latest)

On this page

This guide walks you through creating a pipeline that uses our [REST API Client](https://dlthub.com/docs/general-usage/http/rest-client)
to connect to [DuckDB](https://dlthub.com/docs/dlt-ecosystem/destinations/duckdb).

tip

We're using DuckDB as a destination here, but you can adapt the steps to any [source](https://dlthub.com/docs/dlt-ecosystem/verified-sources/) and [destination](https://dlthub.com/docs/dlt-ecosystem/destinations/) by
using the [command](https://dlthub.com/docs/reference/command-line-interface#dlt-init) `dlt init <source> <destination>` and tweaking the pipeline accordingly.

Please make sure you have [installed `dlt`](https://dlthub.com/docs/reference/installation) before following the
steps below.

## Task overview [​](https://dlthub.com/docs/walkthroughs/create-a-pipeline\#task-overview "Direct link to Task overview")

Imagine you want to analyze issues from a GitHub project locally.
To achieve this, you need to write code that accomplishes the following:

1. Constructs a correct request.
2. Authenticates your request.
3. Fetches and handles paginated issue data.
4. Stores the data for analysis.

This may sound complicated, but dlt provides a [REST API Client](https://dlthub.com/docs/general-usage/http/rest-client) that allows you to focus more on your data rather than on managing API interactions.

## 1\. Initialize project [​](https://dlthub.com/docs/walkthroughs/create-a-pipeline\#1-initialize-project "Direct link to 1. Initialize project")

Create a new empty directory for your `dlt` project by running:

```codeBlockLines_RjmQ
mkdir github_api_duckdb && cd github_api_duckdb

```

Start a `dlt` project with a pipeline template that loads data to DuckDB by running:

```codeBlockLines_RjmQ
dlt init github_api duckdb

```

Install the dependencies necessary for DuckDB:

```codeBlockLines_RjmQ
pip install -r requirements.txt

```

## 2\. Obtain and add API credentials from GitHub [​](https://dlthub.com/docs/walkthroughs/create-a-pipeline\#2-obtain-and-add-api-credentials-from-github "Direct link to 2. Obtain and add API credentials from GitHub")

You will need to [sign in](https://github.com/login) to your GitHub account and create your access token via the [Personal access tokens page](https://github.com/settings/tokens).

Copy your new access token over to `.dlt/secrets.toml`:

```codeBlockLines_RjmQ
[sources]
api_secret_key = '<api key value>'

```

This token will be used by `github_api_source()` to authenticate requests.

The **secret name** corresponds to the **argument name** in the source function.
Below, `api_secret_key` [will get its value](https://dlthub.com/docs/general-usage/credentials/advanced)
from `secrets.toml` when `github_api_source()` is called.

```codeBlockLines_RjmQ
@dlt.source
def github_api_source(api_secret_key: str = dlt.secrets.value):
    return github_api_resource(api_secret_key=api_secret_key)

```

Run the `github_api_pipeline.py` pipeline script to test that authentication headers look fine:

```codeBlockLines_RjmQ
python github_api_pipeline.py

```

Your API key should be printed out to stdout along with some test data.

## 3\. Request project issues from the GitHub API [​](https://dlthub.com/docs/walkthroughs/create-a-pipeline\#3-request-project-issues-from-the-github-api "Direct link to 3. Request project issues from the GitHub API")

tip

We will use the `dlt` repository as an example GitHub project [https://github.com/dlt-hub/dlt](https://github.com/dlt-hub/dlt), feel free to replace it with your own repository.

Modify `github_api_resource` in `github_api_pipeline.py` to request issues data from your GitHub project's API:

```codeBlockLines_RjmQ
from dlt.sources.helpers.rest_client import paginate
from dlt.sources.helpers.rest_client.auth import BearerTokenAuth
from dlt.sources.helpers.rest_client.paginators import HeaderLinkPaginator

@dlt.resource(write_disposition="replace")
def github_api_resource(api_secret_key: str = dlt.secrets.value):
    url = "https://api.github.com/repos/dlt-hub/dlt/issues"

    for page in paginate(
        url,
        auth=BearerTokenAuth(api_secret_key), # type: ignore
        paginator=HeaderLinkPaginator(),
        params={"state": "open"}
    ):
        yield page

```

## 4\. Load the data [​](https://dlthub.com/docs/walkthroughs/create-a-pipeline\#4-load-the-data "Direct link to 4. Load the data")

Uncomment the commented-out code in the `main` function in `github_api_pipeline.py`, so that running the
`python github_api_pipeline.py` command will now also run the pipeline:

```codeBlockLines_RjmQ
if __name__=='__main__':
    # configure the pipeline with your destination details
    pipeline = dlt.pipeline(
        pipeline_name='github_api_pipeline',
        destination='duckdb',
        dataset_name='github_api_data'
    )

    # print credentials by running the resource
    data = list(github_api_resource())

    # print the data yielded from resource
    print(data)

    # run the pipeline with your parameters
    load_info = pipeline.run(github_api_source())

    # pretty print the information on data that was loaded
    print(load_info)

```

Run the `github_api_pipeline.py` pipeline script to test that the API call works:

```codeBlockLines_RjmQ
python github_api_pipeline.py

```

This should print out JSON data containing the issues in the GitHub project.

It also prints the `load_info` object.

Let's explore the loaded data with the [command](https://dlthub.com/docs/reference/command-line-interface#dlt-pipeline-show) `dlt pipeline <pipeline_name> show`.

info

Make sure you have `streamlit` installed: `pip install streamlit`

```codeBlockLines_RjmQ
dlt pipeline github_api_pipeline show

```

This will open a Streamlit app that gives you an overview of the data loaded.

## 5\. Next steps [​](https://dlthub.com/docs/walkthroughs/create-a-pipeline\#5-next-steps "Direct link to 5. Next steps")

With a functioning pipeline, consider exploring:

- Our [REST Client](https://dlthub.com/docs/general-usage/http/rest-client).
- [Deploy this pipeline with GitHub Actions](https://dlthub.com/docs/walkthroughs/deploy-a-pipeline/deploy-with-github-actions), so that the data is automatically loaded on a schedule.
- Transform the [loaded data](https://dlthub.com/docs/dlt-ecosystem/transformations) with dbt or in Pandas DataFrames.
- Learn how to [run](https://dlthub.com/docs/running-in-production/running), [monitor](https://dlthub.com/docs/running-in-production/monitoring), and [alert](https://dlthub.com/docs/running-in-production/alerting) when you put your pipeline in production.
- Try loading data to a different destination like [Google BigQuery](https://dlthub.com/docs/dlt-ecosystem/destinations/bigquery), [Amazon Redshift](https://dlthub.com/docs/dlt-ecosystem/destinations/redshift), or [Postgres](https://dlthub.com/docs/dlt-ecosystem/destinations/postgres).

- [Task overview](https://dlthub.com/docs/walkthroughs/create-a-pipeline#task-overview)
- [1\. Initialize project](https://dlthub.com/docs/walkthroughs/create-a-pipeline#1-initialize-project)
- [2\. Obtain and add API credentials from GitHub](https://dlthub.com/docs/walkthroughs/create-a-pipeline#2-obtain-and-add-api-credentials-from-github)
- [3\. Request project issues from the GitHub API](https://dlthub.com/docs/walkthroughs/create-a-pipeline#3-request-project-issues-from-the-github-api)
- [4\. Load the data](https://dlthub.com/docs/walkthroughs/create-a-pipeline#4-load-the-data)
- [5\. Next steps](https://dlthub.com/docs/walkthroughs/create-a-pipeline#5-next-steps)

[Create a pipeline with GPT-4](https://dlthub.com/docs/walkthroughs/create-a-pipeline#)

# Create a pipeline with GPT-4

Create dlt pipeline using the data source of your liking and let the GPT-4 write the resource functions and help you to debug the code.

Creating a DLT Pipeline with Continue 🚀

1.2×

4 min⚡️4 min 49 sec3 min 51 sec3 min 12 sec2 min 34 sec2 min 16 sec1 min 55 sec1 min 32 sec

Your user agent does not support the HTML5 Video element.

1.2×

4 min⚡️4 min 49 sec3 min 51 sec3 min 12 sec2 min 34 sec2 min 16 sec1 min 55 sec1 min 32 sec

----- https://dlthub.com/docs/general-usage/incremental/lag -----

Version: 1.11.0 (latest)

On this page

In many cases, certain data should be reacquired during incremental loading. For example, you may want to always capture the last 7 days of data when fetching daily analytics reports, or refresh Slack message replies with a moving window of 7 days. This is where the concept of "lag" or "attribution window" comes into play.

The `lag` parameter is a float that supports several types of incremental cursors: `datetime`, `date`, `integer`, and `float`. It can only be used with `last_value_func` set to `min` or `max` (default is `max`).

### How `lag` works [​](https://dlthub.com/docs/general-usage/incremental/lag\#how-lag-works "Direct link to how-lag-works")

- **Datetime cursors**: `lag` is the number of seconds added or subtracted from the `last_value` loaded.
- **Date cursors**: `lag` represents days.
- **Numeric cursors (integer or float)**: `lag` respects the given unit of the cursor.

This flexibility allows `lag` to adapt to different data contexts.

### Example using `datetime` incremental cursor with `merge` as `write_disposition` [​](https://dlthub.com/docs/general-usage/incremental/lag\#example-using-datetime-incremental-cursor-with-merge-as-write_disposition "Direct link to example-using-datetime-incremental-cursor-with-merge-as-write_disposition")

This example demonstrates how to use a `datetime` cursor with a `lag` parameter, applying `merge` as the `write_disposition`. The setup runs twice, and during the second run, the `lag` parameter re-fetches recent entries to capture updates.

1. **First Run**: Loads `initial_entries`.
2. **Second Run**: Loads `second_run_events` with the specified lag, refreshing previously loaded entries.

This setup demonstrates how `lag` ensures that a defined period of data remains refreshed, capturing updates or changes within the attribution window.

```codeBlockLines_RjmQ
pipeline = dlt.pipeline(
    destination=dlt.destinations.duckdb(credentials=duckdb.connect(":memory:")),
)

# Flag to indicate the second run
is_second_run = False

@dlt.resource(name="events", primary_key="id", write_disposition="merge")
def events_resource(
    _=dlt.sources.incremental("created_at", lag=3600, last_value_func=max)
):
    global is_second_run

    # Data for the initial run
    initial_entries = [\
        {"id": 1, "created_at": "2023-03-03T01:00:00Z", "event": "1"},\
        {"id": 2, "created_at": "2023-03-03T02:00:00Z", "event": "2"},  # lag applied during second run\
    ]

    # Data for the second run
    second_run_events = [\
        {"id": 1, "created_at": "2023-03-03T01:00:00Z", "event": "1_updated"},\
        {"id": 2, "created_at": "2023-03-03T02:00:01Z", "event": "2_updated"},\
        {"id": 3, "created_at": "2023-03-03T03:00:00Z", "event": "3"},\
    ]

    # Yield data based on the current run
    yield from second_run_events if is_second_run else initial_entries

# Run the pipeline twice
pipeline.run(events_resource)
is_second_run = True  # Update flag for second run
pipeline.run(events_resource)

```

- [How `lag` works](https://dlthub.com/docs/general-usage/incremental/lag#how-lag-works)
- [Example using `datetime` incremental cursor with `merge` as `write_disposition`](https://dlthub.com/docs/general-usage/incremental/lag#example-using-datetime-incremental-cursor-with-merge-as-write_disposition)

----- https://dlthub.com/docs/general-usage/resource#dispatch-data-to-many-tables -----

Version: 1.11.0 (latest)

On this page

## Declare a resource [​](https://dlthub.com/docs/general-usage/resource\#declare-a-resource "Direct link to Declare a resource")

A [resource](https://dlthub.com/docs/general-usage/glossary#resource) is an ( [optionally async](https://dlthub.com/docs/reference/performance#parallelism-within-a-pipeline)) function that yields data. To create a resource, we add the `@dlt.resource` decorator to that function.

Commonly used arguments:

- `name`: The name of the table generated by this resource. Defaults to the decorated function name.
- `write_disposition`: How should the data be loaded at the destination? Currently supported: `append`,
`replace`, and `merge`. Defaults to `append.`

Example:

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows():
	for i in range(10):
		yield {'id': i, 'example_string': 'abc'}

@dlt.source
def source_name():
    return generate_rows

```

To get the data of a resource, we could do:

```codeBlockLines_RjmQ
for row in generate_rows():
    print(row)

for row in source_name().resources.get('table_name'):
    print(row)

```

Typically, resources are declared and grouped with related resources within a [source](https://dlthub.com/docs/general-usage/source) function.

### Define schema [​](https://dlthub.com/docs/general-usage/resource\#define-schema "Direct link to Define schema")

`dlt` will infer the [schema](https://dlthub.com/docs/general-usage/schema) for tables associated with resources from the resource's data.
You can modify the generation process by using the table and column hints. The resource decorator accepts the following arguments:

1. `table_name`: the name of the table, if different from the resource name.
2. `primary_key` and `merge_key`: define the name of the columns (compound keys are allowed) that will receive those hints. Used in [incremental loading](https://dlthub.com/docs/general-usage/incremental-loading) and [merge loading](https://dlthub.com/docs/general-usage/merge-loading).
3. `columns`: lets you define one or more columns, including the data types, nullability, and other hints. The column definition is a `TypedDict`: `TTableSchemaColumns`. In the example below, we tell `dlt` that the column `tags` (containing a list of tags) in the `user` table should have type `json`, which means that it will be loaded as JSON/struct and not as a separate nested table.

```codeBlockLines_RjmQ
@dlt.resource(name="user", columns={"tags": {"data_type": "json"}})
def get_users():
  ...

# the `table_schema` method gets the table schema generated by a resource
print(get_users().compute_table_schema())

```

note

You can pass dynamic hints which are functions that take the data item as input and return a hint value. This lets you create table and column schemas depending on the data. See an [example below](https://dlthub.com/docs/general-usage/resource#adjust-schema-when-you-yield-data).

### Put a contract on tables, columns, and data [​](https://dlthub.com/docs/general-usage/resource\#put-a-contract-on-tables-columns-and-data "Direct link to Put a contract on tables, columns, and data")

Use the `schema_contract` argument to tell dlt how to [deal with new tables, data types, and bad data types](https://dlthub.com/docs/general-usage/schema-contracts). For example, if you set it to **freeze**, `dlt` will not allow for any new tables, columns, or data types to be introduced to the schema - it will raise an exception. Learn more about available contract modes [here](https://dlthub.com/docs/general-usage/schema-contracts#setting-up-the-contract).

### Define schema of nested tables [​](https://dlthub.com/docs/general-usage/resource\#define-schema-of-nested-tables "Direct link to Define schema of nested tables")

`dlt` creates [nested tables](https://dlthub.com/docs/general-usage/schema#nested-references-root-and-nested-tables) to store [list of objects](https://dlthub.com/docs/general-usage/destination-tables#nested-tables) if present in your data.
You can define the schema of such tables with `nested_hints` argument to `@dlt.resource`:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(
    nested_hints={
        "purchases": dlt.mark.make_nested_hints(
            columns=[{"name": "price", "data_type": "decimal"}],
            schema_contract={"columns": "freeze"},
        )
    },
)
def customers():
    """Load customer data from a simple python list."""
    yield [\
        {\
            "id": 1,\
            "name": "simon",\
            "city": "berlin",\
            "purchases": [{"id": 1, "name": "apple", "price": "1.50"}],\
        },\
    ]

```

Here we convert the `price` field in list of `purchases` to decimal type and set the schema contract to lock the list
of columns in it. We use convenience function `dlt.mark.make_nested_hints` to generate nested hints dictionary. You are
free to use it directly.

Mind that `purchases` list will be stored as table with name `customers__purchases`. When declaring nested hints you just need
to specify nested field(s) name(s). In case of deeper nesting ie. let's say each `purchase` has a list of `coupons` applied,
you can apply hints to coupons and define `customers__purchases__coupons` table schema:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(
    nested_hints={
        "purchases": {},
        ("purchases", "coupons"): {
            "columns": {"registered_at": {"data_type": "timestamp"}}
        }
    },
)
def customers():
    ...

```

Here we use `("purchases", "coupons")` to locate list at the depth of 2 and set the data type on `registered_at` column
to `timestamp`. We do that by directly using nested hints dict.
Note that we specified `purchases` with an empty list of hints. **You are required to specify all parent hints, even if they**
**are empty. Currently we are not adding missing path elements automatically**.

You can use `nested_hints` primarily to set column hints and schema contract, those work exactly as in case of root tables.

- `file_format` has no effect (not implemented yet)
- `write_disposition` works as expected but leads to unintended consequences (ie. you can set nested table to `replace`) while root table is `append`.
- `references` will create [table references](https://dlthub.com/docs/general-usage/schema#table-references-1) (annotations) as expected.
- `primary_key` and `merge_key`: **setting those will convert nested table into a regular table, with a separate write disposition, file format etc.** [It allows you to create custom table relationships ie. using natural primary and foreign keys present in the data.](https://dlthub.com/docs/general-usage/schema#generate-custom-linking-for-nested-tables)

tip

[REST API Source](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic) accepts `nested_hints` argument as well.

You can apply nested hints after the resource was created by using [apply\_hints](https://dlthub.com/docs/general-usage/resource#set-table-name-and-adjust-schema).

### Define a schema with Pydantic [​](https://dlthub.com/docs/general-usage/resource\#define-a-schema-with-pydantic "Direct link to Define a schema with Pydantic")

You can alternatively use a [Pydantic](https://pydantic-docs.helpmanual.io/) model to define the schema.
For example:

```codeBlockLines_RjmQ
from pydantic import BaseModel
from typing import List, Optional, Union

class Address(BaseModel):
    street: str
    city: str
    postal_code: str

class User(BaseModel):
    id: int
    name: str
    tags: List[str]
    email: Optional[str]
    address: Address
    status: Union[int, str]

@dlt.resource(name="user", columns=User)
def get_users():
    ...

```

The data types of the table columns are inferred from the types of the Pydantic fields. These use the same type conversions
as when the schema is automatically generated from the data.

Pydantic models integrate well with [schema contracts](https://dlthub.com/docs/general-usage/schema-contracts) as data validators.

Things to note:

- Fields with an `Optional` type are marked as `nullable`.
- Fields with a `Union` type are converted to the first (not `None`) type listed in the union. For example, `status: Union[int, str]` results in a `bigint` column.
- `list`, `dict`, and nested Pydantic model fields will use the `json` type, which means they'll be stored as a JSON object in the database instead of creating nested tables.

You can override this by configuring the Pydantic model:

```codeBlockLines_RjmQ
from typing import ClassVar
from dlt.common.libs.pydantic import DltConfig

class UserWithNesting(User):
  dlt_config: ClassVar[DltConfig] = {"skip_nested_types": True}

@dlt.resource(name="user", columns=UserWithNesting)
def get_users():
    ...

```

`"skip_nested_types"` omits any `dict`/ `list`/ `BaseModel` type fields from the schema, so dlt will fall back on the default
behavior of creating nested tables for these fields.

We do not support `RootModel` that validate simple types. You can add such a validator yourself, see [data filtering section](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data).

### Dispatch data to many tables [​](https://dlthub.com/docs/general-usage/resource\#dispatch-data-to-many-tables "Direct link to Dispatch data to many tables")

You can load data to many tables from a single resource. The most common case is a stream of events
of different types, each with a different data schema. To deal with this, you can use the `table_name`
argument on `dlt.resource`. You could pass the table name as a function with the data item as an
argument and the `table_name` string as a return value.

For example, a resource that loads GitHub repository events wants to send `issue`, `pull request`,
and `comment` events to separate tables. The type of the event is in the "type" field.

```codeBlockLines_RjmQ
# send item to a table with name item["type"]
@dlt.resource(table_name=lambda event: event['type'])
def repo_events() -> Iterator[TDataItems]:
    yield item

# the `table_schema` method gets the table schema generated by a resource and takes an optional
# data item to evaluate dynamic hints
print(repo_events().compute_table_schema({"type": "WatchEvent", "id": ...}))

```

In more advanced cases, you can dispatch data to different tables directly in the code of the
resource function:

```codeBlockLines_RjmQ
@dlt.resource
def repo_events() -> Iterator[TDataItems]:
    # mark the "item" to be sent to the table with the name item["type"]
    yield dlt.mark.with_table_name(item, item["type"])

```

### Parametrize a resource [​](https://dlthub.com/docs/general-usage/resource\#parametrize-a-resource "Direct link to Parametrize a resource")

You can add arguments to your resource functions like to any other. Below we parametrize our
`generate_rows` resource to generate the number of rows we request:

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

for row in generate_rows(10):
    print(row)

for row in generate_rows(20):
    print(row)

```

tip

You can mark some resource arguments as [configuration and credentials](https://dlthub.com/docs/general-usage/credentials) values so `dlt` can pass them automatically to your functions.

### Process resources with `dlt.transformer` [​](https://dlthub.com/docs/general-usage/resource\#process-resources-with-dlttransformer "Direct link to process-resources-with-dlttransformer")

You can feed data from one resource into another. The most common case is when you have an API that returns a list of objects (i.e., users) in one endpoint and user details in another. You can deal with this by declaring a resource that obtains a list of users and another resource that receives items from the list and downloads the profiles.

```codeBlockLines_RjmQ
@dlt.resource(write_disposition="replace")
def users(limit=None):
    for u in _get_users(limit):
        yield u

# Feed data from users as user_item below,
# all transformers must have at least one
# argument that will receive data from the parent resource
@dlt.transformer(data_from=users)
def users_details(user_item):
    for detail in _get_details(user_item["user_id"]):
        yield detail

# Just load the users_details.
# dlt figures out dependencies for you.
pipeline.run(users_details)

```

In the example above, `users_details` will receive data from the default instance of the `users` resource (with `limit` set to `None`). You can also use the **pipe \|** operator to bind resources dynamically.

```codeBlockLines_RjmQ
# You can be more explicit and use a pipe operator.
# With it, you can create dynamic pipelines where the dependencies
# are set at run time and resources are parametrized, i.e.,
# below we want to load only 100 users from the `users` endpoint.
pipeline.run(users(limit=100) | users_details)

```

tip

Transformers are allowed not only to **yield** but also to **return** values and can decorate **async** functions and [**async generators**](https://dlthub.com/docs/reference/performance#extract). Below we decorate an async function and request details on two pokemons. HTTP calls are made in parallel via the httpx library.

```codeBlockLines_RjmQ
import dlt
import httpx

@dlt.transformer
async def pokemon(id):
    async with httpx.AsyncClient() as client:
        r = await client.get(f"https://pokeapi.co/api/v2/pokemon/{id}")
        return r.json()

# Get Bulbasaur and Ivysaur (you need dlt 0.4.6 for the pipe operator working with lists).
print(list([1,2] | pokemon()))

```

### Declare a standalone resource [​](https://dlthub.com/docs/general-usage/resource\#declare-a-standalone-resource "Direct link to Declare a standalone resource")

A standalone resource is defined on a function that is top-level in a module (not an inner function) that accepts config and secrets values. Additionally, if the `standalone` flag is specified, the decorated function signature and docstring will be preserved. `dlt.resource` will just wrap the decorated function, and the user must call the wrapper to get the actual resource. Below we declare a `filesystem` resource that must be called before use.

```codeBlockLines_RjmQ
@dlt.resource(standalone=True)
def fs_resource(bucket_url=dlt.config.value):
  """List and yield files in `bucket_url`."""
  ...

# `filesystem` must be called before it is extracted or used in any other way.
pipeline.run(fs_resource("s3://my-bucket/reports"), table_name="reports")

```

Standalone may have a dynamic name that depends on the arguments passed to the decorated function. For example:

```codeBlockLines_RjmQ
@dlt.resource(standalone=True, name=lambda args: args["stream_name"])
def kinesis(stream_name: str):
    ...

kinesis_stream = kinesis("telemetry_stream")

```

`kinesis_stream` resource has a name **telemetry\_stream**.

### Declare parallel and async resources [​](https://dlthub.com/docs/general-usage/resource\#declare-parallel-and-async-resources "Direct link to Declare parallel and async resources")

You can extract multiple resources in parallel threads or with async IO.
To enable this for a sync resource, you can set the `parallelized` flag to `True` in the resource decorator:

```codeBlockLines_RjmQ
@dlt.resource(parallelized=True)
def get_users():
    for u in _get_users():
        yield u

@dlt.resource(parallelized=True)
def get_orders():
    for o in _get_orders():
        yield o

# users and orders will be iterated in parallel in two separate threads
pipeline.run([get_users(), get_orders()])

```

Async generators are automatically extracted concurrently with other resources:

```codeBlockLines_RjmQ
@dlt.resource
async def get_users():
    async for u in _get_users():  # Assuming _get_users is an async generator
        yield u

```

Please find more details in [extract performance](https://dlthub.com/docs/reference/performance#extract)

## Customize resources [​](https://dlthub.com/docs/general-usage/resource\#customize-resources "Direct link to Customize resources")

### Filter, transform, and pivot data [​](https://dlthub.com/docs/general-usage/resource\#filter-transform-and-pivot-data "Direct link to Filter, transform, and pivot data")

You can attach any number of transformations that are evaluated on an item-per-item basis to your
resource. The available transformation types:

- **map** \- transform the data item ( `resource.add_map`).
- **filter** \- filter the data item ( `resource.add_filter`).
- **yield map** \- a map that returns an iterator (so a single row may generate many rows -
`resource.add_yield_map`).

Example: We have a resource that loads a list of users from an API endpoint. We want to customize it
so:

1. We remove users with `user_id == "me"`.
2. We anonymize user data.

Here's our resource:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(write_disposition="replace")
def users():
    ...
    users = requests.get(RESOURCE_URL)
    ...
    yield users

```

Here's our script that defines transformations and loads the data:

```codeBlockLines_RjmQ
from pipedrive import users

def anonymize_user(user_data):
    user_data["user_id"] = _hash_str(user_data["user_id"])
    user_data["user_email"] = _hash_str(user_data["user_email"])
    return user_data

# add the filter and anonymize function to users resource and enumerate
for user in users().add_filter(lambda user: user["user_id"] != "me").add_map(anonymize_user):
    print(user)

```

### Reduce the nesting level of generated tables [​](https://dlthub.com/docs/general-usage/resource\#reduce-the-nesting-level-of-generated-tables "Direct link to Reduce the nesting level of generated tables")

You can limit how deep `dlt` goes when generating nested tables and flattening dicts into columns. By default, the library will descend
and generate nested tables for all nested lists, without limit.

note

`max_table_nesting` is optional so you can skip it, in this case, dlt will
use it from the source if it is specified there or fallback to the default
value which has 1000 as the maximum nesting level.

```codeBlockLines_RjmQ
import dlt

@dlt.resource(max_table_nesting=1)
def my_resource():
    yield {
        "id": 1,
        "name": "random name",
        "properties": [\
            {\
                "name": "customer_age",\
                "type": "int",\
                "label": "Age",\
                "notes": [\
                    {\
                        "text": "string",\
                        "author": "string",\
                    }\
                ]\
            }\
        ]
    }

```

In the example above, we want only 1 level of nested tables to be generated (so there are no nested
tables of a nested table). Typical settings:

- `max_table_nesting=0` will not generate nested tables and will not flatten dicts into columns at all. All nested data will be
represented as JSON.
- `max_table_nesting=1` will generate nested tables of root tables and nothing more. All nested
data in nested tables will be represented as JSON.

You can achieve the same effect after the resource instance is created:

```codeBlockLines_RjmQ
resource = my_resource()
resource.max_table_nesting = 0

```

Several data sources are prone to contain semi-structured documents with very deep nesting, i.e.,
MongoDB databases. Our practical experience is that setting the `max_nesting_level` to 2 or 3
produces the clearest and human-readable schemas.

### Sample from large data [​](https://dlthub.com/docs/general-usage/resource\#sample-from-large-data "Direct link to Sample from large data")

If your resource loads thousands of pages of data from a REST API or millions of rows from a database table, you may want to sample just a fragment of it in order to quickly see the dataset with example data and test your transformations, etc. To do this, you limit how many items will be yielded by a resource (or source) by calling the `add_limit` method. This method will close the generator that produces the data after the limit is reached.

In the example below, we load just the first 10 items from an infinite counter - that would otherwise never end.

```codeBlockLines_RjmQ
r = dlt.resource(itertools.count(), name="infinity").add_limit(10)
assert list(r) == list(range(10))

```

note

Note that `add_limit` **does not limit the number of records** but rather the "number of yields". Depending on how your resource is set up, the number of extracted rows may vary. For example, consider this resource:

```codeBlockLines_RjmQ
@dlt.resource
def my_resource():
    for i in range(100):
        yield [{"record_id": j} for j in range(15)]

dlt.pipeline(destination="duckdb").run(my_resource().add_limit(10))

```

The code above will extract `15*10=150` records. This is happening because in each iteration, 15 records are yielded, and we're limiting the number of iterations to 10.

Altenatively you can also apply a time limit to the resource. The code below will run the extraction for 10 seconds and extract how ever many items are yielded in that time. In combination with incrementals, this can be useful for batched loading or for loading on machines that have a run time limit.

```codeBlockLines_RjmQ
dlt.pipeline(destination="duckdb").run(my_resource().add_limit(max_time=10))

```

You can also apply a combination of both limits. In this case the extraction will stop as soon as either limit is reached.

```codeBlockLines_RjmQ
dlt.pipeline(destination="duckdb").run(my_resource().add_limit(max_items=10, max_time=10))

```

Some notes about the `add_limit`:

1. `add_limit` does not skip any items. It closes the iterator/generator that produces data after the limit is reached.
2. You cannot limit transformers. They should process all the data they receive fully to avoid inconsistencies in generated datasets.
3. Async resources with a limit added may occasionally produce one item more than the limit on some runs. This behavior is not deterministic.
4. Calling add limit on a resource will replace any previously set limits settings.
5. For time-limited resources, the timer starts when the first item is processed. When resources are processed sequentially (FIFO mode), each resource's time limit applies also sequentially. In the default round robin mode, the time limits will usually run concurrently.

tip

If you are parameterizing the value of `add_limit` and sometimes need it to be disabled, you can set `None` or `-1` to disable the limiting.
You can also set the limit to `0` for the resource to not yield any items.

### Set table name and adjust schema [​](https://dlthub.com/docs/general-usage/resource\#set-table-name-and-adjust-schema "Direct link to Set table name and adjust schema")

You can change the schema of a resource, whether it is standalone or part of a source. Look for a method named `apply_hints` which takes the same arguments as the resource decorator. Obviously, you should call this method before data is extracted from the resource. The example below converts an `append` resource loading the `users` table into a [merge](https://dlthub.com/docs/general-usage/merge-loading) resource that will keep just one updated record per `user_id`. It also adds ["last value" incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor) on the `created_at` column to prevent requesting again the already loaded records:

```codeBlockLines_RjmQ
tables = sql_database()
tables.users.apply_hints(
    write_disposition="merge",
    primary_key="user_id",
    incremental=dlt.sources.incremental("updated_at")
)
pipeline.run(tables)

```

To change the name of a table to which the resource will load data, do the following:

```codeBlockLines_RjmQ
tables = sql_database()
tables.users.table_name = "other_users"

```

### Adjust schema when you yield data [​](https://dlthub.com/docs/general-usage/resource\#adjust-schema-when-you-yield-data "Direct link to Adjust schema when you yield data")

You can set or update the table name, columns, and other schema elements when your resource is executed, and you already yield data. Such changes will be merged with the existing schema in the same way the `apply_hints` method above works. There are many reasons to adjust the schema at runtime. For example, when using Airflow, you should avoid lengthy operations (i.e., reflecting database tables) during the creation of the DAG, so it is better to do it when the DAG executes. You may also emit partial hints (i.e., precision and scale for decimal types) for columns to help `dlt` type inference.

```codeBlockLines_RjmQ
@dlt.resource
def sql_table(credentials, schema, table):
    # Create a SQL Alchemy engine
    engine = engine_from_credentials(credentials)
    engine.execution_options(stream_results=True)
    metadata = MetaData(schema=schema)
    # Reflect the table schema
    table_obj = Table(table, metadata, autoload_with=engine)

    for idx, batch in enumerate(table_rows(engine, table_obj)):
      if idx == 0:
        # Emit the first row with hints, table_to_columns and _get_primary_key are helpers that extract dlt schema from
        # SqlAlchemy model
        yield dlt.mark.with_hints(
            batch,
            dlt.mark.make_hints(columns=table_to_columns(table_obj), primary_key=_get_primary_key(table_obj)),
        )
      else:
        # Just yield all the other rows
        yield batch

```

In the example above, we use `dlt.mark.with_hints` and `dlt.mark.make_hints` to emit columns and primary key with the first extracted item. The table schema will be adjusted after the `batch` is processed in the extract pipeline but before any schema contracts are applied, and data is persisted in the load package.

tip

You can emit columns as a Pydantic model and use dynamic hints (i.e., lambda for table name) as well. You should avoid redefining `Incremental` this way.

### Import external files [​](https://dlthub.com/docs/general-usage/resource\#import-external-files "Direct link to Import external files")

You can import external files, i.e., CSV, Parquet, and JSONL, by yielding items marked with `with_file_import`, optionally passing a table schema corresponding to the imported file. dlt will not read, parse, or normalize any names (i.e., CSV or Arrow headers) and will attempt to copy the file into the destination as is.

```codeBlockLines_RjmQ
import os
import dlt
from dlt.sources.filesystem import filesystem

columns: List[TColumnSchema] = [\
    {"name": "id", "data_type": "bigint"},\
    {"name": "name", "data_type": "text"},\
    {"name": "description", "data_type": "text"},\
    {"name": "ordered_at", "data_type": "date"},\
    {"name": "price", "data_type": "decimal"},\
]

import_folder = "/tmp/import"

@dlt.transformer(columns=columns)
def orders(items: Iterator[FileItemDict]):
  for item in items:
    # copy the file locally
      dest_file = os.path.join(import_folder, item["file_name"])
      # download the file
      item.fsspec.download(item["file_url"], dest_file)
      # tell dlt to import the dest_file as `csv`
      yield dlt.mark.with_file_import(dest_file, "csv")

# use the filesystem core source to glob a bucket

downloader = filesystem(
  bucket_url="s3://my_bucket/csv",
  file_glob="today/*.csv.gz") | orders

info = pipeline.run(orders, destination="snowflake")

```

In the example above, we glob all zipped csv files present on **my\_bucket/csv/today** (using the `filesystem` verified source) and send file descriptors to the `orders` transformer. The transformer downloads and imports the files into the extract package. At the end, `dlt` sends them to Snowflake (the table will be created because we use `column` hints to define the schema).

If imported `csv` files are not in `dlt` [default format](https://dlthub.com/docs/dlt-ecosystem/file-formats/csv#default-settings), you may need to pass additional configuration.

```codeBlockLines_RjmQ
[destination.snowflake.csv_format]
delimiter="|"
include_header=false
on_error_continue=true

```

You can sniff the schema from the data, i.e., using DuckDB to infer the table schema from a CSV file. `dlt.mark.with_file_import` accepts additional arguments that you can use to pass hints at runtime.

note

- If you do not define any columns, the table will not be created in the destination. `dlt` will still attempt to load data into it, so if you create a fitting table upfront, the load process will succeed.
- Files are imported using hard links if possible to avoid copying and duplicating the storage space needed.

### Duplicate and rename resources [​](https://dlthub.com/docs/general-usage/resource\#duplicate-and-rename-resources "Direct link to Duplicate and rename resources")

There are cases when your resources are generic (i.e., bucket filesystem) and you want to load several instances of it (i.e., files from different folders) into separate tables. In the example below, we use the `filesystem` source to load csvs from two different folders into separate tables:

```codeBlockLines_RjmQ
@dlt.resource(standalone=True)
def fs_resource(bucket_url):
  # list and yield files in bucket_url
  ...

@dlt.transformer
def csv_reader(file_item):
  # load csv, parse, and yield rows in file_item
  ...

# create two extract pipes that list files from the bucket and send them to the reader.
# by default, both pipes will load data to the same table (csv_reader)
reports_pipe = fs_resource("s3://my-bucket/reports") | csv_reader()
transactions_pipe = fs_resource("s3://my-bucket/transactions") | csv_reader()

# so we rename resources to load to "reports" and "transactions" tables
pipeline.run(
  [reports_pipe.with_name("reports"), transactions_pipe.with_name("transactions")]
)

```

The `with_name` method returns a deep copy of the original resource, its data pipe, and the data pipes of a parent resource. A renamed clone is fully separated from the original resource (and other clones) when loading: it maintains a separate [resource state](https://dlthub.com/docs/general-usage/state#read-and-write-pipeline-state-in-a-resource) and will load to a table.

## Load resources [​](https://dlthub.com/docs/general-usage/resource\#load-resources "Direct link to Load resources")

You can pass individual resources or a list of resources to the `dlt.pipeline` object. The resources loaded outside the source context will be added to the [default schema](https://dlthub.com/docs/general-usage/schema) of the pipeline.

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

pipeline = dlt.pipeline(
    pipeline_name="rows_pipeline",
    destination="duckdb",
    dataset_name="rows_data"
)
# load an individual resource
pipeline.run(generate_rows(10))
# load a list of resources
pipeline.run([generate_rows(10), generate_rows(20)])

```

### Pick loader file format for a particular resource [​](https://dlthub.com/docs/general-usage/resource\#pick-loader-file-format-for-a-particular-resource "Direct link to Pick loader file format for a particular resource")

You can request a particular loader file format to be used for a resource.

```codeBlockLines_RjmQ
@dlt.resource(file_format="parquet")
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

```

The resource above will be saved and loaded from a Parquet file (if the destination supports it).

note

A special `file_format`: **preferred** will load the resource using a format that is preferred by a destination. This setting supersedes the `loader_file_format` passed to the `run` method.

### Do a full refresh [​](https://dlthub.com/docs/general-usage/resource\#do-a-full-refresh "Direct link to Do a full refresh")

To do a full refresh of an `append` or `merge` resource, you set the `refresh` argument on the `run` method to `drop_data`. This will truncate the tables without dropping them.

```codeBlockLines_RjmQ
p.run(merge_source(), refresh="drop_data")

```

You can also [fully drop the tables](https://dlthub.com/docs/general-usage/pipeline#refresh-pipeline-data-and-state) in the `merge_source`:

```codeBlockLines_RjmQ
p.run(merge_source(), refresh="drop_sources")

```

- [Declare a resource](https://dlthub.com/docs/general-usage/resource#declare-a-resource)
  - [Define schema](https://dlthub.com/docs/general-usage/resource#define-schema)
  - [Put a contract on tables, columns, and data](https://dlthub.com/docs/general-usage/resource#put-a-contract-on-tables-columns-and-data)
  - [Define schema of nested tables](https://dlthub.com/docs/general-usage/resource#define-schema-of-nested-tables)
  - [Define a schema with Pydantic](https://dlthub.com/docs/general-usage/resource#define-a-schema-with-pydantic)
  - [Dispatch data to many tables](https://dlthub.com/docs/general-usage/resource#dispatch-data-to-many-tables)
  - [Parametrize a resource](https://dlthub.com/docs/general-usage/resource#parametrize-a-resource)
  - [Process resources with `dlt.transformer`](https://dlthub.com/docs/general-usage/resource#process-resources-with-dlttransformer)
  - [Declare a standalone resource](https://dlthub.com/docs/general-usage/resource#declare-a-standalone-resource)
  - [Declare parallel and async resources](https://dlthub.com/docs/general-usage/resource#declare-parallel-and-async-resources)
- [Customize resources](https://dlthub.com/docs/general-usage/resource#customize-resources)
  - [Filter, transform, and pivot data](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data)
  - [Reduce the nesting level of generated tables](https://dlthub.com/docs/general-usage/resource#reduce-the-nesting-level-of-generated-tables)
  - [Sample from large data](https://dlthub.com/docs/general-usage/resource#sample-from-large-data)
  - [Set table name and adjust schema](https://dlthub.com/docs/general-usage/resource#set-table-name-and-adjust-schema)
  - [Adjust schema when you yield data](https://dlthub.com/docs/general-usage/resource#adjust-schema-when-you-yield-data)
  - [Import external files](https://dlthub.com/docs/general-usage/resource#import-external-files)
  - [Duplicate and rename resources](https://dlthub.com/docs/general-usage/resource#duplicate-and-rename-resources)
- [Load resources](https://dlthub.com/docs/general-usage/resource#load-resources)
  - [Pick loader file format for a particular resource](https://dlthub.com/docs/general-usage/resource#pick-loader-file-format-for-a-particular-resource)
  - [Do a full refresh](https://dlthub.com/docs/general-usage/resource#do-a-full-refresh)

----- https://dlthub.com/docs/general-usage/resource#define-a-schema-with-pydantic -----

PreferencesDeclineAccept

Version: 1.11.0 (latest)

On this page

## Declare a resource [​](https://dlthub.com/docs/general-usage/resource\#declare-a-resource "Direct link to Declare a resource")

A [resource](https://dlthub.com/docs/general-usage/glossary#resource) is an ( [optionally async](https://dlthub.com/docs/reference/performance#parallelism-within-a-pipeline)) function that yields data. To create a resource, we add the `@dlt.resource` decorator to that function.

Commonly used arguments:

- `name`: The name of the table generated by this resource. Defaults to the decorated function name.
- `write_disposition`: How should the data be loaded at the destination? Currently supported: `append`,
`replace`, and `merge`. Defaults to `append.`

Example:

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows():
	for i in range(10):
		yield {'id': i, 'example_string': 'abc'}

@dlt.source
def source_name():
    return generate_rows

```

To get the data of a resource, we could do:

```codeBlockLines_RjmQ
for row in generate_rows():
    print(row)

for row in source_name().resources.get('table_name'):
    print(row)

```

Typically, resources are declared and grouped with related resources within a [source](https://dlthub.com/docs/general-usage/source) function.

### Define schema [​](https://dlthub.com/docs/general-usage/resource\#define-schema "Direct link to Define schema")

`dlt` will infer the [schema](https://dlthub.com/docs/general-usage/schema) for tables associated with resources from the resource's data.
You can modify the generation process by using the table and column hints. The resource decorator accepts the following arguments:

1. `table_name`: the name of the table, if different from the resource name.
2. `primary_key` and `merge_key`: define the name of the columns (compound keys are allowed) that will receive those hints. Used in [incremental loading](https://dlthub.com/docs/general-usage/incremental-loading) and [merge loading](https://dlthub.com/docs/general-usage/merge-loading).
3. `columns`: lets you define one or more columns, including the data types, nullability, and other hints. The column definition is a `TypedDict`: `TTableSchemaColumns`. In the example below, we tell `dlt` that the column `tags` (containing a list of tags) in the `user` table should have type `json`, which means that it will be loaded as JSON/struct and not as a separate nested table.

```codeBlockLines_RjmQ
@dlt.resource(name="user", columns={"tags": {"data_type": "json"}})
def get_users():
  ...

# the `table_schema` method gets the table schema generated by a resource
print(get_users().compute_table_schema())

```

note

You can pass dynamic hints which are functions that take the data item as input and return a hint value. This lets you create table and column schemas depending on the data. See an [example below](https://dlthub.com/docs/general-usage/resource#adjust-schema-when-you-yield-data).

### Put a contract on tables, columns, and data [​](https://dlthub.com/docs/general-usage/resource\#put-a-contract-on-tables-columns-and-data "Direct link to Put a contract on tables, columns, and data")

Use the `schema_contract` argument to tell dlt how to [deal with new tables, data types, and bad data types](https://dlthub.com/docs/general-usage/schema-contracts). For example, if you set it to **freeze**, `dlt` will not allow for any new tables, columns, or data types to be introduced to the schema - it will raise an exception. Learn more about available contract modes [here](https://dlthub.com/docs/general-usage/schema-contracts#setting-up-the-contract).

### Define schema of nested tables [​](https://dlthub.com/docs/general-usage/resource\#define-schema-of-nested-tables "Direct link to Define schema of nested tables")

`dlt` creates [nested tables](https://dlthub.com/docs/general-usage/schema#nested-references-root-and-nested-tables) to store [list of objects](https://dlthub.com/docs/general-usage/destination-tables#nested-tables) if present in your data.
You can define the schema of such tables with `nested_hints` argument to `@dlt.resource`:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(
    nested_hints={
        "purchases": dlt.mark.make_nested_hints(
            columns=[{"name": "price", "data_type": "decimal"}],
            schema_contract={"columns": "freeze"},
        )
    },
)
def customers():
    """Load customer data from a simple python list."""
    yield [\
        {\
            "id": 1,\
            "name": "simon",\
            "city": "berlin",\
            "purchases": [{"id": 1, "name": "apple", "price": "1.50"}],\
        },\
    ]

```

Here we convert the `price` field in list of `purchases` to decimal type and set the schema contract to lock the list
of columns in it. We use convenience function `dlt.mark.make_nested_hints` to generate nested hints dictionary. You are
free to use it directly.

Mind that `purchases` list will be stored as table with name `customers__purchases`. When declaring nested hints you just need
to specify nested field(s) name(s). In case of deeper nesting ie. let's say each `purchase` has a list of `coupons` applied,
you can apply hints to coupons and define `customers__purchases__coupons` table schema:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(
    nested_hints={
        "purchases": {},
        ("purchases", "coupons"): {
            "columns": {"registered_at": {"data_type": "timestamp"}}
        }
    },
)
def customers():
    ...

```

Here we use `("purchases", "coupons")` to locate list at the depth of 2 and set the data type on `registered_at` column
to `timestamp`. We do that by directly using nested hints dict.
Note that we specified `purchases` with an empty list of hints. **You are required to specify all parent hints, even if they**
**are empty. Currently we are not adding missing path elements automatically**.

You can use `nested_hints` primarily to set column hints and schema contract, those work exactly as in case of root tables.

- `file_format` has no effect (not implemented yet)
- `write_disposition` works as expected but leads to unintended consequences (ie. you can set nested table to `replace`) while root table is `append`.
- `references` will create [table references](https://dlthub.com/docs/general-usage/schema#table-references-1) (annotations) as expected.
- `primary_key` and `merge_key`: **setting those will convert nested table into a regular table, with a separate write disposition, file format etc.** [It allows you to create custom table relationships ie. using natural primary and foreign keys present in the data.](https://dlthub.com/docs/general-usage/schema#generate-custom-linking-for-nested-tables)

tip

[REST API Source](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic) accepts `nested_hints` argument as well.

You can apply nested hints after the resource was created by using [apply\_hints](https://dlthub.com/docs/general-usage/resource#set-table-name-and-adjust-schema).

### Define a schema with Pydantic [​](https://dlthub.com/docs/general-usage/resource\#define-a-schema-with-pydantic "Direct link to Define a schema with Pydantic")

You can alternatively use a [Pydantic](https://pydantic-docs.helpmanual.io/) model to define the schema.
For example:

```codeBlockLines_RjmQ
from pydantic import BaseModel
from typing import List, Optional, Union

class Address(BaseModel):
    street: str
    city: str
    postal_code: str

class User(BaseModel):
    id: int
    name: str
    tags: List[str]
    email: Optional[str]
    address: Address
    status: Union[int, str]

@dlt.resource(name="user", columns=User)
def get_users():
    ...

```

The data types of the table columns are inferred from the types of the Pydantic fields. These use the same type conversions
as when the schema is automatically generated from the data.

Pydantic models integrate well with [schema contracts](https://dlthub.com/docs/general-usage/schema-contracts) as data validators.

Things to note:

- Fields with an `Optional` type are marked as `nullable`.
- Fields with a `Union` type are converted to the first (not `None`) type listed in the union. For example, `status: Union[int, str]` results in a `bigint` column.
- `list`, `dict`, and nested Pydantic model fields will use the `json` type, which means they'll be stored as a JSON object in the database instead of creating nested tables.

You can override this by configuring the Pydantic model:

```codeBlockLines_RjmQ
from typing import ClassVar
from dlt.common.libs.pydantic import DltConfig

class UserWithNesting(User):
  dlt_config: ClassVar[DltConfig] = {"skip_nested_types": True}

@dlt.resource(name="user", columns=UserWithNesting)
def get_users():
    ...

```

`"skip_nested_types"` omits any `dict`/ `list`/ `BaseModel` type fields from the schema, so dlt will fall back on the default
behavior of creating nested tables for these fields.

We do not support `RootModel` that validate simple types. You can add such a validator yourself, see [data filtering section](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data).

### Dispatch data to many tables [​](https://dlthub.com/docs/general-usage/resource\#dispatch-data-to-many-tables "Direct link to Dispatch data to many tables")

You can load data to many tables from a single resource. The most common case is a stream of events
of different types, each with a different data schema. To deal with this, you can use the `table_name`
argument on `dlt.resource`. You could pass the table name as a function with the data item as an
argument and the `table_name` string as a return value.

For example, a resource that loads GitHub repository events wants to send `issue`, `pull request`,
and `comment` events to separate tables. The type of the event is in the "type" field.

```codeBlockLines_RjmQ
# send item to a table with name item["type"]
@dlt.resource(table_name=lambda event: event['type'])
def repo_events() -> Iterator[TDataItems]:
    yield item

# the `table_schema` method gets the table schema generated by a resource and takes an optional
# data item to evaluate dynamic hints
print(repo_events().compute_table_schema({"type": "WatchEvent", "id": ...}))

```

In more advanced cases, you can dispatch data to different tables directly in the code of the
resource function:

```codeBlockLines_RjmQ
@dlt.resource
def repo_events() -> Iterator[TDataItems]:
    # mark the "item" to be sent to the table with the name item["type"]
    yield dlt.mark.with_table_name(item, item["type"])

```

### Parametrize a resource [​](https://dlthub.com/docs/general-usage/resource\#parametrize-a-resource "Direct link to Parametrize a resource")

You can add arguments to your resource functions like to any other. Below we parametrize our
`generate_rows` resource to generate the number of rows we request:

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

for row in generate_rows(10):
    print(row)

for row in generate_rows(20):
    print(row)

```

tip

You can mark some resource arguments as [configuration and credentials](https://dlthub.com/docs/general-usage/credentials) values so `dlt` can pass them automatically to your functions.

### Process resources with `dlt.transformer` [​](https://dlthub.com/docs/general-usage/resource\#process-resources-with-dlttransformer "Direct link to process-resources-with-dlttransformer")

You can feed data from one resource into another. The most common case is when you have an API that returns a list of objects (i.e., users) in one endpoint and user details in another. You can deal with this by declaring a resource that obtains a list of users and another resource that receives items from the list and downloads the profiles.

```codeBlockLines_RjmQ
@dlt.resource(write_disposition="replace")
def users(limit=None):
    for u in _get_users(limit):
        yield u

# Feed data from users as user_item below,
# all transformers must have at least one
# argument that will receive data from the parent resource
@dlt.transformer(data_from=users)
def users_details(user_item):
    for detail in _get_details(user_item["user_id"]):
        yield detail

# Just load the users_details.
# dlt figures out dependencies for you.
pipeline.run(users_details)

```

In the example above, `users_details` will receive data from the default instance of the `users` resource (with `limit` set to `None`). You can also use the **pipe \|** operator to bind resources dynamically.

```codeBlockLines_RjmQ
# You can be more explicit and use a pipe operator.
# With it, you can create dynamic pipelines where the dependencies
# are set at run time and resources are parametrized, i.e.,
# below we want to load only 100 users from the `users` endpoint.
pipeline.run(users(limit=100) | users_details)

```

tip

Transformers are allowed not only to **yield** but also to **return** values and can decorate **async** functions and [**async generators**](https://dlthub.com/docs/reference/performance#extract). Below we decorate an async function and request details on two pokemons. HTTP calls are made in parallel via the httpx library.

```codeBlockLines_RjmQ
import dlt
import httpx

@dlt.transformer
async def pokemon(id):
    async with httpx.AsyncClient() as client:
        r = await client.get(f"https://pokeapi.co/api/v2/pokemon/{id}")
        return r.json()

# Get Bulbasaur and Ivysaur (you need dlt 0.4.6 for the pipe operator working with lists).
print(list([1,2] | pokemon()))

```

### Declare a standalone resource [​](https://dlthub.com/docs/general-usage/resource\#declare-a-standalone-resource "Direct link to Declare a standalone resource")

A standalone resource is defined on a function that is top-level in a module (not an inner function) that accepts config and secrets values. Additionally, if the `standalone` flag is specified, the decorated function signature and docstring will be preserved. `dlt.resource` will just wrap the decorated function, and the user must call the wrapper to get the actual resource. Below we declare a `filesystem` resource that must be called before use.

```codeBlockLines_RjmQ
@dlt.resource(standalone=True)
def fs_resource(bucket_url=dlt.config.value):
  """List and yield files in `bucket_url`."""
  ...

# `filesystem` must be called before it is extracted or used in any other way.
pipeline.run(fs_resource("s3://my-bucket/reports"), table_name="reports")

```

Standalone may have a dynamic name that depends on the arguments passed to the decorated function. For example:

```codeBlockLines_RjmQ
@dlt.resource(standalone=True, name=lambda args: args["stream_name"])
def kinesis(stream_name: str):
    ...

kinesis_stream = kinesis("telemetry_stream")

```

`kinesis_stream` resource has a name **telemetry\_stream**.

### Declare parallel and async resources [​](https://dlthub.com/docs/general-usage/resource\#declare-parallel-and-async-resources "Direct link to Declare parallel and async resources")

You can extract multiple resources in parallel threads or with async IO.
To enable this for a sync resource, you can set the `parallelized` flag to `True` in the resource decorator:

```codeBlockLines_RjmQ
@dlt.resource(parallelized=True)
def get_users():
    for u in _get_users():
        yield u

@dlt.resource(parallelized=True)
def get_orders():
    for o in _get_orders():
        yield o

# users and orders will be iterated in parallel in two separate threads
pipeline.run([get_users(), get_orders()])

```

Async generators are automatically extracted concurrently with other resources:

```codeBlockLines_RjmQ
@dlt.resource
async def get_users():
    async for u in _get_users():  # Assuming _get_users is an async generator
        yield u

```

Please find more details in [extract performance](https://dlthub.com/docs/reference/performance#extract)

## Customize resources [​](https://dlthub.com/docs/general-usage/resource\#customize-resources "Direct link to Customize resources")

### Filter, transform, and pivot data [​](https://dlthub.com/docs/general-usage/resource\#filter-transform-and-pivot-data "Direct link to Filter, transform, and pivot data")

You can attach any number of transformations that are evaluated on an item-per-item basis to your
resource. The available transformation types:

- **map** \- transform the data item ( `resource.add_map`).
- **filter** \- filter the data item ( `resource.add_filter`).
- **yield map** \- a map that returns an iterator (so a single row may generate many rows -
`resource.add_yield_map`).

Example: We have a resource that loads a list of users from an API endpoint. We want to customize it
so:

1. We remove users with `user_id == "me"`.
2. We anonymize user data.

Here's our resource:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(write_disposition="replace")
def users():
    ...
    users = requests.get(RESOURCE_URL)
    ...
    yield users

```

Here's our script that defines transformations and loads the data:

```codeBlockLines_RjmQ
from pipedrive import users

def anonymize_user(user_data):
    user_data["user_id"] = _hash_str(user_data["user_id"])
    user_data["user_email"] = _hash_str(user_data["user_email"])
    return user_data

# add the filter and anonymize function to users resource and enumerate
for user in users().add_filter(lambda user: user["user_id"] != "me").add_map(anonymize_user):
    print(user)

```

### Reduce the nesting level of generated tables [​](https://dlthub.com/docs/general-usage/resource\#reduce-the-nesting-level-of-generated-tables "Direct link to Reduce the nesting level of generated tables")

You can limit how deep `dlt` goes when generating nested tables and flattening dicts into columns. By default, the library will descend
and generate nested tables for all nested lists, without limit.

note

`max_table_nesting` is optional so you can skip it, in this case, dlt will
use it from the source if it is specified there or fallback to the default
value which has 1000 as the maximum nesting level.

```codeBlockLines_RjmQ
import dlt

@dlt.resource(max_table_nesting=1)
def my_resource():
    yield {
        "id": 1,
        "name": "random name",
        "properties": [\
            {\
                "name": "customer_age",\
                "type": "int",\
                "label": "Age",\
                "notes": [\
                    {\
                        "text": "string",\
                        "author": "string",\
                    }\
                ]\
            }\
        ]
    }

```

In the example above, we want only 1 level of nested tables to be generated (so there are no nested
tables of a nested table). Typical settings:

- `max_table_nesting=0` will not generate nested tables and will not flatten dicts into columns at all. All nested data will be
represented as JSON.
- `max_table_nesting=1` will generate nested tables of root tables and nothing more. All nested
data in nested tables will be represented as JSON.

You can achieve the same effect after the resource instance is created:

```codeBlockLines_RjmQ
resource = my_resource()
resource.max_table_nesting = 0

```

Several data sources are prone to contain semi-structured documents with very deep nesting, i.e.,
MongoDB databases. Our practical experience is that setting the `max_nesting_level` to 2 or 3
produces the clearest and human-readable schemas.

### Sample from large data [​](https://dlthub.com/docs/general-usage/resource\#sample-from-large-data "Direct link to Sample from large data")

If your resource loads thousands of pages of data from a REST API or millions of rows from a database table, you may want to sample just a fragment of it in order to quickly see the dataset with example data and test your transformations, etc. To do this, you limit how many items will be yielded by a resource (or source) by calling the `add_limit` method. This method will close the generator that produces the data after the limit is reached.

In the example below, we load just the first 10 items from an infinite counter - that would otherwise never end.

```codeBlockLines_RjmQ
r = dlt.resource(itertools.count(), name="infinity").add_limit(10)
assert list(r) == list(range(10))

```

note

Note that `add_limit` **does not limit the number of records** but rather the "number of yields". Depending on how your resource is set up, the number of extracted rows may vary. For example, consider this resource:

```codeBlockLines_RjmQ
@dlt.resource
def my_resource():
    for i in range(100):
        yield [{"record_id": j} for j in range(15)]

dlt.pipeline(destination="duckdb").run(my_resource().add_limit(10))

```

The code above will extract `15*10=150` records. This is happening because in each iteration, 15 records are yielded, and we're limiting the number of iterations to 10.

Altenatively you can also apply a time limit to the resource. The code below will run the extraction for 10 seconds and extract how ever many items are yielded in that time. In combination with incrementals, this can be useful for batched loading or for loading on machines that have a run time limit.

```codeBlockLines_RjmQ
dlt.pipeline(destination="duckdb").run(my_resource().add_limit(max_time=10))

```

You can also apply a combination of both limits. In this case the extraction will stop as soon as either limit is reached.

```codeBlockLines_RjmQ
dlt.pipeline(destination="duckdb").run(my_resource().add_limit(max_items=10, max_time=10))

```

Some notes about the `add_limit`:

1. `add_limit` does not skip any items. It closes the iterator/generator that produces data after the limit is reached.
2. You cannot limit transformers. They should process all the data they receive fully to avoid inconsistencies in generated datasets.
3. Async resources with a limit added may occasionally produce one item more than the limit on some runs. This behavior is not deterministic.
4. Calling add limit on a resource will replace any previously set limits settings.
5. For time-limited resources, the timer starts when the first item is processed. When resources are processed sequentially (FIFO mode), each resource's time limit applies also sequentially. In the default round robin mode, the time limits will usually run concurrently.

tip

If you are parameterizing the value of `add_limit` and sometimes need it to be disabled, you can set `None` or `-1` to disable the limiting.
You can also set the limit to `0` for the resource to not yield any items.

### Set table name and adjust schema [​](https://dlthub.com/docs/general-usage/resource\#set-table-name-and-adjust-schema "Direct link to Set table name and adjust schema")

You can change the schema of a resource, whether it is standalone or part of a source. Look for a method named `apply_hints` which takes the same arguments as the resource decorator. Obviously, you should call this method before data is extracted from the resource. The example below converts an `append` resource loading the `users` table into a [merge](https://dlthub.com/docs/general-usage/merge-loading) resource that will keep just one updated record per `user_id`. It also adds ["last value" incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor) on the `created_at` column to prevent requesting again the already loaded records:

```codeBlockLines_RjmQ
tables = sql_database()
tables.users.apply_hints(
    write_disposition="merge",
    primary_key="user_id",
    incremental=dlt.sources.incremental("updated_at")
)
pipeline.run(tables)

```

To change the name of a table to which the resource will load data, do the following:

```codeBlockLines_RjmQ
tables = sql_database()
tables.users.table_name = "other_users"

```

### Adjust schema when you yield data [​](https://dlthub.com/docs/general-usage/resource\#adjust-schema-when-you-yield-data "Direct link to Adjust schema when you yield data")

You can set or update the table name, columns, and other schema elements when your resource is executed, and you already yield data. Such changes will be merged with the existing schema in the same way the `apply_hints` method above works. There are many reasons to adjust the schema at runtime. For example, when using Airflow, you should avoid lengthy operations (i.e., reflecting database tables) during the creation of the DAG, so it is better to do it when the DAG executes. You may also emit partial hints (i.e., precision and scale for decimal types) for columns to help `dlt` type inference.

```codeBlockLines_RjmQ
@dlt.resource
def sql_table(credentials, schema, table):
    # Create a SQL Alchemy engine
    engine = engine_from_credentials(credentials)
    engine.execution_options(stream_results=True)
    metadata = MetaData(schema=schema)
    # Reflect the table schema
    table_obj = Table(table, metadata, autoload_with=engine)

    for idx, batch in enumerate(table_rows(engine, table_obj)):
      if idx == 0:
        # Emit the first row with hints, table_to_columns and _get_primary_key are helpers that extract dlt schema from
        # SqlAlchemy model
        yield dlt.mark.with_hints(
            batch,
            dlt.mark.make_hints(columns=table_to_columns(table_obj), primary_key=_get_primary_key(table_obj)),
        )
      else:
        # Just yield all the other rows
        yield batch

```

In the example above, we use `dlt.mark.with_hints` and `dlt.mark.make_hints` to emit columns and primary key with the first extracted item. The table schema will be adjusted after the `batch` is processed in the extract pipeline but before any schema contracts are applied, and data is persisted in the load package.

tip

You can emit columns as a Pydantic model and use dynamic hints (i.e., lambda for table name) as well. You should avoid redefining `Incremental` this way.

### Import external files [​](https://dlthub.com/docs/general-usage/resource\#import-external-files "Direct link to Import external files")

You can import external files, i.e., CSV, Parquet, and JSONL, by yielding items marked with `with_file_import`, optionally passing a table schema corresponding to the imported file. dlt will not read, parse, or normalize any names (i.e., CSV or Arrow headers) and will attempt to copy the file into the destination as is.

```codeBlockLines_RjmQ
import os
import dlt
from dlt.sources.filesystem import filesystem

columns: List[TColumnSchema] = [\
    {"name": "id", "data_type": "bigint"},\
    {"name": "name", "data_type": "text"},\
    {"name": "description", "data_type": "text"},\
    {"name": "ordered_at", "data_type": "date"},\
    {"name": "price", "data_type": "decimal"},\
]

import_folder = "/tmp/import"

@dlt.transformer(columns=columns)
def orders(items: Iterator[FileItemDict]):
  for item in items:
    # copy the file locally
      dest_file = os.path.join(import_folder, item["file_name"])
      # download the file
      item.fsspec.download(item["file_url"], dest_file)
      # tell dlt to import the dest_file as `csv`
      yield dlt.mark.with_file_import(dest_file, "csv")

# use the filesystem core source to glob a bucket

downloader = filesystem(
  bucket_url="s3://my_bucket/csv",
  file_glob="today/*.csv.gz") | orders

info = pipeline.run(orders, destination="snowflake")

```

In the example above, we glob all zipped csv files present on **my\_bucket/csv/today** (using the `filesystem` verified source) and send file descriptors to the `orders` transformer. The transformer downloads and imports the files into the extract package. At the end, `dlt` sends them to Snowflake (the table will be created because we use `column` hints to define the schema).

If imported `csv` files are not in `dlt` [default format](https://dlthub.com/docs/dlt-ecosystem/file-formats/csv#default-settings), you may need to pass additional configuration.

```codeBlockLines_RjmQ
[destination.snowflake.csv_format]
delimiter="|"
include_header=false
on_error_continue=true

```

You can sniff the schema from the data, i.e., using DuckDB to infer the table schema from a CSV file. `dlt.mark.with_file_import` accepts additional arguments that you can use to pass hints at runtime.

note

- If you do not define any columns, the table will not be created in the destination. `dlt` will still attempt to load data into it, so if you create a fitting table upfront, the load process will succeed.
- Files are imported using hard links if possible to avoid copying and duplicating the storage space needed.

### Duplicate and rename resources [​](https://dlthub.com/docs/general-usage/resource\#duplicate-and-rename-resources "Direct link to Duplicate and rename resources")

There are cases when your resources are generic (i.e., bucket filesystem) and you want to load several instances of it (i.e., files from different folders) into separate tables. In the example below, we use the `filesystem` source to load csvs from two different folders into separate tables:

```codeBlockLines_RjmQ
@dlt.resource(standalone=True)
def fs_resource(bucket_url):
  # list and yield files in bucket_url
  ...

@dlt.transformer
def csv_reader(file_item):
  # load csv, parse, and yield rows in file_item
  ...

# create two extract pipes that list files from the bucket and send them to the reader.
# by default, both pipes will load data to the same table (csv_reader)
reports_pipe = fs_resource("s3://my-bucket/reports") | csv_reader()
transactions_pipe = fs_resource("s3://my-bucket/transactions") | csv_reader()

# so we rename resources to load to "reports" and "transactions" tables
pipeline.run(
  [reports_pipe.with_name("reports"), transactions_pipe.with_name("transactions")]
)

```

The `with_name` method returns a deep copy of the original resource, its data pipe, and the data pipes of a parent resource. A renamed clone is fully separated from the original resource (and other clones) when loading: it maintains a separate [resource state](https://dlthub.com/docs/general-usage/state#read-and-write-pipeline-state-in-a-resource) and will load to a table.

## Load resources [​](https://dlthub.com/docs/general-usage/resource\#load-resources "Direct link to Load resources")

You can pass individual resources or a list of resources to the `dlt.pipeline` object. The resources loaded outside the source context will be added to the [default schema](https://dlthub.com/docs/general-usage/schema) of the pipeline.

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

pipeline = dlt.pipeline(
    pipeline_name="rows_pipeline",
    destination="duckdb",
    dataset_name="rows_data"
)
# load an individual resource
pipeline.run(generate_rows(10))
# load a list of resources
pipeline.run([generate_rows(10), generate_rows(20)])

```

### Pick loader file format for a particular resource [​](https://dlthub.com/docs/general-usage/resource\#pick-loader-file-format-for-a-particular-resource "Direct link to Pick loader file format for a particular resource")

You can request a particular loader file format to be used for a resource.

```codeBlockLines_RjmQ
@dlt.resource(file_format="parquet")
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

```

The resource above will be saved and loaded from a Parquet file (if the destination supports it).

note

A special `file_format`: **preferred** will load the resource using a format that is preferred by a destination. This setting supersedes the `loader_file_format` passed to the `run` method.

### Do a full refresh [​](https://dlthub.com/docs/general-usage/resource\#do-a-full-refresh "Direct link to Do a full refresh")

To do a full refresh of an `append` or `merge` resource, you set the `refresh` argument on the `run` method to `drop_data`. This will truncate the tables without dropping them.

```codeBlockLines_RjmQ
p.run(merge_source(), refresh="drop_data")

```

You can also [fully drop the tables](https://dlthub.com/docs/general-usage/pipeline#refresh-pipeline-data-and-state) in the `merge_source`:

```codeBlockLines_RjmQ
p.run(merge_source(), refresh="drop_sources")

```

- [Declare a resource](https://dlthub.com/docs/general-usage/resource#declare-a-resource)
  - [Define schema](https://dlthub.com/docs/general-usage/resource#define-schema)
  - [Put a contract on tables, columns, and data](https://dlthub.com/docs/general-usage/resource#put-a-contract-on-tables-columns-and-data)
  - [Define schema of nested tables](https://dlthub.com/docs/general-usage/resource#define-schema-of-nested-tables)
  - [Define a schema with Pydantic](https://dlthub.com/docs/general-usage/resource#define-a-schema-with-pydantic)
  - [Dispatch data to many tables](https://dlthub.com/docs/general-usage/resource#dispatch-data-to-many-tables)
  - [Parametrize a resource](https://dlthub.com/docs/general-usage/resource#parametrize-a-resource)
  - [Process resources with `dlt.transformer`](https://dlthub.com/docs/general-usage/resource#process-resources-with-dlttransformer)
  - [Declare a standalone resource](https://dlthub.com/docs/general-usage/resource#declare-a-standalone-resource)
  - [Declare parallel and async resources](https://dlthub.com/docs/general-usage/resource#declare-parallel-and-async-resources)
- [Customize resources](https://dlthub.com/docs/general-usage/resource#customize-resources)
  - [Filter, transform, and pivot data](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data)
  - [Reduce the nesting level of generated tables](https://dlthub.com/docs/general-usage/resource#reduce-the-nesting-level-of-generated-tables)
  - [Sample from large data](https://dlthub.com/docs/general-usage/resource#sample-from-large-data)
  - [Set table name and adjust schema](https://dlthub.com/docs/general-usage/resource#set-table-name-and-adjust-schema)
  - [Adjust schema when you yield data](https://dlthub.com/docs/general-usage/resource#adjust-schema-when-you-yield-data)
  - [Import external files](https://dlthub.com/docs/general-usage/resource#import-external-files)
  - [Duplicate and rename resources](https://dlthub.com/docs/general-usage/resource#duplicate-and-rename-resources)
- [Load resources](https://dlthub.com/docs/general-usage/resource#load-resources)
  - [Pick loader file format for a particular resource](https://dlthub.com/docs/general-usage/resource#pick-loader-file-format-for-a-particular-resource)
  - [Do a full refresh](https://dlthub.com/docs/general-usage/resource#do-a-full-refresh)

[iframe](https://

----- https://dlthub.com/docs/general-usage/incremental/cursor#using-airflow-schedule-for-backfill-and-incremental-loading -----

Version: 1.11.0 (latest)

On this page

In most REST APIs (and other data sources, i.e., database tables), you can request new or updated data by passing a timestamp or ID of the "last" record to a query. The API/database returns just the new/updated records from which you take the maximum/minimum timestamp/ID for the next load.

To do incremental loading this way, we need to:

- Figure out which field is used to track changes (the so-called **cursor field**) (e.g., "inserted\_at", "updated\_at", etc.);
- Determine how to pass the "last" (maximum/minimum) value of the cursor field to an API to get just new or modified data (how we do this depends on the source API).

Once you've figured that out, `dlt` takes care of finding maximum/minimum cursor field values, removing duplicates, and managing the state with the last values of the cursor. Take a look at the GitHub example below, where we request recently created issues.

```codeBlockLines_RjmQ
@dlt.resource(primary_key="id")
def repo_issues(
    access_token,
    repository,
    updated_at = dlt.sources.incremental("updated_at", initial_value="1970-01-01T00:00:00Z")
):
    # Get issues since "updated_at" stored in state on previous run (or initial_value on first run)
    for page in _get_issues_page(access_token, repository, since=updated_at.start_value):
        yield page
        # Last_value is updated after every page
        print(updated_at.last_value)

```

Here we add an `updated_at` argument that will receive incremental state, initialized to `1970-01-01T00:00:00Z`. It is configured to track the `updated_at` field in issues yielded by the `repo_issues` resource. It will store the newest `updated_at` value in `dlt` [state](https://dlthub.com/docs/general-usage/state) and make it available in `updated_at.start_value` on the next pipeline run. This value is inserted in the `_get_issues_page` function into the request query param **since** to the [GitHub API](https://docs.github.com/en/rest/issues/issues?#list-repository-issues).

In essence, the `dlt.sources.incremental` instance above:

- **updated\_at.initial\_value** which is always equal to "1970-01-01T00:00:00Z" passed in the constructor
- **updated\_at.start\_value** a maximum `updated_at` value from the previous run or the **initial\_value** on the first run
- **updated\_at.last\_value** a "real-time" `updated_at` value updated with each yielded item or page. Before the first yield, it equals **start\_value**
- **updated\_at.end\_value** (here not used) [marking the end of the backfill range](https://dlthub.com/docs/general-usage/incremental/cursor#using-end_value-for-backfill)

When paginating, you probably need the **start\_value** which does not change during the execution of the resource, however, most paginators will return a **next page** link which you should use.

Behind the scenes, dlt will deduplicate the results, i.e., in case the last issue is returned again ( `updated_at` filter is inclusive) and skip already loaded ones.

In the example below, we incrementally load the GitHub events, where the API does not let us filter for the newest events - it always returns all of them. Nevertheless, `dlt` will load only the new items, filtering out all the duplicates and past issues.

```codeBlockLines_RjmQ
# Use naming function in table name to generate separate tables for each event
@dlt.resource(primary_key="id", table_name=lambda i: i['type'])  # type: ignore
def repo_events(
    last_created_at = dlt.sources.incremental("created_at", initial_value="1970-01-01T00:00:00Z", last_value_func=max), row_order="desc"
) -> Iterator[TDataItems]:
    repos_path = "/repos/%s/%s/events" % (urllib.parse.quote(owner), urllib.parse.quote(name))
    for page in _get_rest_pages(access_token, repos_path + "?per_page=100"):
        yield page

```

We just yield all the events and `dlt` does the filtering (using the `id` column declared as `primary_key`).

GitHub returns events ordered from newest to oldest. So we declare the `rows_order` as **descending** to [stop requesting more pages once the incremental value is out of range](https://dlthub.com/docs/general-usage/incremental/cursor#declare-row-order-to-not-request-unnecessary-data). We stop requesting more data from the API after finding the first event with `created_at` earlier than `initial_value`.

note

`dlt.sources.incremental` is implemented as a [filter function](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data) that is executed **after** all other transforms you add with `add_map` or `add_filter`. This means that you can manipulate the data item before the incremental filter sees it. For example:

- You can create a surrogate primary key from other columns
- You can modify the cursor value or create a new field composed of other fields
- Dump Pydantic models to Python dicts to allow incremental to find custom values

[Data validation with Pydantic](https://dlthub.com/docs/general-usage/schema-contracts#use-pydantic-models-for-data-validation) happens **before** incremental filtering.

## Max, min, or custom `last_value_func` [​](https://dlthub.com/docs/general-usage/incremental/cursor\#max-min-or-custom-last_value_func "Direct link to max-min-or-custom-last_value_func")

`dlt.sources.incremental` allows you to choose a function that orders (compares) cursor values to the current `last_value`.

- The default function is the built-in `max`, which returns the larger value of the two.
- Another built-in, `min`, returns the smaller value.

You can also pass your custom function. This lets you define
`last_value` on nested types, i.e., dictionaries, and store indexes of last values, not just simple
types. The `last_value` argument is a [JSON Path](https://github.com/json-path/JsonPath#operators)
and lets you select nested data (including the whole data item when `$` is used).
The example below creates a last value which is a dictionary holding a max `created_at` value for each
created table name:

```codeBlockLines_RjmQ
def by_event_type(event):
    last_value = None
    if len(event) == 1:
        item, = event
    else:
        item, last_value = event

    if last_value is None:
        last_value = {}
    else:
        last_value = dict(last_value)
    item_type = item["type"]
    last_value[item_type] = max(item["created_at"], last_value.get(item_type, "1970-01-01T00:00:00Z"))
    return last_value

@dlt.resource(primary_key="id", table_name=lambda i: i['type'])
def get_events(last_created_at = dlt.sources.incremental("$", last_value_func=by_event_type)):
    with open("tests/normalize/cases/github.events.load_page_1_duck.json", "r", encoding="utf-8") as f:
        yield json.load(f)

```

## Using `end_value` for backfill [​](https://dlthub.com/docs/general-usage/incremental/cursor\#using-end_value-for-backfill "Direct link to using-end_value-for-backfill")

You can specify both initial and end dates when defining incremental loading. Let's go back to our Github example:

```codeBlockLines_RjmQ
@dlt.resource(primary_key="id")
def repo_issues(
    access_token,
    repository,
    created_at=dlt.sources.incremental("created_at", initial_value="1970-01-01T00:00:00Z", end_value="2022-07-01T00:00:00Z")
):
    # get issues created from the last "created_at" value
    for page in _get_issues_page(access_token, repository, since=created_at.start_value, until=created_at.end_value):
        yield page

```

Above, we use the `initial_value` and `end_value` arguments of the `incremental` to define the range of issues that we want to retrieve
and pass this range to the Github API ( `since` and `until`). As in the examples above, `dlt` will make sure that only the issues from
the defined range are returned.

Please note that when `end_date` is specified, `dlt` **will not modify the existing incremental state**. The backfill is **stateless** and:

1. You can run backfill and incremental load in parallel (i.e., in an Airflow DAG) in a single pipeline.
2. You can partition your backfill into several smaller chunks and run them in parallel as well.

To define specific ranges to load, you can simply override the incremental argument in the resource, for example:

```codeBlockLines_RjmQ
july_issues = repo_issues(
    created_at=dlt.sources.incremental(
        initial_value='2022-07-01T00:00:00Z', end_value='2022-08-01T00:00:00Z'
    )
)
august_issues = repo_issues(
    created_at=dlt.sources.incremental(
        initial_value='2022-08-01T00:00:00Z', end_value='2022-09-01T00:00:00Z'
    )
)
...

```

Note that dlt's incremental filtering considers the ranges half-closed. `initial_value` is inclusive, `end_value` is exclusive, so chaining ranges like above works without overlaps. This behaviour can be changed with the `range_start` (default `"closed"`) and `range_end` (default `"open"`) arguments.

## Declare row order to not request unnecessary data [​](https://dlthub.com/docs/general-usage/incremental/cursor\#declare-row-order-to-not-request-unnecessary-data "Direct link to Declare row order to not request unnecessary data")

With the `row_order` argument set, dlt will stop retrieving data from the data source (e.g., GitHub API) if it detects that the values of the cursor field are out of the range of **start** and **end** values.

In particular:

- dlt stops processing when the resource yields any item with a cursor value _equal to or greater than_ the `end_value` and `row_order` is set to **asc**. ( `end_value` is not included)
- dlt stops processing when the resource yields any item with a cursor value _lower_ than the `last_value` and `row_order` is set to **desc**. ( `last_value` is included)

note

"higher" and "lower" here refer to when the default `last_value_func` is used ( `max()`),
when using `min()` "higher" and "lower" are inverted.

caution

If you use `row_order`, **make sure that the data source returns ordered records** (ascending / descending) on the cursor field,
e.g., if an API returns results both higher and lower
than the given `end_value` in no particular order, data reading stops and you'll miss the data items that were out of order.

Row order is most useful when:

1. The data source does **not** offer start/end filtering of results (e.g., there is no `start_time/end_time` query parameter or similar).
2. The source returns results **ordered by the cursor field**.

The GitHub events example is exactly such a case. The results are ordered on cursor value descending, but there's no way to tell the API to limit returned items to those created before a certain date. Without the `row_order` setting, we'd be getting all events, each time we extract the `github_events` resource.

In the same fashion, the `row_order` can be used to **optimize backfill** so we don't continue
making unnecessary API requests after the end of the range is reached. For example:

```codeBlockLines_RjmQ
@dlt.resource(primary_key="id")
def tickets(
    zendesk_client,
    updated_at=dlt.sources.incremental(
        "updated_at",
        initial_value="2023-01-01T00:00:00Z",
        end_value="2023-02-01T00:00:00Z",
        row_order="asc"
    ),
):
    for page in zendesk_client.get_pages(
        "/api/v2/incremental/tickets", "tickets", start_time=updated_at.start_value
    ):
        yield page

```

In this example, we're loading tickets from Zendesk. The Zendesk API yields items paginated and ordered from oldest to newest,
but only offers a `start_time` parameter for filtering, so we cannot tell it to
stop retrieving data at `end_value`. Instead, we set `row_order` to `asc` and `dlt` will stop
getting more pages from the API after the first page with a cursor value `updated_at` is found older
than `end_value`.

caution

In rare cases when you use Incremental with a transformer, `dlt` will not be able to automatically close
the generator associated with a row that is out of range. You can still call the `can_close()` method on
incremental and exit the yield loop when true.

tip

The `dlt.sources.incremental` instance provides `start_out_of_range` and `end_out_of_range`
attributes which are set when the resource yields an element with a higher/lower cursor value than the
initial or end values. If you do not want `dlt` to stop processing automatically and instead want to handle such events yourself, do not specify `row_order`:

```codeBlockLines_RjmQ
@dlt.transformer(primary_key="id")
def tickets(
    zendesk_client,
    updated_at=dlt.sources.incremental(
        "updated_at",
        initial_value="2023-01-01T00:00:00Z",
        end_value="2023-02-01T00:00:00Z",
        row_order="asc"
    ),
):
    for page in zendesk_client.get_pages(
        "/api/v2/incremental/tickets", "tickets", start_time=updated_at.start_value
    ):
        yield page
        # Stop loading when we reach the end value
        if updated_at.end_out_of_range:
            return

```

## Deduplicate overlapping ranges with primary key [​](https://dlthub.com/docs/general-usage/incremental/cursor\#deduplicate-overlapping-ranges-with-primary-key "Direct link to Deduplicate overlapping ranges with primary key")

`Incremental` **does not** deduplicate datasets like the **merge** write disposition does. However, it ensures that when another portion of data is extracted, records that were previously loaded won't be included again. `dlt` assumes that you load a range of data, where the lower bound is inclusive (i.e., greater than or equal). This ensures that you never lose any data but will also re-acquire some rows. For example, if you have a database table with a cursor field on `updated_at` which has a day resolution, then there's a high chance that after you extract data on a given day, more records will still be added. When you extract on the next day, you should reacquire data from the last day to ensure all records are present; however, this will create overlap with data from the previous extract.

By default, a content hash (a hash of the JSON representation of a row) will be used to deduplicate. This may be slow, so `dlt.sources.incremental` will inherit the primary key that is set on the resource. You can optionally set a `primary_key` that is used exclusively to deduplicate and which does not become a table hint. The same setting lets you disable the deduplication altogether when an empty tuple is passed. Below, we pass `primary_key` directly to `incremental` to disable deduplication. That overrides the `delta` primary\_key set in the resource:

```codeBlockLines_RjmQ
@dlt.resource(primary_key="delta")
# disable the unique value check by passing () as primary key to incremental
def some_data(last_timestamp=dlt.sources.incremental("item.ts", primary_key=())):
    for i in range(-10, 10):
        yield {"delta": i, "item": {"ts": pendulum.now().timestamp()}}

```

This deduplication process is always enabled when `range_start` is set to `"closed"` (default).
When you pass `range_start="open"` no deduplication is done as it is not needed as rows with the previous cursor value are excluded. This can be a useful optimization to avoid the performance overhead of deduplication if the cursor field is guaranteed to be unique.

## Using `dlt.sources.incremental` with dynamically created resources [​](https://dlthub.com/docs/general-usage/incremental/cursor\#using-dltsourcesincremental-with-dynamically-created-resources "Direct link to using-dltsourcesincremental-with-dynamically-created-resources")

When resources are [created dynamically](https://dlthub.com/docs/general-usage/source#create-resources-dynamically), it is possible to use the `dlt.sources.incremental` definition as well.

```codeBlockLines_RjmQ
@dlt.source
def stripe():
    # declare a generator function
    def get_resource(
        endpoints: List[str] = ENDPOINTS,
        created: dlt.sources.incremental=dlt.sources.incremental("created")
    ):
        ...

    # create resources for several endpoints on a single decorator function
    for endpoint in endpoints:
        yield dlt.resource(
            get_resource,
            name=endpoint.value,
            write_disposition="merge",
            primary_key="id"
        )(endpoint)

```

Please note that in the example above, `get_resource` is passed as a function to `dlt.resource` to which we bind the endpoint: **dlt.resource(...)(endpoint)**.

caution

The typical mistake is to pass a generator (not a function) as below:

`yield dlt.resource(get_resource(endpoint), name=endpoint.value, write_disposition="merge", primary_key="id")`.

Here we call **get\_resource(endpoint)** and that creates an un-evaluated generator on which the resource is created. That prevents `dlt` from controlling the **created** argument during runtime and will result in an `IncrementalUnboundError` exception.

## Using Airflow schedule for backfill and incremental loading [​](https://dlthub.com/docs/general-usage/incremental/cursor\#using-airflow-schedule-for-backfill-and-incremental-loading "Direct link to Using Airflow schedule for backfill and incremental loading")

When [running an Airflow task](https://dlthub.com/docs/walkthroughs/deploy-a-pipeline/deploy-with-airflow-composer#2-modify-dag-file), you can opt-in your resource to get the `initial_value`/ `start_value` and `end_value` from the Airflow schedule associated with your DAG. Let's assume that the **Zendesk tickets** resource contains a year of data with thousands of tickets. We want to backfill the last year of data week by week and then continue with incremental loading daily.

```codeBlockLines_RjmQ
@dlt.resource(primary_key="id")
def tickets(
    zendesk_client,
    updated_at=dlt.sources.incremental[int](
        "updated_at",
        allow_external_schedulers=True
    ),
):
    for page in zendesk_client.get_pages(
        "/api/v2/incremental/tickets", "tickets", start_time=updated_at.start_value
    ):
        yield page

```

We opt-in to the Airflow scheduler by setting `allow_external_schedulers` to `True`:

1. When running on Airflow, the start and end values are controlled by Airflow and the dlt [state](https://dlthub.com/docs/general-usage/state) is not used.
2. In all other environments, the `incremental` behaves as usual, maintaining the dlt state.

Let's generate a deployment with `dlt deploy zendesk_pipeline.py airflow-composer` and customize the DAG:

```codeBlockLines_RjmQ
from dlt.helpers.airflow_helper import PipelineTasksGroup

@dag(
    schedule_interval='@weekly',
    start_date=pendulum.DateTime(2023, 2, 1),
    end_date=pendulum.DateTime(2023, 8, 1),
    catchup=True,
    max_active_runs=1,
    default_args=default_task_args
)
def zendesk_backfill_bigquery():
    tasks = PipelineTasksGroup("zendesk_support_backfill", use_data_folder=False, wipe_local_data=True)

    # import zendesk like in the demo script
    from zendesk import zendesk_support

    pipeline = dlt.pipeline(
        pipeline_name="zendesk_support_backfill",
        dataset_name="zendesk_support_data",
        destination='bigquery',
    )
    # select only incremental endpoints in support api
    data = zendesk_support().with_resources("tickets", "ticket_events", "ticket_metric_events")
    # create the source, the "serialize" decompose option will convert dlt resources into Airflow tasks. use "none" to disable it
    tasks.add_run(pipeline, data, decompose="serialize", trigger_rule="all_done", retries=0, provide_context=True)

zendesk_backfill_bigquery()

```

What got customized:

1. We use a weekly schedule and want to get the data from February 2023 ( `start_date`) until the end of July ( `end_date`).
2. We make Airflow generate all weekly runs ( `catchup` is True).
3. We create `zendesk_support` resources where we select only the incremental resources we want to backfill.

When you enable the DAG in Airflow, it will generate several runs and start executing them, starting in February and ending in August. Your resource will receive subsequent weekly intervals starting with `2023-02-12, 00:00:00 UTC` to `2023-02-19, 00:00:00 UTC`.

You can repurpose the DAG above to start loading new data incrementally after (or during) the backfill:

```codeBlockLines_RjmQ
@dag(
    schedule_interval='@daily',
    start_date=pendulum.DateTime(2023, 2, 1),
    catchup=False,
    max_active_runs=1,
    default_args=default_task_args
)
def zendesk_new_bigquery():
    tasks = PipelineTasksGroup("zendesk_support_new", use_data_folder=False, wipe_local_data=True)

    # import your source from pipeline script
    from zendesk import zendesk_support

    pipeline = dlt.pipeline(
        pipeline_name="zendesk_support_new",
        dataset_name="zendesk_support_data",
        destination='bigquery',
    )
    tasks.add_run(pipeline, zendesk_support(), decompose="serialize", trigger_rule="all_done", retries=0, provide_context=True)

```

Above, we switch to a daily schedule and disable catchup and end date. We also load all the support resources to the same dataset as backfill ( `zendesk_support_data`).
If you want to run this DAG parallel with the backfill DAG, change the pipeline name, for example, to `zendesk_support_new` as above.

**Under the hood**

Before `dlt` starts executing incremental resources, it looks for `data_interval_start` and `data_interval_end` Airflow task context variables. These are mapped to `initial_value` and `end_value` of the `Incremental` class:

1. `dlt` is smart enough to convert Airflow datetime to ISO strings or Unix timestamps if your resource is using them. In our example, we instantiate `updated_at=dlt.sources.incremental[int]`, where we declare the last value type to be **int**. `dlt` can also infer the type if you provide the `initial_value` argument.
2. If `data_interval_end` is in the future or is None, `dlt` sets the `end_value` to **now**.
3. If `data_interval_start` == `data_interval_end`, we have a manually triggered DAG run. In that case, `data_interval_end` will also be set to **now**.

**Manual runs**

You can run DAGs manually, but you must remember to specify the Airflow logical date of the run in the past (use the Run with config option). For such a run, `dlt` will load all data from that past date until now.
If you do not specify the past date, a run with a range (now, now) will happen, yielding no data.

## Reading incremental loading parameters from configuration [​](https://dlthub.com/docs/general-usage/incremental/cursor\#reading-incremental-loading-parameters-from-configuration "Direct link to Reading incremental loading parameters from configuration")

Consider the example below for reading incremental loading parameters from "config.toml". We create a `generate_incremental_records` resource that yields "id", "idAfter", and "name". This resource retrieves `cursor_path` and `initial_value` from "config.toml".

1. In "config.toml", define the `cursor_path` and `initial_value` as:

```codeBlockLines_RjmQ
# Configuration snippet for an incremental resource
[pipeline_with_incremental.sources.id_after]
cursor_path = "idAfter"
initial_value = 10

```

`cursor_path` is assigned the value "idAfter" with an initial value of 10.

2. Here's how the `generate_incremental_records` resource uses the `cursor_path` defined in "config.toml":

```codeBlockLines_RjmQ
@dlt.resource(table_name="incremental_records")
def generate_incremental_records(id_after: dlt.sources.incremental = dlt.config.value):
       for i in range(150):
           yield {"id": i, "idAfter": i, "name": "name-" + str(i)}

pipeline = dlt.pipeline(
       pipeline_name="pipeline_with_incremental",
       destination="duckdb",
)

pipeline.run(generate_incremental_records)

```

`id_after` incrementally stores the latest `cursor_path` value for future pipeline runs.

## Loading when incremental cursor path is missing or value is None/NULL [​](https://dlthub.com/docs/general-usage/incremental/cursor\#loading-when-incremental-cursor-path-is-missing-or-value-is-nonenull "Direct link to Loading when incremental cursor path is missing or value is None/NULL")

You can customize the incremental processing of dlt by setting the parameter `on_cursor_value_missing`.

When loading incrementally with the default settings, there are two assumptions:

1. Each row contains the cursor path.
2. Each row is expected to contain a value at the cursor path that is not `None`.

For example, the two following source data will raise an error:

```codeBlockLines_RjmQ
@dlt.resource
def some_data_without_cursor_path(updated_at=dlt.sources.incremental("updated_at")):
    yield [\
        {"id": 1, "created_at": 1, "updated_at": 1},\
        {"id": 2, "created_at": 2},  # cursor field is missing\
    ]

list(some_data_without_cursor_path())

@dlt.resource
def some_data_without_cursor_value(updated_at=dlt.sources.incremental("updated_at")):
    yield [\
        {"id": 1, "created_at": 1, "updated_at": 1},\
        {"id": 3, "created_at": 4, "updated_at": None},  # value at cursor field is None\
    ]

list(some_data_without_cursor_value())

```

To process a data set where some records do not include the incremental cursor path or where the values at the cursor path are `None`, there are the following four options:

1. Configure the incremental load to raise an exception in case there is a row where the cursor path is missing or has the value `None` using `incremental(..., on_cursor_value_missing="raise")`. This is the default behavior.
2. Configure the incremental load to tolerate the missing cursor path and `None` values using `incremental(..., on_cursor_value_missing="include")`.
3. Configure the incremental load to exclude the missing cursor path and `None` values using `incremental(..., on_cursor_value_missing="exclude")`.
4. Before the incremental processing begins: Ensure that the incremental field is present and transform the values at the incremental cursor to a value different from `None`. [See docs below](https://dlthub.com/docs/general-usage/incremental/cursor#transform-records-before-incremental-processing)

Here is an example of including rows where the incremental cursor value is missing or `None`:

```codeBlockLines_RjmQ
@dlt.resource
def some_data(updated_at=dlt.sources.incremental("updated_at", on_cursor_value_missing="include")):
    yield [\
        {"id": 1, "created_at": 1, "updated_at": 1},\
        {"id": 2, "created_at": 2},\
        {"id": 3, "created_at": 4, "updated_at": None},\
    ]

result = list(some_data())
assert len(result) == 3
assert result[1] == {"id": 2, "created_at": 2}
assert result[2] == {"id": 3, "created_at": 4, "updated_at": None}

```

If you do not want to import records without the cursor path or where the value at the cursor path is `None`, use the following incremental configuration:

```codeBlockLines_RjmQ
@dlt.resource
def some_data(updated_at=dlt.sources.incremental("updated_at", on_cursor_value_missing="exclude")):
    yield [\
        {"id": 1, "created_at": 1, "updated_at": 1},\
        {"id": 2, "created_at": 2},\
        {"id": 3, "created_at": 4, "updated_at": None},\
    ]

result = list(some_data())
assert len(result) == 1

```

## Transform records before incremental processing [​](https://dlthub.com/docs/general-usage/incremental/cursor\#transform-records-before-incremental-processing "Direct link to Transform records before incremental processing")

If you want to load data that includes `None` values, you can transform the records before the incremental processing.
You can add steps to the pipeline that [filter, transform, or pivot your data](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data).

caution

It is important to set the `insert_at` parameter of the `add_map` function to control the order of execution and ensure that your custom steps are executed before the incremental processing starts.
In the following example, the step of data yielding is at `index = 0`, the custom transformation at `index = 1`, and the incremental processing at `index = 2`.

See below how you can modify rows before the incremental processing using `add_map()` and filter rows using `add_filter()`.

```codeBlockLines_RjmQ
@dlt.resource
def some_data(updated_at=dlt.sources.incremental("updated_at")):
    yield [\
        {"id": 1, "created_at": 1, "updated_at": 1},\
        {"id": 2, "created_at": 2, "updated_at": 2},\
        {"id": 3, "created_at": 4, "updated_at": None},\
    ]

def set_default_updated_at(record):
    if record.get("updated_at") is None:
        record["updated_at"] = record.get("created_at")
    return record

# Modifies records before the incremental processing
with_default_values = some_data().add_map(set_default_updated_at, insert_at=1)
result = list(with_default_values)
assert len(result) == 3
assert result[2]["updated_at"] == 4

# Removes records before the incremental processing
without_none = some_data().add_filter(lambda r: r.get("updated_at") is not None, insert_at=1)
result_filtered = list(without_none)
assert len(result_filtered) == 2

```

- [Max, min, or custom `last_value_func`](https://dlthub.com/docs/general-usage/incremental/cursor#max-min-or-custom-last_value_func)
- [Using `end_value` for backfill](https://dlthub.com/docs/general-usage/incremental/cursor#using-end_value-for-backfill)
- [Declare row order to not request unnecessary data](https://dlthub.com/docs/general-usage/incremental/cursor#declare-row-order-to-not-request-unnecessary-data)
- [Deduplicate overlapping ranges with primary key](https://dlthub.com/docs/general-usage/incremental/cursor#deduplicate-overlapping-ranges-with-primary-key)
- [Using `dlt.sources.incremental` with dynamically created resources](https://dlthub.com/docs/general-usage/incremental/cursor#using-dltsourcesincremental-with-dynamically-created-resources)
- [Using Airflow schedule for backfill and incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor#using-airflow-schedule-for-backfill-and-incremental-loading)
- [Reading incremental loading parameters from configuration](https://dlthub.com/docs/general-usage/incremental/cursor#reading-incremental-loading-parameters-from-configuration)
- [Loading when incremental cursor path is missing or value is None/NULL](https://dlthub.com/docs/general-usage/incremental/cursor#loading-when-incremental-cursor-path-is-missing-or-value-is-nonenull)
- [Transform records before incremental processing](https://dlthub.com/docs/general-usage/incremental/cursor#transform-records-before-incremental-processing)

----- https://dlthub.com/docs/general-usage/resource#duplicate-and-rename-resources -----

Version: 1.11.0 (latest)

On this page

## Declare a resource [​](https://dlthub.com/docs/general-usage/resource\#declare-a-resource "Direct link to Declare a resource")

A [resource](https://dlthub.com/docs/general-usage/glossary#resource) is an ( [optionally async](https://dlthub.com/docs/reference/performance#parallelism-within-a-pipeline)) function that yields data. To create a resource, we add the `@dlt.resource` decorator to that function.

Commonly used arguments:

- `name`: The name of the table generated by this resource. Defaults to the decorated function name.
- `write_disposition`: How should the data be loaded at the destination? Currently supported: `append`,
`replace`, and `merge`. Defaults to `append.`

Example:

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows():
	for i in range(10):
		yield {'id': i, 'example_string': 'abc'}

@dlt.source
def source_name():
    return generate_rows

```

To get the data of a resource, we could do:

```codeBlockLines_RjmQ
for row in generate_rows():
    print(row)

for row in source_name().resources.get('table_name'):
    print(row)

```

Typically, resources are declared and grouped with related resources within a [source](https://dlthub.com/docs/general-usage/source) function.

### Define schema [​](https://dlthub.com/docs/general-usage/resource\#define-schema "Direct link to Define schema")

`dlt` will infer the [schema](https://dlthub.com/docs/general-usage/schema) for tables associated with resources from the resource's data.
You can modify the generation process by using the table and column hints. The resource decorator accepts the following arguments:

1. `table_name`: the name of the table, if different from the resource name.
2. `primary_key` and `merge_key`: define the name of the columns (compound keys are allowed) that will receive those hints. Used in [incremental loading](https://dlthub.com/docs/general-usage/incremental-loading) and [merge loading](https://dlthub.com/docs/general-usage/merge-loading).
3. `columns`: lets you define one or more columns, including the data types, nullability, and other hints. The column definition is a `TypedDict`: `TTableSchemaColumns`. In the example below, we tell `dlt` that the column `tags` (containing a list of tags) in the `user` table should have type `json`, which means that it will be loaded as JSON/struct and not as a separate nested table.

```codeBlockLines_RjmQ
@dlt.resource(name="user", columns={"tags": {"data_type": "json"}})
def get_users():
  ...

# the `table_schema` method gets the table schema generated by a resource
print(get_users().compute_table_schema())

```

note

You can pass dynamic hints which are functions that take the data item as input and return a hint value. This lets you create table and column schemas depending on the data. See an [example below](https://dlthub.com/docs/general-usage/resource#adjust-schema-when-you-yield-data).

### Put a contract on tables, columns, and data [​](https://dlthub.com/docs/general-usage/resource\#put-a-contract-on-tables-columns-and-data "Direct link to Put a contract on tables, columns, and data")

Use the `schema_contract` argument to tell dlt how to [deal with new tables, data types, and bad data types](https://dlthub.com/docs/general-usage/schema-contracts). For example, if you set it to **freeze**, `dlt` will not allow for any new tables, columns, or data types to be introduced to the schema - it will raise an exception. Learn more about available contract modes [here](https://dlthub.com/docs/general-usage/schema-contracts#setting-up-the-contract).

### Define schema of nested tables [​](https://dlthub.com/docs/general-usage/resource\#define-schema-of-nested-tables "Direct link to Define schema of nested tables")

`dlt` creates [nested tables](https://dlthub.com/docs/general-usage/schema#nested-references-root-and-nested-tables) to store [list of objects](https://dlthub.com/docs/general-usage/destination-tables#nested-tables) if present in your data.
You can define the schema of such tables with `nested_hints` argument to `@dlt.resource`:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(
    nested_hints={
        "purchases": dlt.mark.make_nested_hints(
            columns=[{"name": "price", "data_type": "decimal"}],
            schema_contract={"columns": "freeze"},
        )
    },
)
def customers():
    """Load customer data from a simple python list."""
    yield [\
        {\
            "id": 1,\
            "name": "simon",\
            "city": "berlin",\
            "purchases": [{"id": 1, "name": "apple", "price": "1.50"}],\
        },\
    ]

```

Here we convert the `price` field in list of `purchases` to decimal type and set the schema contract to lock the list
of columns in it. We use convenience function `dlt.mark.make_nested_hints` to generate nested hints dictionary. You are
free to use it directly.

Mind that `purchases` list will be stored as table with name `customers__purchases`. When declaring nested hints you just need
to specify nested field(s) name(s). In case of deeper nesting ie. let's say each `purchase` has a list of `coupons` applied,
you can apply hints to coupons and define `customers__purchases__coupons` table schema:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(
    nested_hints={
        "purchases": {},
        ("purchases", "coupons"): {
            "columns": {"registered_at": {"data_type": "timestamp"}}
        }
    },
)
def customers():
    ...

```

Here we use `("purchases", "coupons")` to locate list at the depth of 2 and set the data type on `registered_at` column
to `timestamp`. We do that by directly using nested hints dict.
Note that we specified `purchases` with an empty list of hints. **You are required to specify all parent hints, even if they**
**are empty. Currently we are not adding missing path elements automatically**.

You can use `nested_hints` primarily to set column hints and schema contract, those work exactly as in case of root tables.

- `file_format` has no effect (not implemented yet)
- `write_disposition` works as expected but leads to unintended consequences (ie. you can set nested table to `replace`) while root table is `append`.
- `references` will create [table references](https://dlthub.com/docs/general-usage/schema#table-references-1) (annotations) as expected.
- `primary_key` and `merge_key`: **setting those will convert nested table into a regular table, with a separate write disposition, file format etc.** [It allows you to create custom table relationships ie. using natural primary and foreign keys present in the data.](https://dlthub.com/docs/general-usage/schema#generate-custom-linking-for-nested-tables)

tip

[REST API Source](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic) accepts `nested_hints` argument as well.

You can apply nested hints after the resource was created by using [apply\_hints](https://dlthub.com/docs/general-usage/resource#set-table-name-and-adjust-schema).

### Define a schema with Pydantic [​](https://dlthub.com/docs/general-usage/resource\#define-a-schema-with-pydantic "Direct link to Define a schema with Pydantic")

You can alternatively use a [Pydantic](https://pydantic-docs.helpmanual.io/) model to define the schema.
For example:

```codeBlockLines_RjmQ
from pydantic import BaseModel
from typing import List, Optional, Union

class Address(BaseModel):
    street: str
    city: str
    postal_code: str

class User(BaseModel):
    id: int
    name: str
    tags: List[str]
    email: Optional[str]
    address: Address
    status: Union[int, str]

@dlt.resource(name="user", columns=User)
def get_users():
    ...

```

The data types of the table columns are inferred from the types of the Pydantic fields. These use the same type conversions
as when the schema is automatically generated from the data.

Pydantic models integrate well with [schema contracts](https://dlthub.com/docs/general-usage/schema-contracts) as data validators.

Things to note:

- Fields with an `Optional` type are marked as `nullable`.
- Fields with a `Union` type are converted to the first (not `None`) type listed in the union. For example, `status: Union[int, str]` results in a `bigint` column.
- `list`, `dict`, and nested Pydantic model fields will use the `json` type, which means they'll be stored as a JSON object in the database instead of creating nested tables.

You can override this by configuring the Pydantic model:

```codeBlockLines_RjmQ
from typing import ClassVar
from dlt.common.libs.pydantic import DltConfig

class UserWithNesting(User):
  dlt_config: ClassVar[DltConfig] = {"skip_nested_types": True}

@dlt.resource(name="user", columns=UserWithNesting)
def get_users():
    ...

```

`"skip_nested_types"` omits any `dict`/ `list`/ `BaseModel` type fields from the schema, so dlt will fall back on the default
behavior of creating nested tables for these fields.

We do not support `RootModel` that validate simple types. You can add such a validator yourself, see [data filtering section](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data).

### Dispatch data to many tables [​](https://dlthub.com/docs/general-usage/resource\#dispatch-data-to-many-tables "Direct link to Dispatch data to many tables")

You can load data to many tables from a single resource. The most common case is a stream of events
of different types, each with a different data schema. To deal with this, you can use the `table_name`
argument on `dlt.resource`. You could pass the table name as a function with the data item as an
argument and the `table_name` string as a return value.

For example, a resource that loads GitHub repository events wants to send `issue`, `pull request`,
and `comment` events to separate tables. The type of the event is in the "type" field.

```codeBlockLines_RjmQ
# send item to a table with name item["type"]
@dlt.resource(table_name=lambda event: event['type'])
def repo_events() -> Iterator[TDataItems]:
    yield item

# the `table_schema` method gets the table schema generated by a resource and takes an optional
# data item to evaluate dynamic hints
print(repo_events().compute_table_schema({"type": "WatchEvent", "id": ...}))

```

In more advanced cases, you can dispatch data to different tables directly in the code of the
resource function:

```codeBlockLines_RjmQ
@dlt.resource
def repo_events() -> Iterator[TDataItems]:
    # mark the "item" to be sent to the table with the name item["type"]
    yield dlt.mark.with_table_name(item, item["type"])

```

### Parametrize a resource [​](https://dlthub.com/docs/general-usage/resource\#parametrize-a-resource "Direct link to Parametrize a resource")

You can add arguments to your resource functions like to any other. Below we parametrize our
`generate_rows` resource to generate the number of rows we request:

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

for row in generate_rows(10):
    print(row)

for row in generate_rows(20):
    print(row)

```

tip

You can mark some resource arguments as [configuration and credentials](https://dlthub.com/docs/general-usage/credentials) values so `dlt` can pass them automatically to your functions.

### Process resources with `dlt.transformer` [​](https://dlthub.com/docs/general-usage/resource\#process-resources-with-dlttransformer "Direct link to process-resources-with-dlttransformer")

You can feed data from one resource into another. The most common case is when you have an API that returns a list of objects (i.e., users) in one endpoint and user details in another. You can deal with this by declaring a resource that obtains a list of users and another resource that receives items from the list and downloads the profiles.

```codeBlockLines_RjmQ
@dlt.resource(write_disposition="replace")
def users(limit=None):
    for u in _get_users(limit):
        yield u

# Feed data from users as user_item below,
# all transformers must have at least one
# argument that will receive data from the parent resource
@dlt.transformer(data_from=users)
def users_details(user_item):
    for detail in _get_details(user_item["user_id"]):
        yield detail

# Just load the users_details.
# dlt figures out dependencies for you.
pipeline.run(users_details)

```

In the example above, `users_details` will receive data from the default instance of the `users` resource (with `limit` set to `None`). You can also use the **pipe \|** operator to bind resources dynamically.

```codeBlockLines_RjmQ
# You can be more explicit and use a pipe operator.
# With it, you can create dynamic pipelines where the dependencies
# are set at run time and resources are parametrized, i.e.,
# below we want to load only 100 users from the `users` endpoint.
pipeline.run(users(limit=100) | users_details)

```

tip

Transformers are allowed not only to **yield** but also to **return** values and can decorate **async** functions and [**async generators**](https://dlthub.com/docs/reference/performance#extract). Below we decorate an async function and request details on two pokemons. HTTP calls are made in parallel via the httpx library.

```codeBlockLines_RjmQ
import dlt
import httpx

@dlt.transformer
async def pokemon(id):
    async with httpx.AsyncClient() as client:
        r = await client.get(f"https://pokeapi.co/api/v2/pokemon/{id}")
        return r.json()

# Get Bulbasaur and Ivysaur (you need dlt 0.4.6 for the pipe operator working with lists).
print(list([1,2] | pokemon()))

```

### Declare a standalone resource [​](https://dlthub.com/docs/general-usage/resource\#declare-a-standalone-resource "Direct link to Declare a standalone resource")

A standalone resource is defined on a function that is top-level in a module (not an inner function) that accepts config and secrets values. Additionally, if the `standalone` flag is specified, the decorated function signature and docstring will be preserved. `dlt.resource` will just wrap the decorated function, and the user must call the wrapper to get the actual resource. Below we declare a `filesystem` resource that must be called before use.

```codeBlockLines_RjmQ
@dlt.resource(standalone=True)
def fs_resource(bucket_url=dlt.config.value):
  """List and yield files in `bucket_url`."""
  ...

# `filesystem` must be called before it is extracted or used in any other way.
pipeline.run(fs_resource("s3://my-bucket/reports"), table_name="reports")

```

Standalone may have a dynamic name that depends on the arguments passed to the decorated function. For example:

```codeBlockLines_RjmQ
@dlt.resource(standalone=True, name=lambda args: args["stream_name"])
def kinesis(stream_name: str):
    ...

kinesis_stream = kinesis("telemetry_stream")

```

`kinesis_stream` resource has a name **telemetry\_stream**.

### Declare parallel and async resources [​](https://dlthub.com/docs/general-usage/resource\#declare-parallel-and-async-resources "Direct link to Declare parallel and async resources")

You can extract multiple resources in parallel threads or with async IO.
To enable this for a sync resource, you can set the `parallelized` flag to `True` in the resource decorator:

```codeBlockLines_RjmQ
@dlt.resource(parallelized=True)
def get_users():
    for u in _get_users():
        yield u

@dlt.resource(parallelized=True)
def get_orders():
    for o in _get_orders():
        yield o

# users and orders will be iterated in parallel in two separate threads
pipeline.run([get_users(), get_orders()])

```

Async generators are automatically extracted concurrently with other resources:

```codeBlockLines_RjmQ
@dlt.resource
async def get_users():
    async for u in _get_users():  # Assuming _get_users is an async generator
        yield u

```

Please find more details in [extract performance](https://dlthub.com/docs/reference/performance#extract)

## Customize resources [​](https://dlthub.com/docs/general-usage/resource\#customize-resources "Direct link to Customize resources")

### Filter, transform, and pivot data [​](https://dlthub.com/docs/general-usage/resource\#filter-transform-and-pivot-data "Direct link to Filter, transform, and pivot data")

You can attach any number of transformations that are evaluated on an item-per-item basis to your
resource. The available transformation types:

- **map** \- transform the data item ( `resource.add_map`).
- **filter** \- filter the data item ( `resource.add_filter`).
- **yield map** \- a map that returns an iterator (so a single row may generate many rows -
`resource.add_yield_map`).

Example: We have a resource that loads a list of users from an API endpoint. We want to customize it
so:

1. We remove users with `user_id == "me"`.
2. We anonymize user data.

Here's our resource:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(write_disposition="replace")
def users():
    ...
    users = requests.get(RESOURCE_URL)
    ...
    yield users

```

Here's our script that defines transformations and loads the data:

```codeBlockLines_RjmQ
from pipedrive import users

def anonymize_user(user_data):
    user_data["user_id"] = _hash_str(user_data["user_id"])
    user_data["user_email"] = _hash_str(user_data["user_email"])
    return user_data

# add the filter and anonymize function to users resource and enumerate
for user in users().add_filter(lambda user: user["user_id"] != "me").add_map(anonymize_user):
    print(user)

```

### Reduce the nesting level of generated tables [​](https://dlthub.com/docs/general-usage/resource\#reduce-the-nesting-level-of-generated-tables "Direct link to Reduce the nesting level of generated tables")

You can limit how deep `dlt` goes when generating nested tables and flattening dicts into columns. By default, the library will descend
and generate nested tables for all nested lists, without limit.

note

`max_table_nesting` is optional so you can skip it, in this case, dlt will
use it from the source if it is specified there or fallback to the default
value which has 1000 as the maximum nesting level.

```codeBlockLines_RjmQ
import dlt

@dlt.resource(max_table_nesting=1)
def my_resource():
    yield {
        "id": 1,
        "name": "random name",
        "properties": [\
            {\
                "name": "customer_age",\
                "type": "int",\
                "label": "Age",\
                "notes": [\
                    {\
                        "text": "string",\
                        "author": "string",\
                    }\
                ]\
            }\
        ]
    }

```

In the example above, we want only 1 level of nested tables to be generated (so there are no nested
tables of a nested table). Typical settings:

- `max_table_nesting=0` will not generate nested tables and will not flatten dicts into columns at all. All nested data will be
represented as JSON.
- `max_table_nesting=1` will generate nested tables of root tables and nothing more. All nested
data in nested tables will be represented as JSON.

You can achieve the same effect after the resource instance is created:

```codeBlockLines_RjmQ
resource = my_resource()
resource.max_table_nesting = 0

```

Several data sources are prone to contain semi-structured documents with very deep nesting, i.e.,
MongoDB databases. Our practical experience is that setting the `max_nesting_level` to 2 or 3
produces the clearest and human-readable schemas.

### Sample from large data [​](https://dlthub.com/docs/general-usage/resource\#sample-from-large-data "Direct link to Sample from large data")

If your resource loads thousands of pages of data from a REST API or millions of rows from a database table, you may want to sample just a fragment of it in order to quickly see the dataset with example data and test your transformations, etc. To do this, you limit how many items will be yielded by a resource (or source) by calling the `add_limit` method. This method will close the generator that produces the data after the limit is reached.

In the example below, we load just the first 10 items from an infinite counter - that would otherwise never end.

```codeBlockLines_RjmQ
r = dlt.resource(itertools.count(), name="infinity").add_limit(10)
assert list(r) == list(range(10))

```

note

Note that `add_limit` **does not limit the number of records** but rather the "number of yields". Depending on how your resource is set up, the number of extracted rows may vary. For example, consider this resource:

```codeBlockLines_RjmQ
@dlt.resource
def my_resource():
    for i in range(100):
        yield [{"record_id": j} for j in range(15)]

dlt.pipeline(destination="duckdb").run(my_resource().add_limit(10))

```

The code above will extract `15*10=150` records. This is happening because in each iteration, 15 records are yielded, and we're limiting the number of iterations to 10.

Altenatively you can also apply a time limit to the resource. The code below will run the extraction for 10 seconds and extract how ever many items are yielded in that time. In combination with incrementals, this can be useful for batched loading or for loading on machines that have a run time limit.

```codeBlockLines_RjmQ
dlt.pipeline(destination="duckdb").run(my_resource().add_limit(max_time=10))

```

You can also apply a combination of both limits. In this case the extraction will stop as soon as either limit is reached.

```codeBlockLines_RjmQ
dlt.pipeline(destination="duckdb").run(my_resource().add_limit(max_items=10, max_time=10))

```

Some notes about the `add_limit`:

1. `add_limit` does not skip any items. It closes the iterator/generator that produces data after the limit is reached.
2. You cannot limit transformers. They should process all the data they receive fully to avoid inconsistencies in generated datasets.
3. Async resources with a limit added may occasionally produce one item more than the limit on some runs. This behavior is not deterministic.
4. Calling add limit on a resource will replace any previously set limits settings.
5. For time-limited resources, the timer starts when the first item is processed. When resources are processed sequentially (FIFO mode), each resource's time limit applies also sequentially. In the default round robin mode, the time limits will usually run concurrently.

tip

If you are parameterizing the value of `add_limit` and sometimes need it to be disabled, you can set `None` or `-1` to disable the limiting.
You can also set the limit to `0` for the resource to not yield any items.

### Set table name and adjust schema [​](https://dlthub.com/docs/general-usage/resource\#set-table-name-and-adjust-schema "Direct link to Set table name and adjust schema")

You can change the schema of a resource, whether it is standalone or part of a source. Look for a method named `apply_hints` which takes the same arguments as the resource decorator. Obviously, you should call this method before data is extracted from the resource. The example below converts an `append` resource loading the `users` table into a [merge](https://dlthub.com/docs/general-usage/merge-loading) resource that will keep just one updated record per `user_id`. It also adds ["last value" incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor) on the `created_at` column to prevent requesting again the already loaded records:

```codeBlockLines_RjmQ
tables = sql_database()
tables.users.apply_hints(
    write_disposition="merge",
    primary_key="user_id",
    incremental=dlt.sources.incremental("updated_at")
)
pipeline.run(tables)

```

To change the name of a table to which the resource will load data, do the following:

```codeBlockLines_RjmQ
tables = sql_database()
tables.users.table_name = "other_users"

```

### Adjust schema when you yield data [​](https://dlthub.com/docs/general-usage/resource\#adjust-schema-when-you-yield-data "Direct link to Adjust schema when you yield data")

You can set or update the table name, columns, and other schema elements when your resource is executed, and you already yield data. Such changes will be merged with the existing schema in the same way the `apply_hints` method above works. There are many reasons to adjust the schema at runtime. For example, when using Airflow, you should avoid lengthy operations (i.e., reflecting database tables) during the creation of the DAG, so it is better to do it when the DAG executes. You may also emit partial hints (i.e., precision and scale for decimal types) for columns to help `dlt` type inference.

```codeBlockLines_RjmQ
@dlt.resource
def sql_table(credentials, schema, table):
    # Create a SQL Alchemy engine
    engine = engine_from_credentials(credentials)
    engine.execution_options(stream_results=True)
    metadata = MetaData(schema=schema)
    # Reflect the table schema
    table_obj = Table(table, metadata, autoload_with=engine)

    for idx, batch in enumerate(table_rows(engine, table_obj)):
      if idx == 0:
        # Emit the first row with hints, table_to_columns and _get_primary_key are helpers that extract dlt schema from
        # SqlAlchemy model
        yield dlt.mark.with_hints(
            batch,
            dlt.mark.make_hints(columns=table_to_columns(table_obj), primary_key=_get_primary_key(table_obj)),
        )
      else:
        # Just yield all the other rows
        yield batch

```

In the example above, we use `dlt.mark.with_hints` and `dlt.mark.make_hints` to emit columns and primary key with the first extracted item. The table schema will be adjusted after the `batch` is processed in the extract pipeline but before any schema contracts are applied, and data is persisted in the load package.

tip

You can emit columns as a Pydantic model and use dynamic hints (i.e., lambda for table name) as well. You should avoid redefining `Incremental` this way.

### Import external files [​](https://dlthub.com/docs/general-usage/resource\#import-external-files "Direct link to Import external files")

You can import external files, i.e., CSV, Parquet, and JSONL, by yielding items marked with `with_file_import`, optionally passing a table schema corresponding to the imported file. dlt will not read, parse, or normalize any names (i.e., CSV or Arrow headers) and will attempt to copy the file into the destination as is.

```codeBlockLines_RjmQ
import os
import dlt
from dlt.sources.filesystem import filesystem

columns: List[TColumnSchema] = [\
    {"name": "id", "data_type": "bigint"},\
    {"name": "name", "data_type": "text"},\
    {"name": "description", "data_type": "text"},\
    {"name": "ordered_at", "data_type": "date"},\
    {"name": "price", "data_type": "decimal"},\
]

import_folder = "/tmp/import"

@dlt.transformer(columns=columns)
def orders(items: Iterator[FileItemDict]):
  for item in items:
    # copy the file locally
      dest_file = os.path.join(import_folder, item["file_name"])
      # download the file
      item.fsspec.download(item["file_url"], dest_file)
      # tell dlt to import the dest_file as `csv`
      yield dlt.mark.with_file_import(dest_file, "csv")

# use the filesystem core source to glob a bucket

downloader = filesystem(
  bucket_url="s3://my_bucket/csv",
  file_glob="today/*.csv.gz") | orders

info = pipeline.run(orders, destination="snowflake")

```

In the example above, we glob all zipped csv files present on **my\_bucket/csv/today** (using the `filesystem` verified source) and send file descriptors to the `orders` transformer. The transformer downloads and imports the files into the extract package. At the end, `dlt` sends them to Snowflake (the table will be created because we use `column` hints to define the schema).

If imported `csv` files are not in `dlt` [default format](https://dlthub.com/docs/dlt-ecosystem/file-formats/csv#default-settings), you may need to pass additional configuration.

```codeBlockLines_RjmQ
[destination.snowflake.csv_format]
delimiter="|"
include_header=false
on_error_continue=true

```

You can sniff the schema from the data, i.e., using DuckDB to infer the table schema from a CSV file. `dlt.mark.with_file_import` accepts additional arguments that you can use to pass hints at runtime.

note

- If you do not define any columns, the table will not be created in the destination. `dlt` will still attempt to load data into it, so if you create a fitting table upfront, the load process will succeed.
- Files are imported using hard links if possible to avoid copying and duplicating the storage space needed.

### Duplicate and rename resources [​](https://dlthub.com/docs/general-usage/resource\#duplicate-and-rename-resources "Direct link to Duplicate and rename resources")

There are cases when your resources are generic (i.e., bucket filesystem) and you want to load several instances of it (i.e., files from different folders) into separate tables. In the example below, we use the `filesystem` source to load csvs from two different folders into separate tables:

```codeBlockLines_RjmQ
@dlt.resource(standalone=True)
def fs_resource(bucket_url):
  # list and yield files in bucket_url
  ...

@dlt.transformer
def csv_reader(file_item):
  # load csv, parse, and yield rows in file_item
  ...

# create two extract pipes that list files from the bucket and send them to the reader.
# by default, both pipes will load data to the same table (csv_reader)
reports_pipe = fs_resource("s3://my-bucket/reports") | csv_reader()
transactions_pipe = fs_resource("s3://my-bucket/transactions") | csv_reader()

# so we rename resources to load to "reports" and "transactions" tables
pipeline.run(
  [reports_pipe.with_name("reports"), transactions_pipe.with_name("transactions")]
)

```

The `with_name` method returns a deep copy of the original resource, its data pipe, and the data pipes of a parent resource. A renamed clone is fully separated from the original resource (and other clones) when loading: it maintains a separate [resource state](https://dlthub.com/docs/general-usage/state#read-and-write-pipeline-state-in-a-resource) and will load to a table.

## Load resources [​](https://dlthub.com/docs/general-usage/resource\#load-resources "Direct link to Load resources")

You can pass individual resources or a list of resources to the `dlt.pipeline` object. The resources loaded outside the source context will be added to the [default schema](https://dlthub.com/docs/general-usage/schema) of the pipeline.

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

pipeline = dlt.pipeline(
    pipeline_name="rows_pipeline",
    destination="duckdb",
    dataset_name="rows_data"
)
# load an individual resource
pipeline.run(generate_rows(10))
# load a list of resources
pipeline.run([generate_rows(10), generate_rows(20)])

```

### Pick loader file format for a particular resource [​](https://dlthub.com/docs/general-usage/resource\#pick-loader-file-format-for-a-particular-resource "Direct link to Pick loader file format for a particular resource")

You can request a particular loader file format to be used for a resource.

```codeBlockLines_RjmQ
@dlt.resource(file_format="parquet")
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

```

The resource above will be saved and loaded from a Parquet file (if the destination supports it).

note

A special `file_format`: **preferred** will load the resource using a format that is preferred by a destination. This setting supersedes the `loader_file_format` passed to the `run` method.

### Do a full refresh [​](https://dlthub.com/docs/general-usage/resource\#do-a-full-refresh "Direct link to Do a full refresh")

To do a full refresh of an `append` or `merge` resource, you set the `refresh` argument on the `run` method to `drop_data`. This will truncate the tables without dropping them.

```codeBlockLines_RjmQ
p.run(merge_source(), refresh="drop_data")

```

You can also [fully drop the tables](https://dlthub.com/docs/general-usage/pipeline#refresh-pipeline-data-and-state) in the `merge_source`:

```codeBlockLines_RjmQ
p.run(merge_source(), refresh="drop_sources")

```

- [Declare a resource](https://dlthub.com/docs/general-usage/resource#declare-a-resource)
  - [Define schema](https://dlthub.com/docs/general-usage/resource#define-schema)
  - [Put a contract on tables, columns, and data](https://dlthub.com/docs/general-usage/resource#put-a-contract-on-tables-columns-and-data)
  - [Define schema of nested tables](https://dlthub.com/docs/general-usage/resource#define-schema-of-nested-tables)
  - [Define a schema with Pydantic](https://dlthub.com/docs/general-usage/resource#define-a-schema-with-pydantic)
  - [Dispatch data to many tables](https://dlthub.com/docs/general-usage/resource#dispatch-data-to-many-tables)
  - [Parametrize a resource](https://dlthub.com/docs/general-usage/resource#parametrize-a-resource)
  - [Process resources with `dlt.transformer`](https://dlthub.com/docs/general-usage/resource#process-resources-with-dlttransformer)
  - [Declare a standalone resource](https://dlthub.com/docs/general-usage/resource#declare-a-standalone-resource)
  - [Declare parallel and async resources](https://dlthub.com/docs/general-usage/resource#declare-parallel-and-async-resources)
- [Customize resources](https://dlthub.com/docs/general-usage/resource#customize-resources)
  - [Filter, transform, and pivot data](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data)
  - [Reduce the nesting level of generated tables](https://dlthub.com/docs/general-usage/resource#reduce-the-nesting-level-of-generated-tables)
  - [Sample from large data](https://dlthub.com/docs/general-usage/resource#sample-from-large-data)
  - [Set table name and adjust schema](https://dlthub.com/docs/general-usage/resource#set-table-name-and-adjust-schema)
  - [Adjust schema when you yield data](https://dlthub.com/docs/general-usage/resource#adjust-schema-when-you-yield-data)
  - [Import external files](https://dlthub.com/docs/general-usage/resource#import-external-files)
  - [Duplicate and rename resources](https://dlthub.com/docs/general-usage/resource#duplicate-and-rename-resources)
- [Load resources](https://dlthub.com/docs/general-usage/resource#load-resources)
  - [Pick loader file format for a particular resource](https://dlthub.com/docs/general-usage/resource#pick-loader-file-format-for-a-particular-resource)
  - [Do a full refresh](https://dlthub.com/docs/general-usage/resource#do-a-full-refresh)

----- https://dlthub.com/docs/general-usage/resource#declare-a-standalone-resource -----

PreferencesDeclineAccept

Version: 1.11.0 (latest)

On this page

## Declare a resource [​](https://dlthub.com/docs/general-usage/resource\#declare-a-resource "Direct link to Declare a resource")

A [resource](https://dlthub.com/docs/general-usage/glossary#resource) is an ( [optionally async](https://dlthub.com/docs/reference/performance#parallelism-within-a-pipeline)) function that yields data. To create a resource, we add the `@dlt.resource` decorator to that function.

Commonly used arguments:

- `name`: The name of the table generated by this resource. Defaults to the decorated function name.
- `write_disposition`: How should the data be loaded at the destination? Currently supported: `append`,
`replace`, and `merge`. Defaults to `append.`

Example:

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows():
	for i in range(10):
		yield {'id': i, 'example_string': 'abc'}

@dlt.source
def source_name():
    return generate_rows

```

To get the data of a resource, we could do:

```codeBlockLines_RjmQ
for row in generate_rows():
    print(row)

for row in source_name().resources.get('table_name'):
    print(row)

```

Typically, resources are declared and grouped with related resources within a [source](https://dlthub.com/docs/general-usage/source) function.

### Define schema [​](https://dlthub.com/docs/general-usage/resource\#define-schema "Direct link to Define schema")

`dlt` will infer the [schema](https://dlthub.com/docs/general-usage/schema) for tables associated with resources from the resource's data.
You can modify the generation process by using the table and column hints. The resource decorator accepts the following arguments:

1. `table_name`: the name of the table, if different from the resource name.
2. `primary_key` and `merge_key`: define the name of the columns (compound keys are allowed) that will receive those hints. Used in [incremental loading](https://dlthub.com/docs/general-usage/incremental-loading) and [merge loading](https://dlthub.com/docs/general-usage/merge-loading).
3. `columns`: lets you define one or more columns, including the data types, nullability, and other hints. The column definition is a `TypedDict`: `TTableSchemaColumns`. In the example below, we tell `dlt` that the column `tags` (containing a list of tags) in the `user` table should have type `json`, which means that it will be loaded as JSON/struct and not as a separate nested table.

```codeBlockLines_RjmQ
@dlt.resource(name="user", columns={"tags": {"data_type": "json"}})
def get_users():
  ...

# the `table_schema` method gets the table schema generated by a resource
print(get_users().compute_table_schema())

```

note

You can pass dynamic hints which are functions that take the data item as input and return a hint value. This lets you create table and column schemas depending on the data. See an [example below](https://dlthub.com/docs/general-usage/resource#adjust-schema-when-you-yield-data).

### Put a contract on tables, columns, and data [​](https://dlthub.com/docs/general-usage/resource\#put-a-contract-on-tables-columns-and-data "Direct link to Put a contract on tables, columns, and data")

Use the `schema_contract` argument to tell dlt how to [deal with new tables, data types, and bad data types](https://dlthub.com/docs/general-usage/schema-contracts). For example, if you set it to **freeze**, `dlt` will not allow for any new tables, columns, or data types to be introduced to the schema - it will raise an exception. Learn more about available contract modes [here](https://dlthub.com/docs/general-usage/schema-contracts#setting-up-the-contract).

### Define schema of nested tables [​](https://dlthub.com/docs/general-usage/resource\#define-schema-of-nested-tables "Direct link to Define schema of nested tables")

`dlt` creates [nested tables](https://dlthub.com/docs/general-usage/schema#nested-references-root-and-nested-tables) to store [list of objects](https://dlthub.com/docs/general-usage/destination-tables#nested-tables) if present in your data.
You can define the schema of such tables with `nested_hints` argument to `@dlt.resource`:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(
    nested_hints={
        "purchases": dlt.mark.make_nested_hints(
            columns=[{"name": "price", "data_type": "decimal"}],
            schema_contract={"columns": "freeze"},
        )
    },
)
def customers():
    """Load customer data from a simple python list."""
    yield [\
        {\
            "id": 1,\
            "name": "simon",\
            "city": "berlin",\
            "purchases": [{"id": 1, "name": "apple", "price": "1.50"}],\
        },\
    ]

```

Here we convert the `price` field in list of `purchases` to decimal type and set the schema contract to lock the list
of columns in it. We use convenience function `dlt.mark.make_nested_hints` to generate nested hints dictionary. You are
free to use it directly.

Mind that `purchases` list will be stored as table with name `customers__purchases`. When declaring nested hints you just need
to specify nested field(s) name(s). In case of deeper nesting ie. let's say each `purchase` has a list of `coupons` applied,
you can apply hints to coupons and define `customers__purchases__coupons` table schema:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(
    nested_hints={
        "purchases": {},
        ("purchases", "coupons"): {
            "columns": {"registered_at": {"data_type": "timestamp"}}
        }
    },
)
def customers():
    ...

```

Here we use `("purchases", "coupons")` to locate list at the depth of 2 and set the data type on `registered_at` column
to `timestamp`. We do that by directly using nested hints dict.
Note that we specified `purchases` with an empty list of hints. **You are required to specify all parent hints, even if they**
**are empty. Currently we are not adding missing path elements automatically**.

You can use `nested_hints` primarily to set column hints and schema contract, those work exactly as in case of root tables.

- `file_format` has no effect (not implemented yet)
- `write_disposition` works as expected but leads to unintended consequences (ie. you can set nested table to `replace`) while root table is `append`.
- `references` will create [table references](https://dlthub.com/docs/general-usage/schema#table-references-1) (annotations) as expected.
- `primary_key` and `merge_key`: **setting those will convert nested table into a regular table, with a separate write disposition, file format etc.** [It allows you to create custom table relationships ie. using natural primary and foreign keys present in the data.](https://dlthub.com/docs/general-usage/schema#generate-custom-linking-for-nested-tables)

tip

[REST API Source](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic) accepts `nested_hints` argument as well.

You can apply nested hints after the resource was created by using [apply\_hints](https://dlthub.com/docs/general-usage/resource#set-table-name-and-adjust-schema).

### Define a schema with Pydantic [​](https://dlthub.com/docs/general-usage/resource\#define-a-schema-with-pydantic "Direct link to Define a schema with Pydantic")

You can alternatively use a [Pydantic](https://pydantic-docs.helpmanual.io/) model to define the schema.
For example:

```codeBlockLines_RjmQ
from pydantic import BaseModel
from typing import List, Optional, Union

class Address(BaseModel):
    street: str
    city: str
    postal_code: str

class User(BaseModel):
    id: int
    name: str
    tags: List[str]
    email: Optional[str]
    address: Address
    status: Union[int, str]

@dlt.resource(name="user", columns=User)
def get_users():
    ...

```

The data types of the table columns are inferred from the types of the Pydantic fields. These use the same type conversions
as when the schema is automatically generated from the data.

Pydantic models integrate well with [schema contracts](https://dlthub.com/docs/general-usage/schema-contracts) as data validators.

Things to note:

- Fields with an `Optional` type are marked as `nullable`.
- Fields with a `Union` type are converted to the first (not `None`) type listed in the union. For example, `status: Union[int, str]` results in a `bigint` column.
- `list`, `dict`, and nested Pydantic model fields will use the `json` type, which means they'll be stored as a JSON object in the database instead of creating nested tables.

You can override this by configuring the Pydantic model:

```codeBlockLines_RjmQ
from typing import ClassVar
from dlt.common.libs.pydantic import DltConfig

class UserWithNesting(User):
  dlt_config: ClassVar[DltConfig] = {"skip_nested_types": True}

@dlt.resource(name="user", columns=UserWithNesting)
def get_users():
    ...

```

`"skip_nested_types"` omits any `dict`/ `list`/ `BaseModel` type fields from the schema, so dlt will fall back on the default
behavior of creating nested tables for these fields.

We do not support `RootModel` that validate simple types. You can add such a validator yourself, see [data filtering section](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data).

### Dispatch data to many tables [​](https://dlthub.com/docs/general-usage/resource\#dispatch-data-to-many-tables "Direct link to Dispatch data to many tables")

You can load data to many tables from a single resource. The most common case is a stream of events
of different types, each with a different data schema. To deal with this, you can use the `table_name`
argument on `dlt.resource`. You could pass the table name as a function with the data item as an
argument and the `table_name` string as a return value.

For example, a resource that loads GitHub repository events wants to send `issue`, `pull request`,
and `comment` events to separate tables. The type of the event is in the "type" field.

```codeBlockLines_RjmQ
# send item to a table with name item["type"]
@dlt.resource(table_name=lambda event: event['type'])
def repo_events() -> Iterator[TDataItems]:
    yield item

# the `table_schema` method gets the table schema generated by a resource and takes an optional
# data item to evaluate dynamic hints
print(repo_events().compute_table_schema({"type": "WatchEvent", "id": ...}))

```

In more advanced cases, you can dispatch data to different tables directly in the code of the
resource function:

```codeBlockLines_RjmQ
@dlt.resource
def repo_events() -> Iterator[TDataItems]:
    # mark the "item" to be sent to the table with the name item["type"]
    yield dlt.mark.with_table_name(item, item["type"])

```

### Parametrize a resource [​](https://dlthub.com/docs/general-usage/resource\#parametrize-a-resource "Direct link to Parametrize a resource")

You can add arguments to your resource functions like to any other. Below we parametrize our
`generate_rows` resource to generate the number of rows we request:

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

for row in generate_rows(10):
    print(row)

for row in generate_rows(20):
    print(row)

```

tip

You can mark some resource arguments as [configuration and credentials](https://dlthub.com/docs/general-usage/credentials) values so `dlt` can pass them automatically to your functions.

### Process resources with `dlt.transformer` [​](https://dlthub.com/docs/general-usage/resource\#process-resources-with-dlttransformer "Direct link to process-resources-with-dlttransformer")

You can feed data from one resource into another. The most common case is when you have an API that returns a list of objects (i.e., users) in one endpoint and user details in another. You can deal with this by declaring a resource that obtains a list of users and another resource that receives items from the list and downloads the profiles.

```codeBlockLines_RjmQ
@dlt.resource(write_disposition="replace")
def users(limit=None):
    for u in _get_users(limit):
        yield u

# Feed data from users as user_item below,
# all transformers must have at least one
# argument that will receive data from the parent resource
@dlt.transformer(data_from=users)
def users_details(user_item):
    for detail in _get_details(user_item["user_id"]):
        yield detail

# Just load the users_details.
# dlt figures out dependencies for you.
pipeline.run(users_details)

```

In the example above, `users_details` will receive data from the default instance of the `users` resource (with `limit` set to `None`). You can also use the **pipe \|** operator to bind resources dynamically.

```codeBlockLines_RjmQ
# You can be more explicit and use a pipe operator.
# With it, you can create dynamic pipelines where the dependencies
# are set at run time and resources are parametrized, i.e.,
# below we want to load only 100 users from the `users` endpoint.
pipeline.run(users(limit=100) | users_details)

```

tip

Transformers are allowed not only to **yield** but also to **return** values and can decorate **async** functions and [**async generators**](https://dlthub.com/docs/reference/performance#extract). Below we decorate an async function and request details on two pokemons. HTTP calls are made in parallel via the httpx library.

```codeBlockLines_RjmQ
import dlt
import httpx

@dlt.transformer
async def pokemon(id):
    async with httpx.AsyncClient() as client:
        r = await client.get(f"https://pokeapi.co/api/v2/pokemon/{id}")
        return r.json()

# Get Bulbasaur and Ivysaur (you need dlt 0.4.6 for the pipe operator working with lists).
print(list([1,2] | pokemon()))

```

### Declare a standalone resource [​](https://dlthub.com/docs/general-usage/resource\#declare-a-standalone-resource "Direct link to Declare a standalone resource")

A standalone resource is defined on a function that is top-level in a module (not an inner function) that accepts config and secrets values. Additionally, if the `standalone` flag is specified, the decorated function signature and docstring will be preserved. `dlt.resource` will just wrap the decorated function, and the user must call the wrapper to get the actual resource. Below we declare a `filesystem` resource that must be called before use.

```codeBlockLines_RjmQ
@dlt.resource(standalone=True)
def fs_resource(bucket_url=dlt.config.value):
  """List and yield files in `bucket_url`."""
  ...

# `filesystem` must be called before it is extracted or used in any other way.
pipeline.run(fs_resource("s3://my-bucket/reports"), table_name="reports")

```

Standalone may have a dynamic name that depends on the arguments passed to the decorated function. For example:

```codeBlockLines_RjmQ
@dlt.resource(standalone=True, name=lambda args: args["stream_name"])
def kinesis(stream_name: str):
    ...

kinesis_stream = kinesis("telemetry_stream")

```

`kinesis_stream` resource has a name **telemetry\_stream**.

### Declare parallel and async resources [​](https://dlthub.com/docs/general-usage/resource\#declare-parallel-and-async-resources "Direct link to Declare parallel and async resources")

You can extract multiple resources in parallel threads or with async IO.
To enable this for a sync resource, you can set the `parallelized` flag to `True` in the resource decorator:

```codeBlockLines_RjmQ
@dlt.resource(parallelized=True)
def get_users():
    for u in _get_users():
        yield u

@dlt.resource(parallelized=True)
def get_orders():
    for o in _get_orders():
        yield o

# users and orders will be iterated in parallel in two separate threads
pipeline.run([get_users(), get_orders()])

```

Async generators are automatically extracted concurrently with other resources:

```codeBlockLines_RjmQ
@dlt.resource
async def get_users():
    async for u in _get_users():  # Assuming _get_users is an async generator
        yield u

```

Please find more details in [extract performance](https://dlthub.com/docs/reference/performance#extract)

## Customize resources [​](https://dlthub.com/docs/general-usage/resource\#customize-resources "Direct link to Customize resources")

### Filter, transform, and pivot data [​](https://dlthub.com/docs/general-usage/resource\#filter-transform-and-pivot-data "Direct link to Filter, transform, and pivot data")

You can attach any number of transformations that are evaluated on an item-per-item basis to your
resource. The available transformation types:

- **map** \- transform the data item ( `resource.add_map`).
- **filter** \- filter the data item ( `resource.add_filter`).
- **yield map** \- a map that returns an iterator (so a single row may generate many rows -
`resource.add_yield_map`).

Example: We have a resource that loads a list of users from an API endpoint. We want to customize it
so:

1. We remove users with `user_id == "me"`.
2. We anonymize user data.

Here's our resource:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(write_disposition="replace")
def users():
    ...
    users = requests.get(RESOURCE_URL)
    ...
    yield users

```

Here's our script that defines transformations and loads the data:

```codeBlockLines_RjmQ
from pipedrive import users

def anonymize_user(user_data):
    user_data["user_id"] = _hash_str(user_data["user_id"])
    user_data["user_email"] = _hash_str(user_data["user_email"])
    return user_data

# add the filter and anonymize function to users resource and enumerate
for user in users().add_filter(lambda user: user["user_id"] != "me").add_map(anonymize_user):
    print(user)

```

### Reduce the nesting level of generated tables [​](https://dlthub.com/docs/general-usage/resource\#reduce-the-nesting-level-of-generated-tables "Direct link to Reduce the nesting level of generated tables")

You can limit how deep `dlt` goes when generating nested tables and flattening dicts into columns. By default, the library will descend
and generate nested tables for all nested lists, without limit.

note

`max_table_nesting` is optional so you can skip it, in this case, dlt will
use it from the source if it is specified there or fallback to the default
value which has 1000 as the maximum nesting level.

```codeBlockLines_RjmQ
import dlt

@dlt.resource(max_table_nesting=1)
def my_resource():
    yield {
        "id": 1,
        "name": "random name",
        "properties": [\
            {\
                "name": "customer_age",\
                "type": "int",\
                "label": "Age",\
                "notes": [\
                    {\
                        "text": "string",\
                        "author": "string",\
                    }\
                ]\
            }\
        ]
    }

```

In the example above, we want only 1 level of nested tables to be generated (so there are no nested
tables of a nested table). Typical settings:

- `max_table_nesting=0` will not generate nested tables and will not flatten dicts into columns at all. All nested data will be
represented as JSON.
- `max_table_nesting=1` will generate nested tables of root tables and nothing more. All nested
data in nested tables will be represented as JSON.

You can achieve the same effect after the resource instance is created:

```codeBlockLines_RjmQ
resource = my_resource()
resource.max_table_nesting = 0

```

Several data sources are prone to contain semi-structured documents with very deep nesting, i.e.,
MongoDB databases. Our practical experience is that setting the `max_nesting_level` to 2 or 3
produces the clearest and human-readable schemas.

### Sample from large data [​](https://dlthub.com/docs/general-usage/resource\#sample-from-large-data "Direct link to Sample from large data")

If your resource loads thousands of pages of data from a REST API or millions of rows from a database table, you may want to sample just a fragment of it in order to quickly see the dataset with example data and test your transformations, etc. To do this, you limit how many items will be yielded by a resource (or source) by calling the `add_limit` method. This method will close the generator that produces the data after the limit is reached.

In the example below, we load just the first 10 items from an infinite counter - that would otherwise never end.

```codeBlockLines_RjmQ
r = dlt.resource(itertools.count(), name="infinity").add_limit(10)
assert list(r) == list(range(10))

```

note

Note that `add_limit` **does not limit the number of records** but rather the "number of yields". Depending on how your resource is set up, the number of extracted rows may vary. For example, consider this resource:

```codeBlockLines_RjmQ
@dlt.resource
def my_resource():
    for i in range(100):
        yield [{"record_id": j} for j in range(15)]

dlt.pipeline(destination="duckdb").run(my_resource().add_limit(10))

```

The code above will extract `15*10=150` records. This is happening because in each iteration, 15 records are yielded, and we're limiting the number of iterations to 10.

Altenatively you can also apply a time limit to the resource. The code below will run the extraction for 10 seconds and extract how ever many items are yielded in that time. In combination with incrementals, this can be useful for batched loading or for loading on machines that have a run time limit.

```codeBlockLines_RjmQ
dlt.pipeline(destination="duckdb").run(my_resource().add_limit(max_time=10))

```

You can also apply a combination of both limits. In this case the extraction will stop as soon as either limit is reached.

```codeBlockLines_RjmQ
dlt.pipeline(destination="duckdb").run(my_resource().add_limit(max_items=10, max_time=10))

```

Some notes about the `add_limit`:

1. `add_limit` does not skip any items. It closes the iterator/generator that produces data after the limit is reached.
2. You cannot limit transformers. They should process all the data they receive fully to avoid inconsistencies in generated datasets.
3. Async resources with a limit added may occasionally produce one item more than the limit on some runs. This behavior is not deterministic.
4. Calling add limit on a resource will replace any previously set limits settings.
5. For time-limited resources, the timer starts when the first item is processed. When resources are processed sequentially (FIFO mode), each resource's time limit applies also sequentially. In the default round robin mode, the time limits will usually run concurrently.

tip

If you are parameterizing the value of `add_limit` and sometimes need it to be disabled, you can set `None` or `-1` to disable the limiting.
You can also set the limit to `0` for the resource to not yield any items.

### Set table name and adjust schema [​](https://dlthub.com/docs/general-usage/resource\#set-table-name-and-adjust-schema "Direct link to Set table name and adjust schema")

You can change the schema of a resource, whether it is standalone or part of a source. Look for a method named `apply_hints` which takes the same arguments as the resource decorator. Obviously, you should call this method before data is extracted from the resource. The example below converts an `append` resource loading the `users` table into a [merge](https://dlthub.com/docs/general-usage/merge-loading) resource that will keep just one updated record per `user_id`. It also adds ["last value" incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor) on the `created_at` column to prevent requesting again the already loaded records:

```codeBlockLines_RjmQ
tables = sql_database()
tables.users.apply_hints(
    write_disposition="merge",
    primary_key="user_id",
    incremental=dlt.sources.incremental("updated_at")
)
pipeline.run(tables)

```

To change the name of a table to which the resource will load data, do the following:

```codeBlockLines_RjmQ
tables = sql_database()
tables.users.table_name = "other_users"

```

### Adjust schema when you yield data [​](https://dlthub.com/docs/general-usage/resource\#adjust-schema-when-you-yield-data "Direct link to Adjust schema when you yield data")

You can set or update the table name, columns, and other schema elements when your resource is executed, and you already yield data. Such changes will be merged with the existing schema in the same way the `apply_hints` method above works. There are many reasons to adjust the schema at runtime. For example, when using Airflow, you should avoid lengthy operations (i.e., reflecting database tables) during the creation of the DAG, so it is better to do it when the DAG executes. You may also emit partial hints (i.e., precision and scale for decimal types) for columns to help `dlt` type inference.

```codeBlockLines_RjmQ
@dlt.resource
def sql_table(credentials, schema, table):
    # Create a SQL Alchemy engine
    engine = engine_from_credentials(credentials)
    engine.execution_options(stream_results=True)
    metadata = MetaData(schema=schema)
    # Reflect the table schema
    table_obj = Table(table, metadata, autoload_with=engine)

    for idx, batch in enumerate(table_rows(engine, table_obj)):
      if idx == 0:
        # Emit the first row with hints, table_to_columns and _get_primary_key are helpers that extract dlt schema from
        # SqlAlchemy model
        yield dlt.mark.with_hints(
            batch,
            dlt.mark.make_hints(columns=table_to_columns(table_obj), primary_key=_get_primary_key(table_obj)),
        )
      else:
        # Just yield all the other rows
        yield batch

```

In the example above, we use `dlt.mark.with_hints` and `dlt.mark.make_hints` to emit columns and primary key with the first extracted item. The table schema will be adjusted after the `batch` is processed in the extract pipeline but before any schema contracts are applied, and data is persisted in the load package.

tip

You can emit columns as a Pydantic model and use dynamic hints (i.e., lambda for table name) as well. You should avoid redefining `Incremental` this way.

### Import external files [​](https://dlthub.com/docs/general-usage/resource\#import-external-files "Direct link to Import external files")

You can import external files, i.e., CSV, Parquet, and JSONL, by yielding items marked with `with_file_import`, optionally passing a table schema corresponding to the imported file. dlt will not read, parse, or normalize any names (i.e., CSV or Arrow headers) and will attempt to copy the file into the destination as is.

```codeBlockLines_RjmQ
import os
import dlt
from dlt.sources.filesystem import filesystem

columns: List[TColumnSchema] = [\
    {"name": "id", "data_type": "bigint"},\
    {"name": "name", "data_type": "text"},\
    {"name": "description", "data_type": "text"},\
    {"name": "ordered_at", "data_type": "date"},\
    {"name": "price", "data_type": "decimal"},\
]

import_folder = "/tmp/import"

@dlt.transformer(columns=columns)
def orders(items: Iterator[FileItemDict]):
  for item in items:
    # copy the file locally
      dest_file = os.path.join(import_folder, item["file_name"])
      # download the file
      item.fsspec.download(item["file_url"], dest_file)
      # tell dlt to import the dest_file as `csv`
      yield dlt.mark.with_file_import(dest_file, "csv")

# use the filesystem core source to glob a bucket

downloader = filesystem(
  bucket_url="s3://my_bucket/csv",
  file_glob="today/*.csv.gz") | orders

info = pipeline.run(orders, destination="snowflake")

```

In the example above, we glob all zipped csv files present on **my\_bucket/csv/today** (using the `filesystem` verified source) and send file descriptors to the `orders` transformer. The transformer downloads and imports the files into the extract package. At the end, `dlt` sends them to Snowflake (the table will be created because we use `column` hints to define the schema).

If imported `csv` files are not in `dlt` [default format](https://dlthub.com/docs/dlt-ecosystem/file-formats/csv#default-settings), you may need to pass additional configuration.

```codeBlockLines_RjmQ
[destination.snowflake.csv_format]
delimiter="|"
include_header=false
on_error_continue=true

```

You can sniff the schema from the data, i.e., using DuckDB to infer the table schema from a CSV file. `dlt.mark.with_file_import` accepts additional arguments that you can use to pass hints at runtime.

note

- If you do not define any columns, the table will not be created in the destination. `dlt` will still attempt to load data into it, so if you create a fitting table upfront, the load process will succeed.
- Files are imported using hard links if possible to avoid copying and duplicating the storage space needed.

### Duplicate and rename resources [​](https://dlthub.com/docs/general-usage/resource\#duplicate-and-rename-resources "Direct link to Duplicate and rename resources")

There are cases when your resources are generic (i.e., bucket filesystem) and you want to load several instances of it (i.e., files from different folders) into separate tables. In the example below, we use the `filesystem` source to load csvs from two different folders into separate tables:

```codeBlockLines_RjmQ
@dlt.resource(standalone=True)
def fs_resource(bucket_url):
  # list and yield files in bucket_url
  ...

@dlt.transformer
def csv_reader(file_item):
  # load csv, parse, and yield rows in file_item
  ...

# create two extract pipes that list files from the bucket and send them to the reader.
# by default, both pipes will load data to the same table (csv_reader)
reports_pipe = fs_resource("s3://my-bucket/reports") | csv_reader()
transactions_pipe = fs_resource("s3://my-bucket/transactions") | csv_reader()

# so we rename resources to load to "reports" and "transactions" tables
pipeline.run(
  [reports_pipe.with_name("reports"), transactions_pipe.with_name("transactions")]
)

```

The `with_name` method returns a deep copy of the original resource, its data pipe, and the data pipes of a parent resource. A renamed clone is fully separated from the original resource (and other clones) when loading: it maintains a separate [resource state](https://dlthub.com/docs/general-usage/state#read-and-write-pipeline-state-in-a-resource) and will load to a table.

## Load resources [​](https://dlthub.com/docs/general-usage/resource\#load-resources "Direct link to Load resources")

You can pass individual resources or a list of resources to the `dlt.pipeline` object. The resources loaded outside the source context will be added to the [default schema](https://dlthub.com/docs/general-usage/schema) of the pipeline.

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

pipeline = dlt.pipeline(
    pipeline_name="rows_pipeline",
    destination="duckdb",
    dataset_name="rows_data"
)
# load an individual resource
pipeline.run(generate_rows(10))
# load a list of resources
pipeline.run([generate_rows(10), generate_rows(20)])

```

### Pick loader file format for a particular resource [​](https://dlthub.com/docs/general-usage/resource\#pick-loader-file-format-for-a-particular-resource "Direct link to Pick loader file format for a particular resource")

You can request a particular loader file format to be used for a resource.

```codeBlockLines_RjmQ
@dlt.resource(file_format="parquet")
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

```

The resource above will be saved and loaded from a Parquet file (if the destination supports it).

note

A special `file_format`: **preferred** will load the resource using a format that is preferred by a destination. This setting supersedes the `loader_file_format` passed to the `run` method.

### Do a full refresh [​](https://dlthub.com/docs/general-usage/resource\#do-a-full-refresh "Direct link to Do a full refresh")

To do a full refresh of an `append` or `merge` resource, you set the `refresh` argument on the `run` method to `drop_data`. This will truncate the tables without dropping them.

```codeBlockLines_RjmQ
p.run(merge_source(), refresh="drop_data")

```

You can also [fully drop the tables](https://dlthub.com/docs/general-usage/pipeline#refresh-pipeline-data-and-state) in the `merge_source`:

```codeBlockLines_RjmQ
p.run(merge_source(), refresh="drop_sources")

```

- [Declare a resource](https://dlthub.com/docs/general-usage/resource#declare-a-resource)
  - [Define schema](https://dlthub.com/docs/general-usage/resource#define-schema)
  - [Put a contract on tables, columns, and data](https://dlthub.com/docs/general-usage/resource#put-a-contract-on-tables-columns-and-data)
  - [Define schema of nested tables](https://dlthub.com/docs/general-usage/resource#define-schema-of-nested-tables)
  - [Define a schema with Pydantic](https://dlthub.com/docs/general-usage/resource#define-a-schema-with-pydantic)
  - [Dispatch data to many tables](https://dlthub.com/docs/general-usage/resource#dispatch-data-to-many-tables)
  - [Parametrize a resource](https://dlthub.com/docs/general-usage/resource#parametrize-a-resource)
  - [Process resources with `dlt.transformer`](https://dlthub.com/docs/general-usage/resource#process-resources-with-dlttransformer)
  - [Declare a standalone resource](https://dlthub.com/docs/general-usage/resource#declare-a-standalone-resource)
  - [Declare parallel and async resources](https://dlthub.com/docs/general-usage/resource#declare-parallel-and-async-resources)
- [Customize resources](https://dlthub.com/docs/general-usage/resource#customize-resources)
  - [Filter, transform, and pivot data](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data)
  - [Reduce the nesting level of generated tables](https://dlthub.com/docs/general-usage/resource#reduce-the-nesting-level-of-generated-tables)
  - [Sample from large data](https://dlthub.com/docs/general-usage/resource#sample-from-large-data)
  - [Set table name and adjust schema](https://dlthub.com/docs/general-usage/resource#set-table-name-and-adjust-schema)
  - [Adjust schema when you yield data](https://dlthub.com/docs/general-usage/resource#adjust-schema-when-you-yield-data)
  - [Import external files](https://dlthub.com/docs/general-usage/resource#import-external-files)
  - [Duplicate and rename resources](https://dlthub.com/docs/general-usage/resource#duplicate-and-rename-resources)
- [Load resources](https://dlthub.com/docs/general-usage/resource#load-resources)
  - [Pick loader file format for a particular resource](https://dlthub.com/docs/general-usage/resource#pick-loader-file-format-for-a-particular-resource)
  - [Do a full refresh](https://dlthub.com/docs/general-usage/resource#do-a-full-refresh)

----- https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads#grab-credentials -----

Version: 1.11.0 (latest)

On this page

Need help deploying these sources or figuring out how to run them in your data stack?

[Join our Slack community](https://dlthub.com/community) or [Get in touch](https://dlthub.com/contact) with the dltHub Customer Success team.

Facebook Ads is the advertising platform that lets businesses and individuals create targeted ads on
Facebook and its affiliated apps like Instagram and Messenger.

This Facebook `dlt` verified source and
[pipeline example](https://github.com/dlt-hub/verified-sources/blob/master/sources/facebook_ads_pipeline.py)
loads data using the [Facebook Marketing API](https://developers.facebook.com/products/marketing-api/) to the destination of your choice.

The endpoints that this verified source supports are:

| Name | Description |
| --- | --- |
| campaigns | A structured marketing initiative that focuses on a specific objective or goal |
| ad\_sets | A subset or group of ads within a campaign |
| ads | An individual advertisement that is created and displayed within an ad set |
| creatives | Visual and textual elements that make up an advertisement |
| ad\_leads | Information collected from users who have interacted with lead generation ads |
| facebook\_insights | Data on audience demographics, post reach, and engagement metrics |

To get a complete list of sub-endpoints that can be loaded, see
[facebook\_ads/settings.py.](https://github.com/dlt-hub/verified-sources/blob/master/sources/facebook_ads/settings.py)

## Setup guide [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads\#setup-guide "Direct link to Setup guide")

### Grab credentials [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads\#grab-credentials "Direct link to Grab credentials")

#### Grab `Account ID` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads\#grab-account-id "Direct link to grab-account-id")

1. Ensure that you have Ads Manager active for your Facebook account.
2. Find your account ID, which is a long number. You can locate it by clicking on the Account
Overview dropdown in Ads Manager or by checking the link address. For example,
[https://adsmanager.facebook.com/adsmanager/manage/accounts?act=10150974068878324](https://adsmanager.facebook.com/adsmanager/manage/accounts?act=10150974068878324).
3. Note this account ID as it will further be used in configuring dlt.

#### Grab `Access_Token` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads\#grab-access_token "Direct link to grab-access_token")

1. Sign up for a developer account on
[developers.facebook.com](https://developers.facebook.com/).
2. Log in to your developer account and click on "My Apps" in the top right corner.
3. Create an app, select "Other" as the type, choose "Business" as the category, and click "Next".
4. Enter the name of your app and select the associated business manager account.
5. Go to the "Basic" settings in the left-hand side menu.
6. Copy the "App ID" and "App secret" and paste them as "client\_id" and "client\_secret" in the
secrets.toml file in the .dlt folder.
7. Next, obtain a short-lived access token at [https://developers.facebook.com/tools/explorer/](https://developers.facebook.com/tools/explorer/).
8. Select the created app, add "ads\_read" and "lead\_retrieval" permissions, and generate a
short-lived access token.
9. Copy the access token and update it in the `.dlt/secrets.toml` file.

#### Exchange short-lived token for a long-lived token [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads\#exchange-short-lived-token-for-a-long-lived-token "Direct link to Exchange short-lived token for a long-lived token")

By default, Facebook access tokens have a short lifespan of one hour. To exchange a short-lived Facebook access token for a long-lived token, update the `.dlt/secrets.toml` with client\_id and client\_secret, and execute the provided Python code.

```codeBlockLines_RjmQ
from facebook_ads import get_long_lived_token
print(get_long_lived_token("your short-lived token"))

```

Replace the `access_token` in the `.dlt/secrets.toml` file with the long-lived token obtained from the above code snippet.

To retrieve the expiry date and the associated scopes of the token, you can use the following command:

```codeBlockLines_RjmQ
from facebook_ads import debug_access_token
debug_access_token()

```

We highly recommend you add the token expiration timestamp to get notified a week before token expiration that you need to rotate it. Right now, the notifications are sent to the logger with error level. In `config.toml` / `secrets.toml`:

```codeBlockLines_RjmQ
[sources.facebook_ads]
access_token_expires_at=1688821881

```

> Note: The Facebook UI, which is described here, might change.
> The full guide is available at [this link.](https://developers.facebook.com/docs/marketing-apis/overview/authentication)

### Initialize the verified source [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads\#initialize-the-verified-source "Direct link to Initialize the verified source")

To get started with your data pipeline, follow these steps:

1. Enter the following command:

```codeBlockLines_RjmQ
dlt init facebook_ads duckdb

```

[This command](https://dlthub.com/docs/reference/command-line-interface) will initialize [the pipeline example](https://github.com/dlt-hub/verified-sources/blob/master/sources/facebook_ads_pipeline.py) with Facebook Ads as the [source](https://dlthub.com/docs/general-usage/source) and [duckdb](https://dlthub.com/docs/dlt-ecosystem/destinations/duckdb) as the [destination](https://dlthub.com/docs/dlt-ecosystem/destinations).

2. If you'd like to use a different destination, simply replace `duckdb` with the name of your preferred [destination](https://dlthub.com/docs/dlt-ecosystem/destinations).

3. After running this command, a new directory will be created with the necessary files and configuration settings to get started.

For more information, read the guide on [how to add a verified source](https://dlthub.com/docs/walkthroughs/add-a-verified-source).

### Add credential [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads\#add-credential "Direct link to Add credential")

1. Inside the `.dlt` folder, you'll find a file called `secrets.toml`, which is where you can securely store your access tokens and other sensitive information. It's important to handle this file with care and keep it safe. Here's what the file looks like:

```codeBlockLines_RjmQ
# put your secret values and credentials here
# do not share this file and do not push it to github
[sources.facebook_ads]
access_token="set me up!"

```

2. Replace the access\_token value with the [previously copied one](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads#grab-credentials) to ensure secure access to your Facebook Ads resources.

3. Next, follow the [destination documentation](https://dlthub.com/docs/dlt-ecosystem/destinations) instructions to add credentials for your chosen destination, ensuring proper routing of your data to the final destination.

4. It is strongly recommended to add the token expiration timestamp to your `config.toml` or `secrets.toml` file.

5. Next, store your pipeline configuration details in the `.dlt/config.toml`.

Here's what the `config.toml` looks like:

```codeBlockLines_RjmQ
[sources.facebook_ads]
account_id = "Please set me up!"

```

6. Replace the value of the "account id" with the one [copied above](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads#grab-account-id).

For more information, read the [General Usage: Credentials.](https://dlthub.com/docs/general-usage/credentials)

## Run the pipeline [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads\#run-the-pipeline "Direct link to Run the pipeline")

1. Before running the pipeline, ensure that you have installed all the necessary dependencies by
running the command:

```codeBlockLines_RjmQ
pip install -r requirements.txt

```

2. You're now ready to run the pipeline! To get started, run the following command:

```codeBlockLines_RjmQ
python facebook_ads_pipeline.py

```

3. Once the pipeline has finished running, you can verify that everything loaded correctly by using
the following command:

```codeBlockLines_RjmQ
dlt pipeline <pipeline_name> show

```

For example, the `pipeline_name` for the above pipeline example is `facebook_ads`. You may also
use any custom name instead.

For more information, read the guide on [how to run a pipeline](https://dlthub.com/docs/walkthroughs/run-a-pipeline).

## Sources and resources [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads\#sources-and-resources "Direct link to Sources and resources")

`dlt` works on the principle of [sources](https://dlthub.com/docs/general-usage/source) and
[resources](https://dlthub.com/docs/general-usage/resource).

### Default endpoints [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads\#default-endpoints "Direct link to Default endpoints")

You can write your own pipelines to load data to a destination using this verified source. However,
it is important to note the complete list of the default endpoints given in
[facebook\_ads/settings.py.](https://github.com/dlt-hub/verified-sources/blob/master/sources/facebook_ads/settings.py)

### Source `facebook_ads_source` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads\#source-facebook_ads_source "Direct link to source-facebook_ads_source")

This function returns a list of resources to load campaigns, ad sets, ads, creatives, and ad leads
data from the Facebook Marketing API.

```codeBlockLines_RjmQ
@dlt.source(name="facebook_ads")
def facebook_ads_source(
    account_id: str = dlt.config.value,
    access_token: str = dlt.secrets.value,
    chunk_size: int = 50,
    request_timeout: float = 300.0,
    app_api_version: str = None,
) -> Sequence[DltResource]:
   ...

```

`account_id`: Account ID associated with the ad manager, configured in "config.toml".

`access_token`: Access token associated with the Business Facebook App, configured in
"secrets.toml".

`chunk_size`: The size of the page and batch request. You may need to decrease it if you request a lot
of fields. Defaults to 50.

`request_timeout`: Connection timeout. Defaults to 300.0.

`app_api_version`: A version of the Facebook API required by the app for which the access tokens
were issued, e.g., 'v17.0'. Defaults to the _facebook\_business_ library default version.

### Resource `ads` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads\#resource-ads "Direct link to resource-ads")

The ads function fetches ad data. It retrieves ads from a specified account with specific fields and
states.

```codeBlockLines_RjmQ
@dlt.resource(primary_key="id", write_disposition="replace")
def ads(
    fields: Sequence[str] = DEFAULT_AD_FIELDS,
    states: Sequence[str] = None,
    chunk_size: int = 50
) -> Iterator[TDataItems]:

  yield _get_data_chunked(account.get_ads, fields, states, chunk_size)

```

`fields`: Retrieves fields for each ad. For example, “id”, “name”, “adset\_id”, etc.

`states`: The possible states include "Active," "Paused," "Pending Review," "Disapproved,"
"Completed," and "Archived."

### Resources for `facebook_ads_source` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads\#resources-for-facebook_ads_source "Direct link to resources-for-facebook_ads_source")

Similar to resource `ads`, the following resources have been defined in the `__init__.py` for source
`facebook_ads_source`:

| Resource | Description |
| --- | --- |
| campaigns | Fetches all `DEFAULT_CAMPAIGN_FIELDS` |
| ad\_sets | Fetches all `DEFAULT_ADSET_FIELDS` |
| leads | Fetches all `DEFAULT_LEAD_FIELDS`, uses `@dlt.transformer` decorator |
| ad\_creatives | Fetches all `DEFAULT_ADCREATIVE_FIELDS` |

The default fields are defined in
[facebook\_ads/settings.py](https://github.com/dlt-hub/verified-sources/blob/master/sources/facebook_ads/settings.py)

### Source `facebook_insights_source` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads\#source-facebook_insights_source "Direct link to source-facebook_insights_source")

This function returns a list of resources to load facebook\_insights.

```codeBlockLines_RjmQ
@dlt.source(name="facebook_ads")
def facebook_insights_source(
    account_id: str = dlt.config.value,
    access_token: str = dlt.secrets.value,
    initial_load_past_days: int = 30,
    fields: Sequence[str] = DEFAULT_INSIGHT_FIELDS,
    attribution_window_days_lag: int = 7,
    time_increment_days: int = 1,
    breakdowns: TInsightsBreakdownOptions = "ads_insights_age_and_gender",
    action_breakdowns: Sequence[str] = ALL_ACTION_BREAKDOWNS,
    level: TInsightsLevels = "ad",
    action_attribution_windows: Sequence[str] = ALL_ACTION_ATTRIBUTION_WINDOWS,
    batch_size: int = 50,
    request_timeout: int = 300,
    app_api_version: str = None,
) -> DltResource:
   ...

```

`account_id`: Account ID associated with ads manager, configured in _config.toml_.

`access_token`: Access token associated with the Business Facebook App, configured in _secrets.toml_.

`initial_load_past_days`: How many past days (starting from today) to initially load. Defaults to 30.

`fields`: A list of fields to include in each report. Note that the “breakdowns” option adds fields automatically. Defaults to DEFAULT\_INSIGHT\_FIELDS.

`attribution_window_days_lag`: Attribution window in days. The reports in the attribution window are refreshed on each run. Defaults to 7.

`time_increment_days`: The report aggregation window in days. Use 7 for weekly aggregation. Defaults to 1.

`breakdowns`: Presents with common aggregations. See [settings.py](https://github.com/dlt-hub/verified-sources/blob/master/sources/facebook_ads/settings.py) for details. Defaults to "ads\_insights\_age\_and\_gender".

`action_breakdowns`: Action aggregation types. See [settings.py](https://github.com/dlt-hub/verified-sources/blob/master/sources/facebook_ads/settings.py) for details. Defaults to ALL\_ACTION\_BREAKDOWNS.

`level`: The granularity level. Defaults to "ad".

`action_attribution_windows`: Attribution windows for actions. Defaults to ALL\_ACTION\_ATTRIBUTION\_WINDOWS.

`batch_size`: Page size when reading data from a particular report. Defaults to 50.

`request_timeout`: Connection timeout. Defaults to 300.

`app_api_version`: A version of the Facebook API required by the app for which the access tokens were issued, e.g., 'v17.0'. Defaults to the facebook\_business library default version.

### Resource `facebook_insights` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads\#resource-facebook_insights "Direct link to resource-facebook_insights")

This function fetches Facebook insights data incrementally from a specified start date until the current date, in day steps.

```codeBlockLines_RjmQ
@dlt.resource(primary_key=INSIGHTS_PRIMARY_KEY, write_disposition="merge")
def facebook_insights(
    date_start: dlt.sources.incremental[str] = dlt.sources.incremental(
        "date_start", initial_value=START_DATE_STRING
    )
) -> Iterator[TDataItems]:
   ...

```

`date_start`: Parameter sets the initial value for the "date\_start" parameter in dlt.sources.incremental. It is based on the last pipeline run or defaults to today's date minus the specified number of days in the "initial\_load\_past\_days" parameter.

## Customization [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads\#customization "Direct link to Customization")

### Create your own pipeline [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads\#create-your-own-pipeline "Direct link to Create your own pipeline")

If you wish to create your own pipelines, you can leverage source and resource methods from this verified source.

1. Configure the pipeline by specifying the pipeline name, destination, and dataset as follows:

```codeBlockLines_RjmQ
pipeline = dlt.pipeline(
       pipeline_name="facebook_ads",  # Use a custom name if desired
       destination="duckdb",  # Choose the appropriate destination (e.g., duckdb, redshift, post)
       dataset_name="facebook_ads_data"  # Use a custom name if desired
)

```

To read more about pipeline configuration, please refer to our [documentation](https://dlthub.com/docs/general-usage/pipeline).

2. To load all the data from campaigns, ad sets, ads, ad creatives, and leads:

```codeBlockLines_RjmQ
load_data = facebook_ads_source()
load_info = pipeline.run(load_data)
print(load_info)

```

3. To merge the Facebook Ads with the state "DISAPPROVED" and with ads state "PAUSED", you can do the following:

```codeBlockLines_RjmQ
load_data = facebook_ads_source()
# It is recommended to enable root key propagation on a source that is not a merge one by default. This is not required if you always use merge but below we start with replace
load_data.root_key = True

# Load only disapproved ads
load_data.ads.bind(states=("DISAPPROVED",))
load_info = pipeline.run(load_data.with_resources("ads"), write_disposition="replace")
print(load_info)

# Here we merge the paused ads but the disapproved ads stay there!
load_data = facebook_ads_source()
load_data.ads.bind(states=("PAUSED",))
load_info = pipeline.run(load_data.with_resources("ads"), write_disposition="merge")
print(load_info)

```

In the above steps, we first load the "ads" data with the "DISAPPROVED" state in _replace_ mode and then merge the ads data with the "PAUSED" state on that.

4. To load data with a custom field, for example, to load only "id" from Facebook ads, you can do the following:

```codeBlockLines_RjmQ
load_data = facebook_ads_source()
# Only loads ad ids, works the same for campaigns, leads, etc.
load_data.ads.bind(fields=("id",))
load_info = pipeline.run(load_data.with_resources("ads"))
print(load_info)

```

5. This pipeline includes an enrichment transformation called `enrich_ad_objects` that you can apply to any resource to obtain additional data per object using `object.get_api`. The following code demonstrates how to enrich objects by adding an enrichment transformation that includes additional fields.

```codeBlockLines_RjmQ
# You can reduce the chunk size for smaller requests
load_data = facebook_ads_source(chunk_size=2)

# Request only the "id" field for ad_creatives
load_data.ad_creatives.bind(fields=("id",))

# Add a transformation to the ad_creatives resource
load_data.ad_creatives.add_step(
       # Specify the AdCreative object type and request the desired fields
       enrich_ad_objects(AdCreative, DEFAULT_ADCREATIVE_FIELDS)
)

# Run the pipeline with the ad_creatives resource
load_info = pipeline.run(load_data.with_resources("ad_creatives"))

print(load_info)

```

In the above code, the "load\_data" object represents the Facebook Ads source, and we specify the desired chunk size for the requests. We then bind the "id" field for the "ad\_creatives" resource using the "bind()" method.

To enrich the ad\_creatives objects, we add a transformation using the "add\_step()" method. The "enrich\_ad\_objects" function is used to specify the AdCreative object type and request the fields defined in _DEFAULT\_ADCREATIVE\_FIELDS_.

Finally, we run the pipeline with the ad\_creatives resource and store the load information in the `load_info`.

6. You can also load insights reports incrementally with defined granularity levels, fields, breakdowns, etc., as defined in the `facebook_insights_source`. This function generates daily reports for a specified number of past days.

```codeBlockLines_RjmQ
load_data = facebook_insights_source(
       initial_load_past_days=30,
       attribution_window_days_lag=7,
       time_increment_days=1
)
load_info = pipeline.run(load_data)
print(load_info)

```

> By default, daily reports are generated from `initial_load_past_days` ago to today. On subsequent runs, only new reports are loaded, with the past `attribution_window_days_lag` days (default is 7) being refreshed to accommodate any changes. You can adjust `time_increment_days` to change report frequency (default set to one).

- [Setup guide](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads#setup-guide)
  - [Grab credentials](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads#grab-credentials)
  - [Initialize the verified source](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads#initialize-the-verified-source)
  - [Add credential](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads#add-credential)
- [Run the pipeline](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads#run-the-pipeline)
- [Sources and resources](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads#sources-and-resources)
  - [Default endpoints](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads#default-endpoints)
  - [Source `facebook_ads_source`](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads#source-facebook_ads_source)
  - [Resource `ads`](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads#resource-ads)
  - [Resources for `facebook_ads_source`](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads#resources-for-facebook_ads_source)
  - [Source `facebook_insights_source`](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads#source-facebook_insights_source)
  - [Resource `facebook_insights`](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads#resource-facebook_insights)
- [Customization](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads#customization)
  - [Create your own pipeline](https://dlthub.com/docs/dlt-ecosystem/verified-sources/facebook_ads#create-your-own-pipeline)

----- https://dlthub.com/docs/general-usage/resource#import-external-files -----

Version: 1.11.0 (latest)

On this page

## Declare a resource [​](https://dlthub.com/docs/general-usage/resource\#declare-a-resource "Direct link to Declare a resource")

A [resource](https://dlthub.com/docs/general-usage/glossary#resource) is an ( [optionally async](https://dlthub.com/docs/reference/performance#parallelism-within-a-pipeline)) function that yields data. To create a resource, we add the `@dlt.resource` decorator to that function.

Commonly used arguments:

- `name`: The name of the table generated by this resource. Defaults to the decorated function name.
- `write_disposition`: How should the data be loaded at the destination? Currently supported: `append`,
`replace`, and `merge`. Defaults to `append.`

Example:

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows():
	for i in range(10):
		yield {'id': i, 'example_string': 'abc'}

@dlt.source
def source_name():
    return generate_rows

```

To get the data of a resource, we could do:

```codeBlockLines_RjmQ
for row in generate_rows():
    print(row)

for row in source_name().resources.get('table_name'):
    print(row)

```

Typically, resources are declared and grouped with related resources within a [source](https://dlthub.com/docs/general-usage/source) function.

### Define schema [​](https://dlthub.com/docs/general-usage/resource\#define-schema "Direct link to Define schema")

`dlt` will infer the [schema](https://dlthub.com/docs/general-usage/schema) for tables associated with resources from the resource's data.
You can modify the generation process by using the table and column hints. The resource decorator accepts the following arguments:

1. `table_name`: the name of the table, if different from the resource name.
2. `primary_key` and `merge_key`: define the name of the columns (compound keys are allowed) that will receive those hints. Used in [incremental loading](https://dlthub.com/docs/general-usage/incremental-loading) and [merge loading](https://dlthub.com/docs/general-usage/merge-loading).
3. `columns`: lets you define one or more columns, including the data types, nullability, and other hints. The column definition is a `TypedDict`: `TTableSchemaColumns`. In the example below, we tell `dlt` that the column `tags` (containing a list of tags) in the `user` table should have type `json`, which means that it will be loaded as JSON/struct and not as a separate nested table.

```codeBlockLines_RjmQ
@dlt.resource(name="user", columns={"tags": {"data_type": "json"}})
def get_users():
  ...

# the `table_schema` method gets the table schema generated by a resource
print(get_users().compute_table_schema())

```

note

You can pass dynamic hints which are functions that take the data item as input and return a hint value. This lets you create table and column schemas depending on the data. See an [example below](https://dlthub.com/docs/general-usage/resource#adjust-schema-when-you-yield-data).

### Put a contract on tables, columns, and data [​](https://dlthub.com/docs/general-usage/resource\#put-a-contract-on-tables-columns-and-data "Direct link to Put a contract on tables, columns, and data")

Use the `schema_contract` argument to tell dlt how to [deal with new tables, data types, and bad data types](https://dlthub.com/docs/general-usage/schema-contracts). For example, if you set it to **freeze**, `dlt` will not allow for any new tables, columns, or data types to be introduced to the schema - it will raise an exception. Learn more about available contract modes [here](https://dlthub.com/docs/general-usage/schema-contracts#setting-up-the-contract).

### Define schema of nested tables [​](https://dlthub.com/docs/general-usage/resource\#define-schema-of-nested-tables "Direct link to Define schema of nested tables")

`dlt` creates [nested tables](https://dlthub.com/docs/general-usage/schema#nested-references-root-and-nested-tables) to store [list of objects](https://dlthub.com/docs/general-usage/destination-tables#nested-tables) if present in your data.
You can define the schema of such tables with `nested_hints` argument to `@dlt.resource`:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(
    nested_hints={
        "purchases": dlt.mark.make_nested_hints(
            columns=[{"name": "price", "data_type": "decimal"}],
            schema_contract={"columns": "freeze"},
        )
    },
)
def customers():
    """Load customer data from a simple python list."""
    yield [\
        {\
            "id": 1,\
            "name": "simon",\
            "city": "berlin",\
            "purchases": [{"id": 1, "name": "apple", "price": "1.50"}],\
        },\
    ]

```

Here we convert the `price` field in list of `purchases` to decimal type and set the schema contract to lock the list
of columns in it. We use convenience function `dlt.mark.make_nested_hints` to generate nested hints dictionary. You are
free to use it directly.

Mind that `purchases` list will be stored as table with name `customers__purchases`. When declaring nested hints you just need
to specify nested field(s) name(s). In case of deeper nesting ie. let's say each `purchase` has a list of `coupons` applied,
you can apply hints to coupons and define `customers__purchases__coupons` table schema:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(
    nested_hints={
        "purchases": {},
        ("purchases", "coupons"): {
            "columns": {"registered_at": {"data_type": "timestamp"}}
        }
    },
)
def customers():
    ...

```

Here we use `("purchases", "coupons")` to locate list at the depth of 2 and set the data type on `registered_at` column
to `timestamp`. We do that by directly using nested hints dict.
Note that we specified `purchases` with an empty list of hints. **You are required to specify all parent hints, even if they**
**are empty. Currently we are not adding missing path elements automatically**.

You can use `nested_hints` primarily to set column hints and schema contract, those work exactly as in case of root tables.

- `file_format` has no effect (not implemented yet)
- `write_disposition` works as expected but leads to unintended consequences (ie. you can set nested table to `replace`) while root table is `append`.
- `references` will create [table references](https://dlthub.com/docs/general-usage/schema#table-references-1) (annotations) as expected.
- `primary_key` and `merge_key`: **setting those will convert nested table into a regular table, with a separate write disposition, file format etc.** [It allows you to create custom table relationships ie. using natural primary and foreign keys present in the data.](https://dlthub.com/docs/general-usage/schema#generate-custom-linking-for-nested-tables)

tip

[REST API Source](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic) accepts `nested_hints` argument as well.

You can apply nested hints after the resource was created by using [apply\_hints](https://dlthub.com/docs/general-usage/resource#set-table-name-and-adjust-schema).

### Define a schema with Pydantic [​](https://dlthub.com/docs/general-usage/resource\#define-a-schema-with-pydantic "Direct link to Define a schema with Pydantic")

You can alternatively use a [Pydantic](https://pydantic-docs.helpmanual.io/) model to define the schema.
For example:

```codeBlockLines_RjmQ
from pydantic import BaseModel
from typing import List, Optional, Union

class Address(BaseModel):
    street: str
    city: str
    postal_code: str

class User(BaseModel):
    id: int
    name: str
    tags: List[str]
    email: Optional[str]
    address: Address
    status: Union[int, str]

@dlt.resource(name="user", columns=User)
def get_users():
    ...

```

The data types of the table columns are inferred from the types of the Pydantic fields. These use the same type conversions
as when the schema is automatically generated from the data.

Pydantic models integrate well with [schema contracts](https://dlthub.com/docs/general-usage/schema-contracts) as data validators.

Things to note:

- Fields with an `Optional` type are marked as `nullable`.
- Fields with a `Union` type are converted to the first (not `None`) type listed in the union. For example, `status: Union[int, str]` results in a `bigint` column.
- `list`, `dict`, and nested Pydantic model fields will use the `json` type, which means they'll be stored as a JSON object in the database instead of creating nested tables.

You can override this by configuring the Pydantic model:

```codeBlockLines_RjmQ
from typing import ClassVar
from dlt.common.libs.pydantic import DltConfig

class UserWithNesting(User):
  dlt_config: ClassVar[DltConfig] = {"skip_nested_types": True}

@dlt.resource(name="user", columns=UserWithNesting)
def get_users():
    ...

```

`"skip_nested_types"` omits any `dict`/ `list`/ `BaseModel` type fields from the schema, so dlt will fall back on the default
behavior of creating nested tables for these fields.

We do not support `RootModel` that validate simple types. You can add such a validator yourself, see [data filtering section](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data).

### Dispatch data to many tables [​](https://dlthub.com/docs/general-usage/resource\#dispatch-data-to-many-tables "Direct link to Dispatch data to many tables")

You can load data to many tables from a single resource. The most common case is a stream of events
of different types, each with a different data schema. To deal with this, you can use the `table_name`
argument on `dlt.resource`. You could pass the table name as a function with the data item as an
argument and the `table_name` string as a return value.

For example, a resource that loads GitHub repository events wants to send `issue`, `pull request`,
and `comment` events to separate tables. The type of the event is in the "type" field.

```codeBlockLines_RjmQ
# send item to a table with name item["type"]
@dlt.resource(table_name=lambda event: event['type'])
def repo_events() -> Iterator[TDataItems]:
    yield item

# the `table_schema` method gets the table schema generated by a resource and takes an optional
# data item to evaluate dynamic hints
print(repo_events().compute_table_schema({"type": "WatchEvent", "id": ...}))

```

In more advanced cases, you can dispatch data to different tables directly in the code of the
resource function:

```codeBlockLines_RjmQ
@dlt.resource
def repo_events() -> Iterator[TDataItems]:
    # mark the "item" to be sent to the table with the name item["type"]
    yield dlt.mark.with_table_name(item, item["type"])

```

### Parametrize a resource [​](https://dlthub.com/docs/general-usage/resource\#parametrize-a-resource "Direct link to Parametrize a resource")

You can add arguments to your resource functions like to any other. Below we parametrize our
`generate_rows` resource to generate the number of rows we request:

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

for row in generate_rows(10):
    print(row)

for row in generate_rows(20):
    print(row)

```

tip

You can mark some resource arguments as [configuration and credentials](https://dlthub.com/docs/general-usage/credentials) values so `dlt` can pass them automatically to your functions.

### Process resources with `dlt.transformer` [​](https://dlthub.com/docs/general-usage/resource\#process-resources-with-dlttransformer "Direct link to process-resources-with-dlttransformer")

You can feed data from one resource into another. The most common case is when you have an API that returns a list of objects (i.e., users) in one endpoint and user details in another. You can deal with this by declaring a resource that obtains a list of users and another resource that receives items from the list and downloads the profiles.

```codeBlockLines_RjmQ
@dlt.resource(write_disposition="replace")
def users(limit=None):
    for u in _get_users(limit):
        yield u

# Feed data from users as user_item below,
# all transformers must have at least one
# argument that will receive data from the parent resource
@dlt.transformer(data_from=users)
def users_details(user_item):
    for detail in _get_details(user_item["user_id"]):
        yield detail

# Just load the users_details.
# dlt figures out dependencies for you.
pipeline.run(users_details)

```

In the example above, `users_details` will receive data from the default instance of the `users` resource (with `limit` set to `None`). You can also use the **pipe \|** operator to bind resources dynamically.

```codeBlockLines_RjmQ
# You can be more explicit and use a pipe operator.
# With it, you can create dynamic pipelines where the dependencies
# are set at run time and resources are parametrized, i.e.,
# below we want to load only 100 users from the `users` endpoint.
pipeline.run(users(limit=100) | users_details)

```

tip

Transformers are allowed not only to **yield** but also to **return** values and can decorate **async** functions and [**async generators**](https://dlthub.com/docs/reference/performance#extract). Below we decorate an async function and request details on two pokemons. HTTP calls are made in parallel via the httpx library.

```codeBlockLines_RjmQ
import dlt
import httpx

@dlt.transformer
async def pokemon(id):
    async with httpx.AsyncClient() as client:
        r = await client.get(f"https://pokeapi.co/api/v2/pokemon/{id}")
        return r.json()

# Get Bulbasaur and Ivysaur (you need dlt 0.4.6 for the pipe operator working with lists).
print(list([1,2] | pokemon()))

```

### Declare a standalone resource [​](https://dlthub.com/docs/general-usage/resource\#declare-a-standalone-resource "Direct link to Declare a standalone resource")

A standalone resource is defined on a function that is top-level in a module (not an inner function) that accepts config and secrets values. Additionally, if the `standalone` flag is specified, the decorated function signature and docstring will be preserved. `dlt.resource` will just wrap the decorated function, and the user must call the wrapper to get the actual resource. Below we declare a `filesystem` resource that must be called before use.

```codeBlockLines_RjmQ
@dlt.resource(standalone=True)
def fs_resource(bucket_url=dlt.config.value):
  """List and yield files in `bucket_url`."""
  ...

# `filesystem` must be called before it is extracted or used in any other way.
pipeline.run(fs_resource("s3://my-bucket/reports"), table_name="reports")

```

Standalone may have a dynamic name that depends on the arguments passed to the decorated function. For example:

```codeBlockLines_RjmQ
@dlt.resource(standalone=True, name=lambda args: args["stream_name"])
def kinesis(stream_name: str):
    ...

kinesis_stream = kinesis("telemetry_stream")

```

`kinesis_stream` resource has a name **telemetry\_stream**.

### Declare parallel and async resources [​](https://dlthub.com/docs/general-usage/resource\#declare-parallel-and-async-resources "Direct link to Declare parallel and async resources")

You can extract multiple resources in parallel threads or with async IO.
To enable this for a sync resource, you can set the `parallelized` flag to `True` in the resource decorator:

```codeBlockLines_RjmQ
@dlt.resource(parallelized=True)
def get_users():
    for u in _get_users():
        yield u

@dlt.resource(parallelized=True)
def get_orders():
    for o in _get_orders():
        yield o

# users and orders will be iterated in parallel in two separate threads
pipeline.run([get_users(), get_orders()])

```

Async generators are automatically extracted concurrently with other resources:

```codeBlockLines_RjmQ
@dlt.resource
async def get_users():
    async for u in _get_users():  # Assuming _get_users is an async generator
        yield u

```

Please find more details in [extract performance](https://dlthub.com/docs/reference/performance#extract)

## Customize resources [​](https://dlthub.com/docs/general-usage/resource\#customize-resources "Direct link to Customize resources")

### Filter, transform, and pivot data [​](https://dlthub.com/docs/general-usage/resource\#filter-transform-and-pivot-data "Direct link to Filter, transform, and pivot data")

You can attach any number of transformations that are evaluated on an item-per-item basis to your
resource. The available transformation types:

- **map** \- transform the data item ( `resource.add_map`).
- **filter** \- filter the data item ( `resource.add_filter`).
- **yield map** \- a map that returns an iterator (so a single row may generate many rows -
`resource.add_yield_map`).

Example: We have a resource that loads a list of users from an API endpoint. We want to customize it
so:

1. We remove users with `user_id == "me"`.
2. We anonymize user data.

Here's our resource:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(write_disposition="replace")
def users():
    ...
    users = requests.get(RESOURCE_URL)
    ...
    yield users

```

Here's our script that defines transformations and loads the data:

```codeBlockLines_RjmQ
from pipedrive import users

def anonymize_user(user_data):
    user_data["user_id"] = _hash_str(user_data["user_id"])
    user_data["user_email"] = _hash_str(user_data["user_email"])
    return user_data

# add the filter and anonymize function to users resource and enumerate
for user in users().add_filter(lambda user: user["user_id"] != "me").add_map(anonymize_user):
    print(user)

```

### Reduce the nesting level of generated tables [​](https://dlthub.com/docs/general-usage/resource\#reduce-the-nesting-level-of-generated-tables "Direct link to Reduce the nesting level of generated tables")

You can limit how deep `dlt` goes when generating nested tables and flattening dicts into columns. By default, the library will descend
and generate nested tables for all nested lists, without limit.

note

`max_table_nesting` is optional so you can skip it, in this case, dlt will
use it from the source if it is specified there or fallback to the default
value which has 1000 as the maximum nesting level.

```codeBlockLines_RjmQ
import dlt

@dlt.resource(max_table_nesting=1)
def my_resource():
    yield {
        "id": 1,
        "name": "random name",
        "properties": [\
            {\
                "name": "customer_age",\
                "type": "int",\
                "label": "Age",\
                "notes": [\
                    {\
                        "text": "string",\
                        "author": "string",\
                    }\
                ]\
            }\
        ]
    }

```

In the example above, we want only 1 level of nested tables to be generated (so there are no nested
tables of a nested table). Typical settings:

- `max_table_nesting=0` will not generate nested tables and will not flatten dicts into columns at all. All nested data will be
represented as JSON.
- `max_table_nesting=1` will generate nested tables of root tables and nothing more. All nested
data in nested tables will be represented as JSON.

You can achieve the same effect after the resource instance is created:

```codeBlockLines_RjmQ
resource = my_resource()
resource.max_table_nesting = 0

```

Several data sources are prone to contain semi-structured documents with very deep nesting, i.e.,
MongoDB databases. Our practical experience is that setting the `max_nesting_level` to 2 or 3
produces the clearest and human-readable schemas.

### Sample from large data [​](https://dlthub.com/docs/general-usage/resource\#sample-from-large-data "Direct link to Sample from large data")

If your resource loads thousands of pages of data from a REST API or millions of rows from a database table, you may want to sample just a fragment of it in order to quickly see the dataset with example data and test your transformations, etc. To do this, you limit how many items will be yielded by a resource (or source) by calling the `add_limit` method. This method will close the generator that produces the data after the limit is reached.

In the example below, we load just the first 10 items from an infinite counter - that would otherwise never end.

```codeBlockLines_RjmQ
r = dlt.resource(itertools.count(), name="infinity").add_limit(10)
assert list(r) == list(range(10))

```

note

Note that `add_limit` **does not limit the number of records** but rather the "number of yields". Depending on how your resource is set up, the number of extracted rows may vary. For example, consider this resource:

```codeBlockLines_RjmQ
@dlt.resource
def my_resource():
    for i in range(100):
        yield [{"record_id": j} for j in range(15)]

dlt.pipeline(destination="duckdb").run(my_resource().add_limit(10))

```

The code above will extract `15*10=150` records. This is happening because in each iteration, 15 records are yielded, and we're limiting the number of iterations to 10.

Altenatively you can also apply a time limit to the resource. The code below will run the extraction for 10 seconds and extract how ever many items are yielded in that time. In combination with incrementals, this can be useful for batched loading or for loading on machines that have a run time limit.

```codeBlockLines_RjmQ
dlt.pipeline(destination="duckdb").run(my_resource().add_limit(max_time=10))

```

You can also apply a combination of both limits. In this case the extraction will stop as soon as either limit is reached.

```codeBlockLines_RjmQ
dlt.pipeline(destination="duckdb").run(my_resource().add_limit(max_items=10, max_time=10))

```

Some notes about the `add_limit`:

1. `add_limit` does not skip any items. It closes the iterator/generator that produces data after the limit is reached.
2. You cannot limit transformers. They should process all the data they receive fully to avoid inconsistencies in generated datasets.
3. Async resources with a limit added may occasionally produce one item more than the limit on some runs. This behavior is not deterministic.
4. Calling add limit on a resource will replace any previously set limits settings.
5. For time-limited resources, the timer starts when the first item is processed. When resources are processed sequentially (FIFO mode), each resource's time limit applies also sequentially. In the default round robin mode, the time limits will usually run concurrently.

tip

If you are parameterizing the value of `add_limit` and sometimes need it to be disabled, you can set `None` or `-1` to disable the limiting.
You can also set the limit to `0` for the resource to not yield any items.

### Set table name and adjust schema [​](https://dlthub.com/docs/general-usage/resource\#set-table-name-and-adjust-schema "Direct link to Set table name and adjust schema")

You can change the schema of a resource, whether it is standalone or part of a source. Look for a method named `apply_hints` which takes the same arguments as the resource decorator. Obviously, you should call this method before data is extracted from the resource. The example below converts an `append` resource loading the `users` table into a [merge](https://dlthub.com/docs/general-usage/merge-loading) resource that will keep just one updated record per `user_id`. It also adds ["last value" incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor) on the `created_at` column to prevent requesting again the already loaded records:

```codeBlockLines_RjmQ
tables = sql_database()
tables.users.apply_hints(
    write_disposition="merge",
    primary_key="user_id",
    incremental=dlt.sources.incremental("updated_at")
)
pipeline.run(tables)

```

To change the name of a table to which the resource will load data, do the following:

```codeBlockLines_RjmQ
tables = sql_database()
tables.users.table_name = "other_users"

```

### Adjust schema when you yield data [​](https://dlthub.com/docs/general-usage/resource\#adjust-schema-when-you-yield-data "Direct link to Adjust schema when you yield data")

You can set or update the table name, columns, and other schema elements when your resource is executed, and you already yield data. Such changes will be merged with the existing schema in the same way the `apply_hints` method above works. There are many reasons to adjust the schema at runtime. For example, when using Airflow, you should avoid lengthy operations (i.e., reflecting database tables) during the creation of the DAG, so it is better to do it when the DAG executes. You may also emit partial hints (i.e., precision and scale for decimal types) for columns to help `dlt` type inference.

```codeBlockLines_RjmQ
@dlt.resource
def sql_table(credentials, schema, table):
    # Create a SQL Alchemy engine
    engine = engine_from_credentials(credentials)
    engine.execution_options(stream_results=True)
    metadata = MetaData(schema=schema)
    # Reflect the table schema
    table_obj = Table(table, metadata, autoload_with=engine)

    for idx, batch in enumerate(table_rows(engine, table_obj)):
      if idx == 0:
        # Emit the first row with hints, table_to_columns and _get_primary_key are helpers that extract dlt schema from
        # SqlAlchemy model
        yield dlt.mark.with_hints(
            batch,
            dlt.mark.make_hints(columns=table_to_columns(table_obj), primary_key=_get_primary_key(table_obj)),
        )
      else:
        # Just yield all the other rows
        yield batch

```

In the example above, we use `dlt.mark.with_hints` and `dlt.mark.make_hints` to emit columns and primary key with the first extracted item. The table schema will be adjusted after the `batch` is processed in the extract pipeline but before any schema contracts are applied, and data is persisted in the load package.

tip

You can emit columns as a Pydantic model and use dynamic hints (i.e., lambda for table name) as well. You should avoid redefining `Incremental` this way.

### Import external files [​](https://dlthub.com/docs/general-usage/resource\#import-external-files "Direct link to Import external files")

You can import external files, i.e., CSV, Parquet, and JSONL, by yielding items marked with `with_file_import`, optionally passing a table schema corresponding to the imported file. dlt will not read, parse, or normalize any names (i.e., CSV or Arrow headers) and will attempt to copy the file into the destination as is.

```codeBlockLines_RjmQ
import os
import dlt
from dlt.sources.filesystem import filesystem

columns: List[TColumnSchema] = [\
    {"name": "id", "data_type": "bigint"},\
    {"name": "name", "data_type": "text"},\
    {"name": "description", "data_type": "text"},\
    {"name": "ordered_at", "data_type": "date"},\
    {"name": "price", "data_type": "decimal"},\
]

import_folder = "/tmp/import"

@dlt.transformer(columns=columns)
def orders(items: Iterator[FileItemDict]):
  for item in items:
    # copy the file locally
      dest_file = os.path.join(import_folder, item["file_name"])
      # download the file
      item.fsspec.download(item["file_url"], dest_file)
      # tell dlt to import the dest_file as `csv`
      yield dlt.mark.with_file_import(dest_file, "csv")

# use the filesystem core source to glob a bucket

downloader = filesystem(
  bucket_url="s3://my_bucket/csv",
  file_glob="today/*.csv.gz") | orders

info = pipeline.run(orders, destination="snowflake")

```

In the example above, we glob all zipped csv files present on **my\_bucket/csv/today** (using the `filesystem` verified source) and send file descriptors to the `orders` transformer. The transformer downloads and imports the files into the extract package. At the end, `dlt` sends them to Snowflake (the table will be created because we use `column` hints to define the schema).

If imported `csv` files are not in `dlt` [default format](https://dlthub.com/docs/dlt-ecosystem/file-formats/csv#default-settings), you may need to pass additional configuration.

```codeBlockLines_RjmQ
[destination.snowflake.csv_format]
delimiter="|"
include_header=false
on_error_continue=true

```

You can sniff the schema from the data, i.e., using DuckDB to infer the table schema from a CSV file. `dlt.mark.with_file_import` accepts additional arguments that you can use to pass hints at runtime.

note

- If you do not define any columns, the table will not be created in the destination. `dlt` will still attempt to load data into it, so if you create a fitting table upfront, the load process will succeed.
- Files are imported using hard links if possible to avoid copying and duplicating the storage space needed.

### Duplicate and rename resources [​](https://dlthub.com/docs/general-usage/resource\#duplicate-and-rename-resources "Direct link to Duplicate and rename resources")

There are cases when your resources are generic (i.e., bucket filesystem) and you want to load several instances of it (i.e., files from different folders) into separate tables. In the example below, we use the `filesystem` source to load csvs from two different folders into separate tables:

```codeBlockLines_RjmQ
@dlt.resource(standalone=True)
def fs_resource(bucket_url):
  # list and yield files in bucket_url
  ...

@dlt.transformer
def csv_reader(file_item):
  # load csv, parse, and yield rows in file_item
  ...

# create two extract pipes that list files from the bucket and send them to the reader.
# by default, both pipes will load data to the same table (csv_reader)
reports_pipe = fs_resource("s3://my-bucket/reports") | csv_reader()
transactions_pipe = fs_resource("s3://my-bucket/transactions") | csv_reader()

# so we rename resources to load to "reports" and "transactions" tables
pipeline.run(
  [reports_pipe.with_name("reports"), transactions_pipe.with_name("transactions")]
)

```

The `with_name` method returns a deep copy of the original resource, its data pipe, and the data pipes of a parent resource. A renamed clone is fully separated from the original resource (and other clones) when loading: it maintains a separate [resource state](https://dlthub.com/docs/general-usage/state#read-and-write-pipeline-state-in-a-resource) and will load to a table.

## Load resources [​](https://dlthub.com/docs/general-usage/resource\#load-resources "Direct link to Load resources")

You can pass individual resources or a list of resources to the `dlt.pipeline` object. The resources loaded outside the source context will be added to the [default schema](https://dlthub.com/docs/general-usage/schema) of the pipeline.

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

pipeline = dlt.pipeline(
    pipeline_name="rows_pipeline",
    destination="duckdb",
    dataset_name="rows_data"
)
# load an individual resource
pipeline.run(generate_rows(10))
# load a list of resources
pipeline.run([generate_rows(10), generate_rows(20)])

```

### Pick loader file format for a particular resource [​](https://dlthub.com/docs/general-usage/resource\#pick-loader-file-format-for-a-particular-resource "Direct link to Pick loader file format for a particular resource")

You can request a particular loader file format to be used for a resource.

```codeBlockLines_RjmQ
@dlt.resource(file_format="parquet")
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

```

The resource above will be saved and loaded from a Parquet file (if the destination supports it).

note

A special `file_format`: **preferred** will load the resource using a format that is preferred by a destination. This setting supersedes the `loader_file_format` passed to the `run` method.

### Do a full refresh [​](https://dlthub.com/docs/general-usage/resource\#do-a-full-refresh "Direct link to Do a full refresh")

To do a full refresh of an `append` or `merge` resource, you set the `refresh` argument on the `run` method to `drop_data`. This will truncate the tables without dropping them.

```codeBlockLines_RjmQ
p.run(merge_source(), refresh="drop_data")

```

You can also [fully drop the tables](https://dlthub.com/docs/general-usage/pipeline#refresh-pipeline-data-and-state) in the `merge_source`:

```codeBlockLines_RjmQ
p.run(merge_source(), refresh="drop_sources")

```

- [Declare a resource](https://dlthub.com/docs/general-usage/resource#declare-a-resource)
  - [Define schema](https://dlthub.com/docs/general-usage/resource#define-schema)
  - [Put a contract on tables, columns, and data](https://dlthub.com/docs/general-usage/resource#put-a-contract-on-tables-columns-and-data)
  - [Define schema of nested tables](https://dlthub.com/docs/general-usage/resource#define-schema-of-nested-tables)
  - [Define a schema with Pydantic](https://dlthub.com/docs/general-usage/resource#define-a-schema-with-pydantic)
  - [Dispatch data to many tables](https://dlthub.com/docs/general-usage/resource#dispatch-data-to-many-tables)
  - [Parametrize a resource](https://dlthub.com/docs/general-usage/resource#parametrize-a-resource)
  - [Process resources with `dlt.transformer`](https://dlthub.com/docs/general-usage/resource#process-resources-with-dlttransformer)
  - [Declare a standalone resource](https://dlthub.com/docs/general-usage/resource#declare-a-standalone-resource)
  - [Declare parallel and async resources](https://dlthub.com/docs/general-usage/resource#declare-parallel-and-async-resources)
- [Customize resources](https://dlthub.com/docs/general-usage/resource#customize-resources)
  - [Filter, transform, and pivot data](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data)
  - [Reduce the nesting level of generated tables](https://dlthub.com/docs/general-usage/resource#reduce-the-nesting-level-of-generated-tables)
  - [Sample from large data](https://dlthub.com/docs/general-usage/resource#sample-from-large-data)
  - [Set table name and adjust schema](https://dlthub.com/docs/general-usage/resource#set-table-name-and-adjust-schema)
  - [Adjust schema when you yield data](https://dlthub.com/docs/general-usage/resource#adjust-schema-when-you-yield-data)
  - [Import external files](https://dlthub.com/docs/general-usage/resource#import-external-files)
  - [Duplicate and rename resources](https://dlthub.com/docs/general-usage/resource#duplicate-and-rename-resources)
- [Load resources](https://dlthub.com/docs/general-usage/resource#load-resources)
  - [Pick loader file format for a particular resource](https://dlthub.com/docs/general-usage/resource#pick-loader-file-format-for-a-particular-resource)
  - [Do a full refresh](https://dlthub.com/docs/general-usage/resource#do-a-full-refresh)

----- https://dlthub.com/docs/general-usage/resource#pick-loader-file-format-for-a-particular-resource -----

Version: 1.11.0 (latest)

On this page

## Declare a resource [​](https://dlthub.com/docs/general-usage/resource\#declare-a-resource "Direct link to Declare a resource")

A [resource](https://dlthub.com/docs/general-usage/glossary#resource) is an ( [optionally async](https://dlthub.com/docs/reference/performance#parallelism-within-a-pipeline)) function that yields data. To create a resource, we add the `@dlt.resource` decorator to that function.

Commonly used arguments:

- `name`: The name of the table generated by this resource. Defaults to the decorated function name.
- `write_disposition`: How should the data be loaded at the destination? Currently supported: `append`,
`replace`, and `merge`. Defaults to `append.`

Example:

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows():
	for i in range(10):
		yield {'id': i, 'example_string': 'abc'}

@dlt.source
def source_name():
    return generate_rows

```

To get the data of a resource, we could do:

```codeBlockLines_RjmQ
for row in generate_rows():
    print(row)

for row in source_name().resources.get('table_name'):
    print(row)

```

Typically, resources are declared and grouped with related resources within a [source](https://dlthub.com/docs/general-usage/source) function.

### Define schema [​](https://dlthub.com/docs/general-usage/resource\#define-schema "Direct link to Define schema")

`dlt` will infer the [schema](https://dlthub.com/docs/general-usage/schema) for tables associated with resources from the resource's data.
You can modify the generation process by using the table and column hints. The resource decorator accepts the following arguments:

1. `table_name`: the name of the table, if different from the resource name.
2. `primary_key` and `merge_key`: define the name of the columns (compound keys are allowed) that will receive those hints. Used in [incremental loading](https://dlthub.com/docs/general-usage/incremental-loading) and [merge loading](https://dlthub.com/docs/general-usage/merge-loading).
3. `columns`: lets you define one or more columns, including the data types, nullability, and other hints. The column definition is a `TypedDict`: `TTableSchemaColumns`. In the example below, we tell `dlt` that the column `tags` (containing a list of tags) in the `user` table should have type `json`, which means that it will be loaded as JSON/struct and not as a separate nested table.

```codeBlockLines_RjmQ
@dlt.resource(name="user", columns={"tags": {"data_type": "json"}})
def get_users():
  ...

# the `table_schema` method gets the table schema generated by a resource
print(get_users().compute_table_schema())

```

note

You can pass dynamic hints which are functions that take the data item as input and return a hint value. This lets you create table and column schemas depending on the data. See an [example below](https://dlthub.com/docs/general-usage/resource#adjust-schema-when-you-yield-data).

### Put a contract on tables, columns, and data [​](https://dlthub.com/docs/general-usage/resource\#put-a-contract-on-tables-columns-and-data "Direct link to Put a contract on tables, columns, and data")

Use the `schema_contract` argument to tell dlt how to [deal with new tables, data types, and bad data types](https://dlthub.com/docs/general-usage/schema-contracts). For example, if you set it to **freeze**, `dlt` will not allow for any new tables, columns, or data types to be introduced to the schema - it will raise an exception. Learn more about available contract modes [here](https://dlthub.com/docs/general-usage/schema-contracts#setting-up-the-contract).

### Define schema of nested tables [​](https://dlthub.com/docs/general-usage/resource\#define-schema-of-nested-tables "Direct link to Define schema of nested tables")

`dlt` creates [nested tables](https://dlthub.com/docs/general-usage/schema#nested-references-root-and-nested-tables) to store [list of objects](https://dlthub.com/docs/general-usage/destination-tables#nested-tables) if present in your data.
You can define the schema of such tables with `nested_hints` argument to `@dlt.resource`:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(
    nested_hints={
        "purchases": dlt.mark.make_nested_hints(
            columns=[{"name": "price", "data_type": "decimal"}],
            schema_contract={"columns": "freeze"},
        )
    },
)
def customers():
    """Load customer data from a simple python list."""
    yield [\
        {\
            "id": 1,\
            "name": "simon",\
            "city": "berlin",\
            "purchases": [{"id": 1, "name": "apple", "price": "1.50"}],\
        },\
    ]

```

Here we convert the `price` field in list of `purchases` to decimal type and set the schema contract to lock the list
of columns in it. We use convenience function `dlt.mark.make_nested_hints` to generate nested hints dictionary. You are
free to use it directly.

Mind that `purchases` list will be stored as table with name `customers__purchases`. When declaring nested hints you just need
to specify nested field(s) name(s). In case of deeper nesting ie. let's say each `purchase` has a list of `coupons` applied,
you can apply hints to coupons and define `customers__purchases__coupons` table schema:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(
    nested_hints={
        "purchases": {},
        ("purchases", "coupons"): {
            "columns": {"registered_at": {"data_type": "timestamp"}}
        }
    },
)
def customers():
    ...

```

Here we use `("purchases", "coupons")` to locate list at the depth of 2 and set the data type on `registered_at` column
to `timestamp`. We do that by directly using nested hints dict.
Note that we specified `purchases` with an empty list of hints. **You are required to specify all parent hints, even if they**
**are empty. Currently we are not adding missing path elements automatically**.

You can use `nested_hints` primarily to set column hints and schema contract, those work exactly as in case of root tables.

- `file_format` has no effect (not implemented yet)
- `write_disposition` works as expected but leads to unintended consequences (ie. you can set nested table to `replace`) while root table is `append`.
- `references` will create [table references](https://dlthub.com/docs/general-usage/schema#table-references-1) (annotations) as expected.
- `primary_key` and `merge_key`: **setting those will convert nested table into a regular table, with a separate write disposition, file format etc.** [It allows you to create custom table relationships ie. using natural primary and foreign keys present in the data.](https://dlthub.com/docs/general-usage/schema#generate-custom-linking-for-nested-tables)

tip

[REST API Source](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic) accepts `nested_hints` argument as well.

You can apply nested hints after the resource was created by using [apply\_hints](https://dlthub.com/docs/general-usage/resource#set-table-name-and-adjust-schema).

### Define a schema with Pydantic [​](https://dlthub.com/docs/general-usage/resource\#define-a-schema-with-pydantic "Direct link to Define a schema with Pydantic")

You can alternatively use a [Pydantic](https://pydantic-docs.helpmanual.io/) model to define the schema.
For example:

```codeBlockLines_RjmQ
from pydantic import BaseModel
from typing import List, Optional, Union

class Address(BaseModel):
    street: str
    city: str
    postal_code: str

class User(BaseModel):
    id: int
    name: str
    tags: List[str]
    email: Optional[str]
    address: Address
    status: Union[int, str]

@dlt.resource(name="user", columns=User)
def get_users():
    ...

```

The data types of the table columns are inferred from the types of the Pydantic fields. These use the same type conversions
as when the schema is automatically generated from the data.

Pydantic models integrate well with [schema contracts](https://dlthub.com/docs/general-usage/schema-contracts) as data validators.

Things to note:

- Fields with an `Optional` type are marked as `nullable`.
- Fields with a `Union` type are converted to the first (not `None`) type listed in the union. For example, `status: Union[int, str]` results in a `bigint` column.
- `list`, `dict`, and nested Pydantic model fields will use the `json` type, which means they'll be stored as a JSON object in the database instead of creating nested tables.

You can override this by configuring the Pydantic model:

```codeBlockLines_RjmQ
from typing import ClassVar
from dlt.common.libs.pydantic import DltConfig

class UserWithNesting(User):
  dlt_config: ClassVar[DltConfig] = {"skip_nested_types": True}

@dlt.resource(name="user", columns=UserWithNesting)
def get_users():
    ...

```

`"skip_nested_types"` omits any `dict`/ `list`/ `BaseModel` type fields from the schema, so dlt will fall back on the default
behavior of creating nested tables for these fields.

We do not support `RootModel` that validate simple types. You can add such a validator yourself, see [data filtering section](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data).

### Dispatch data to many tables [​](https://dlthub.com/docs/general-usage/resource\#dispatch-data-to-many-tables "Direct link to Dispatch data to many tables")

You can load data to many tables from a single resource. The most common case is a stream of events
of different types, each with a different data schema. To deal with this, you can use the `table_name`
argument on `dlt.resource`. You could pass the table name as a function with the data item as an
argument and the `table_name` string as a return value.

For example, a resource that loads GitHub repository events wants to send `issue`, `pull request`,
and `comment` events to separate tables. The type of the event is in the "type" field.

```codeBlockLines_RjmQ
# send item to a table with name item["type"]
@dlt.resource(table_name=lambda event: event['type'])
def repo_events() -> Iterator[TDataItems]:
    yield item

# the `table_schema` method gets the table schema generated by a resource and takes an optional
# data item to evaluate dynamic hints
print(repo_events().compute_table_schema({"type": "WatchEvent", "id": ...}))

```

In more advanced cases, you can dispatch data to different tables directly in the code of the
resource function:

```codeBlockLines_RjmQ
@dlt.resource
def repo_events() -> Iterator[TDataItems]:
    # mark the "item" to be sent to the table with the name item["type"]
    yield dlt.mark.with_table_name(item, item["type"])

```

### Parametrize a resource [​](https://dlthub.com/docs/general-usage/resource\#parametrize-a-resource "Direct link to Parametrize a resource")

You can add arguments to your resource functions like to any other. Below we parametrize our
`generate_rows` resource to generate the number of rows we request:

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

for row in generate_rows(10):
    print(row)

for row in generate_rows(20):
    print(row)

```

tip

You can mark some resource arguments as [configuration and credentials](https://dlthub.com/docs/general-usage/credentials) values so `dlt` can pass them automatically to your functions.

### Process resources with `dlt.transformer` [​](https://dlthub.com/docs/general-usage/resource\#process-resources-with-dlttransformer "Direct link to process-resources-with-dlttransformer")

You can feed data from one resource into another. The most common case is when you have an API that returns a list of objects (i.e., users) in one endpoint and user details in another. You can deal with this by declaring a resource that obtains a list of users and another resource that receives items from the list and downloads the profiles.

```codeBlockLines_RjmQ
@dlt.resource(write_disposition="replace")
def users(limit=None):
    for u in _get_users(limit):
        yield u

# Feed data from users as user_item below,
# all transformers must have at least one
# argument that will receive data from the parent resource
@dlt.transformer(data_from=users)
def users_details(user_item):
    for detail in _get_details(user_item["user_id"]):
        yield detail

# Just load the users_details.
# dlt figures out dependencies for you.
pipeline.run(users_details)

```

In the example above, `users_details` will receive data from the default instance of the `users` resource (with `limit` set to `None`). You can also use the **pipe \|** operator to bind resources dynamically.

```codeBlockLines_RjmQ
# You can be more explicit and use a pipe operator.
# With it, you can create dynamic pipelines where the dependencies
# are set at run time and resources are parametrized, i.e.,
# below we want to load only 100 users from the `users` endpoint.
pipeline.run(users(limit=100) | users_details)

```

tip

Transformers are allowed not only to **yield** but also to **return** values and can decorate **async** functions and [**async generators**](https://dlthub.com/docs/reference/performance#extract). Below we decorate an async function and request details on two pokemons. HTTP calls are made in parallel via the httpx library.

```codeBlockLines_RjmQ
import dlt
import httpx

@dlt.transformer
async def pokemon(id):
    async with httpx.AsyncClient() as client:
        r = await client.get(f"https://pokeapi.co/api/v2/pokemon/{id}")
        return r.json()

# Get Bulbasaur and Ivysaur (you need dlt 0.4.6 for the pipe operator working with lists).
print(list([1,2] | pokemon()))

```

### Declare a standalone resource [​](https://dlthub.com/docs/general-usage/resource\#declare-a-standalone-resource "Direct link to Declare a standalone resource")

A standalone resource is defined on a function that is top-level in a module (not an inner function) that accepts config and secrets values. Additionally, if the `standalone` flag is specified, the decorated function signature and docstring will be preserved. `dlt.resource` will just wrap the decorated function, and the user must call the wrapper to get the actual resource. Below we declare a `filesystem` resource that must be called before use.

```codeBlockLines_RjmQ
@dlt.resource(standalone=True)
def fs_resource(bucket_url=dlt.config.value):
  """List and yield files in `bucket_url`."""
  ...

# `filesystem` must be called before it is extracted or used in any other way.
pipeline.run(fs_resource("s3://my-bucket/reports"), table_name="reports")

```

Standalone may have a dynamic name that depends on the arguments passed to the decorated function. For example:

```codeBlockLines_RjmQ
@dlt.resource(standalone=True, name=lambda args: args["stream_name"])
def kinesis(stream_name: str):
    ...

kinesis_stream = kinesis("telemetry_stream")

```

`kinesis_stream` resource has a name **telemetry\_stream**.

### Declare parallel and async resources [​](https://dlthub.com/docs/general-usage/resource\#declare-parallel-and-async-resources "Direct link to Declare parallel and async resources")

You can extract multiple resources in parallel threads or with async IO.
To enable this for a sync resource, you can set the `parallelized` flag to `True` in the resource decorator:

```codeBlockLines_RjmQ
@dlt.resource(parallelized=True)
def get_users():
    for u in _get_users():
        yield u

@dlt.resource(parallelized=True)
def get_orders():
    for o in _get_orders():
        yield o

# users and orders will be iterated in parallel in two separate threads
pipeline.run([get_users(), get_orders()])

```

Async generators are automatically extracted concurrently with other resources:

```codeBlockLines_RjmQ
@dlt.resource
async def get_users():
    async for u in _get_users():  # Assuming _get_users is an async generator
        yield u

```

Please find more details in [extract performance](https://dlthub.com/docs/reference/performance#extract)

## Customize resources [​](https://dlthub.com/docs/general-usage/resource\#customize-resources "Direct link to Customize resources")

### Filter, transform, and pivot data [​](https://dlthub.com/docs/general-usage/resource\#filter-transform-and-pivot-data "Direct link to Filter, transform, and pivot data")

You can attach any number of transformations that are evaluated on an item-per-item basis to your
resource. The available transformation types:

- **map** \- transform the data item ( `resource.add_map`).
- **filter** \- filter the data item ( `resource.add_filter`).
- **yield map** \- a map that returns an iterator (so a single row may generate many rows -
`resource.add_yield_map`).

Example: We have a resource that loads a list of users from an API endpoint. We want to customize it
so:

1. We remove users with `user_id == "me"`.
2. We anonymize user data.

Here's our resource:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(write_disposition="replace")
def users():
    ...
    users = requests.get(RESOURCE_URL)
    ...
    yield users

```

Here's our script that defines transformations and loads the data:

```codeBlockLines_RjmQ
from pipedrive import users

def anonymize_user(user_data):
    user_data["user_id"] = _hash_str(user_data["user_id"])
    user_data["user_email"] = _hash_str(user_data["user_email"])
    return user_data

# add the filter and anonymize function to users resource and enumerate
for user in users().add_filter(lambda user: user["user_id"] != "me").add_map(anonymize_user):
    print(user)

```

### Reduce the nesting level of generated tables [​](https://dlthub.com/docs/general-usage/resource\#reduce-the-nesting-level-of-generated-tables "Direct link to Reduce the nesting level of generated tables")

You can limit how deep `dlt` goes when generating nested tables and flattening dicts into columns. By default, the library will descend
and generate nested tables for all nested lists, without limit.

note

`max_table_nesting` is optional so you can skip it, in this case, dlt will
use it from the source if it is specified there or fallback to the default
value which has 1000 as the maximum nesting level.

```codeBlockLines_RjmQ
import dlt

@dlt.resource(max_table_nesting=1)
def my_resource():
    yield {
        "id": 1,
        "name": "random name",
        "properties": [\
            {\
                "name": "customer_age",\
                "type": "int",\
                "label": "Age",\
                "notes": [\
                    {\
                        "text": "string",\
                        "author": "string",\
                    }\
                ]\
            }\
        ]
    }

```

In the example above, we want only 1 level of nested tables to be generated (so there are no nested
tables of a nested table). Typical settings:

- `max_table_nesting=0` will not generate nested tables and will not flatten dicts into columns at all. All nested data will be
represented as JSON.
- `max_table_nesting=1` will generate nested tables of root tables and nothing more. All nested
data in nested tables will be represented as JSON.

You can achieve the same effect after the resource instance is created:

```codeBlockLines_RjmQ
resource = my_resource()
resource.max_table_nesting = 0

```

Several data sources are prone to contain semi-structured documents with very deep nesting, i.e.,
MongoDB databases. Our practical experience is that setting the `max_nesting_level` to 2 or 3
produces the clearest and human-readable schemas.

### Sample from large data [​](https://dlthub.com/docs/general-usage/resource\#sample-from-large-data "Direct link to Sample from large data")

If your resource loads thousands of pages of data from a REST API or millions of rows from a database table, you may want to sample just a fragment of it in order to quickly see the dataset with example data and test your transformations, etc. To do this, you limit how many items will be yielded by a resource (or source) by calling the `add_limit` method. This method will close the generator that produces the data after the limit is reached.

In the example below, we load just the first 10 items from an infinite counter - that would otherwise never end.

```codeBlockLines_RjmQ
r = dlt.resource(itertools.count(), name="infinity").add_limit(10)
assert list(r) == list(range(10))

```

note

Note that `add_limit` **does not limit the number of records** but rather the "number of yields". Depending on how your resource is set up, the number of extracted rows may vary. For example, consider this resource:

```codeBlockLines_RjmQ
@dlt.resource
def my_resource():
    for i in range(100):
        yield [{"record_id": j} for j in range(15)]

dlt.pipeline(destination="duckdb").run(my_resource().add_limit(10))

```

The code above will extract `15*10=150` records. This is happening because in each iteration, 15 records are yielded, and we're limiting the number of iterations to 10.

Altenatively you can also apply a time limit to the resource. The code below will run the extraction for 10 seconds and extract how ever many items are yielded in that time. In combination with incrementals, this can be useful for batched loading or for loading on machines that have a run time limit.

```codeBlockLines_RjmQ
dlt.pipeline(destination="duckdb").run(my_resource().add_limit(max_time=10))

```

You can also apply a combination of both limits. In this case the extraction will stop as soon as either limit is reached.

```codeBlockLines_RjmQ
dlt.pipeline(destination="duckdb").run(my_resource().add_limit(max_items=10, max_time=10))

```

Some notes about the `add_limit`:

1. `add_limit` does not skip any items. It closes the iterator/generator that produces data after the limit is reached.
2. You cannot limit transformers. They should process all the data they receive fully to avoid inconsistencies in generated datasets.
3. Async resources with a limit added may occasionally produce one item more than the limit on some runs. This behavior is not deterministic.
4. Calling add limit on a resource will replace any previously set limits settings.
5. For time-limited resources, the timer starts when the first item is processed. When resources are processed sequentially (FIFO mode), each resource's time limit applies also sequentially. In the default round robin mode, the time limits will usually run concurrently.

tip

If you are parameterizing the value of `add_limit` and sometimes need it to be disabled, you can set `None` or `-1` to disable the limiting.
You can also set the limit to `0` for the resource to not yield any items.

### Set table name and adjust schema [​](https://dlthub.com/docs/general-usage/resource\#set-table-name-and-adjust-schema "Direct link to Set table name and adjust schema")

You can change the schema of a resource, whether it is standalone or part of a source. Look for a method named `apply_hints` which takes the same arguments as the resource decorator. Obviously, you should call this method before data is extracted from the resource. The example below converts an `append` resource loading the `users` table into a [merge](https://dlthub.com/docs/general-usage/merge-loading) resource that will keep just one updated record per `user_id`. It also adds ["last value" incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor) on the `created_at` column to prevent requesting again the already loaded records:

```codeBlockLines_RjmQ
tables = sql_database()
tables.users.apply_hints(
    write_disposition="merge",
    primary_key="user_id",
    incremental=dlt.sources.incremental("updated_at")
)
pipeline.run(tables)

```

To change the name of a table to which the resource will load data, do the following:

```codeBlockLines_RjmQ
tables = sql_database()
tables.users.table_name = "other_users"

```

### Adjust schema when you yield data [​](https://dlthub.com/docs/general-usage/resource\#adjust-schema-when-you-yield-data "Direct link to Adjust schema when you yield data")

You can set or update the table name, columns, and other schema elements when your resource is executed, and you already yield data. Such changes will be merged with the existing schema in the same way the `apply_hints` method above works. There are many reasons to adjust the schema at runtime. For example, when using Airflow, you should avoid lengthy operations (i.e., reflecting database tables) during the creation of the DAG, so it is better to do it when the DAG executes. You may also emit partial hints (i.e., precision and scale for decimal types) for columns to help `dlt` type inference.

```codeBlockLines_RjmQ
@dlt.resource
def sql_table(credentials, schema, table):
    # Create a SQL Alchemy engine
    engine = engine_from_credentials(credentials)
    engine.execution_options(stream_results=True)
    metadata = MetaData(schema=schema)
    # Reflect the table schema
    table_obj = Table(table, metadata, autoload_with=engine)

    for idx, batch in enumerate(table_rows(engine, table_obj)):
      if idx == 0:
        # Emit the first row with hints, table_to_columns and _get_primary_key are helpers that extract dlt schema from
        # SqlAlchemy model
        yield dlt.mark.with_hints(
            batch,
            dlt.mark.make_hints(columns=table_to_columns(table_obj), primary_key=_get_primary_key(table_obj)),
        )
      else:
        # Just yield all the other rows
        yield batch

```

In the example above, we use `dlt.mark.with_hints` and `dlt.mark.make_hints` to emit columns and primary key with the first extracted item. The table schema will be adjusted after the `batch` is processed in the extract pipeline but before any schema contracts are applied, and data is persisted in the load package.

tip

You can emit columns as a Pydantic model and use dynamic hints (i.e., lambda for table name) as well. You should avoid redefining `Incremental` this way.

### Import external files [​](https://dlthub.com/docs/general-usage/resource\#import-external-files "Direct link to Import external files")

You can import external files, i.e., CSV, Parquet, and JSONL, by yielding items marked with `with_file_import`, optionally passing a table schema corresponding to the imported file. dlt will not read, parse, or normalize any names (i.e., CSV or Arrow headers) and will attempt to copy the file into the destination as is.

```codeBlockLines_RjmQ
import os
import dlt
from dlt.sources.filesystem import filesystem

columns: List[TColumnSchema] = [\
    {"name": "id", "data_type": "bigint"},\
    {"name": "name", "data_type": "text"},\
    {"name": "description", "data_type": "text"},\
    {"name": "ordered_at", "data_type": "date"},\
    {"name": "price", "data_type": "decimal"},\
]

import_folder = "/tmp/import"

@dlt.transformer(columns=columns)
def orders(items: Iterator[FileItemDict]):
  for item in items:
    # copy the file locally
      dest_file = os.path.join(import_folder, item["file_name"])
      # download the file
      item.fsspec.download(item["file_url"], dest_file)
      # tell dlt to import the dest_file as `csv`
      yield dlt.mark.with_file_import(dest_file, "csv")

# use the filesystem core source to glob a bucket

downloader = filesystem(
  bucket_url="s3://my_bucket/csv",
  file_glob="today/*.csv.gz") | orders

info = pipeline.run(orders, destination="snowflake")

```

In the example above, we glob all zipped csv files present on **my\_bucket/csv/today** (using the `filesystem` verified source) and send file descriptors to the `orders` transformer. The transformer downloads and imports the files into the extract package. At the end, `dlt` sends them to Snowflake (the table will be created because we use `column` hints to define the schema).

If imported `csv` files are not in `dlt` [default format](https://dlthub.com/docs/dlt-ecosystem/file-formats/csv#default-settings), you may need to pass additional configuration.

```codeBlockLines_RjmQ
[destination.snowflake.csv_format]
delimiter="|"
include_header=false
on_error_continue=true

```

You can sniff the schema from the data, i.e., using DuckDB to infer the table schema from a CSV file. `dlt.mark.with_file_import` accepts additional arguments that you can use to pass hints at runtime.

note

- If you do not define any columns, the table will not be created in the destination. `dlt` will still attempt to load data into it, so if you create a fitting table upfront, the load process will succeed.
- Files are imported using hard links if possible to avoid copying and duplicating the storage space needed.

### Duplicate and rename resources [​](https://dlthub.com/docs/general-usage/resource\#duplicate-and-rename-resources "Direct link to Duplicate and rename resources")

There are cases when your resources are generic (i.e., bucket filesystem) and you want to load several instances of it (i.e., files from different folders) into separate tables. In the example below, we use the `filesystem` source to load csvs from two different folders into separate tables:

```codeBlockLines_RjmQ
@dlt.resource(standalone=True)
def fs_resource(bucket_url):
  # list and yield files in bucket_url
  ...

@dlt.transformer
def csv_reader(file_item):
  # load csv, parse, and yield rows in file_item
  ...

# create two extract pipes that list files from the bucket and send them to the reader.
# by default, both pipes will load data to the same table (csv_reader)
reports_pipe = fs_resource("s3://my-bucket/reports") | csv_reader()
transactions_pipe = fs_resource("s3://my-bucket/transactions") | csv_reader()

# so we rename resources to load to "reports" and "transactions" tables
pipeline.run(
  [reports_pipe.with_name("reports"), transactions_pipe.with_name("transactions")]
)

```

The `with_name` method returns a deep copy of the original resource, its data pipe, and the data pipes of a parent resource. A renamed clone is fully separated from the original resource (and other clones) when loading: it maintains a separate [resource state](https://dlthub.com/docs/general-usage/state#read-and-write-pipeline-state-in-a-resource) and will load to a table.

## Load resources [​](https://dlthub.com/docs/general-usage/resource\#load-resources "Direct link to Load resources")

You can pass individual resources or a list of resources to the `dlt.pipeline` object. The resources loaded outside the source context will be added to the [default schema](https://dlthub.com/docs/general-usage/schema) of the pipeline.

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

pipeline = dlt.pipeline(
    pipeline_name="rows_pipeline",
    destination="duckdb",
    dataset_name="rows_data"
)
# load an individual resource
pipeline.run(generate_rows(10))
# load a list of resources
pipeline.run([generate_rows(10), generate_rows(20)])

```

### Pick loader file format for a particular resource [​](https://dlthub.com/docs/general-usage/resource\#pick-loader-file-format-for-a-particular-resource "Direct link to Pick loader file format for a particular resource")

You can request a particular loader file format to be used for a resource.

```codeBlockLines_RjmQ
@dlt.resource(file_format="parquet")
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

```

The resource above will be saved and loaded from a Parquet file (if the destination supports it).

note

A special `file_format`: **preferred** will load the resource using a format that is preferred by a destination. This setting supersedes the `loader_file_format` passed to the `run` method.

### Do a full refresh [​](https://dlthub.com/docs/general-usage/resource\#do-a-full-refresh "Direct link to Do a full refresh")

To do a full refresh of an `append` or `merge` resource, you set the `refresh` argument on the `run` method to `drop_data`. This will truncate the tables without dropping them.

```codeBlockLines_RjmQ
p.run(merge_source(), refresh="drop_data")

```

You can also [fully drop the tables](https://dlthub.com/docs/general-usage/pipeline#refresh-pipeline-data-and-state) in the `merge_source`:

```codeBlockLines_RjmQ
p.run(merge_source(), refresh="drop_sources")

```

- [Declare a resource](https://dlthub.com/docs/general-usage/resource#declare-a-resource)
  - [Define schema](https://dlthub.com/docs/general-usage/resource#define-schema)
  - [Put a contract on tables, columns, and data](https://dlthub.com/docs/general-usage/resource#put-a-contract-on-tables-columns-and-data)
  - [Define schema of nested tables](https://dlthub.com/docs/general-usage/resource#define-schema-of-nested-tables)
  - [Define a schema with Pydantic](https://dlthub.com/docs/general-usage/resource#define-a-schema-with-pydantic)
  - [Dispatch data to many tables](https://dlthub.com/docs/general-usage/resource#dispatch-data-to-many-tables)
  - [Parametrize a resource](https://dlthub.com/docs/general-usage/resource#parametrize-a-resource)
  - [Process resources with `dlt.transformer`](https://dlthub.com/docs/general-usage/resource#process-resources-with-dlttransformer)
  - [Declare a standalone resource](https://dlthub.com/docs/general-usage/resource#declare-a-standalone-resource)
  - [Declare parallel and async resources](https://dlthub.com/docs/general-usage/resource#declare-parallel-and-async-resources)
- [Customize resources](https://dlthub.com/docs/general-usage/resource#customize-resources)
  - [Filter, transform, and pivot data](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data)
  - [Reduce the nesting level of generated tables](https://dlthub.com/docs/general-usage/resource#reduce-the-nesting-level-of-generated-tables)
  - [Sample from large data](https://dlthub.com/docs/general-usage/resource#sample-from-large-data)
  - [Set table name and adjust schema](https://dlthub.com/docs/general-usage/resource#set-table-name-and-adjust-schema)
  - [Adjust schema when you yield data](https://dlthub.com/docs/general-usage/resource#adjust-schema-when-you-yield-data)
  - [Import external files](https://dlthub.com/docs/general-usage/resource#import-external-files)
  - [Duplicate and rename resources](https://dlthub.com/docs/general-usage/resource#duplicate-and-rename-resources)
- [Load resources](https://dlthub.com/docs/general-usage/resource#load-resources)
  - [Pick loader file format for a particular resource](https://dlthub.com/docs/general-usage/resource#pick-loader-file-format-for-a-particular-resource)
  - [Do a full refresh](https://dlthub.com/docs/general-usage/resource#do-a-full-refresh)

----- https://dlthub.com/docs/general-usage/incremental/cursor#using-end_value-for-backfill -----

Version: 1.11.0 (latest)

On this page

In most REST APIs (and other data sources, i.e., database tables), you can request new or updated data by passing a timestamp or ID of the "last" record to a query. The API/database returns just the new/updated records from which you take the maximum/minimum timestamp/ID for the next load.

To do incremental loading this way, we need to:

- Figure out which field is used to track changes (the so-called **cursor field**) (e.g., "inserted\_at", "updated\_at", etc.);
- Determine how to pass the "last" (maximum/minimum) value of the cursor field to an API to get just new or modified data (how we do this depends on the source API).

Once you've figured that out, `dlt` takes care of finding maximum/minimum cursor field values, removing duplicates, and managing the state with the last values of the cursor. Take a look at the GitHub example below, where we request recently created issues.

```codeBlockLines_RjmQ
@dlt.resource(primary_key="id")
def repo_issues(
    access_token,
    repository,
    updated_at = dlt.sources.incremental("updated_at", initial_value="1970-01-01T00:00:00Z")
):
    # Get issues since "updated_at" stored in state on previous run (or initial_value on first run)
    for page in _get_issues_page(access_token, repository, since=updated_at.start_value):
        yield page
        # Last_value is updated after every page
        print(updated_at.last_value)

```

Here we add an `updated_at` argument that will receive incremental state, initialized to `1970-01-01T00:00:00Z`. It is configured to track the `updated_at` field in issues yielded by the `repo_issues` resource. It will store the newest `updated_at` value in `dlt` [state](https://dlthub.com/docs/general-usage/state) and make it available in `updated_at.start_value` on the next pipeline run. This value is inserted in the `_get_issues_page` function into the request query param **since** to the [GitHub API](https://docs.github.com/en/rest/issues/issues?#list-repository-issues).

In essence, the `dlt.sources.incremental` instance above:

- **updated\_at.initial\_value** which is always equal to "1970-01-01T00:00:00Z" passed in the constructor
- **updated\_at.start\_value** a maximum `updated_at` value from the previous run or the **initial\_value** on the first run
- **updated\_at.last\_value** a "real-time" `updated_at` value updated with each yielded item or page. Before the first yield, it equals **start\_value**
- **updated\_at.end\_value** (here not used) [marking the end of the backfill range](https://dlthub.com/docs/general-usage/incremental/cursor#using-end_value-for-backfill)

When paginating, you probably need the **start\_value** which does not change during the execution of the resource, however, most paginators will return a **next page** link which you should use.

Behind the scenes, dlt will deduplicate the results, i.e., in case the last issue is returned again ( `updated_at` filter is inclusive) and skip already loaded ones.

In the example below, we incrementally load the GitHub events, where the API does not let us filter for the newest events - it always returns all of them. Nevertheless, `dlt` will load only the new items, filtering out all the duplicates and past issues.

```codeBlockLines_RjmQ
# Use naming function in table name to generate separate tables for each event
@dlt.resource(primary_key="id", table_name=lambda i: i['type'])  # type: ignore
def repo_events(
    last_created_at = dlt.sources.incremental("created_at", initial_value="1970-01-01T00:00:00Z", last_value_func=max), row_order="desc"
) -> Iterator[TDataItems]:
    repos_path = "/repos/%s/%s/events" % (urllib.parse.quote(owner), urllib.parse.quote(name))
    for page in _get_rest_pages(access_token, repos_path + "?per_page=100"):
        yield page

```

We just yield all the events and `dlt` does the filtering (using the `id` column declared as `primary_key`).

GitHub returns events ordered from newest to oldest. So we declare the `rows_order` as **descending** to [stop requesting more pages once the incremental value is out of range](https://dlthub.com/docs/general-usage/incremental/cursor#declare-row-order-to-not-request-unnecessary-data). We stop requesting more data from the API after finding the first event with `created_at` earlier than `initial_value`.

note

`dlt.sources.incremental` is implemented as a [filter function](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data) that is executed **after** all other transforms you add with `add_map` or `add_filter`. This means that you can manipulate the data item before the incremental filter sees it. For example:

- You can create a surrogate primary key from other columns
- You can modify the cursor value or create a new field composed of other fields
- Dump Pydantic models to Python dicts to allow incremental to find custom values

[Data validation with Pydantic](https://dlthub.com/docs/general-usage/schema-contracts#use-pydantic-models-for-data-validation) happens **before** incremental filtering.

## Max, min, or custom `last_value_func` [​](https://dlthub.com/docs/general-usage/incremental/cursor\#max-min-or-custom-last_value_func "Direct link to max-min-or-custom-last_value_func")

`dlt.sources.incremental` allows you to choose a function that orders (compares) cursor values to the current `last_value`.

- The default function is the built-in `max`, which returns the larger value of the two.
- Another built-in, `min`, returns the smaller value.

You can also pass your custom function. This lets you define
`last_value` on nested types, i.e., dictionaries, and store indexes of last values, not just simple
types. The `last_value` argument is a [JSON Path](https://github.com/json-path/JsonPath#operators)
and lets you select nested data (including the whole data item when `$` is used).
The example below creates a last value which is a dictionary holding a max `created_at` value for each
created table name:

```codeBlockLines_RjmQ
def by_event_type(event):
    last_value = None
    if len(event) == 1:
        item, = event
    else:
        item, last_value = event

    if last_value is None:
        last_value = {}
    else:
        last_value = dict(last_value)
    item_type = item["type"]
    last_value[item_type] = max(item["created_at"], last_value.get(item_type, "1970-01-01T00:00:00Z"))
    return last_value

@dlt.resource(primary_key="id", table_name=lambda i: i['type'])
def get_events(last_created_at = dlt.sources.incremental("$", last_value_func=by_event_type)):
    with open("tests/normalize/cases/github.events.load_page_1_duck.json", "r", encoding="utf-8") as f:
        yield json.load(f)

```

## Using `end_value` for backfill [​](https://dlthub.com/docs/general-usage/incremental/cursor\#using-end_value-for-backfill "Direct link to using-end_value-for-backfill")

You can specify both initial and end dates when defining incremental loading. Let's go back to our Github example:

```codeBlockLines_RjmQ
@dlt.resource(primary_key="id")
def repo_issues(
    access_token,
    repository,
    created_at=dlt.sources.incremental("created_at", initial_value="1970-01-01T00:00:00Z", end_value="2022-07-01T00:00:00Z")
):
    # get issues created from the last "created_at" value
    for page in _get_issues_page(access_token, repository, since=created_at.start_value, until=created_at.end_value):
        yield page

```

Above, we use the `initial_value` and `end_value` arguments of the `incremental` to define the range of issues that we want to retrieve
and pass this range to the Github API ( `since` and `until`). As in the examples above, `dlt` will make sure that only the issues from
the defined range are returned.

Please note that when `end_date` is specified, `dlt` **will not modify the existing incremental state**. The backfill is **stateless** and:

1. You can run backfill and incremental load in parallel (i.e., in an Airflow DAG) in a single pipeline.
2. You can partition your backfill into several smaller chunks and run them in parallel as well.

To define specific ranges to load, you can simply override the incremental argument in the resource, for example:

```codeBlockLines_RjmQ
july_issues = repo_issues(
    created_at=dlt.sources.incremental(
        initial_value='2022-07-01T00:00:00Z', end_value='2022-08-01T00:00:00Z'
    )
)
august_issues = repo_issues(
    created_at=dlt.sources.incremental(
        initial_value='2022-08-01T00:00:00Z', end_value='2022-09-01T00:00:00Z'
    )
)
...

```

Note that dlt's incremental filtering considers the ranges half-closed. `initial_value` is inclusive, `end_value` is exclusive, so chaining ranges like above works without overlaps. This behaviour can be changed with the `range_start` (default `"closed"`) and `range_end` (default `"open"`) arguments.

## Declare row order to not request unnecessary data [​](https://dlthub.com/docs/general-usage/incremental/cursor\#declare-row-order-to-not-request-unnecessary-data "Direct link to Declare row order to not request unnecessary data")

With the `row_order` argument set, dlt will stop retrieving data from the data source (e.g., GitHub API) if it detects that the values of the cursor field are out of the range of **start** and **end** values.

In particular:

- dlt stops processing when the resource yields any item with a cursor value _equal to or greater than_ the `end_value` and `row_order` is set to **asc**. ( `end_value` is not included)
- dlt stops processing when the resource yields any item with a cursor value _lower_ than the `last_value` and `row_order` is set to **desc**. ( `last_value` is included)

note

"higher" and "lower" here refer to when the default `last_value_func` is used ( `max()`),
when using `min()` "higher" and "lower" are inverted.

caution

If you use `row_order`, **make sure that the data source returns ordered records** (ascending / descending) on the cursor field,
e.g., if an API returns results both higher and lower
than the given `end_value` in no particular order, data reading stops and you'll miss the data items that were out of order.

Row order is most useful when:

1. The data source does **not** offer start/end filtering of results (e.g., there is no `start_time/end_time` query parameter or similar).
2. The source returns results **ordered by the cursor field**.

The GitHub events example is exactly such a case. The results are ordered on cursor value descending, but there's no way to tell the API to limit returned items to those created before a certain date. Without the `row_order` setting, we'd be getting all events, each time we extract the `github_events` resource.

In the same fashion, the `row_order` can be used to **optimize backfill** so we don't continue
making unnecessary API requests after the end of the range is reached. For example:

```codeBlockLines_RjmQ
@dlt.resource(primary_key="id")
def tickets(
    zendesk_client,
    updated_at=dlt.sources.incremental(
        "updated_at",
        initial_value="2023-01-01T00:00:00Z",
        end_value="2023-02-01T00:00:00Z",
        row_order="asc"
    ),
):
    for page in zendesk_client.get_pages(
        "/api/v2/incremental/tickets", "tickets", start_time=updated_at.start_value
    ):
        yield page

```

In this example, we're loading tickets from Zendesk. The Zendesk API yields items paginated and ordered from oldest to newest,
but only offers a `start_time` parameter for filtering, so we cannot tell it to
stop retrieving data at `end_value`. Instead, we set `row_order` to `asc` and `dlt` will stop
getting more pages from the API after the first page with a cursor value `updated_at` is found older
than `end_value`.

caution

In rare cases when you use Incremental with a transformer, `dlt` will not be able to automatically close
the generator associated with a row that is out of range. You can still call the `can_close()` method on
incremental and exit the yield loop when true.

tip

The `dlt.sources.incremental` instance provides `start_out_of_range` and `end_out_of_range`
attributes which are set when the resource yields an element with a higher/lower cursor value than the
initial or end values. If you do not want `dlt` to stop processing automatically and instead want to handle such events yourself, do not specify `row_order`:

```codeBlockLines_RjmQ
@dlt.transformer(primary_key="id")
def tickets(
    zendesk_client,
    updated_at=dlt.sources.incremental(
        "updated_at",
        initial_value="2023-01-01T00:00:00Z",
        end_value="2023-02-01T00:00:00Z",
        row_order="asc"
    ),
):
    for page in zendesk_client.get_pages(
        "/api/v2/incremental/tickets", "tickets", start_time=updated_at.start_value
    ):
        yield page
        # Stop loading when we reach the end value
        if updated_at.end_out_of_range:
            return

```

## Deduplicate overlapping ranges with primary key [​](https://dlthub.com/docs/general-usage/incremental/cursor\#deduplicate-overlapping-ranges-with-primary-key "Direct link to Deduplicate overlapping ranges with primary key")

`Incremental` **does not** deduplicate datasets like the **merge** write disposition does. However, it ensures that when another portion of data is extracted, records that were previously loaded won't be included again. `dlt` assumes that you load a range of data, where the lower bound is inclusive (i.e., greater than or equal). This ensures that you never lose any data but will also re-acquire some rows. For example, if you have a database table with a cursor field on `updated_at` which has a day resolution, then there's a high chance that after you extract data on a given day, more records will still be added. When you extract on the next day, you should reacquire data from the last day to ensure all records are present; however, this will create overlap with data from the previous extract.

By default, a content hash (a hash of the JSON representation of a row) will be used to deduplicate. This may be slow, so `dlt.sources.incremental` will inherit the primary key that is set on the resource. You can optionally set a `primary_key` that is used exclusively to deduplicate and which does not become a table hint. The same setting lets you disable the deduplication altogether when an empty tuple is passed. Below, we pass `primary_key` directly to `incremental` to disable deduplication. That overrides the `delta` primary\_key set in the resource:

```codeBlockLines_RjmQ
@dlt.resource(primary_key="delta")
# disable the unique value check by passing () as primary key to incremental
def some_data(last_timestamp=dlt.sources.incremental("item.ts", primary_key=())):
    for i in range(-10, 10):
        yield {"delta": i, "item": {"ts": pendulum.now().timestamp()}}

```

This deduplication process is always enabled when `range_start` is set to `"closed"` (default).
When you pass `range_start="open"` no deduplication is done as it is not needed as rows with the previous cursor value are excluded. This can be a useful optimization to avoid the performance overhead of deduplication if the cursor field is guaranteed to be unique.

## Using `dlt.sources.incremental` with dynamically created resources [​](https://dlthub.com/docs/general-usage/incremental/cursor\#using-dltsourcesincremental-with-dynamically-created-resources "Direct link to using-dltsourcesincremental-with-dynamically-created-resources")

When resources are [created dynamically](https://dlthub.com/docs/general-usage/source#create-resources-dynamically), it is possible to use the `dlt.sources.incremental` definition as well.

```codeBlockLines_RjmQ
@dlt.source
def stripe():
    # declare a generator function
    def get_resource(
        endpoints: List[str] = ENDPOINTS,
        created: dlt.sources.incremental=dlt.sources.incremental("created")
    ):
        ...

    # create resources for several endpoints on a single decorator function
    for endpoint in endpoints:
        yield dlt.resource(
            get_resource,
            name=endpoint.value,
            write_disposition="merge",
            primary_key="id"
        )(endpoint)

```

Please note that in the example above, `get_resource` is passed as a function to `dlt.resource` to which we bind the endpoint: **dlt.resource(...)(endpoint)**.

caution

The typical mistake is to pass a generator (not a function) as below:

`yield dlt.resource(get_resource(endpoint), name=endpoint.value, write_disposition="merge", primary_key="id")`.

Here we call **get\_resource(endpoint)** and that creates an un-evaluated generator on which the resource is created. That prevents `dlt` from controlling the **created** argument during runtime and will result in an `IncrementalUnboundError` exception.

## Using Airflow schedule for backfill and incremental loading [​](https://dlthub.com/docs/general-usage/incremental/cursor\#using-airflow-schedule-for-backfill-and-incremental-loading "Direct link to Using Airflow schedule for backfill and incremental loading")

When [running an Airflow task](https://dlthub.com/docs/walkthroughs/deploy-a-pipeline/deploy-with-airflow-composer#2-modify-dag-file), you can opt-in your resource to get the `initial_value`/ `start_value` and `end_value` from the Airflow schedule associated with your DAG. Let's assume that the **Zendesk tickets** resource contains a year of data with thousands of tickets. We want to backfill the last year of data week by week and then continue with incremental loading daily.

```codeBlockLines_RjmQ
@dlt.resource(primary_key="id")
def tickets(
    zendesk_client,
    updated_at=dlt.sources.incremental[int](
        "updated_at",
        allow_external_schedulers=True
    ),
):
    for page in zendesk_client.get_pages(
        "/api/v2/incremental/tickets", "tickets", start_time=updated_at.start_value
    ):
        yield page

```

We opt-in to the Airflow scheduler by setting `allow_external_schedulers` to `True`:

1. When running on Airflow, the start and end values are controlled by Airflow and the dlt [state](https://dlthub.com/docs/general-usage/state) is not used.
2. In all other environments, the `incremental` behaves as usual, maintaining the dlt state.

Let's generate a deployment with `dlt deploy zendesk_pipeline.py airflow-composer` and customize the DAG:

```codeBlockLines_RjmQ
from dlt.helpers.airflow_helper import PipelineTasksGroup

@dag(
    schedule_interval='@weekly',
    start_date=pendulum.DateTime(2023, 2, 1),
    end_date=pendulum.DateTime(2023, 8, 1),
    catchup=True,
    max_active_runs=1,
    default_args=default_task_args
)
def zendesk_backfill_bigquery():
    tasks = PipelineTasksGroup("zendesk_support_backfill", use_data_folder=False, wipe_local_data=True)

    # import zendesk like in the demo script
    from zendesk import zendesk_support

    pipeline = dlt.pipeline(
        pipeline_name="zendesk_support_backfill",
        dataset_name="zendesk_support_data",
        destination='bigquery',
    )
    # select only incremental endpoints in support api
    data = zendesk_support().with_resources("tickets", "ticket_events", "ticket_metric_events")
    # create the source, the "serialize" decompose option will convert dlt resources into Airflow tasks. use "none" to disable it
    tasks.add_run(pipeline, data, decompose="serialize", trigger_rule="all_done", retries=0, provide_context=True)

zendesk_backfill_bigquery()

```

What got customized:

1. We use a weekly schedule and want to get the data from February 2023 ( `start_date`) until the end of July ( `end_date`).
2. We make Airflow generate all weekly runs ( `catchup` is True).
3. We create `zendesk_support` resources where we select only the incremental resources we want to backfill.

When you enable the DAG in Airflow, it will generate several runs and start executing them, starting in February and ending in August. Your resource will receive subsequent weekly intervals starting with `2023-02-12, 00:00:00 UTC` to `2023-02-19, 00:00:00 UTC`.

You can repurpose the DAG above to start loading new data incrementally after (or during) the backfill:

```codeBlockLines_RjmQ
@dag(
    schedule_interval='@daily',
    start_date=pendulum.DateTime(2023, 2, 1),
    catchup=False,
    max_active_runs=1,
    default_args=default_task_args
)
def zendesk_new_bigquery():
    tasks = PipelineTasksGroup("zendesk_support_new", use_data_folder=False, wipe_local_data=True)

    # import your source from pipeline script
    from zendesk import zendesk_support

    pipeline = dlt.pipeline(
        pipeline_name="zendesk_support_new",
        dataset_name="zendesk_support_data",
        destination='bigquery',
    )
    tasks.add_run(pipeline, zendesk_support(), decompose="serialize", trigger_rule="all_done", retries=0, provide_context=True)

```

Above, we switch to a daily schedule and disable catchup and end date. We also load all the support resources to the same dataset as backfill ( `zendesk_support_data`).
If you want to run this DAG parallel with the backfill DAG, change the pipeline name, for example, to `zendesk_support_new` as above.

**Under the hood**

Before `dlt` starts executing incremental resources, it looks for `data_interval_start` and `data_interval_end` Airflow task context variables. These are mapped to `initial_value` and `end_value` of the `Incremental` class:

1. `dlt` is smart enough to convert Airflow datetime to ISO strings or Unix timestamps if your resource is using them. In our example, we instantiate `updated_at=dlt.sources.incremental[int]`, where we declare the last value type to be **int**. `dlt` can also infer the type if you provide the `initial_value` argument.
2. If `data_interval_end` is in the future or is None, `dlt` sets the `end_value` to **now**.
3. If `data_interval_start` == `data_interval_end`, we have a manually triggered DAG run. In that case, `data_interval_end` will also be set to **now**.

**Manual runs**

You can run DAGs manually, but you must remember to specify the Airflow logical date of the run in the past (use the Run with config option). For such a run, `dlt` will load all data from that past date until now.
If you do not specify the past date, a run with a range (now, now) will happen, yielding no data.

## Reading incremental loading parameters from configuration [​](https://dlthub.com/docs/general-usage/incremental/cursor\#reading-incremental-loading-parameters-from-configuration "Direct link to Reading incremental loading parameters from configuration")

Consider the example below for reading incremental loading parameters from "config.toml". We create a `generate_incremental_records` resource that yields "id", "idAfter", and "name". This resource retrieves `cursor_path` and `initial_value` from "config.toml".

1. In "config.toml", define the `cursor_path` and `initial_value` as:

```codeBlockLines_RjmQ
# Configuration snippet for an incremental resource
[pipeline_with_incremental.sources.id_after]
cursor_path = "idAfter"
initial_value = 10

```

`cursor_path` is assigned the value "idAfter" with an initial value of 10.

2. Here's how the `generate_incremental_records` resource uses the `cursor_path` defined in "config.toml":

```codeBlockLines_RjmQ
@dlt.resource(table_name="incremental_records")
def generate_incremental_records(id_after: dlt.sources.incremental = dlt.config.value):
       for i in range(150):
           yield {"id": i, "idAfter": i, "name": "name-" + str(i)}

pipeline = dlt.pipeline(
       pipeline_name="pipeline_with_incremental",
       destination="duckdb",
)

pipeline.run(generate_incremental_records)

```

`id_after` incrementally stores the latest `cursor_path` value for future pipeline runs.

## Loading when incremental cursor path is missing or value is None/NULL [​](https://dlthub.com/docs/general-usage/incremental/cursor\#loading-when-incremental-cursor-path-is-missing-or-value-is-nonenull "Direct link to Loading when incremental cursor path is missing or value is None/NULL")

You can customize the incremental processing of dlt by setting the parameter `on_cursor_value_missing`.

When loading incrementally with the default settings, there are two assumptions:

1. Each row contains the cursor path.
2. Each row is expected to contain a value at the cursor path that is not `None`.

For example, the two following source data will raise an error:

```codeBlockLines_RjmQ
@dlt.resource
def some_data_without_cursor_path(updated_at=dlt.sources.incremental("updated_at")):
    yield [\
        {"id": 1, "created_at": 1, "updated_at": 1},\
        {"id": 2, "created_at": 2},  # cursor field is missing\
    ]

list(some_data_without_cursor_path())

@dlt.resource
def some_data_without_cursor_value(updated_at=dlt.sources.incremental("updated_at")):
    yield [\
        {"id": 1, "created_at": 1, "updated_at": 1},\
        {"id": 3, "created_at": 4, "updated_at": None},  # value at cursor field is None\
    ]

list(some_data_without_cursor_value())

```

To process a data set where some records do not include the incremental cursor path or where the values at the cursor path are `None`, there are the following four options:

1. Configure the incremental load to raise an exception in case there is a row where the cursor path is missing or has the value `None` using `incremental(..., on_cursor_value_missing="raise")`. This is the default behavior.
2. Configure the incremental load to tolerate the missing cursor path and `None` values using `incremental(..., on_cursor_value_missing="include")`.
3. Configure the incremental load to exclude the missing cursor path and `None` values using `incremental(..., on_cursor_value_missing="exclude")`.
4. Before the incremental processing begins: Ensure that the incremental field is present and transform the values at the incremental cursor to a value different from `None`. [See docs below](https://dlthub.com/docs/general-usage/incremental/cursor#transform-records-before-incremental-processing)

Here is an example of including rows where the incremental cursor value is missing or `None`:

```codeBlockLines_RjmQ
@dlt.resource
def some_data(updated_at=dlt.sources.incremental("updated_at", on_cursor_value_missing="include")):
    yield [\
        {"id": 1, "created_at": 1, "updated_at": 1},\
        {"id": 2, "created_at": 2},\
        {"id": 3, "created_at": 4, "updated_at": None},\
    ]

result = list(some_data())
assert len(result) == 3
assert result[1] == {"id": 2, "created_at": 2}
assert result[2] == {"id": 3, "created_at": 4, "updated_at": None}

```

If you do not want to import records without the cursor path or where the value at the cursor path is `None`, use the following incremental configuration:

```codeBlockLines_RjmQ
@dlt.resource
def some_data(updated_at=dlt.sources.incremental("updated_at", on_cursor_value_missing="exclude")):
    yield [\
        {"id": 1, "created_at": 1, "updated_at": 1},\
        {"id": 2, "created_at": 2},\
        {"id": 3, "created_at": 4, "updated_at": None},\
    ]

result = list(some_data())
assert len(result) == 1

```

## Transform records before incremental processing [​](https://dlthub.com/docs/general-usage/incremental/cursor\#transform-records-before-incremental-processing "Direct link to Transform records before incremental processing")

If you want to load data that includes `None` values, you can transform the records before the incremental processing.
You can add steps to the pipeline that [filter, transform, or pivot your data](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data).

caution

It is important to set the `insert_at` parameter of the `add_map` function to control the order of execution and ensure that your custom steps are executed before the incremental processing starts.
In the following example, the step of data yielding is at `index = 0`, the custom transformation at `index = 1`, and the incremental processing at `index = 2`.

See below how you can modify rows before the incremental processing using `add_map()` and filter rows using `add_filter()`.

```codeBlockLines_RjmQ
@dlt.resource
def some_data(updated_at=dlt.sources.incremental("updated_at")):
    yield [\
        {"id": 1, "created_at": 1, "updated_at": 1},\
        {"id": 2, "created_at": 2, "updated_at": 2},\
        {"id": 3, "created_at": 4, "updated_at": None},\
    ]

def set_default_updated_at(record):
    if record.get("updated_at") is None:
        record["updated_at"] = record.get("created_at")
    return record

# Modifies records before the incremental processing
with_default_values = some_data().add_map(set_default_updated_at, insert_at=1)
result = list(with_default_values)
assert len(result) == 3
assert result[2]["updated_at"] == 4

# Removes records before the incremental processing
without_none = some_data().add_filter(lambda r: r.get("updated_at") is not None, insert_at=1)
result_filtered = list(without_none)
assert len(result_filtered) == 2

```

- [Max, min, or custom `last_value_func`](https://dlthub.com/docs/general-usage/incremental/cursor#max-min-or-custom-last_value_func)
- [Using `end_value` for backfill](https://dlthub.com/docs/general-usage/incremental/cursor#using-end_value-for-backfill)
- [Declare row order to not request unnecessary data](https://dlthub.com/docs/general-usage/incremental/cursor#declare-row-order-to-not-request-unnecessary-data)
- [Deduplicate overlapping ranges with primary key](https://dlthub.com/docs/general-usage/incremental/cursor#deduplicate-overlapping-ranges-with-primary-key)
- [Using `dlt.sources.incremental` with dynamically created resources](https://dlthub.com/docs/general-usage/incremental/cursor#using-dltsourcesincremental-with-dynamically-created-resources)
- [Using Airflow schedule for backfill and incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor#using-airflow-schedule-for-backfill-and-incremental-loading)
- [Reading incremental loading parameters from configuration](https://dlthub.com/docs/general-usage/incremental/cursor#reading-incremental-loading-parameters-from-configuration)
- [Loading when incremental cursor path is missing or value is None/NULL](https://dlthub.com/docs/general-usage/incremental/cursor#loading-when-incremental-cursor-path-is-missing-or-value-is-nonenull)
- [Transform records before incremental processing](https://dlthub.com/docs/general-usage/incremental/cursor#transform-records-before-incremental-processing)

----- https://dlthub.com/docs/general-usage/resource#reduce-the-nesting-level-of-generated-tables -----

Version: 1.11.0 (latest)

On this page

## Declare a resource [​](https://dlthub.com/docs/general-usage/resource\#declare-a-resource "Direct link to Declare a resource")

A [resource](https://dlthub.com/docs/general-usage/glossary#resource) is an ( [optionally async](https://dlthub.com/docs/reference/performance#parallelism-within-a-pipeline)) function that yields data. To create a resource, we add the `@dlt.resource` decorator to that function.

Commonly used arguments:

- `name`: The name of the table generated by this resource. Defaults to the decorated function name.
- `write_disposition`: How should the data be loaded at the destination? Currently supported: `append`,
`replace`, and `merge`. Defaults to `append.`

Example:

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows():
	for i in range(10):
		yield {'id': i, 'example_string': 'abc'}

@dlt.source
def source_name():
    return generate_rows

```

To get the data of a resource, we could do:

```codeBlockLines_RjmQ
for row in generate_rows():
    print(row)

for row in source_name().resources.get('table_name'):
    print(row)

```

Typically, resources are declared and grouped with related resources within a [source](https://dlthub.com/docs/general-usage/source) function.

### Define schema [​](https://dlthub.com/docs/general-usage/resource\#define-schema "Direct link to Define schema")

`dlt` will infer the [schema](https://dlthub.com/docs/general-usage/schema) for tables associated with resources from the resource's data.
You can modify the generation process by using the table and column hints. The resource decorator accepts the following arguments:

1. `table_name`: the name of the table, if different from the resource name.
2. `primary_key` and `merge_key`: define the name of the columns (compound keys are allowed) that will receive those hints. Used in [incremental loading](https://dlthub.com/docs/general-usage/incremental-loading) and [merge loading](https://dlthub.com/docs/general-usage/merge-loading).
3. `columns`: lets you define one or more columns, including the data types, nullability, and other hints. The column definition is a `TypedDict`: `TTableSchemaColumns`. In the example below, we tell `dlt` that the column `tags` (containing a list of tags) in the `user` table should have type `json`, which means that it will be loaded as JSON/struct and not as a separate nested table.

```codeBlockLines_RjmQ
@dlt.resource(name="user", columns={"tags": {"data_type": "json"}})
def get_users():
  ...

# the `table_schema` method gets the table schema generated by a resource
print(get_users().compute_table_schema())

```

note

You can pass dynamic hints which are functions that take the data item as input and return a hint value. This lets you create table and column schemas depending on the data. See an [example below](https://dlthub.com/docs/general-usage/resource#adjust-schema-when-you-yield-data).

### Put a contract on tables, columns, and data [​](https://dlthub.com/docs/general-usage/resource\#put-a-contract-on-tables-columns-and-data "Direct link to Put a contract on tables, columns, and data")

Use the `schema_contract` argument to tell dlt how to [deal with new tables, data types, and bad data types](https://dlthub.com/docs/general-usage/schema-contracts). For example, if you set it to **freeze**, `dlt` will not allow for any new tables, columns, or data types to be introduced to the schema - it will raise an exception. Learn more about available contract modes [here](https://dlthub.com/docs/general-usage/schema-contracts#setting-up-the-contract).

### Define schema of nested tables [​](https://dlthub.com/docs/general-usage/resource\#define-schema-of-nested-tables "Direct link to Define schema of nested tables")

`dlt` creates [nested tables](https://dlthub.com/docs/general-usage/schema#nested-references-root-and-nested-tables) to store [list of objects](https://dlthub.com/docs/general-usage/destination-tables#nested-tables) if present in your data.
You can define the schema of such tables with `nested_hints` argument to `@dlt.resource`:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(
    nested_hints={
        "purchases": dlt.mark.make_nested_hints(
            columns=[{"name": "price", "data_type": "decimal"}],
            schema_contract={"columns": "freeze"},
        )
    },
)
def customers():
    """Load customer data from a simple python list."""
    yield [\
        {\
            "id": 1,\
            "name": "simon",\
            "city": "berlin",\
            "purchases": [{"id": 1, "name": "apple", "price": "1.50"}],\
        },\
    ]

```

Here we convert the `price` field in list of `purchases` to decimal type and set the schema contract to lock the list
of columns in it. We use convenience function `dlt.mark.make_nested_hints` to generate nested hints dictionary. You are
free to use it directly.

Mind that `purchases` list will be stored as table with name `customers__purchases`. When declaring nested hints you just need
to specify nested field(s) name(s). In case of deeper nesting ie. let's say each `purchase` has a list of `coupons` applied,
you can apply hints to coupons and define `customers__purchases__coupons` table schema:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(
    nested_hints={
        "purchases": {},
        ("purchases", "coupons"): {
            "columns": {"registered_at": {"data_type": "timestamp"}}
        }
    },
)
def customers():
    ...

```

Here we use `("purchases", "coupons")` to locate list at the depth of 2 and set the data type on `registered_at` column
to `timestamp`. We do that by directly using nested hints dict.
Note that we specified `purchases` with an empty list of hints. **You are required to specify all parent hints, even if they**
**are empty. Currently we are not adding missing path elements automatically**.

You can use `nested_hints` primarily to set column hints and schema contract, those work exactly as in case of root tables.

- `file_format` has no effect (not implemented yet)
- `write_disposition` works as expected but leads to unintended consequences (ie. you can set nested table to `replace`) while root table is `append`.
- `references` will create [table references](https://dlthub.com/docs/general-usage/schema#table-references-1) (annotations) as expected.
- `primary_key` and `merge_key`: **setting those will convert nested table into a regular table, with a separate write disposition, file format etc.** [It allows you to create custom table relationships ie. using natural primary and foreign keys present in the data.](https://dlthub.com/docs/general-usage/schema#generate-custom-linking-for-nested-tables)

tip

[REST API Source](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic) accepts `nested_hints` argument as well.

You can apply nested hints after the resource was created by using [apply\_hints](https://dlthub.com/docs/general-usage/resource#set-table-name-and-adjust-schema).

### Define a schema with Pydantic [​](https://dlthub.com/docs/general-usage/resource\#define-a-schema-with-pydantic "Direct link to Define a schema with Pydantic")

You can alternatively use a [Pydantic](https://pydantic-docs.helpmanual.io/) model to define the schema.
For example:

```codeBlockLines_RjmQ
from pydantic import BaseModel
from typing import List, Optional, Union

class Address(BaseModel):
    street: str
    city: str
    postal_code: str

class User(BaseModel):
    id: int
    name: str
    tags: List[str]
    email: Optional[str]
    address: Address
    status: Union[int, str]

@dlt.resource(name="user", columns=User)
def get_users():
    ...

```

The data types of the table columns are inferred from the types of the Pydantic fields. These use the same type conversions
as when the schema is automatically generated from the data.

Pydantic models integrate well with [schema contracts](https://dlthub.com/docs/general-usage/schema-contracts) as data validators.

Things to note:

- Fields with an `Optional` type are marked as `nullable`.
- Fields with a `Union` type are converted to the first (not `None`) type listed in the union. For example, `status: Union[int, str]` results in a `bigint` column.
- `list`, `dict`, and nested Pydantic model fields will use the `json` type, which means they'll be stored as a JSON object in the database instead of creating nested tables.

You can override this by configuring the Pydantic model:

```codeBlockLines_RjmQ
from typing import ClassVar
from dlt.common.libs.pydantic import DltConfig

class UserWithNesting(User):
  dlt_config: ClassVar[DltConfig] = {"skip_nested_types": True}

@dlt.resource(name="user", columns=UserWithNesting)
def get_users():
    ...

```

`"skip_nested_types"` omits any `dict`/ `list`/ `BaseModel` type fields from the schema, so dlt will fall back on the default
behavior of creating nested tables for these fields.

We do not support `RootModel` that validate simple types. You can add such a validator yourself, see [data filtering section](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data).

### Dispatch data to many tables [​](https://dlthub.com/docs/general-usage/resource\#dispatch-data-to-many-tables "Direct link to Dispatch data to many tables")

You can load data to many tables from a single resource. The most common case is a stream of events
of different types, each with a different data schema. To deal with this, you can use the `table_name`
argument on `dlt.resource`. You could pass the table name as a function with the data item as an
argument and the `table_name` string as a return value.

For example, a resource that loads GitHub repository events wants to send `issue`, `pull request`,
and `comment` events to separate tables. The type of the event is in the "type" field.

```codeBlockLines_RjmQ
# send item to a table with name item["type"]
@dlt.resource(table_name=lambda event: event['type'])
def repo_events() -> Iterator[TDataItems]:
    yield item

# the `table_schema` method gets the table schema generated by a resource and takes an optional
# data item to evaluate dynamic hints
print(repo_events().compute_table_schema({"type": "WatchEvent", "id": ...}))

```

In more advanced cases, you can dispatch data to different tables directly in the code of the
resource function:

```codeBlockLines_RjmQ
@dlt.resource
def repo_events() -> Iterator[TDataItems]:
    # mark the "item" to be sent to the table with the name item["type"]
    yield dlt.mark.with_table_name(item, item["type"])

```

### Parametrize a resource [​](https://dlthub.com/docs/general-usage/resource\#parametrize-a-resource "Direct link to Parametrize a resource")

You can add arguments to your resource functions like to any other. Below we parametrize our
`generate_rows` resource to generate the number of rows we request:

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

for row in generate_rows(10):
    print(row)

for row in generate_rows(20):
    print(row)

```

tip

You can mark some resource arguments as [configuration and credentials](https://dlthub.com/docs/general-usage/credentials) values so `dlt` can pass them automatically to your functions.

### Process resources with `dlt.transformer` [​](https://dlthub.com/docs/general-usage/resource\#process-resources-with-dlttransformer "Direct link to process-resources-with-dlttransformer")

You can feed data from one resource into another. The most common case is when you have an API that returns a list of objects (i.e., users) in one endpoint and user details in another. You can deal with this by declaring a resource that obtains a list of users and another resource that receives items from the list and downloads the profiles.

```codeBlockLines_RjmQ
@dlt.resource(write_disposition="replace")
def users(limit=None):
    for u in _get_users(limit):
        yield u

# Feed data from users as user_item below,
# all transformers must have at least one
# argument that will receive data from the parent resource
@dlt.transformer(data_from=users)
def users_details(user_item):
    for detail in _get_details(user_item["user_id"]):
        yield detail

# Just load the users_details.
# dlt figures out dependencies for you.
pipeline.run(users_details)

```

In the example above, `users_details` will receive data from the default instance of the `users` resource (with `limit` set to `None`). You can also use the **pipe \|** operator to bind resources dynamically.

```codeBlockLines_RjmQ
# You can be more explicit and use a pipe operator.
# With it, you can create dynamic pipelines where the dependencies
# are set at run time and resources are parametrized, i.e.,
# below we want to load only 100 users from the `users` endpoint.
pipeline.run(users(limit=100) | users_details)

```

tip

Transformers are allowed not only to **yield** but also to **return** values and can decorate **async** functions and [**async generators**](https://dlthub.com/docs/reference/performance#extract). Below we decorate an async function and request details on two pokemons. HTTP calls are made in parallel via the httpx library.

```codeBlockLines_RjmQ
import dlt
import httpx

@dlt.transformer
async def pokemon(id):
    async with httpx.AsyncClient() as client:
        r = await client.get(f"https://pokeapi.co/api/v2/pokemon/{id}")
        return r.json()

# Get Bulbasaur and Ivysaur (you need dlt 0.4.6 for the pipe operator working with lists).
print(list([1,2] | pokemon()))

```

### Declare a standalone resource [​](https://dlthub.com/docs/general-usage/resource\#declare-a-standalone-resource "Direct link to Declare a standalone resource")

A standalone resource is defined on a function that is top-level in a module (not an inner function) that accepts config and secrets values. Additionally, if the `standalone` flag is specified, the decorated function signature and docstring will be preserved. `dlt.resource` will just wrap the decorated function, and the user must call the wrapper to get the actual resource. Below we declare a `filesystem` resource that must be called before use.

```codeBlockLines_RjmQ
@dlt.resource(standalone=True)
def fs_resource(bucket_url=dlt.config.value):
  """List and yield files in `bucket_url`."""
  ...

# `filesystem` must be called before it is extracted or used in any other way.
pipeline.run(fs_resource("s3://my-bucket/reports"), table_name="reports")

```

Standalone may have a dynamic name that depends on the arguments passed to the decorated function. For example:

```codeBlockLines_RjmQ
@dlt.resource(standalone=True, name=lambda args: args["stream_name"])
def kinesis(stream_name: str):
    ...

kinesis_stream = kinesis("telemetry_stream")

```

`kinesis_stream` resource has a name **telemetry\_stream**.

### Declare parallel and async resources [​](https://dlthub.com/docs/general-usage/resource\#declare-parallel-and-async-resources "Direct link to Declare parallel and async resources")

You can extract multiple resources in parallel threads or with async IO.
To enable this for a sync resource, you can set the `parallelized` flag to `True` in the resource decorator:

```codeBlockLines_RjmQ
@dlt.resource(parallelized=True)
def get_users():
    for u in _get_users():
        yield u

@dlt.resource(parallelized=True)
def get_orders():
    for o in _get_orders():
        yield o

# users and orders will be iterated in parallel in two separate threads
pipeline.run([get_users(), get_orders()])

```

Async generators are automatically extracted concurrently with other resources:

```codeBlockLines_RjmQ
@dlt.resource
async def get_users():
    async for u in _get_users():  # Assuming _get_users is an async generator
        yield u

```

Please find more details in [extract performance](https://dlthub.com/docs/reference/performance#extract)

## Customize resources [​](https://dlthub.com/docs/general-usage/resource\#customize-resources "Direct link to Customize resources")

### Filter, transform, and pivot data [​](https://dlthub.com/docs/general-usage/resource\#filter-transform-and-pivot-data "Direct link to Filter, transform, and pivot data")

You can attach any number of transformations that are evaluated on an item-per-item basis to your
resource. The available transformation types:

- **map** \- transform the data item ( `resource.add_map`).
- **filter** \- filter the data item ( `resource.add_filter`).
- **yield map** \- a map that returns an iterator (so a single row may generate many rows -
`resource.add_yield_map`).

Example: We have a resource that loads a list of users from an API endpoint. We want to customize it
so:

1. We remove users with `user_id == "me"`.
2. We anonymize user data.

Here's our resource:

```codeBlockLines_RjmQ
import dlt

@dlt.resource(write_disposition="replace")
def users():
    ...
    users = requests.get(RESOURCE_URL)
    ...
    yield users

```

Here's our script that defines transformations and loads the data:

```codeBlockLines_RjmQ
from pipedrive import users

def anonymize_user(user_data):
    user_data["user_id"] = _hash_str(user_data["user_id"])
    user_data["user_email"] = _hash_str(user_data["user_email"])
    return user_data

# add the filter and anonymize function to users resource and enumerate
for user in users().add_filter(lambda user: user["user_id"] != "me").add_map(anonymize_user):
    print(user)

```

### Reduce the nesting level of generated tables [​](https://dlthub.com/docs/general-usage/resource\#reduce-the-nesting-level-of-generated-tables "Direct link to Reduce the nesting level of generated tables")

You can limit how deep `dlt` goes when generating nested tables and flattening dicts into columns. By default, the library will descend
and generate nested tables for all nested lists, without limit.

note

`max_table_nesting` is optional so you can skip it, in this case, dlt will
use it from the source if it is specified there or fallback to the default
value which has 1000 as the maximum nesting level.

```codeBlockLines_RjmQ
import dlt

@dlt.resource(max_table_nesting=1)
def my_resource():
    yield {
        "id": 1,
        "name": "random name",
        "properties": [\
            {\
                "name": "customer_age",\
                "type": "int",\
                "label": "Age",\
                "notes": [\
                    {\
                        "text": "string",\
                        "author": "string",\
                    }\
                ]\
            }\
        ]
    }

```

In the example above, we want only 1 level of nested tables to be generated (so there are no nested
tables of a nested table). Typical settings:

- `max_table_nesting=0` will not generate nested tables and will not flatten dicts into columns at all. All nested data will be
represented as JSON.
- `max_table_nesting=1` will generate nested tables of root tables and nothing more. All nested
data in nested tables will be represented as JSON.

You can achieve the same effect after the resource instance is created:

```codeBlockLines_RjmQ
resource = my_resource()
resource.max_table_nesting = 0

```

Several data sources are prone to contain semi-structured documents with very deep nesting, i.e.,
MongoDB databases. Our practical experience is that setting the `max_nesting_level` to 2 or 3
produces the clearest and human-readable schemas.

### Sample from large data [​](https://dlthub.com/docs/general-usage/resource\#sample-from-large-data "Direct link to Sample from large data")

If your resource loads thousands of pages of data from a REST API or millions of rows from a database table, you may want to sample just a fragment of it in order to quickly see the dataset with example data and test your transformations, etc. To do this, you limit how many items will be yielded by a resource (or source) by calling the `add_limit` method. This method will close the generator that produces the data after the limit is reached.

In the example below, we load just the first 10 items from an infinite counter - that would otherwise never end.

```codeBlockLines_RjmQ
r = dlt.resource(itertools.count(), name="infinity").add_limit(10)
assert list(r) == list(range(10))

```

note

Note that `add_limit` **does not limit the number of records** but rather the "number of yields". Depending on how your resource is set up, the number of extracted rows may vary. For example, consider this resource:

```codeBlockLines_RjmQ
@dlt.resource
def my_resource():
    for i in range(100):
        yield [{"record_id": j} for j in range(15)]

dlt.pipeline(destination="duckdb").run(my_resource().add_limit(10))

```

The code above will extract `15*10=150` records. This is happening because in each iteration, 15 records are yielded, and we're limiting the number of iterations to 10.

Altenatively you can also apply a time limit to the resource. The code below will run the extraction for 10 seconds and extract how ever many items are yielded in that time. In combination with incrementals, this can be useful for batched loading or for loading on machines that have a run time limit.

```codeBlockLines_RjmQ
dlt.pipeline(destination="duckdb").run(my_resource().add_limit(max_time=10))

```

You can also apply a combination of both limits. In this case the extraction will stop as soon as either limit is reached.

```codeBlockLines_RjmQ
dlt.pipeline(destination="duckdb").run(my_resource().add_limit(max_items=10, max_time=10))

```

Some notes about the `add_limit`:

1. `add_limit` does not skip any items. It closes the iterator/generator that produces data after the limit is reached.
2. You cannot limit transformers. They should process all the data they receive fully to avoid inconsistencies in generated datasets.
3. Async resources with a limit added may occasionally produce one item more than the limit on some runs. This behavior is not deterministic.
4. Calling add limit on a resource will replace any previously set limits settings.
5. For time-limited resources, the timer starts when the first item is processed. When resources are processed sequentially (FIFO mode), each resource's time limit applies also sequentially. In the default round robin mode, the time limits will usually run concurrently.

tip

If you are parameterizing the value of `add_limit` and sometimes need it to be disabled, you can set `None` or `-1` to disable the limiting.
You can also set the limit to `0` for the resource to not yield any items.

### Set table name and adjust schema [​](https://dlthub.com/docs/general-usage/resource\#set-table-name-and-adjust-schema "Direct link to Set table name and adjust schema")

You can change the schema of a resource, whether it is standalone or part of a source. Look for a method named `apply_hints` which takes the same arguments as the resource decorator. Obviously, you should call this method before data is extracted from the resource. The example below converts an `append` resource loading the `users` table into a [merge](https://dlthub.com/docs/general-usage/merge-loading) resource that will keep just one updated record per `user_id`. It also adds ["last value" incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor) on the `created_at` column to prevent requesting again the already loaded records:

```codeBlockLines_RjmQ
tables = sql_database()
tables.users.apply_hints(
    write_disposition="merge",
    primary_key="user_id",
    incremental=dlt.sources.incremental("updated_at")
)
pipeline.run(tables)

```

To change the name of a table to which the resource will load data, do the following:

```codeBlockLines_RjmQ
tables = sql_database()
tables.users.table_name = "other_users"

```

### Adjust schema when you yield data [​](https://dlthub.com/docs/general-usage/resource\#adjust-schema-when-you-yield-data "Direct link to Adjust schema when you yield data")

You can set or update the table name, columns, and other schema elements when your resource is executed, and you already yield data. Such changes will be merged with the existing schema in the same way the `apply_hints` method above works. There are many reasons to adjust the schema at runtime. For example, when using Airflow, you should avoid lengthy operations (i.e., reflecting database tables) during the creation of the DAG, so it is better to do it when the DAG executes. You may also emit partial hints (i.e., precision and scale for decimal types) for columns to help `dlt` type inference.

```codeBlockLines_RjmQ
@dlt.resource
def sql_table(credentials, schema, table):
    # Create a SQL Alchemy engine
    engine = engine_from_credentials(credentials)
    engine.execution_options(stream_results=True)
    metadata = MetaData(schema=schema)
    # Reflect the table schema
    table_obj = Table(table, metadata, autoload_with=engine)

    for idx, batch in enumerate(table_rows(engine, table_obj)):
      if idx == 0:
        # Emit the first row with hints, table_to_columns and _get_primary_key are helpers that extract dlt schema from
        # SqlAlchemy model
        yield dlt.mark.with_hints(
            batch,
            dlt.mark.make_hints(columns=table_to_columns(table_obj), primary_key=_get_primary_key(table_obj)),
        )
      else:
        # Just yield all the other rows
        yield batch

```

In the example above, we use `dlt.mark.with_hints` and `dlt.mark.make_hints` to emit columns and primary key with the first extracted item. The table schema will be adjusted after the `batch` is processed in the extract pipeline but before any schema contracts are applied, and data is persisted in the load package.

tip

You can emit columns as a Pydantic model and use dynamic hints (i.e., lambda for table name) as well. You should avoid redefining `Incremental` this way.

### Import external files [​](https://dlthub.com/docs/general-usage/resource\#import-external-files "Direct link to Import external files")

You can import external files, i.e., CSV, Parquet, and JSONL, by yielding items marked with `with_file_import`, optionally passing a table schema corresponding to the imported file. dlt will not read, parse, or normalize any names (i.e., CSV or Arrow headers) and will attempt to copy the file into the destination as is.

```codeBlockLines_RjmQ
import os
import dlt
from dlt.sources.filesystem import filesystem

columns: List[TColumnSchema] = [\
    {"name": "id", "data_type": "bigint"},\
    {"name": "name", "data_type": "text"},\
    {"name": "description", "data_type": "text"},\
    {"name": "ordered_at", "data_type": "date"},\
    {"name": "price", "data_type": "decimal"},\
]

import_folder = "/tmp/import"

@dlt.transformer(columns=columns)
def orders(items: Iterator[FileItemDict]):
  for item in items:
    # copy the file locally
      dest_file = os.path.join(import_folder, item["file_name"])
      # download the file
      item.fsspec.download(item["file_url"], dest_file)
      # tell dlt to import the dest_file as `csv`
      yield dlt.mark.with_file_import(dest_file, "csv")

# use the filesystem core source to glob a bucket

downloader = filesystem(
  bucket_url="s3://my_bucket/csv",
  file_glob="today/*.csv.gz") | orders

info = pipeline.run(orders, destination="snowflake")

```

In the example above, we glob all zipped csv files present on **my\_bucket/csv/today** (using the `filesystem` verified source) and send file descriptors to the `orders` transformer. The transformer downloads and imports the files into the extract package. At the end, `dlt` sends them to Snowflake (the table will be created because we use `column` hints to define the schema).

If imported `csv` files are not in `dlt` [default format](https://dlthub.com/docs/dlt-ecosystem/file-formats/csv#default-settings), you may need to pass additional configuration.

```codeBlockLines_RjmQ
[destination.snowflake.csv_format]
delimiter="|"
include_header=false
on_error_continue=true

```

You can sniff the schema from the data, i.e., using DuckDB to infer the table schema from a CSV file. `dlt.mark.with_file_import` accepts additional arguments that you can use to pass hints at runtime.

note

- If you do not define any columns, the table will not be created in the destination. `dlt` will still attempt to load data into it, so if you create a fitting table upfront, the load process will succeed.
- Files are imported using hard links if possible to avoid copying and duplicating the storage space needed.

### Duplicate and rename resources [​](https://dlthub.com/docs/general-usage/resource\#duplicate-and-rename-resources "Direct link to Duplicate and rename resources")

There are cases when your resources are generic (i.e., bucket filesystem) and you want to load several instances of it (i.e., files from different folders) into separate tables. In the example below, we use the `filesystem` source to load csvs from two different folders into separate tables:

```codeBlockLines_RjmQ
@dlt.resource(standalone=True)
def fs_resource(bucket_url):
  # list and yield files in bucket_url
  ...

@dlt.transformer
def csv_reader(file_item):
  # load csv, parse, and yield rows in file_item
  ...

# create two extract pipes that list files from the bucket and send them to the reader.
# by default, both pipes will load data to the same table (csv_reader)
reports_pipe = fs_resource("s3://my-bucket/reports") | csv_reader()
transactions_pipe = fs_resource("s3://my-bucket/transactions") | csv_reader()

# so we rename resources to load to "reports" and "transactions" tables
pipeline.run(
  [reports_pipe.with_name("reports"), transactions_pipe.with_name("transactions")]
)

```

The `with_name` method returns a deep copy of the original resource, its data pipe, and the data pipes of a parent resource. A renamed clone is fully separated from the original resource (and other clones) when loading: it maintains a separate [resource state](https://dlthub.com/docs/general-usage/state#read-and-write-pipeline-state-in-a-resource) and will load to a table.

## Load resources [​](https://dlthub.com/docs/general-usage/resource\#load-resources "Direct link to Load resources")

You can pass individual resources or a list of resources to the `dlt.pipeline` object. The resources loaded outside the source context will be added to the [default schema](https://dlthub.com/docs/general-usage/schema) of the pipeline.

```codeBlockLines_RjmQ
@dlt.resource(name='table_name', write_disposition='replace')
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

pipeline = dlt.pipeline(
    pipeline_name="rows_pipeline",
    destination="duckdb",
    dataset_name="rows_data"
)
# load an individual resource
pipeline.run(generate_rows(10))
# load a list of resources
pipeline.run([generate_rows(10), generate_rows(20)])

```

### Pick loader file format for a particular resource [​](https://dlthub.com/docs/general-usage/resource\#pick-loader-file-format-for-a-particular-resource "Direct link to Pick loader file format for a particular resource")

You can request a particular loader file format to be used for a resource.

```codeBlockLines_RjmQ
@dlt.resource(file_format="parquet")
def generate_rows(nr):
    for i in range(nr):
        yield {'id': i, 'example_string': 'abc'}

```

The resource above will be saved and loaded from a Parquet file (if the destination supports it).

note

A special `file_format`: **preferred** will load the resource using a format that is preferred by a destination. This setting supersedes the `loader_file_format` passed to the `run` method.

### Do a full refresh [​](https://dlthub.com/docs/general-usage/resource\#do-a-full-refresh "Direct link to Do a full refresh")

To do a full refresh of an `append` or `merge` resource, you set the `refresh` argument on the `run` method to `drop_data`. This will truncate the tables without dropping them.

```codeBlockLines_RjmQ
p.run(merge_source(), refresh="drop_data")

```

You can also [fully drop the tables](https://dlthub.com/docs/general-usage/pipeline#refresh-pipeline-data-and-state) in the `merge_source`:

```codeBlockLines_RjmQ
p.run(merge_source(), refresh="drop_sources")

```

- [Declare a resource](https://dlthub.com/docs/general-usage/resource#declare-a-resource)
  - [Define schema](https://dlthub.com/docs/general-usage/resource#define-schema)
  - [Put a contract on tables, columns, and data](https://dlthub.com/docs/general-usage/resource#put-a-contract-on-tables-columns-and-data)
  - [Define schema of nested tables](https://dlthub.com/docs/general-usage/resource#define-schema-of-nested-tables)
  - [Define a schema with Pydantic](https://dlthub.com/docs/general-usage/resource#define-a-schema-with-pydantic)
  - [Dispatch data to many tables](https://dlthub.com/docs/general-usage/resource#dispatch-data-to-many-tables)
  - [Parametrize a resource](https://dlthub.com/docs/general-usage/resource#parametrize-a-resource)
  - [Process resources with `dlt.transformer`](https://dlthub.com/docs/general-usage/resource#process-resources-with-dlttransformer)
  - [Declare a standalone resource](https://dlthub.com/docs/general-usage/resource#declare-a-standalone-resource)
  - [Declare parallel and async resources](https://dlthub.com/docs/general-usage/resource#declare-parallel-and-async-resources)
- [Customize resources](https://dlthub.com/docs/general-usage/resource#customize-resources)
  - [Filter, transform, and pivot data](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data)
  - [Reduce the nesting level of generated tables](https://dlthub.com/docs/general-usage/resource#reduce-the-nesting-level-of-generated-tables)
  - [Sample from large data](https://dlthub.com/docs/general-usage/resource#sample-from-large-data)
  - [Set table name and adjust schema](https://dlthub.com/docs/general-usage/resource#set-table-name-and-adjust-schema)
  - [Adjust schema when you yield data](https://dlthub.com/docs/general-usage/resource#adjust-schema-when-you-yield-data)
  - [Import external files](https://dlthub.com/docs/general-usage/resource#import-external-files)
  - [Duplicate and rename resources](https://dlthub.com/docs/general-usage/resource#duplicate-and-rename-resources)
- [Load resources](https://dlthub.com/docs/general-usage/resource#load-resources)
  - [Pick loader file format for a particular resource](https://dlthub.com/docs/general-usage/resource#pick-loader-file-format-for-a-particular-resource)
  - [Do a full refresh](https://dlthub.com/docs/general-usage/resource#do-a-full-refresh)

----- https://dlthub.com/docs/tutorial/load-data-from-an-api#append-or-replace-your-data -----

Version: 1.11.0 (latest)

On this page

This tutorial introduces you to foundational dlt concepts, demonstrating how to build a custom data pipeline that loads data from pure Python data structures to DuckDB. It starts with a simple example and progresses to more advanced topics and usage scenarios.

## What you will learn [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#what-you-will-learn "Direct link to What you will learn")

- Loading data from a list of Python dictionaries into DuckDB.
- Low-level API usage with a built-in HTTP client.
- Understand and manage data loading behaviors.
- Incrementally load new data and deduplicate existing data.
- Dynamic resource creation and reducing code redundancy.
- Group resources into sources.
- Securely handle secrets.
- Make reusable data sources.

## Prerequisites [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#prerequisites "Direct link to Prerequisites")

- Python 3.9 or higher installed
- Virtual environment set up

## Installing dlt [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#installing-dlt "Direct link to Installing dlt")

Before we start, make sure you have a Python virtual environment set up. Follow the instructions in the [installation guide](https://dlthub.com/docs/reference/installation) to create a new virtual environment and install dlt.

Verify that dlt is installed by running the following command in your terminal:

```codeBlockLines_RjmQ
dlt --version

```

## Quick start [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#quick-start "Direct link to Quick start")

For starters, let's load a list of Python dictionaries into DuckDB and inspect the created dataset. Here is the code:

```codeBlockLines_RjmQ
import dlt

data = [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]

pipeline = dlt.pipeline(
    pipeline_name="quick_start", destination="duckdb", dataset_name="mydata"
)
load_info = pipeline.run(data, table_name="users")

print(load_info)

```

When you look at the code above, you can see that we:

1. Import the `dlt` library.
2. Define our data to load.
3. Create a pipeline that loads data into DuckDB. Here we also specify the `pipeline_name` and `dataset_name`. We'll use both in a moment.
4. Run the pipeline.

Save this Python script with the name `quick_start_pipeline.py` and run the following command:

```codeBlockLines_RjmQ
python quick_start_pipeline.py

```

The output should look like:

```codeBlockLines_RjmQ
Pipeline quick_start completed in 0.59 seconds
1 load package(s) were loaded to destination duckdb and into dataset mydata
The duckdb destination used duckdb:////home/user-name/quick_start/quick_start.duckdb location to store data
Load package 1692364844.460054 is LOADED and contains no failed jobs

```

`dlt` just created a database schema called **mydata** (the `dataset_name`) with a table **users** in it.

### Explore data in Python [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#explore-data-in-python "Direct link to Explore data in Python")

You can use dlt [datasets](https://dlthub.com/docs/general-usage/dataset-access/dataset) to easily query the data in pure Python.

```codeBlockLines_RjmQ
# get the dataset
dataset = pipeline.dataset("mydata")

# get the user relation
table = dataset.users

# query the full table as dataframe
print(table.df())

# query the first 10 rows as arrow table
print(table.limit(10).arrow())

```

### Explore data in Streamlit [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#explore-data-in-streamlit "Direct link to Explore data in Streamlit")

To allow a sneak peek and basic discovery, you can take advantage of [built-in integration with Streamlit](https://dlthub.com/docs/reference/command-line-interface#dlt-pipeline-show):

```codeBlockLines_RjmQ
dlt pipeline quick_start show

```

**quick\_start** is the name of the pipeline from the script above. If you do not have Streamlit installed yet, do:

```codeBlockLines_RjmQ
pip install streamlit

```

Now you should see the **users** table:

![Streamlit Explore data](https://dlthub.com/docs/assets/images/streamlit-new-1a373dbae5d5d59643d6336317ad7c43.png)
Streamlit Explore data. Schema and data for a test pipeline “quick\_start”.

tip

`dlt` works in Jupyter Notebook and Google Colab! See our [Quickstart Colab Demo.](https://colab.research.google.com/drive/1NfSB1DpwbbHX9_t5vlalBTf13utwpMGx?usp=sharing)

Looking for the source code of all the snippets? You can find and run them [from this repository](https://github.com/dlt-hub/dlt/blob/devel/docs/website/docs/getting-started-snippets.py).

Now that you have a basic understanding of how to get started with dlt, you might be eager to dive deeper. For that, we need to switch to a more advanced data source - the GitHub API. We will load issues from our [dlt-hub/dlt](https://github.com/dlt-hub/dlt) repository.

note

This tutorial uses the GitHub REST API for demonstration purposes only. If you need to read data from a REST API, consider using dlt's REST API source. Check out the [REST API source tutorial](https://dlthub.com/docs/tutorial/rest-api) for a quick start or the [REST API source reference](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api) for more details.

## Create a pipeline [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#create-a-pipeline "Direct link to Create a pipeline")

First, we need to create a [pipeline](https://dlthub.com/docs/general-usage/pipeline). Pipelines are the main building blocks of `dlt` and are used to load data from sources to destinations. Open your favorite text editor and create a file called `github_issues.py`. Add the following code to it:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers import requests

# Specify the URL of the API endpoint
url = "https://api.github.com/repos/dlt-hub/dlt/issues"
# Make a request and check if it was successful
response = requests.get(url)
response.raise_for_status()

pipeline = dlt.pipeline(
    pipeline_name="github_issues",
    destination="duckdb",
    dataset_name="github_data",
)
# The response contains a list of issues
load_info = pipeline.run(response.json(), table_name="issues")

print(load_info)

```

Here's what the code above does:

1. It makes a request to the GitHub API endpoint and checks if the response is successful.
2. Then, it creates a dlt pipeline with the name `github_issues` and specifies that the data should be loaded to the `duckdb` destination and the `github_data` dataset. Nothing gets loaded yet.
3. Finally, it runs the pipeline with the data from the API response ( `response.json()`) and specifies that the data should be loaded to the `issues` table. The `run` method returns a `LoadInfo` object that contains information about the loaded data.

## Run the pipeline [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#run-the-pipeline "Direct link to Run the pipeline")

Save `github_issues.py` and run the following command:

```codeBlockLines_RjmQ
python github_issues.py

```

Once the data has been loaded, you can inspect the created dataset using the Streamlit app:

```codeBlockLines_RjmQ
dlt pipeline github_issues show

```

## Append or replace your data [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#append-or-replace-your-data "Direct link to Append or replace your data")

Try running the pipeline again with `python github_issues.py`. You will notice that the **issues** table contains two copies of the same data. This happens because the default load mode is `append`. It is very useful, for example, when you have daily data updates and you want to ingest them.

To get the latest data, we'd need to run the script again. But how to do that without duplicating the data?
One option is to tell `dlt` to replace the data in existing tables in the destination by using the `replace` write disposition. Change the `github_issues.py` script to the following:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers import requests

# Specify the URL of the API endpoint
url = "https://api.github.com/repos/dlt-hub/dlt/issues"
# Make a request and check if it was successful
response = requests.get(url)
response.raise_for_status()

pipeline = dlt.pipeline(
    pipeline_name='github_issues',
    destination='duckdb',
    dataset_name='github_data',
)
# The response contains a list of issues
load_info = pipeline.run(
    response.json(),
    table_name="issues",
    write_disposition="replace"  # <-- Add this line
)

print(load_info)

```

Run this script twice to see that the **issues** table still contains only one copy of the data.

tip

What if the API has changed and new fields get added to the response?
`dlt` will migrate your tables!
See the `replace` mode and table schema migration in action in our [Schema evolution colab demo](https://colab.research.google.com/drive/1H6HKFi-U1V4p0afVucw_Jzv1oiFbH2bu#scrollTo=e4y4sQ78P_OM).

Learn more:

- [Full load - how to replace your data](https://dlthub.com/docs/general-usage/full-loading).
- [Append, replace, and merge your tables](https://dlthub.com/docs/general-usage/incremental-loading).

## Declare loading behavior [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#declare-loading-behavior "Direct link to Declare loading behavior")

So far, we have been passing the data to the `run` method directly. This is a quick way to get started. However, frequently, you receive data in chunks, and you want to load it as it arrives. For example, you might want to load data from an API endpoint with pagination or a large file that does not fit in memory. In such cases, you can use Python generators as a data source.

You can pass a generator to the `run` method directly or use the `@dlt.resource` decorator to turn the generator into a [dlt resource](https://dlthub.com/docs/general-usage/resource). The decorator allows you to specify the loading behavior and relevant resource parameters.

### Load only new data (incremental loading) [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#load-only-new-data-incremental-loading "Direct link to Load only new data (incremental loading)")

Let's improve our GitHub API example and get only issues that were created since the last load.
Instead of using the `replace` write disposition and downloading all issues each time the pipeline is run, we do the following:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers import requests

@dlt.resource(table_name="issues", write_disposition="append")
def get_issues(
    created_at=dlt.sources.incremental("created_at", initial_value="1970-01-01T00:00:00Z")
):
    # NOTE: we read only open issues to minimize number of calls to the API.
    # There's a limit of ~50 calls for not authenticated Github users.
    url = (
        "https://api.github.com/repos/dlt-hub/dlt/issues"
        "?per_page=100&sort=created&directions=desc&state=open"
    )

    while True:
        response = requests.get(url)
        response.raise_for_status()
        yield response.json()

        # Stop requesting pages if the last element was already
        # older than initial value
        # Note: incremental will skip those items anyway, we just
        # do not want to use the api limits
        if created_at.start_out_of_range:
            break

        # get next page
        if "next" not in response.links:
            break
        url = response.links["next"]["url"]

pipeline = dlt.pipeline(
    pipeline_name="github_issues_incremental",
    destination="duckdb",
    dataset_name="github_data_append",
)

load_info = pipeline.run(get_issues)
row_counts = pipeline.last_trace.last_normalize_info

print(row_counts)
print("------")
print(load_info)

```

Let's take a closer look at the code above.

We use the `@dlt.resource` decorator to declare the table name into which data will be loaded and specify the `append` write disposition.

We request issues for the dlt-hub/dlt repository ordered by the **created\_at** field (descending) and yield them page by page in the `get_issues` generator function.

We also use `dlt.sources.incremental` to track the `created_at` field present in each issue to filter in the newly created ones.

Now run the script. It loads all the issues from our repo to `duckdb`. Run it again, and you can see that no issues got added (if no issues were created in the meantime).

Now you can run this script on a daily schedule, and each day you’ll load only issues created after the time of the previous pipeline run.

tip

Between pipeline runs, `dlt` keeps the state in the same database it loaded data into.
Peek into that state, the tables loaded, and get other information with:

```codeBlockLines_RjmQ
dlt pipeline -v github_issues_incremental info

```

Learn more:

- Declare your [resources](https://dlthub.com/docs/general-usage/resource) and group them in [sources](https://dlthub.com/docs/general-usage/source) using Python decorators.
- [Set up "last value" incremental loading.](https://dlthub.com/docs/general-usage/incremental/cursor)
- [Inspect pipeline after loading.](https://dlthub.com/docs/walkthroughs/run-a-pipeline#4-inspect-a-load-process)
- [`dlt` command line interface.](https://dlthub.com/docs/reference/command-line-interface)

### Update and deduplicate your data [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#update-and-deduplicate-your-data "Direct link to Update and deduplicate your data")

The script above finds **new** issues and adds them to the database.
It will ignore any updates to **existing** issue text, emoji reactions, etc.
To always get fresh content of all the issues, combine incremental load with the `merge` write disposition,
like in the script below.

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers import requests

@dlt.resource(
    table_name="issues",
    write_disposition="merge",
    primary_key="id",
)
def get_issues(
    updated_at=dlt.sources.incremental("updated_at", initial_value="1970-01-01T00:00:00Z")
):
    # NOTE: we read only open issues to minimize number of calls to
    # the API. There's a limit of ~50 calls for not authenticated
    # Github users
    url = (
        "https://api.github.com/repos/dlt-hub/dlt/issues"
        f"?since={updated_at.last_value}&per_page=100&sort=updated"
        "&directions=desc&state=open"
    )

    while True:
        response = requests.get(url)
        response.raise_for_status()
        yield response.json()

        # Get next page
        if "next" not in response.links:
            break
        url = response.links["next"]["url"]

pipeline = dlt.pipeline(
    pipeline_name="github_issues_merge",
    destination="duckdb",
    dataset_name="github_data_merge",
)
load_info = pipeline.run(get_issues)
row_counts = pipeline.last_trace.last_normalize_info

print(row_counts)
print("------")
print(load_info)

```

Above, we add the `primary_key` argument to the `dlt.resource()` that tells `dlt` how to identify the issues in the database to find duplicates whose content it will merge.

Note that we now track the `updated_at` field — so we filter in all issues **updated** since the last pipeline run (which also includes those newly created).

Pay attention to how we use the **since** parameter from the [GitHub API](https://docs.github.com/en/rest/issues/issues?apiVersion=2022-11-28#list-repository-issues)
and `updated_at.last_value` to tell GitHub to return issues updated only **after** the date we pass. `updated_at.last_value` holds the last `updated_at` value from the previous run.

[Learn more about merge write disposition](https://dlthub.com/docs/general-usage/merge-loading).

## Using pagination helper [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#using-pagination-helper "Direct link to Using pagination helper")

In the previous examples, we used the `requests` library to make HTTP requests to the GitHub API and handled pagination manually. `dlt` has a built-in [REST client](https://dlthub.com/docs/general-usage/http/rest-client) that simplifies API requests. We'll use the `paginate()` helper from it for the next example. The `paginate` function takes a URL and optional parameters (quite similar to `requests`) and returns a generator that yields pages of data.

Here's how the updated script looks:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers.rest_client import paginate

@dlt.resource(
    table_name="issues",
    write_disposition="merge",
    primary_key="id",
)
def get_issues(
    updated_at=dlt.sources.incremental("updated_at", initial_value="1970-01-01T00:00:00Z")
):
    for page in paginate(
        "https://api.github.com/repos/dlt-hub/dlt/issues",
        params={
            "since": updated_at.last_value,
            "per_page": 100,
            "sort": "updated",
            "direction": "desc",
            "state": "open",
        },
    ):
        yield page

pipeline = dlt.pipeline(
    pipeline_name="github_issues_merge",
    destination="duckdb",
    dataset_name="github_data_merge",
)
load_info = pipeline.run(get_issues)
row_counts = pipeline.last_trace.last_normalize_info

print(row_counts)
print("------")
print(load_info)

```

Let's zoom in on the changes:

1. The `while` loop that handled pagination is replaced with reading pages from the `paginate()` generator.
2. `paginate()` takes the URL of the API endpoint and optional parameters. In this case, we pass the `since` parameter to get only issues updated after the last pipeline run.
3. We're not explicitly setting up pagination; `paginate()` handles it for us. Magic! Under the hood, `paginate()` analyzes the response and detects the pagination method used by the API. Read more about pagination in the [REST client documentation](https://dlthub.com/docs/general-usage/http/rest-client#paginating-api-responses).

If you want to take full advantage of the `dlt` library, then we strongly suggest that you build your sources out of existing building blocks:
To make the most of `dlt`, consider the following:

## Use source decorator [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#use-source-decorator "Direct link to Use source decorator")

In the previous step, we loaded issues from the GitHub API. Now we'll load comments from the API as well. Here's a sample [dlt resource](https://dlthub.com/docs/general-usage/resource) that does that:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers.rest_client import paginate

@dlt.resource(
    table_name="comments",
    write_disposition="merge",
    primary_key="id",
)
def get_comments(
    updated_at=dlt.sources.incremental("updated_at", initial_value="1970-01-01T00:00:00Z")
):
    for page in paginate(
        "https://api.github.com/repos/dlt-hub/dlt/comments",
        params={"per_page": 100}
    ):
        yield page

```

We can load this resource separately from the issues resource; however, loading both issues and comments in one go is more efficient. To do that, we'll use the `@dlt.source` decorator on a function that returns a list of resources:

```codeBlockLines_RjmQ
@dlt.source
def github_source():
    return [get_issues, get_comments]

```

`github_source()` groups resources into a [source](https://dlthub.com/docs/general-usage/source). A dlt source is a logical grouping of resources. You use it to group resources that belong together, for example, to load data from the same API. Loading data from a source can be run in a single pipeline. Here's what our updated script looks like:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers.rest_client import paginate

@dlt.resource(
    table_name="issues",
    write_disposition="merge",
    primary_key="id",
)
def get_issues(
    updated_at=dlt.sources.incremental("updated_at", initial_value="1970-01-01T00:00:00Z")
):
    for page in paginate(
        "https://api.github.com/repos/dlt-hub/dlt/issues",
        params={
            "since": updated_at.last_value,
            "per_page": 100,
            "sort": "updated",
            "direction": "desc",
            "state": "open",
        }
    ):
        yield page

@dlt.resource(
    table_name="comments",
    write_disposition="merge",
    primary_key="id",
)
def get_comments(
    updated_at=dlt.sources.incremental("updated_at", initial_value="1970-01-01T00:00:00Z")
):
    for page in paginate(
        "https://api.github.com/repos/dlt-hub/dlt/comments",
        params={
            "since": updated_at.last_value,
            "per_page": 100,
        }
    ):
        yield page

@dlt.source
def github_source():
    return [get_issues, get_comments]

pipeline = dlt.pipeline(
    pipeline_name='github_with_source',
    destination='duckdb',
    dataset_name='github_data',
)

load_info = pipeline.run(github_source())
print(load_info)

```

### Dynamic resources [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#dynamic-resources "Direct link to Dynamic resources")

You've noticed that there's a lot of code duplication in the `get_issues` and `get_comments` functions. We can reduce that by extracting the common fetching code into a separate function and using it in both resources. Even better, we can use `dlt.resource` as a function and pass it the `fetch_github_data()` generator function directly. Here's the refactored code:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers.rest_client import paginate

BASE_GITHUB_URL = "https://api.github.com/repos/dlt-hub/dlt"

def fetch_github_data(endpoint, params={}):
    url = f"{BASE_GITHUB_URL}/{endpoint}"
    return paginate(url, params=params)

@dlt.source
def github_source():
    for endpoint in ["issues", "comments"]:
        params = {"per_page": 100}
        yield dlt.resource(
            fetch_github_data(endpoint, params),
            name=endpoint,
            write_disposition="merge",
            primary_key="id",
        )

pipeline = dlt.pipeline(
    pipeline_name='github_dynamic_source',
    destination='duckdb',
    dataset_name='github_data',
)
load_info = pipeline.run(github_source())
row_counts = pipeline.last_trace.last_normalize_info

```

## Handle secrets [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#handle-secrets "Direct link to Handle secrets")

For the next step, we'd want to get the [number of repository clones](https://docs.github.com/en/rest/metrics/traffic?apiVersion=2022-11-28#get-repository-clones) for our dlt repo from the GitHub API. However, the `traffic/clones` endpoint that returns the data requires [authentication](https://docs.github.com/en/rest/overview/authenticating-to-the-rest-api?apiVersion=2022-11-28).

Let's handle this by changing our `fetch_github_data()` function first:

```codeBlockLines_RjmQ
from dlt.sources.helpers.rest_client.auth import BearerTokenAuth

def fetch_github_data_with_token(endpoint, params={}, access_token=None):
    url = f"{BASE_GITHUB_URL}/{endpoint}"
    return paginate(
        url,
        params=params,
        auth=BearerTokenAuth(token=access_token) if access_token else None,
    )

@dlt.source
def github_source_with_token(access_token: str):
    for endpoint in ["issues", "comments", "traffic/clones"]:
        params = {"per_page": 100}
        yield dlt.resource(
            fetch_github_data_with_token(endpoint, params, access_token),
            name=endpoint,
            write_disposition="merge",
            primary_key="id",
        )

...

```

Here, we added an `access_token` parameter and now we can use it to pass the access token to the request:

```codeBlockLines_RjmQ
load_info = pipeline.run(github_source_with_token(access_token="ghp_XXXXX"))

```

It's a good start. But we'd want to follow the best practices and not hardcode the token in the script. One option is to set the token as an environment variable, load it with `os.getenv()`, and pass it around as a parameter. dlt offers a more convenient way to handle secrets and credentials: it lets you inject the arguments using a special `dlt.secrets.value` argument value.

To use it, change the `github_source()` function to:

```codeBlockLines_RjmQ
@dlt.source
def github_source_with_token(
    access_token: str = dlt.secrets.value,
):
    ...

```

When you add `dlt.secrets.value` as a default value for an argument, `dlt` will try to load and inject this value from different configuration sources in the following order:

1. Special environment variables.
2. `secrets.toml` file.

The `secrets.toml` file is located in the `~/.dlt` folder (for global configuration) or in the `.dlt` folder in the project folder (for project-specific configuration).

Let's add the token to the `~/.dlt/secrets.toml` file:

```codeBlockLines_RjmQ
[github_with_source_secrets]
access_token = "ghp_A...3aRY"

```

Now we can run the script and it will load the data from the `traffic/clones` endpoint:

```codeBlockLines_RjmQ
...

@dlt.source
def github_source_with_token(
    access_token: str = dlt.secrets.value,
):
    for endpoint in ["issues", "comments", "traffic/clones"]:
        params = {"per_page": 100}
        yield dlt.resource(
            fetch_github_data_with_token(endpoint, params, access_token),
            name=endpoint,
            write_disposition="merge",
            primary_key="id",
        )

pipeline = dlt.pipeline(
    pipeline_name="github_with_source_secrets",
    destination="duckdb",
    dataset_name="github_data",
)
load_info = pipeline.run(github_source())

```

## Configurable sources [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#configurable-sources "Direct link to Configurable sources")

The next step is to make our dlt GitHub source reusable so it can load data from any GitHub repo. We'll do that by changing both the `github_source()` and `fetch_github_data()` functions to accept the repo name as a parameter:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers.rest_client import paginate

BASE_GITHUB_URL = "https://api.github.com/repos/{repo_name}"

def fetch_github_data_with_token_and_params(repo_name, endpoint, params={}, access_token=None):
    """Fetch data from the GitHub API based on repo_name, endpoint, and params."""
    url = BASE_GITHUB_URL.format(repo_name=repo_name) + f"/{endpoint}"
    return paginate(
        url,
        params=params,
        auth=BearerTokenAuth(token=access_token) if access_token else None,
    )

@dlt.source
def github_source_with_token_and_repo(
    repo_name: str = dlt.config.value,
    access_token: str = dlt.secrets.value,
):
    for endpoint in ["issues", "comments", "traffic/clones"]:
        params = {"per_page": 100}
        yield dlt.resource(
            fetch_github_data_with_token_and_params(repo_name, endpoint, params, access_token),
            name=endpoint,
            write_disposition="merge",
            primary_key="id",
        )

pipeline = dlt.pipeline(
    pipeline_name="github_with_source_secrets",
    destination="duckdb",
    dataset_name="github_data",
)
load_info = pipeline.run(github_source())

```

Next, create a `.dlt/config.toml` file in the project folder and add the `repo_name` parameter to it:

```codeBlockLines_RjmQ
[github_with_source_secrets]
repo_name = "dlt-hub/dlt"

```

That's it! Now you have a reusable source that can load data from any GitHub repo.

## What’s next [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#whats-next "Direct link to What’s next")

Congratulations on completing the tutorial! You've come a long way since the [getting started](https://dlthub.com/docs/intro) guide. By now, you've mastered loading data from various GitHub API endpoints, organizing resources into sources, managing secrets securely, and creating reusable sources. You can use these skills to build your own pipelines and load data from any source.

Interested in learning more? Here are some suggestions:

1. You've been running your pipelines locally. Learn how to [deploy and run them in the cloud](https://dlthub.com/docs/walkthroughs/deploy-a-pipeline/).
2. Dive deeper into how dlt works by reading the [Using dlt](https://dlthub.com/docs/general-usage) section. Some highlights:
   - [Set up "last value" incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor).
   - Learn about data loading strategies: append, [replace](https://dlthub.com/docs/general-usage/full-loading), and [merge](https://dlthub.com/docs/general-usage/merge-loading).
   - [Connect the transformers to the resources](https://dlthub.com/docs/general-usage/resource#process-resources-with-dlttransformer) to load additional data or enrich it.
   - [Customize your data schema—set primary and merge keys, define column nullability, and specify data types](https://dlthub.com/docs/general-usage/resource#define-schema).
   - [Create your resources dynamically from data](https://dlthub.com/docs/general-usage/source#create-resources-dynamically).
   - [Transform your data before loading](https://dlthub.com/docs/general-usage/resource#customize-resources) and see some [examples of customizations like column renames and anonymization](https://dlthub.com/docs/general-usage/customising-pipelines/renaming_columns).
   - Employ data transformations using [SQL](https://dlthub.com/docs/dlt-ecosystem/transformations/sql) or [Pandas](https://dlthub.com/docs/dlt-ecosystem/transformations/sql).
   - [Pass config and credentials into your sources and resources](https://dlthub.com/docs/general-usage/credentials).
   - [Run in production: inspecting, tracing, retry policies, and cleaning up](https://dlthub.com/docs/running-in-production/running).
   - [Run resources in parallel, optimize buffers, and local storage](https://dlthub.com/docs/reference/performance)
   - [Use REST API client helpers](https://dlthub.com/docs/general-usage/http/rest-client) to simplify working with REST APIs.
3. Explore [destinations](https://dlthub.com/docs/dlt-ecosystem/destinations/) and [sources](https://dlthub.com/docs/dlt-ecosystem/verified-sources/) provided by us and the community.
4. Explore the [Examples](https://dlthub.com/docs/examples) section to see how dlt can be used in real-world scenarios.

- [What you will learn](https://dlthub.com/docs/tutorial/load-data-from-an-api#what-you-will-learn)
- [Prerequisites](https://dlthub.com/docs/tutorial/load-data-from-an-api#prerequisites)
- [Installing dlt](https://dlthub.com/docs/tutorial/load-data-from-an-api#installing-dlt)
- [Quick start](https://dlthub.com/docs/tutorial/load-data-from-an-api#quick-start)
  - [Explore data in Python](https://dlthub.com/docs/tutorial/load-data-from-an-api#explore-data-in-python)
  - [Explore data in Streamlit](https://dlthub.com/docs/tutorial/load-data-from-an-api#explore-data-in-streamlit)
- [Create a pipeline](https://dlthub.com/docs/tutorial/load-data-from-an-api#create-a-pipeline)
- [Run the pipeline](https://dlthub.com/docs/tutorial/load-data-from-an-api#run-the-pipeline)
- [Append or replace your data](https://dlthub.com/docs/tutorial/load-data-from-an-api#append-or-replace-your-data)
- [Declare loading behavior](https://dlthub.com/docs/tutorial/load-data-from-an-api#declare-loading-behavior)
  - [Load only new data (incremental loading)](https://dlthub.com/docs/tutorial/load-data-from-an-api#load-only-new-data-incremental-loading)
  - [Update and deduplicate your data](https://dlthub.com/docs/tutorial/load-data-from-an-api#update-and-deduplicate-your-data)
- [Using pagination helper](https://dlthub.com/docs/tutorial/load-data-from-an-api#using-pagination-helper)
- [Use source decorator](https://dlthub.com/docs/tutorial/load-data-from-an-api#use-source-decorator)
  - [Dynamic resources](https://dlthub.com/docs/tutorial/load-data-from-an-api#dynamic-resources)
- [Handle secrets](https://dlthub.com/docs/tutorial/load-data-from-an-api#handle-secrets)
- [Configurable sources](https://dlthub.com/docs/tutorial/load-data-from-an-api#configurable-sources)
- [What’s next](https://dlthub.com/docs/tutorial/load-data-from-an-api#whats-next)

----- https://dlthub.com/docs/tutorial/load-data-from-an-api#load-only-new-data-incremental-loading -----

Version: 1.11.0 (latest)

On this page

This tutorial introduces you to foundational dlt concepts, demonstrating how to build a custom data pipeline that loads data from pure Python data structures to DuckDB. It starts with a simple example and progresses to more advanced topics and usage scenarios.

## What you will learn [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#what-you-will-learn "Direct link to What you will learn")

- Loading data from a list of Python dictionaries into DuckDB.
- Low-level API usage with a built-in HTTP client.
- Understand and manage data loading behaviors.
- Incrementally load new data and deduplicate existing data.
- Dynamic resource creation and reducing code redundancy.
- Group resources into sources.
- Securely handle secrets.
- Make reusable data sources.

## Prerequisites [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#prerequisites "Direct link to Prerequisites")

- Python 3.9 or higher installed
- Virtual environment set up

## Installing dlt [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#installing-dlt "Direct link to Installing dlt")

Before we start, make sure you have a Python virtual environment set up. Follow the instructions in the [installation guide](https://dlthub.com/docs/reference/installation) to create a new virtual environment and install dlt.

Verify that dlt is installed by running the following command in your terminal:

```codeBlockLines_RjmQ
dlt --version

```

## Quick start [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#quick-start "Direct link to Quick start")

For starters, let's load a list of Python dictionaries into DuckDB and inspect the created dataset. Here is the code:

```codeBlockLines_RjmQ
import dlt

data = [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]

pipeline = dlt.pipeline(
    pipeline_name="quick_start", destination="duckdb", dataset_name="mydata"
)
load_info = pipeline.run(data, table_name="users")

print(load_info)

```

When you look at the code above, you can see that we:

1. Import the `dlt` library.
2. Define our data to load.
3. Create a pipeline that loads data into DuckDB. Here we also specify the `pipeline_name` and `dataset_name`. We'll use both in a moment.
4. Run the pipeline.

Save this Python script with the name `quick_start_pipeline.py` and run the following command:

```codeBlockLines_RjmQ
python quick_start_pipeline.py

```

The output should look like:

```codeBlockLines_RjmQ
Pipeline quick_start completed in 0.59 seconds
1 load package(s) were loaded to destination duckdb and into dataset mydata
The duckdb destination used duckdb:////home/user-name/quick_start/quick_start.duckdb location to store data
Load package 1692364844.460054 is LOADED and contains no failed jobs

```

`dlt` just created a database schema called **mydata** (the `dataset_name`) with a table **users** in it.

### Explore data in Python [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#explore-data-in-python "Direct link to Explore data in Python")

You can use dlt [datasets](https://dlthub.com/docs/general-usage/dataset-access/dataset) to easily query the data in pure Python.

```codeBlockLines_RjmQ
# get the dataset
dataset = pipeline.dataset("mydata")

# get the user relation
table = dataset.users

# query the full table as dataframe
print(table.df())

# query the first 10 rows as arrow table
print(table.limit(10).arrow())

```

### Explore data in Streamlit [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#explore-data-in-streamlit "Direct link to Explore data in Streamlit")

To allow a sneak peek and basic discovery, you can take advantage of [built-in integration with Streamlit](https://dlthub.com/docs/reference/command-line-interface#dlt-pipeline-show):

```codeBlockLines_RjmQ
dlt pipeline quick_start show

```

**quick\_start** is the name of the pipeline from the script above. If you do not have Streamlit installed yet, do:

```codeBlockLines_RjmQ
pip install streamlit

```

Now you should see the **users** table:

![Streamlit Explore data](https://dlthub.com/docs/assets/images/streamlit-new-1a373dbae5d5d59643d6336317ad7c43.png)
Streamlit Explore data. Schema and data for a test pipeline “quick\_start”.

tip

`dlt` works in Jupyter Notebook and Google Colab! See our [Quickstart Colab Demo.](https://colab.research.google.com/drive/1NfSB1DpwbbHX9_t5vlalBTf13utwpMGx?usp=sharing)

Looking for the source code of all the snippets? You can find and run them [from this repository](https://github.com/dlt-hub/dlt/blob/devel/docs/website/docs/getting-started-snippets.py).

Now that you have a basic understanding of how to get started with dlt, you might be eager to dive deeper. For that, we need to switch to a more advanced data source - the GitHub API. We will load issues from our [dlt-hub/dlt](https://github.com/dlt-hub/dlt) repository.

note

This tutorial uses the GitHub REST API for demonstration purposes only. If you need to read data from a REST API, consider using dlt's REST API source. Check out the [REST API source tutorial](https://dlthub.com/docs/tutorial/rest-api) for a quick start or the [REST API source reference](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api) for more details.

## Create a pipeline [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#create-a-pipeline "Direct link to Create a pipeline")

First, we need to create a [pipeline](https://dlthub.com/docs/general-usage/pipeline). Pipelines are the main building blocks of `dlt` and are used to load data from sources to destinations. Open your favorite text editor and create a file called `github_issues.py`. Add the following code to it:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers import requests

# Specify the URL of the API endpoint
url = "https://api.github.com/repos/dlt-hub/dlt/issues"
# Make a request and check if it was successful
response = requests.get(url)
response.raise_for_status()

pipeline = dlt.pipeline(
    pipeline_name="github_issues",
    destination="duckdb",
    dataset_name="github_data",
)
# The response contains a list of issues
load_info = pipeline.run(response.json(), table_name="issues")

print(load_info)

```

Here's what the code above does:

1. It makes a request to the GitHub API endpoint and checks if the response is successful.
2. Then, it creates a dlt pipeline with the name `github_issues` and specifies that the data should be loaded to the `duckdb` destination and the `github_data` dataset. Nothing gets loaded yet.
3. Finally, it runs the pipeline with the data from the API response ( `response.json()`) and specifies that the data should be loaded to the `issues` table. The `run` method returns a `LoadInfo` object that contains information about the loaded data.

## Run the pipeline [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#run-the-pipeline "Direct link to Run the pipeline")

Save `github_issues.py` and run the following command:

```codeBlockLines_RjmQ
python github_issues.py

```

Once the data has been loaded, you can inspect the created dataset using the Streamlit app:

```codeBlockLines_RjmQ
dlt pipeline github_issues show

```

## Append or replace your data [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#append-or-replace-your-data "Direct link to Append or replace your data")

Try running the pipeline again with `python github_issues.py`. You will notice that the **issues** table contains two copies of the same data. This happens because the default load mode is `append`. It is very useful, for example, when you have daily data updates and you want to ingest them.

To get the latest data, we'd need to run the script again. But how to do that without duplicating the data?
One option is to tell `dlt` to replace the data in existing tables in the destination by using the `replace` write disposition. Change the `github_issues.py` script to the following:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers import requests

# Specify the URL of the API endpoint
url = "https://api.github.com/repos/dlt-hub/dlt/issues"
# Make a request and check if it was successful
response = requests.get(url)
response.raise_for_status()

pipeline = dlt.pipeline(
    pipeline_name='github_issues',
    destination='duckdb',
    dataset_name='github_data',
)
# The response contains a list of issues
load_info = pipeline.run(
    response.json(),
    table_name="issues",
    write_disposition="replace"  # <-- Add this line
)

print(load_info)

```

Run this script twice to see that the **issues** table still contains only one copy of the data.

tip

What if the API has changed and new fields get added to the response?
`dlt` will migrate your tables!
See the `replace` mode and table schema migration in action in our [Schema evolution colab demo](https://colab.research.google.com/drive/1H6HKFi-U1V4p0afVucw_Jzv1oiFbH2bu#scrollTo=e4y4sQ78P_OM).

Learn more:

- [Full load - how to replace your data](https://dlthub.com/docs/general-usage/full-loading).
- [Append, replace, and merge your tables](https://dlthub.com/docs/general-usage/incremental-loading).

## Declare loading behavior [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#declare-loading-behavior "Direct link to Declare loading behavior")

So far, we have been passing the data to the `run` method directly. This is a quick way to get started. However, frequently, you receive data in chunks, and you want to load it as it arrives. For example, you might want to load data from an API endpoint with pagination or a large file that does not fit in memory. In such cases, you can use Python generators as a data source.

You can pass a generator to the `run` method directly or use the `@dlt.resource` decorator to turn the generator into a [dlt resource](https://dlthub.com/docs/general-usage/resource). The decorator allows you to specify the loading behavior and relevant resource parameters.

### Load only new data (incremental loading) [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#load-only-new-data-incremental-loading "Direct link to Load only new data (incremental loading)")

Let's improve our GitHub API example and get only issues that were created since the last load.
Instead of using the `replace` write disposition and downloading all issues each time the pipeline is run, we do the following:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers import requests

@dlt.resource(table_name="issues", write_disposition="append")
def get_issues(
    created_at=dlt.sources.incremental("created_at", initial_value="1970-01-01T00:00:00Z")
):
    # NOTE: we read only open issues to minimize number of calls to the API.
    # There's a limit of ~50 calls for not authenticated Github users.
    url = (
        "https://api.github.com/repos/dlt-hub/dlt/issues"
        "?per_page=100&sort=created&directions=desc&state=open"
    )

    while True:
        response = requests.get(url)
        response.raise_for_status()
        yield response.json()

        # Stop requesting pages if the last element was already
        # older than initial value
        # Note: incremental will skip those items anyway, we just
        # do not want to use the api limits
        if created_at.start_out_of_range:
            break

        # get next page
        if "next" not in response.links:
            break
        url = response.links["next"]["url"]

pipeline = dlt.pipeline(
    pipeline_name="github_issues_incremental",
    destination="duckdb",
    dataset_name="github_data_append",
)

load_info = pipeline.run(get_issues)
row_counts = pipeline.last_trace.last_normalize_info

print(row_counts)
print("------")
print(load_info)

```

Let's take a closer look at the code above.

We use the `@dlt.resource` decorator to declare the table name into which data will be loaded and specify the `append` write disposition.

We request issues for the dlt-hub/dlt repository ordered by the **created\_at** field (descending) and yield them page by page in the `get_issues` generator function.

We also use `dlt.sources.incremental` to track the `created_at` field present in each issue to filter in the newly created ones.

Now run the script. It loads all the issues from our repo to `duckdb`. Run it again, and you can see that no issues got added (if no issues were created in the meantime).

Now you can run this script on a daily schedule, and each day you’ll load only issues created after the time of the previous pipeline run.

tip

Between pipeline runs, `dlt` keeps the state in the same database it loaded data into.
Peek into that state, the tables loaded, and get other information with:

```codeBlockLines_RjmQ
dlt pipeline -v github_issues_incremental info

```

Learn more:

- Declare your [resources](https://dlthub.com/docs/general-usage/resource) and group them in [sources](https://dlthub.com/docs/general-usage/source) using Python decorators.
- [Set up "last value" incremental loading.](https://dlthub.com/docs/general-usage/incremental/cursor)
- [Inspect pipeline after loading.](https://dlthub.com/docs/walkthroughs/run-a-pipeline#4-inspect-a-load-process)
- [`dlt` command line interface.](https://dlthub.com/docs/reference/command-line-interface)

### Update and deduplicate your data [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#update-and-deduplicate-your-data "Direct link to Update and deduplicate your data")

The script above finds **new** issues and adds them to the database.
It will ignore any updates to **existing** issue text, emoji reactions, etc.
To always get fresh content of all the issues, combine incremental load with the `merge` write disposition,
like in the script below.

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers import requests

@dlt.resource(
    table_name="issues",
    write_disposition="merge",
    primary_key="id",
)
def get_issues(
    updated_at=dlt.sources.incremental("updated_at", initial_value="1970-01-01T00:00:00Z")
):
    # NOTE: we read only open issues to minimize number of calls to
    # the API. There's a limit of ~50 calls for not authenticated
    # Github users
    url = (
        "https://api.github.com/repos/dlt-hub/dlt/issues"
        f"?since={updated_at.last_value}&per_page=100&sort=updated"
        "&directions=desc&state=open"
    )

    while True:
        response = requests.get(url)
        response.raise_for_status()
        yield response.json()

        # Get next page
        if "next" not in response.links:
            break
        url = response.links["next"]["url"]

pipeline = dlt.pipeline(
    pipeline_name="github_issues_merge",
    destination="duckdb",
    dataset_name="github_data_merge",
)
load_info = pipeline.run(get_issues)
row_counts = pipeline.last_trace.last_normalize_info

print(row_counts)
print("------")
print(load_info)

```

Above, we add the `primary_key` argument to the `dlt.resource()` that tells `dlt` how to identify the issues in the database to find duplicates whose content it will merge.

Note that we now track the `updated_at` field — so we filter in all issues **updated** since the last pipeline run (which also includes those newly created).

Pay attention to how we use the **since** parameter from the [GitHub API](https://docs.github.com/en/rest/issues/issues?apiVersion=2022-11-28#list-repository-issues)
and `updated_at.last_value` to tell GitHub to return issues updated only **after** the date we pass. `updated_at.last_value` holds the last `updated_at` value from the previous run.

[Learn more about merge write disposition](https://dlthub.com/docs/general-usage/merge-loading).

## Using pagination helper [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#using-pagination-helper "Direct link to Using pagination helper")

In the previous examples, we used the `requests` library to make HTTP requests to the GitHub API and handled pagination manually. `dlt` has a built-in [REST client](https://dlthub.com/docs/general-usage/http/rest-client) that simplifies API requests. We'll use the `paginate()` helper from it for the next example. The `paginate` function takes a URL and optional parameters (quite similar to `requests`) and returns a generator that yields pages of data.

Here's how the updated script looks:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers.rest_client import paginate

@dlt.resource(
    table_name="issues",
    write_disposition="merge",
    primary_key="id",
)
def get_issues(
    updated_at=dlt.sources.incremental("updated_at", initial_value="1970-01-01T00:00:00Z")
):
    for page in paginate(
        "https://api.github.com/repos/dlt-hub/dlt/issues",
        params={
            "since": updated_at.last_value,
            "per_page": 100,
            "sort": "updated",
            "direction": "desc",
            "state": "open",
        },
    ):
        yield page

pipeline = dlt.pipeline(
    pipeline_name="github_issues_merge",
    destination="duckdb",
    dataset_name="github_data_merge",
)
load_info = pipeline.run(get_issues)
row_counts = pipeline.last_trace.last_normalize_info

print(row_counts)
print("------")
print(load_info)

```

Let's zoom in on the changes:

1. The `while` loop that handled pagination is replaced with reading pages from the `paginate()` generator.
2. `paginate()` takes the URL of the API endpoint and optional parameters. In this case, we pass the `since` parameter to get only issues updated after the last pipeline run.
3. We're not explicitly setting up pagination; `paginate()` handles it for us. Magic! Under the hood, `paginate()` analyzes the response and detects the pagination method used by the API. Read more about pagination in the [REST client documentation](https://dlthub.com/docs/general-usage/http/rest-client#paginating-api-responses).

If you want to take full advantage of the `dlt` library, then we strongly suggest that you build your sources out of existing building blocks:
To make the most of `dlt`, consider the following:

## Use source decorator [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#use-source-decorator "Direct link to Use source decorator")

In the previous step, we loaded issues from the GitHub API. Now we'll load comments from the API as well. Here's a sample [dlt resource](https://dlthub.com/docs/general-usage/resource) that does that:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers.rest_client import paginate

@dlt.resource(
    table_name="comments",
    write_disposition="merge",
    primary_key="id",
)
def get_comments(
    updated_at=dlt.sources.incremental("updated_at", initial_value="1970-01-01T00:00:00Z")
):
    for page in paginate(
        "https://api.github.com/repos/dlt-hub/dlt/comments",
        params={"per_page": 100}
    ):
        yield page

```

We can load this resource separately from the issues resource; however, loading both issues and comments in one go is more efficient. To do that, we'll use the `@dlt.source` decorator on a function that returns a list of resources:

```codeBlockLines_RjmQ
@dlt.source
def github_source():
    return [get_issues, get_comments]

```

`github_source()` groups resources into a [source](https://dlthub.com/docs/general-usage/source). A dlt source is a logical grouping of resources. You use it to group resources that belong together, for example, to load data from the same API. Loading data from a source can be run in a single pipeline. Here's what our updated script looks like:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers.rest_client import paginate

@dlt.resource(
    table_name="issues",
    write_disposition="merge",
    primary_key="id",
)
def get_issues(
    updated_at=dlt.sources.incremental("updated_at", initial_value="1970-01-01T00:00:00Z")
):
    for page in paginate(
        "https://api.github.com/repos/dlt-hub/dlt/issues",
        params={
            "since": updated_at.last_value,
            "per_page": 100,
            "sort": "updated",
            "direction": "desc",
            "state": "open",
        }
    ):
        yield page

@dlt.resource(
    table_name="comments",
    write_disposition="merge",
    primary_key="id",
)
def get_comments(
    updated_at=dlt.sources.incremental("updated_at", initial_value="1970-01-01T00:00:00Z")
):
    for page in paginate(
        "https://api.github.com/repos/dlt-hub/dlt/comments",
        params={
            "since": updated_at.last_value,
            "per_page": 100,
        }
    ):
        yield page

@dlt.source
def github_source():
    return [get_issues, get_comments]

pipeline = dlt.pipeline(
    pipeline_name='github_with_source',
    destination='duckdb',
    dataset_name='github_data',
)

load_info = pipeline.run(github_source())
print(load_info)

```

### Dynamic resources [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#dynamic-resources "Direct link to Dynamic resources")

You've noticed that there's a lot of code duplication in the `get_issues` and `get_comments` functions. We can reduce that by extracting the common fetching code into a separate function and using it in both resources. Even better, we can use `dlt.resource` as a function and pass it the `fetch_github_data()` generator function directly. Here's the refactored code:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers.rest_client import paginate

BASE_GITHUB_URL = "https://api.github.com/repos/dlt-hub/dlt"

def fetch_github_data(endpoint, params={}):
    url = f"{BASE_GITHUB_URL}/{endpoint}"
    return paginate(url, params=params)

@dlt.source
def github_source():
    for endpoint in ["issues", "comments"]:
        params = {"per_page": 100}
        yield dlt.resource(
            fetch_github_data(endpoint, params),
            name=endpoint,
            write_disposition="merge",
            primary_key="id",
        )

pipeline = dlt.pipeline(
    pipeline_name='github_dynamic_source',
    destination='duckdb',
    dataset_name='github_data',
)
load_info = pipeline.run(github_source())
row_counts = pipeline.last_trace.last_normalize_info

```

## Handle secrets [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#handle-secrets "Direct link to Handle secrets")

For the next step, we'd want to get the [number of repository clones](https://docs.github.com/en/rest/metrics/traffic?apiVersion=2022-11-28#get-repository-clones) for our dlt repo from the GitHub API. However, the `traffic/clones` endpoint that returns the data requires [authentication](https://docs.github.com/en/rest/overview/authenticating-to-the-rest-api?apiVersion=2022-11-28).

Let's handle this by changing our `fetch_github_data()` function first:

```codeBlockLines_RjmQ
from dlt.sources.helpers.rest_client.auth import BearerTokenAuth

def fetch_github_data_with_token(endpoint, params={}, access_token=None):
    url = f"{BASE_GITHUB_URL}/{endpoint}"
    return paginate(
        url,
        params=params,
        auth=BearerTokenAuth(token=access_token) if access_token else None,
    )

@dlt.source
def github_source_with_token(access_token: str):
    for endpoint in ["issues", "comments", "traffic/clones"]:
        params = {"per_page": 100}
        yield dlt.resource(
            fetch_github_data_with_token(endpoint, params, access_token),
            name=endpoint,
            write_disposition="merge",
            primary_key="id",
        )

...

```

Here, we added an `access_token` parameter and now we can use it to pass the access token to the request:

```codeBlockLines_RjmQ
load_info = pipeline.run(github_source_with_token(access_token="ghp_XXXXX"))

```

It's a good start. But we'd want to follow the best practices and not hardcode the token in the script. One option is to set the token as an environment variable, load it with `os.getenv()`, and pass it around as a parameter. dlt offers a more convenient way to handle secrets and credentials: it lets you inject the arguments using a special `dlt.secrets.value` argument value.

To use it, change the `github_source()` function to:

```codeBlockLines_RjmQ
@dlt.source
def github_source_with_token(
    access_token: str = dlt.secrets.value,
):
    ...

```

When you add `dlt.secrets.value` as a default value for an argument, `dlt` will try to load and inject this value from different configuration sources in the following order:

1. Special environment variables.
2. `secrets.toml` file.

The `secrets.toml` file is located in the `~/.dlt` folder (for global configuration) or in the `.dlt` folder in the project folder (for project-specific configuration).

Let's add the token to the `~/.dlt/secrets.toml` file:

```codeBlockLines_RjmQ
[github_with_source_secrets]
access_token = "ghp_A...3aRY"

```

Now we can run the script and it will load the data from the `traffic/clones` endpoint:

```codeBlockLines_RjmQ
...

@dlt.source
def github_source_with_token(
    access_token: str = dlt.secrets.value,
):
    for endpoint in ["issues", "comments", "traffic/clones"]:
        params = {"per_page": 100}
        yield dlt.resource(
            fetch_github_data_with_token(endpoint, params, access_token),
            name=endpoint,
            write_disposition="merge",
            primary_key="id",
        )

pipeline = dlt.pipeline(
    pipeline_name="github_with_source_secrets",
    destination="duckdb",
    dataset_name="github_data",
)
load_info = pipeline.run(github_source())

```

## Configurable sources [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#configurable-sources "Direct link to Configurable sources")

The next step is to make our dlt GitHub source reusable so it can load data from any GitHub repo. We'll do that by changing both the `github_source()` and `fetch_github_data()` functions to accept the repo name as a parameter:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers.rest_client import paginate

BASE_GITHUB_URL = "https://api.github.com/repos/{repo_name}"

def fetch_github_data_with_token_and_params(repo_name, endpoint, params={}, access_token=None):
    """Fetch data from the GitHub API based on repo_name, endpoint, and params."""
    url = BASE_GITHUB_URL.format(repo_name=repo_name) + f"/{endpoint}"
    return paginate(
        url,
        params=params,
        auth=BearerTokenAuth(token=access_token) if access_token else None,
    )

@dlt.source
def github_source_with_token_and_repo(
    repo_name: str = dlt.config.value,
    access_token: str = dlt.secrets.value,
):
    for endpoint in ["issues", "comments", "traffic/clones"]:
        params = {"per_page": 100}
        yield dlt.resource(
            fetch_github_data_with_token_and_params(repo_name, endpoint, params, access_token),
            name=endpoint,
            write_disposition="merge",
            primary_key="id",
        )

pipeline = dlt.pipeline(
    pipeline_name="github_with_source_secrets",
    destination="duckdb",
    dataset_name="github_data",
)
load_info = pipeline.run(github_source())

```

Next, create a `.dlt/config.toml` file in the project folder and add the `repo_name` parameter to it:

```codeBlockLines_RjmQ
[github_with_source_secrets]
repo_name = "dlt-hub/dlt"

```

That's it! Now you have a reusable source that can load data from any GitHub repo.

## What’s next [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#whats-next "Direct link to What’s next")

Congratulations on completing the tutorial! You've come a long way since the [getting started](https://dlthub.com/docs/intro) guide. By now, you've mastered loading data from various GitHub API endpoints, organizing resources into sources, managing secrets securely, and creating reusable sources. You can use these skills to build your own pipelines and load data from any source.

Interested in learning more? Here are some suggestions:

1. You've been running your pipelines locally. Learn how to [deploy and run them in the cloud](https://dlthub.com/docs/walkthroughs/deploy-a-pipeline/).
2. Dive deeper into how dlt works by reading the [Using dlt](https://dlthub.com/docs/general-usage) section. Some highlights:
   - [Set up "last value" incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor).
   - Learn about data loading strategies: append, [replace](https://dlthub.com/docs/general-usage/full-loading), and [merge](https://dlthub.com/docs/general-usage/merge-loading).
   - [Connect the transformers to the resources](https://dlthub.com/docs/general-usage/resource#process-resources-with-dlttransformer) to load additional data or enrich it.
   - [Customize your data schema—set primary and merge keys, define column nullability, and specify data types](https://dlthub.com/docs/general-usage/resource#define-schema).
   - [Create your resources dynamically from data](https://dlthub.com/docs/general-usage/source#create-resources-dynamically).
   - [Transform your data before loading](https://dlthub.com/docs/general-usage/resource#customize-resources) and see some [examples of customizations like column renames and anonymization](https://dlthub.com/docs/general-usage/customising-pipelines/renaming_columns).
   - Employ data transformations using [SQL](https://dlthub.com/docs/dlt-ecosystem/transformations/sql) or [Pandas](https://dlthub.com/docs/dlt-ecosystem/transformations/sql).
   - [Pass config and credentials into your sources and resources](https://dlthub.com/docs/general-usage/credentials).
   - [Run in production: inspecting, tracing, retry policies, and cleaning up](https://dlthub.com/docs/running-in-production/running).
   - [Run resources in parallel, optimize buffers, and local storage](https://dlthub.com/docs/reference/performance)
   - [Use REST API client helpers](https://dlthub.com/docs/general-usage/http/rest-client) to simplify working with REST APIs.
3. Explore [destinations](https://dlthub.com/docs/dlt-ecosystem/destinations/) and [sources](https://dlthub.com/docs/dlt-ecosystem/verified-sources/) provided by us and the community.
4. Explore the [Examples](https://dlthub.com/docs/examples) section to see how dlt can be used in real-world scenarios.

- [What you will learn](https://dlthub.com/docs/tutorial/load-data-from-an-api#what-you-will-learn)
- [Prerequisites](https://dlthub.com/docs/tutorial/load-data-from-an-api#prerequisites)
- [Installing dlt](https://dlthub.com/docs/tutorial/load-data-from-an-api#installing-dlt)
- [Quick start](https://dlthub.com/docs/tutorial/load-data-from-an-api#quick-start)
  - [Explore data in Python](https://dlthub.com/docs/tutorial/load-data-from-an-api#explore-data-in-python)
  - [Explore data in Streamlit](https://dlthub.com/docs/tutorial/load-data-from-an-api#explore-data-in-streamlit)
- [Create a pipeline](https://dlthub.com/docs/tutorial/load-data-from-an-api#create-a-pipeline)
- [Run the pipeline](https://dlthub.com/docs/tutorial/load-data-from-an-api#run-the-pipeline)
- [Append or replace your data](https://dlthub.com/docs/tutorial/load-data-from-an-api#append-or-replace-your-data)
- [Declare loading behavior](https://dlthub.com/docs/tutorial/load-data-from-an-api#declare-loading-behavior)
  - [Load only new data (incremental loading)](https://dlthub.com/docs/tutorial/load-data-from-an-api#load-only-new-data-incremental-loading)
  - [Update and deduplicate your data](https://dlthub.com/docs/tutorial/load-data-from-an-api#update-and-deduplicate-your-data)
- [Using pagination helper](https://dlthub.com/docs/tutorial/load-data-from-an-api#using-pagination-helper)
- [Use source decorator](https://dlthub.com/docs/tutorial/load-data-from-an-api#use-source-decorator)
  - [Dynamic resources](https://dlthub.com/docs/tutorial/load-data-from-an-api#dynamic-resources)
- [Handle secrets](https://dlthub.com/docs/tutorial/load-data-from-an-api#handle-secrets)
- [Configurable sources](https://dlthub.com/docs/tutorial/load-data-from-an-api#configurable-sources)
- [What’s next](https://dlthub.com/docs/tutorial/load-data-from-an-api#whats-next)

----- https://dlthub.com/docs/general-usage/incremental/cursor#transform-records-before-incremental-processing -----

Version: 1.11.0 (latest)

On this page

In most REST APIs (and other data sources, i.e., database tables), you can request new or updated data by passing a timestamp or ID of the "last" record to a query. The API/database returns just the new/updated records from which you take the maximum/minimum timestamp/ID for the next load.

To do incremental loading this way, we need to:

- Figure out which field is used to track changes (the so-called **cursor field**) (e.g., "inserted\_at", "updated\_at", etc.);
- Determine how to pass the "last" (maximum/minimum) value of the cursor field to an API to get just new or modified data (how we do this depends on the source API).

Once you've figured that out, `dlt` takes care of finding maximum/minimum cursor field values, removing duplicates, and managing the state with the last values of the cursor. Take a look at the GitHub example below, where we request recently created issues.

```codeBlockLines_RjmQ
@dlt.resource(primary_key="id")
def repo_issues(
    access_token,
    repository,
    updated_at = dlt.sources.incremental("updated_at", initial_value="1970-01-01T00:00:00Z")
):
    # Get issues since "updated_at" stored in state on previous run (or initial_value on first run)
    for page in _get_issues_page(access_token, repository, since=updated_at.start_value):
        yield page
        # Last_value is updated after every page
        print(updated_at.last_value)

```

Here we add an `updated_at` argument that will receive incremental state, initialized to `1970-01-01T00:00:00Z`. It is configured to track the `updated_at` field in issues yielded by the `repo_issues` resource. It will store the newest `updated_at` value in `dlt` [state](https://dlthub.com/docs/general-usage/state) and make it available in `updated_at.start_value` on the next pipeline run. This value is inserted in the `_get_issues_page` function into the request query param **since** to the [GitHub API](https://docs.github.com/en/rest/issues/issues?#list-repository-issues).

In essence, the `dlt.sources.incremental` instance above:

- **updated\_at.initial\_value** which is always equal to "1970-01-01T00:00:00Z" passed in the constructor
- **updated\_at.start\_value** a maximum `updated_at` value from the previous run or the **initial\_value** on the first run
- **updated\_at.last\_value** a "real-time" `updated_at` value updated with each yielded item or page. Before the first yield, it equals **start\_value**
- **updated\_at.end\_value** (here not used) [marking the end of the backfill range](https://dlthub.com/docs/general-usage/incremental/cursor#using-end_value-for-backfill)

When paginating, you probably need the **start\_value** which does not change during the execution of the resource, however, most paginators will return a **next page** link which you should use.

Behind the scenes, dlt will deduplicate the results, i.e., in case the last issue is returned again ( `updated_at` filter is inclusive) and skip already loaded ones.

In the example below, we incrementally load the GitHub events, where the API does not let us filter for the newest events - it always returns all of them. Nevertheless, `dlt` will load only the new items, filtering out all the duplicates and past issues.

```codeBlockLines_RjmQ
# Use naming function in table name to generate separate tables for each event
@dlt.resource(primary_key="id", table_name=lambda i: i['type'])  # type: ignore
def repo_events(
    last_created_at = dlt.sources.incremental("created_at", initial_value="1970-01-01T00:00:00Z", last_value_func=max), row_order="desc"
) -> Iterator[TDataItems]:
    repos_path = "/repos/%s/%s/events" % (urllib.parse.quote(owner), urllib.parse.quote(name))
    for page in _get_rest_pages(access_token, repos_path + "?per_page=100"):
        yield page

```

We just yield all the events and `dlt` does the filtering (using the `id` column declared as `primary_key`).

GitHub returns events ordered from newest to oldest. So we declare the `rows_order` as **descending** to [stop requesting more pages once the incremental value is out of range](https://dlthub.com/docs/general-usage/incremental/cursor#declare-row-order-to-not-request-unnecessary-data). We stop requesting more data from the API after finding the first event with `created_at` earlier than `initial_value`.

note

`dlt.sources.incremental` is implemented as a [filter function](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data) that is executed **after** all other transforms you add with `add_map` or `add_filter`. This means that you can manipulate the data item before the incremental filter sees it. For example:

- You can create a surrogate primary key from other columns
- You can modify the cursor value or create a new field composed of other fields
- Dump Pydantic models to Python dicts to allow incremental to find custom values

[Data validation with Pydantic](https://dlthub.com/docs/general-usage/schema-contracts#use-pydantic-models-for-data-validation) happens **before** incremental filtering.

## Max, min, or custom `last_value_func` [​](https://dlthub.com/docs/general-usage/incremental/cursor\#max-min-or-custom-last_value_func "Direct link to max-min-or-custom-last_value_func")

`dlt.sources.incremental` allows you to choose a function that orders (compares) cursor values to the current `last_value`.

- The default function is the built-in `max`, which returns the larger value of the two.
- Another built-in, `min`, returns the smaller value.

You can also pass your custom function. This lets you define
`last_value` on nested types, i.e., dictionaries, and store indexes of last values, not just simple
types. The `last_value` argument is a [JSON Path](https://github.com/json-path/JsonPath#operators)
and lets you select nested data (including the whole data item when `$` is used).
The example below creates a last value which is a dictionary holding a max `created_at` value for each
created table name:

```codeBlockLines_RjmQ
def by_event_type(event):
    last_value = None
    if len(event) == 1:
        item, = event
    else:
        item, last_value = event

    if last_value is None:
        last_value = {}
    else:
        last_value = dict(last_value)
    item_type = item["type"]
    last_value[item_type] = max(item["created_at"], last_value.get(item_type, "1970-01-01T00:00:00Z"))
    return last_value

@dlt.resource(primary_key="id", table_name=lambda i: i['type'])
def get_events(last_created_at = dlt.sources.incremental("$", last_value_func=by_event_type)):
    with open("tests/normalize/cases/github.events.load_page_1_duck.json", "r", encoding="utf-8") as f:
        yield json.load(f)

```

## Using `end_value` for backfill [​](https://dlthub.com/docs/general-usage/incremental/cursor\#using-end_value-for-backfill "Direct link to using-end_value-for-backfill")

You can specify both initial and end dates when defining incremental loading. Let's go back to our Github example:

```codeBlockLines_RjmQ
@dlt.resource(primary_key="id")
def repo_issues(
    access_token,
    repository,
    created_at=dlt.sources.incremental("created_at", initial_value="1970-01-01T00:00:00Z", end_value="2022-07-01T00:00:00Z")
):
    # get issues created from the last "created_at" value
    for page in _get_issues_page(access_token, repository, since=created_at.start_value, until=created_at.end_value):
        yield page

```

Above, we use the `initial_value` and `end_value` arguments of the `incremental` to define the range of issues that we want to retrieve
and pass this range to the Github API ( `since` and `until`). As in the examples above, `dlt` will make sure that only the issues from
the defined range are returned.

Please note that when `end_date` is specified, `dlt` **will not modify the existing incremental state**. The backfill is **stateless** and:

1. You can run backfill and incremental load in parallel (i.e., in an Airflow DAG) in a single pipeline.
2. You can partition your backfill into several smaller chunks and run them in parallel as well.

To define specific ranges to load, you can simply override the incremental argument in the resource, for example:

```codeBlockLines_RjmQ
july_issues = repo_issues(
    created_at=dlt.sources.incremental(
        initial_value='2022-07-01T00:00:00Z', end_value='2022-08-01T00:00:00Z'
    )
)
august_issues = repo_issues(
    created_at=dlt.sources.incremental(
        initial_value='2022-08-01T00:00:00Z', end_value='2022-09-01T00:00:00Z'
    )
)
...

```

Note that dlt's incremental filtering considers the ranges half-closed. `initial_value` is inclusive, `end_value` is exclusive, so chaining ranges like above works without overlaps. This behaviour can be changed with the `range_start` (default `"closed"`) and `range_end` (default `"open"`) arguments.

## Declare row order to not request unnecessary data [​](https://dlthub.com/docs/general-usage/incremental/cursor\#declare-row-order-to-not-request-unnecessary-data "Direct link to Declare row order to not request unnecessary data")

With the `row_order` argument set, dlt will stop retrieving data from the data source (e.g., GitHub API) if it detects that the values of the cursor field are out of the range of **start** and **end** values.

In particular:

- dlt stops processing when the resource yields any item with a cursor value _equal to or greater than_ the `end_value` and `row_order` is set to **asc**. ( `end_value` is not included)
- dlt stops processing when the resource yields any item with a cursor value _lower_ than the `last_value` and `row_order` is set to **desc**. ( `last_value` is included)

note

"higher" and "lower" here refer to when the default `last_value_func` is used ( `max()`),
when using `min()` "higher" and "lower" are inverted.

caution

If you use `row_order`, **make sure that the data source returns ordered records** (ascending / descending) on the cursor field,
e.g., if an API returns results both higher and lower
than the given `end_value` in no particular order, data reading stops and you'll miss the data items that were out of order.

Row order is most useful when:

1. The data source does **not** offer start/end filtering of results (e.g., there is no `start_time/end_time` query parameter or similar).
2. The source returns results **ordered by the cursor field**.

The GitHub events example is exactly such a case. The results are ordered on cursor value descending, but there's no way to tell the API to limit returned items to those created before a certain date. Without the `row_order` setting, we'd be getting all events, each time we extract the `github_events` resource.

In the same fashion, the `row_order` can be used to **optimize backfill** so we don't continue
making unnecessary API requests after the end of the range is reached. For example:

```codeBlockLines_RjmQ
@dlt.resource(primary_key="id")
def tickets(
    zendesk_client,
    updated_at=dlt.sources.incremental(
        "updated_at",
        initial_value="2023-01-01T00:00:00Z",
        end_value="2023-02-01T00:00:00Z",
        row_order="asc"
    ),
):
    for page in zendesk_client.get_pages(
        "/api/v2/incremental/tickets", "tickets", start_time=updated_at.start_value
    ):
        yield page

```

In this example, we're loading tickets from Zendesk. The Zendesk API yields items paginated and ordered from oldest to newest,
but only offers a `start_time` parameter for filtering, so we cannot tell it to
stop retrieving data at `end_value`. Instead, we set `row_order` to `asc` and `dlt` will stop
getting more pages from the API after the first page with a cursor value `updated_at` is found older
than `end_value`.

caution

In rare cases when you use Incremental with a transformer, `dlt` will not be able to automatically close
the generator associated with a row that is out of range. You can still call the `can_close()` method on
incremental and exit the yield loop when true.

tip

The `dlt.sources.incremental` instance provides `start_out_of_range` and `end_out_of_range`
attributes which are set when the resource yields an element with a higher/lower cursor value than the
initial or end values. If you do not want `dlt` to stop processing automatically and instead want to handle such events yourself, do not specify `row_order`:

```codeBlockLines_RjmQ
@dlt.transformer(primary_key="id")
def tickets(
    zendesk_client,
    updated_at=dlt.sources.incremental(
        "updated_at",
        initial_value="2023-01-01T00:00:00Z",
        end_value="2023-02-01T00:00:00Z",
        row_order="asc"
    ),
):
    for page in zendesk_client.get_pages(
        "/api/v2/incremental/tickets", "tickets", start_time=updated_at.start_value
    ):
        yield page
        # Stop loading when we reach the end value
        if updated_at.end_out_of_range:
            return

```

## Deduplicate overlapping ranges with primary key [​](https://dlthub.com/docs/general-usage/incremental/cursor\#deduplicate-overlapping-ranges-with-primary-key "Direct link to Deduplicate overlapping ranges with primary key")

`Incremental` **does not** deduplicate datasets like the **merge** write disposition does. However, it ensures that when another portion of data is extracted, records that were previously loaded won't be included again. `dlt` assumes that you load a range of data, where the lower bound is inclusive (i.e., greater than or equal). This ensures that you never lose any data but will also re-acquire some rows. For example, if you have a database table with a cursor field on `updated_at` which has a day resolution, then there's a high chance that after you extract data on a given day, more records will still be added. When you extract on the next day, you should reacquire data from the last day to ensure all records are present; however, this will create overlap with data from the previous extract.

By default, a content hash (a hash of the JSON representation of a row) will be used to deduplicate. This may be slow, so `dlt.sources.incremental` will inherit the primary key that is set on the resource. You can optionally set a `primary_key` that is used exclusively to deduplicate and which does not become a table hint. The same setting lets you disable the deduplication altogether when an empty tuple is passed. Below, we pass `primary_key` directly to `incremental` to disable deduplication. That overrides the `delta` primary\_key set in the resource:

```codeBlockLines_RjmQ
@dlt.resource(primary_key="delta")
# disable the unique value check by passing () as primary key to incremental
def some_data(last_timestamp=dlt.sources.incremental("item.ts", primary_key=())):
    for i in range(-10, 10):
        yield {"delta": i, "item": {"ts": pendulum.now().timestamp()}}

```

This deduplication process is always enabled when `range_start` is set to `"closed"` (default).
When you pass `range_start="open"` no deduplication is done as it is not needed as rows with the previous cursor value are excluded. This can be a useful optimization to avoid the performance overhead of deduplication if the cursor field is guaranteed to be unique.

## Using `dlt.sources.incremental` with dynamically created resources [​](https://dlthub.com/docs/general-usage/incremental/cursor\#using-dltsourcesincremental-with-dynamically-created-resources "Direct link to using-dltsourcesincremental-with-dynamically-created-resources")

When resources are [created dynamically](https://dlthub.com/docs/general-usage/source#create-resources-dynamically), it is possible to use the `dlt.sources.incremental` definition as well.

```codeBlockLines_RjmQ
@dlt.source
def stripe():
    # declare a generator function
    def get_resource(
        endpoints: List[str] = ENDPOINTS,
        created: dlt.sources.incremental=dlt.sources.incremental("created")
    ):
        ...

    # create resources for several endpoints on a single decorator function
    for endpoint in endpoints:
        yield dlt.resource(
            get_resource,
            name=endpoint.value,
            write_disposition="merge",
            primary_key="id"
        )(endpoint)

```

Please note that in the example above, `get_resource` is passed as a function to `dlt.resource` to which we bind the endpoint: **dlt.resource(...)(endpoint)**.

caution

The typical mistake is to pass a generator (not a function) as below:

`yield dlt.resource(get_resource(endpoint), name=endpoint.value, write_disposition="merge", primary_key="id")`.

Here we call **get\_resource(endpoint)** and that creates an un-evaluated generator on which the resource is created. That prevents `dlt` from controlling the **created** argument during runtime and will result in an `IncrementalUnboundError` exception.

## Using Airflow schedule for backfill and incremental loading [​](https://dlthub.com/docs/general-usage/incremental/cursor\#using-airflow-schedule-for-backfill-and-incremental-loading "Direct link to Using Airflow schedule for backfill and incremental loading")

When [running an Airflow task](https://dlthub.com/docs/walkthroughs/deploy-a-pipeline/deploy-with-airflow-composer#2-modify-dag-file), you can opt-in your resource to get the `initial_value`/ `start_value` and `end_value` from the Airflow schedule associated with your DAG. Let's assume that the **Zendesk tickets** resource contains a year of data with thousands of tickets. We want to backfill the last year of data week by week and then continue with incremental loading daily.

```codeBlockLines_RjmQ
@dlt.resource(primary_key="id")
def tickets(
    zendesk_client,
    updated_at=dlt.sources.incremental[int](
        "updated_at",
        allow_external_schedulers=True
    ),
):
    for page in zendesk_client.get_pages(
        "/api/v2/incremental/tickets", "tickets", start_time=updated_at.start_value
    ):
        yield page

```

We opt-in to the Airflow scheduler by setting `allow_external_schedulers` to `True`:

1. When running on Airflow, the start and end values are controlled by Airflow and the dlt [state](https://dlthub.com/docs/general-usage/state) is not used.
2. In all other environments, the `incremental` behaves as usual, maintaining the dlt state.

Let's generate a deployment with `dlt deploy zendesk_pipeline.py airflow-composer` and customize the DAG:

```codeBlockLines_RjmQ
from dlt.helpers.airflow_helper import PipelineTasksGroup

@dag(
    schedule_interval='@weekly',
    start_date=pendulum.DateTime(2023, 2, 1),
    end_date=pendulum.DateTime(2023, 8, 1),
    catchup=True,
    max_active_runs=1,
    default_args=default_task_args
)
def zendesk_backfill_bigquery():
    tasks = PipelineTasksGroup("zendesk_support_backfill", use_data_folder=False, wipe_local_data=True)

    # import zendesk like in the demo script
    from zendesk import zendesk_support

    pipeline = dlt.pipeline(
        pipeline_name="zendesk_support_backfill",
        dataset_name="zendesk_support_data",
        destination='bigquery',
    )
    # select only incremental endpoints in support api
    data = zendesk_support().with_resources("tickets", "ticket_events", "ticket_metric_events")
    # create the source, the "serialize" decompose option will convert dlt resources into Airflow tasks. use "none" to disable it
    tasks.add_run(pipeline, data, decompose="serialize", trigger_rule="all_done", retries=0, provide_context=True)

zendesk_backfill_bigquery()

```

What got customized:

1. We use a weekly schedule and want to get the data from February 2023 ( `start_date`) until the end of July ( `end_date`).
2. We make Airflow generate all weekly runs ( `catchup` is True).
3. We create `zendesk_support` resources where we select only the incremental resources we want to backfill.

When you enable the DAG in Airflow, it will generate several runs and start executing them, starting in February and ending in August. Your resource will receive subsequent weekly intervals starting with `2023-02-12, 00:00:00 UTC` to `2023-02-19, 00:00:00 UTC`.

You can repurpose the DAG above to start loading new data incrementally after (or during) the backfill:

```codeBlockLines_RjmQ
@dag(
    schedule_interval='@daily',
    start_date=pendulum.DateTime(2023, 2, 1),
    catchup=False,
    max_active_runs=1,
    default_args=default_task_args
)
def zendesk_new_bigquery():
    tasks = PipelineTasksGroup("zendesk_support_new", use_data_folder=False, wipe_local_data=True)

    # import your source from pipeline script
    from zendesk import zendesk_support

    pipeline = dlt.pipeline(
        pipeline_name="zendesk_support_new",
        dataset_name="zendesk_support_data",
        destination='bigquery',
    )
    tasks.add_run(pipeline, zendesk_support(), decompose="serialize", trigger_rule="all_done", retries=0, provide_context=True)

```

Above, we switch to a daily schedule and disable catchup and end date. We also load all the support resources to the same dataset as backfill ( `zendesk_support_data`).
If you want to run this DAG parallel with the backfill DAG, change the pipeline name, for example, to `zendesk_support_new` as above.

**Under the hood**

Before `dlt` starts executing incremental resources, it looks for `data_interval_start` and `data_interval_end` Airflow task context variables. These are mapped to `initial_value` and `end_value` of the `Incremental` class:

1. `dlt` is smart enough to convert Airflow datetime to ISO strings or Unix timestamps if your resource is using them. In our example, we instantiate `updated_at=dlt.sources.incremental[int]`, where we declare the last value type to be **int**. `dlt` can also infer the type if you provide the `initial_value` argument.
2. If `data_interval_end` is in the future or is None, `dlt` sets the `end_value` to **now**.
3. If `data_interval_start` == `data_interval_end`, we have a manually triggered DAG run. In that case, `data_interval_end` will also be set to **now**.

**Manual runs**

You can run DAGs manually, but you must remember to specify the Airflow logical date of the run in the past (use the Run with config option). For such a run, `dlt` will load all data from that past date until now.
If you do not specify the past date, a run with a range (now, now) will happen, yielding no data.

## Reading incremental loading parameters from configuration [​](https://dlthub.com/docs/general-usage/incremental/cursor\#reading-incremental-loading-parameters-from-configuration "Direct link to Reading incremental loading parameters from configuration")

Consider the example below for reading incremental loading parameters from "config.toml". We create a `generate_incremental_records` resource that yields "id", "idAfter", and "name". This resource retrieves `cursor_path` and `initial_value` from "config.toml".

1. In "config.toml", define the `cursor_path` and `initial_value` as:

```codeBlockLines_RjmQ
# Configuration snippet for an incremental resource
[pipeline_with_incremental.sources.id_after]
cursor_path = "idAfter"
initial_value = 10

```

`cursor_path` is assigned the value "idAfter" with an initial value of 10.

2. Here's how the `generate_incremental_records` resource uses the `cursor_path` defined in "config.toml":

```codeBlockLines_RjmQ
@dlt.resource(table_name="incremental_records")
def generate_incremental_records(id_after: dlt.sources.incremental = dlt.config.value):
       for i in range(150):
           yield {"id": i, "idAfter": i, "name": "name-" + str(i)}

pipeline = dlt.pipeline(
       pipeline_name="pipeline_with_incremental",
       destination="duckdb",
)

pipeline.run(generate_incremental_records)

```

`id_after` incrementally stores the latest `cursor_path` value for future pipeline runs.

## Loading when incremental cursor path is missing or value is None/NULL [​](https://dlthub.com/docs/general-usage/incremental/cursor\#loading-when-incremental-cursor-path-is-missing-or-value-is-nonenull "Direct link to Loading when incremental cursor path is missing or value is None/NULL")

You can customize the incremental processing of dlt by setting the parameter `on_cursor_value_missing`.

When loading incrementally with the default settings, there are two assumptions:

1. Each row contains the cursor path.
2. Each row is expected to contain a value at the cursor path that is not `None`.

For example, the two following source data will raise an error:

```codeBlockLines_RjmQ
@dlt.resource
def some_data_without_cursor_path(updated_at=dlt.sources.incremental("updated_at")):
    yield [\
        {"id": 1, "created_at": 1, "updated_at": 1},\
        {"id": 2, "created_at": 2},  # cursor field is missing\
    ]

list(some_data_without_cursor_path())

@dlt.resource
def some_data_without_cursor_value(updated_at=dlt.sources.incremental("updated_at")):
    yield [\
        {"id": 1, "created_at": 1, "updated_at": 1},\
        {"id": 3, "created_at": 4, "updated_at": None},  # value at cursor field is None\
    ]

list(some_data_without_cursor_value())

```

To process a data set where some records do not include the incremental cursor path or where the values at the cursor path are `None`, there are the following four options:

1. Configure the incremental load to raise an exception in case there is a row where the cursor path is missing or has the value `None` using `incremental(..., on_cursor_value_missing="raise")`. This is the default behavior.
2. Configure the incremental load to tolerate the missing cursor path and `None` values using `incremental(..., on_cursor_value_missing="include")`.
3. Configure the incremental load to exclude the missing cursor path and `None` values using `incremental(..., on_cursor_value_missing="exclude")`.
4. Before the incremental processing begins: Ensure that the incremental field is present and transform the values at the incremental cursor to a value different from `None`. [See docs below](https://dlthub.com/docs/general-usage/incremental/cursor#transform-records-before-incremental-processing)

Here is an example of including rows where the incremental cursor value is missing or `None`:

```codeBlockLines_RjmQ
@dlt.resource
def some_data(updated_at=dlt.sources.incremental("updated_at", on_cursor_value_missing="include")):
    yield [\
        {"id": 1, "created_at": 1, "updated_at": 1},\
        {"id": 2, "created_at": 2},\
        {"id": 3, "created_at": 4, "updated_at": None},\
    ]

result = list(some_data())
assert len(result) == 3
assert result[1] == {"id": 2, "created_at": 2}
assert result[2] == {"id": 3, "created_at": 4, "updated_at": None}

```

If you do not want to import records without the cursor path or where the value at the cursor path is `None`, use the following incremental configuration:

```codeBlockLines_RjmQ
@dlt.resource
def some_data(updated_at=dlt.sources.incremental("updated_at", on_cursor_value_missing="exclude")):
    yield [\
        {"id": 1, "created_at": 1, "updated_at": 1},\
        {"id": 2, "created_at": 2},\
        {"id": 3, "created_at": 4, "updated_at": None},\
    ]

result = list(some_data())
assert len(result) == 1

```

## Transform records before incremental processing [​](https://dlthub.com/docs/general-usage/incremental/cursor\#transform-records-before-incremental-processing "Direct link to Transform records before incremental processing")

If you want to load data that includes `None` values, you can transform the records before the incremental processing.
You can add steps to the pipeline that [filter, transform, or pivot your data](https://dlthub.com/docs/general-usage/resource#filter-transform-and-pivot-data).

caution

It is important to set the `insert_at` parameter of the `add_map` function to control the order of execution and ensure that your custom steps are executed before the incremental processing starts.
In the following example, the step of data yielding is at `index = 0`, the custom transformation at `index = 1`, and the incremental processing at `index = 2`.

See below how you can modify rows before the incremental processing using `add_map()` and filter rows using `add_filter()`.

```codeBlockLines_RjmQ
@dlt.resource
def some_data(updated_at=dlt.sources.incremental("updated_at")):
    yield [\
        {"id": 1, "created_at": 1, "updated_at": 1},\
        {"id": 2, "created_at": 2, "updated_at": 2},\
        {"id": 3, "created_at": 4, "updated_at": None},\
    ]

def set_default_updated_at(record):
    if record.get("updated_at") is None:
        record["updated_at"] = record.get("created_at")
    return record

# Modifies records before the incremental processing
with_default_values = some_data().add_map(set_default_updated_at, insert_at=1)
result = list(with_default_values)
assert len(result) == 3
assert result[2]["updated_at"] == 4

# Removes records before the incremental processing
without_none = some_data().add_filter(lambda r: r.get("updated_at") is not None, insert_at=1)
result_filtered = list(without_none)
assert len(result_filtered) == 2

```

- [Max, min, or custom `last_value_func`](https://dlthub.com/docs/general-usage/incremental/cursor#max-min-or-custom-last_value_func)
- [Using `end_value` for backfill](https://dlthub.com/docs/general-usage/incremental/cursor#using-end_value-for-backfill)
- [Declare row order to not request unnecessary data](https://dlthub.com/docs/general-usage/incremental/cursor#declare-row-order-to-not-request-unnecessary-data)
- [Deduplicate overlapping ranges with primary key](https://dlthub.com/docs/general-usage/incremental/cursor#deduplicate-overlapping-ranges-with-primary-key)
- [Using `dlt.sources.incremental` with dynamically created resources](https://dlthub.com/docs/general-usage/incremental/cursor#using-dltsourcesincremental-with-dynamically-created-resources)
- [Using Airflow schedule for backfill and incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor#using-airflow-schedule-for-backfill-and-incremental-loading)
- [Reading incremental loading parameters from configuration](https://dlthub.com/docs/general-usage/incremental/cursor#reading-incremental-loading-parameters-from-configuration)
- [Loading when incremental cursor path is missing or value is None/NULL](https://dlthub.com/docs/general-usage/incremental/cursor#loading-when-incremental-cursor-path-is-missing-or-value-is-nonenull)
- [Transform records before incremental processing](https://dlthub.com/docs/general-usage/incremental/cursor#transform-records-before-incremental-processing)

----- https://dlthub.com/docs/general-usage/incremental-loading#incremental_loading-with-last-value -----

Version: 1.11.0 (latest)

On this page

Incremental loading is the act of loading only new or changed data and not old records that we have already loaded. It enables low-latency and low-cost data transfer.

The challenge of incremental pipelines is that if we do not keep track of the state of the load (i.e., which increments were loaded and which are to be loaded), we may encounter issues. Read more about state [here](https://dlthub.com/docs/general-usage/state).

## Choosing a write disposition [​](https://dlthub.com/docs/general-usage/incremental-loading\#choosing-a-write-disposition "Direct link to Choosing a write disposition")

### The 3 write dispositions: [​](https://dlthub.com/docs/general-usage/incremental-loading\#the-3-write-dispositions "Direct link to The 3 write dispositions:")

- **Full load**: replaces the destination dataset with whatever the source produced on this run. To achieve this, use `write_disposition='replace'` in your resources. Learn more in the [full loading docs](https://dlthub.com/docs/general-usage/full-loading).

- **Append**: appends the new data to the destination. Use `write_disposition='append'`.

- **Merge**: Merges new data into the destination using `merge_key` and/or deduplicates/upserts new data using `primary_key`. Use `write_disposition='merge'`.

### How to choose the right write disposition [​](https://dlthub.com/docs/general-usage/incremental-loading\#how-to-choose-the-right-write-disposition "Direct link to How to choose the right write disposition")

![write disposition flowchart](https://storage.googleapis.com/dlt-blog-images/flowchart_for_scd2.png)

The "write disposition" you choose depends on the dataset and how you can extract it.

To find the "write disposition" you should use, the first question you should ask yourself is "Is my data stateful or stateless"? Stateful data has a state that is subject to change - for example, a user's profile. Stateless data cannot change - for example, a recorded event, such as a page view.

Because stateless data does not need to be updated, we can just append it.

For stateful data, comes a second question - Can I extract it incrementally from the source? If yes, you should use [slowly changing dimensions (Type-2)](https://dlthub.com/docs/general-usage/merge-loading#scd2-strategy), which allow you to maintain historical records of data changes over time.

If not, then we need to replace the entire dataset. However, if we can request the data incrementally, such as "all users added or modified since yesterday," then we can simply apply changes to our existing dataset with the merge write disposition.

## Incremental loading strategies [​](https://dlthub.com/docs/general-usage/incremental-loading\#incremental-loading-strategies "Direct link to Incremental loading strategies")

dlt provides several approaches to incremental loading:

1. [Merge strategies](https://dlthub.com/docs/general-usage/merge-loading#merge-strategies) \- Choose between delete-insert, SCD2, and upsert approaches to incrementally update your data
2. [Cursor-based incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor) \- Track changes using a cursor field (like timestamp or ID)
3. [Lag / Attribution window](https://dlthub.com/docs/general-usage/incremental/lag) \- Refresh data within a specific time window
4. [Advanced state management](https://dlthub.com/docs/general-usage/incremental/advanced-state) \- Custom state tracking

## Doing a full refresh [​](https://dlthub.com/docs/general-usage/incremental-loading\#doing-a-full-refresh "Direct link to Doing a full refresh")

You may force a full refresh of `merge` and `append` pipelines:

1. In the case of a `merge`, the data in the destination is deleted and loaded fresh. Currently, we do not deduplicate data during the full refresh.
2. In the case of `dlt.sources.incremental`, the data is deleted and loaded from scratch. The state of the incremental is reset to the initial value.

Example:

```codeBlockLines_RjmQ
p = dlt.pipeline(destination="bigquery", dataset_name="dataset_name")
# Do a full refresh
p.run(merge_source(), write_disposition="replace")
# Do a full refresh of just one table
p.run(merge_source().with_resources("merge_table"), write_disposition="replace")
# Run a normal merge
p.run(merge_source())

```

Passing write disposition to `replace` will change the write disposition on all the resources in
`repo_events` during the run of the pipeline.

## Next steps [​](https://dlthub.com/docs/general-usage/incremental-loading\#next-steps "Direct link to Next steps")

- [Cursor-based incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor) \- Use timestamps or IDs to track changes
- [Advanced state management](https://dlthub.com/docs/general-usage/incremental/advanced-state) \- Advanced techniques for state tracking
- [Walkthroughs: Add incremental configuration to SQL resources](https://dlthub.com/docs/walkthroughs/sql-incremental-configuration) \- Step-by-step examples
- [Troubleshooting incremental loading](https://dlthub.com/docs/general-usage/incremental/troubleshooting) \- Common issues and how to fix them

- [Choosing a write disposition](https://dlthub.com/docs/general-usage/incremental-loading#choosing-a-write-disposition)
  - [The 3 write dispositions:](https://dlthub.com/docs/general-usage/incremental-loading#the-3-write-dispositions)
  - [How to choose the right write disposition](https://dlthub.com/docs/general-usage/incremental-loading#how-to-choose-the-right-write-disposition)
- [Incremental loading strategies](https://dlthub.com/docs/general-usage/incremental-loading#incremental-loading-strategies)
- [Doing a full refresh](https://dlthub.com/docs/general-usage/incremental-loading#doing-a-full-refresh)
- [Next steps](https://dlthub.com/docs/general-usage/incremental-loading#next-steps)

----- https://dlthub.com/docs/tutorial/load-data-from-an-api#handle-secrets -----

Version: 1.11.0 (latest)

On this page

This tutorial introduces you to foundational dlt concepts, demonstrating how to build a custom data pipeline that loads data from pure Python data structures to DuckDB. It starts with a simple example and progresses to more advanced topics and usage scenarios.

## What you will learn [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#what-you-will-learn "Direct link to What you will learn")

- Loading data from a list of Python dictionaries into DuckDB.
- Low-level API usage with a built-in HTTP client.
- Understand and manage data loading behaviors.
- Incrementally load new data and deduplicate existing data.
- Dynamic resource creation and reducing code redundancy.
- Group resources into sources.
- Securely handle secrets.
- Make reusable data sources.

## Prerequisites [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#prerequisites "Direct link to Prerequisites")

- Python 3.9 or higher installed
- Virtual environment set up

## Installing dlt [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#installing-dlt "Direct link to Installing dlt")

Before we start, make sure you have a Python virtual environment set up. Follow the instructions in the [installation guide](https://dlthub.com/docs/reference/installation) to create a new virtual environment and install dlt.

Verify that dlt is installed by running the following command in your terminal:

```codeBlockLines_RjmQ
dlt --version

```

## Quick start [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#quick-start "Direct link to Quick start")

For starters, let's load a list of Python dictionaries into DuckDB and inspect the created dataset. Here is the code:

```codeBlockLines_RjmQ
import dlt

data = [{"id": 1, "name": "Alice"}, {"id": 2, "name": "Bob"}]

pipeline = dlt.pipeline(
    pipeline_name="quick_start", destination="duckdb", dataset_name="mydata"
)
load_info = pipeline.run(data, table_name="users")

print(load_info)

```

When you look at the code above, you can see that we:

1. Import the `dlt` library.
2. Define our data to load.
3. Create a pipeline that loads data into DuckDB. Here we also specify the `pipeline_name` and `dataset_name`. We'll use both in a moment.
4. Run the pipeline.

Save this Python script with the name `quick_start_pipeline.py` and run the following command:

```codeBlockLines_RjmQ
python quick_start_pipeline.py

```

The output should look like:

```codeBlockLines_RjmQ
Pipeline quick_start completed in 0.59 seconds
1 load package(s) were loaded to destination duckdb and into dataset mydata
The duckdb destination used duckdb:////home/user-name/quick_start/quick_start.duckdb location to store data
Load package 1692364844.460054 is LOADED and contains no failed jobs

```

`dlt` just created a database schema called **mydata** (the `dataset_name`) with a table **users** in it.

### Explore data in Python [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#explore-data-in-python "Direct link to Explore data in Python")

You can use dlt [datasets](https://dlthub.com/docs/general-usage/dataset-access/dataset) to easily query the data in pure Python.

```codeBlockLines_RjmQ
# get the dataset
dataset = pipeline.dataset("mydata")

# get the user relation
table = dataset.users

# query the full table as dataframe
print(table.df())

# query the first 10 rows as arrow table
print(table.limit(10).arrow())

```

### Explore data in Streamlit [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#explore-data-in-streamlit "Direct link to Explore data in Streamlit")

To allow a sneak peek and basic discovery, you can take advantage of [built-in integration with Streamlit](https://dlthub.com/docs/reference/command-line-interface#dlt-pipeline-show):

```codeBlockLines_RjmQ
dlt pipeline quick_start show

```

**quick\_start** is the name of the pipeline from the script above. If you do not have Streamlit installed yet, do:

```codeBlockLines_RjmQ
pip install streamlit

```

Now you should see the **users** table:

![Streamlit Explore data](https://dlthub.com/docs/assets/images/streamlit-new-1a373dbae5d5d59643d6336317ad7c43.png)
Streamlit Explore data. Schema and data for a test pipeline “quick\_start”.

tip

`dlt` works in Jupyter Notebook and Google Colab! See our [Quickstart Colab Demo.](https://colab.research.google.com/drive/1NfSB1DpwbbHX9_t5vlalBTf13utwpMGx?usp=sharing)

Looking for the source code of all the snippets? You can find and run them [from this repository](https://github.com/dlt-hub/dlt/blob/devel/docs/website/docs/getting-started-snippets.py).

Now that you have a basic understanding of how to get started with dlt, you might be eager to dive deeper. For that, we need to switch to a more advanced data source - the GitHub API. We will load issues from our [dlt-hub/dlt](https://github.com/dlt-hub/dlt) repository.

note

This tutorial uses the GitHub REST API for demonstration purposes only. If you need to read data from a REST API, consider using dlt's REST API source. Check out the [REST API source tutorial](https://dlthub.com/docs/tutorial/rest-api) for a quick start or the [REST API source reference](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api) for more details.

## Create a pipeline [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#create-a-pipeline "Direct link to Create a pipeline")

First, we need to create a [pipeline](https://dlthub.com/docs/general-usage/pipeline). Pipelines are the main building blocks of `dlt` and are used to load data from sources to destinations. Open your favorite text editor and create a file called `github_issues.py`. Add the following code to it:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers import requests

# Specify the URL of the API endpoint
url = "https://api.github.com/repos/dlt-hub/dlt/issues"
# Make a request and check if it was successful
response = requests.get(url)
response.raise_for_status()

pipeline = dlt.pipeline(
    pipeline_name="github_issues",
    destination="duckdb",
    dataset_name="github_data",
)
# The response contains a list of issues
load_info = pipeline.run(response.json(), table_name="issues")

print(load_info)

```

Here's what the code above does:

1. It makes a request to the GitHub API endpoint and checks if the response is successful.
2. Then, it creates a dlt pipeline with the name `github_issues` and specifies that the data should be loaded to the `duckdb` destination and the `github_data` dataset. Nothing gets loaded yet.
3. Finally, it runs the pipeline with the data from the API response ( `response.json()`) and specifies that the data should be loaded to the `issues` table. The `run` method returns a `LoadInfo` object that contains information about the loaded data.

## Run the pipeline [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#run-the-pipeline "Direct link to Run the pipeline")

Save `github_issues.py` and run the following command:

```codeBlockLines_RjmQ
python github_issues.py

```

Once the data has been loaded, you can inspect the created dataset using the Streamlit app:

```codeBlockLines_RjmQ
dlt pipeline github_issues show

```

## Append or replace your data [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#append-or-replace-your-data "Direct link to Append or replace your data")

Try running the pipeline again with `python github_issues.py`. You will notice that the **issues** table contains two copies of the same data. This happens because the default load mode is `append`. It is very useful, for example, when you have daily data updates and you want to ingest them.

To get the latest data, we'd need to run the script again. But how to do that without duplicating the data?
One option is to tell `dlt` to replace the data in existing tables in the destination by using the `replace` write disposition. Change the `github_issues.py` script to the following:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers import requests

# Specify the URL of the API endpoint
url = "https://api.github.com/repos/dlt-hub/dlt/issues"
# Make a request and check if it was successful
response = requests.get(url)
response.raise_for_status()

pipeline = dlt.pipeline(
    pipeline_name='github_issues',
    destination='duckdb',
    dataset_name='github_data',
)
# The response contains a list of issues
load_info = pipeline.run(
    response.json(),
    table_name="issues",
    write_disposition="replace"  # <-- Add this line
)

print(load_info)

```

Run this script twice to see that the **issues** table still contains only one copy of the data.

tip

What if the API has changed and new fields get added to the response?
`dlt` will migrate your tables!
See the `replace` mode and table schema migration in action in our [Schema evolution colab demo](https://colab.research.google.com/drive/1H6HKFi-U1V4p0afVucw_Jzv1oiFbH2bu#scrollTo=e4y4sQ78P_OM).

Learn more:

- [Full load - how to replace your data](https://dlthub.com/docs/general-usage/full-loading).
- [Append, replace, and merge your tables](https://dlthub.com/docs/general-usage/incremental-loading).

## Declare loading behavior [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#declare-loading-behavior "Direct link to Declare loading behavior")

So far, we have been passing the data to the `run` method directly. This is a quick way to get started. However, frequently, you receive data in chunks, and you want to load it as it arrives. For example, you might want to load data from an API endpoint with pagination or a large file that does not fit in memory. In such cases, you can use Python generators as a data source.

You can pass a generator to the `run` method directly or use the `@dlt.resource` decorator to turn the generator into a [dlt resource](https://dlthub.com/docs/general-usage/resource). The decorator allows you to specify the loading behavior and relevant resource parameters.

### Load only new data (incremental loading) [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#load-only-new-data-incremental-loading "Direct link to Load only new data (incremental loading)")

Let's improve our GitHub API example and get only issues that were created since the last load.
Instead of using the `replace` write disposition and downloading all issues each time the pipeline is run, we do the following:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers import requests

@dlt.resource(table_name="issues", write_disposition="append")
def get_issues(
    created_at=dlt.sources.incremental("created_at", initial_value="1970-01-01T00:00:00Z")
):
    # NOTE: we read only open issues to minimize number of calls to the API.
    # There's a limit of ~50 calls for not authenticated Github users.
    url = (
        "https://api.github.com/repos/dlt-hub/dlt/issues"
        "?per_page=100&sort=created&directions=desc&state=open"
    )

    while True:
        response = requests.get(url)
        response.raise_for_status()
        yield response.json()

        # Stop requesting pages if the last element was already
        # older than initial value
        # Note: incremental will skip those items anyway, we just
        # do not want to use the api limits
        if created_at.start_out_of_range:
            break

        # get next page
        if "next" not in response.links:
            break
        url = response.links["next"]["url"]

pipeline = dlt.pipeline(
    pipeline_name="github_issues_incremental",
    destination="duckdb",
    dataset_name="github_data_append",
)

load_info = pipeline.run(get_issues)
row_counts = pipeline.last_trace.last_normalize_info

print(row_counts)
print("------")
print(load_info)

```

Let's take a closer look at the code above.

We use the `@dlt.resource` decorator to declare the table name into which data will be loaded and specify the `append` write disposition.

We request issues for the dlt-hub/dlt repository ordered by the **created\_at** field (descending) and yield them page by page in the `get_issues` generator function.

We also use `dlt.sources.incremental` to track the `created_at` field present in each issue to filter in the newly created ones.

Now run the script. It loads all the issues from our repo to `duckdb`. Run it again, and you can see that no issues got added (if no issues were created in the meantime).

Now you can run this script on a daily schedule, and each day you’ll load only issues created after the time of the previous pipeline run.

tip

Between pipeline runs, `dlt` keeps the state in the same database it loaded data into.
Peek into that state, the tables loaded, and get other information with:

```codeBlockLines_RjmQ
dlt pipeline -v github_issues_incremental info

```

Learn more:

- Declare your [resources](https://dlthub.com/docs/general-usage/resource) and group them in [sources](https://dlthub.com/docs/general-usage/source) using Python decorators.
- [Set up "last value" incremental loading.](https://dlthub.com/docs/general-usage/incremental/cursor)
- [Inspect pipeline after loading.](https://dlthub.com/docs/walkthroughs/run-a-pipeline#4-inspect-a-load-process)
- [`dlt` command line interface.](https://dlthub.com/docs/reference/command-line-interface)

### Update and deduplicate your data [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#update-and-deduplicate-your-data "Direct link to Update and deduplicate your data")

The script above finds **new** issues and adds them to the database.
It will ignore any updates to **existing** issue text, emoji reactions, etc.
To always get fresh content of all the issues, combine incremental load with the `merge` write disposition,
like in the script below.

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers import requests

@dlt.resource(
    table_name="issues",
    write_disposition="merge",
    primary_key="id",
)
def get_issues(
    updated_at=dlt.sources.incremental("updated_at", initial_value="1970-01-01T00:00:00Z")
):
    # NOTE: we read only open issues to minimize number of calls to
    # the API. There's a limit of ~50 calls for not authenticated
    # Github users
    url = (
        "https://api.github.com/repos/dlt-hub/dlt/issues"
        f"?since={updated_at.last_value}&per_page=100&sort=updated"
        "&directions=desc&state=open"
    )

    while True:
        response = requests.get(url)
        response.raise_for_status()
        yield response.json()

        # Get next page
        if "next" not in response.links:
            break
        url = response.links["next"]["url"]

pipeline = dlt.pipeline(
    pipeline_name="github_issues_merge",
    destination="duckdb",
    dataset_name="github_data_merge",
)
load_info = pipeline.run(get_issues)
row_counts = pipeline.last_trace.last_normalize_info

print(row_counts)
print("------")
print(load_info)

```

Above, we add the `primary_key` argument to the `dlt.resource()` that tells `dlt` how to identify the issues in the database to find duplicates whose content it will merge.

Note that we now track the `updated_at` field — so we filter in all issues **updated** since the last pipeline run (which also includes those newly created).

Pay attention to how we use the **since** parameter from the [GitHub API](https://docs.github.com/en/rest/issues/issues?apiVersion=2022-11-28#list-repository-issues)
and `updated_at.last_value` to tell GitHub to return issues updated only **after** the date we pass. `updated_at.last_value` holds the last `updated_at` value from the previous run.

[Learn more about merge write disposition](https://dlthub.com/docs/general-usage/merge-loading).

## Using pagination helper [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#using-pagination-helper "Direct link to Using pagination helper")

In the previous examples, we used the `requests` library to make HTTP requests to the GitHub API and handled pagination manually. `dlt` has a built-in [REST client](https://dlthub.com/docs/general-usage/http/rest-client) that simplifies API requests. We'll use the `paginate()` helper from it for the next example. The `paginate` function takes a URL and optional parameters (quite similar to `requests`) and returns a generator that yields pages of data.

Here's how the updated script looks:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers.rest_client import paginate

@dlt.resource(
    table_name="issues",
    write_disposition="merge",
    primary_key="id",
)
def get_issues(
    updated_at=dlt.sources.incremental("updated_at", initial_value="1970-01-01T00:00:00Z")
):
    for page in paginate(
        "https://api.github.com/repos/dlt-hub/dlt/issues",
        params={
            "since": updated_at.last_value,
            "per_page": 100,
            "sort": "updated",
            "direction": "desc",
            "state": "open",
        },
    ):
        yield page

pipeline = dlt.pipeline(
    pipeline_name="github_issues_merge",
    destination="duckdb",
    dataset_name="github_data_merge",
)
load_info = pipeline.run(get_issues)
row_counts = pipeline.last_trace.last_normalize_info

print(row_counts)
print("------")
print(load_info)

```

Let's zoom in on the changes:

1. The `while` loop that handled pagination is replaced with reading pages from the `paginate()` generator.
2. `paginate()` takes the URL of the API endpoint and optional parameters. In this case, we pass the `since` parameter to get only issues updated after the last pipeline run.
3. We're not explicitly setting up pagination; `paginate()` handles it for us. Magic! Under the hood, `paginate()` analyzes the response and detects the pagination method used by the API. Read more about pagination in the [REST client documentation](https://dlthub.com/docs/general-usage/http/rest-client#paginating-api-responses).

If you want to take full advantage of the `dlt` library, then we strongly suggest that you build your sources out of existing building blocks:
To make the most of `dlt`, consider the following:

## Use source decorator [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#use-source-decorator "Direct link to Use source decorator")

In the previous step, we loaded issues from the GitHub API. Now we'll load comments from the API as well. Here's a sample [dlt resource](https://dlthub.com/docs/general-usage/resource) that does that:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers.rest_client import paginate

@dlt.resource(
    table_name="comments",
    write_disposition="merge",
    primary_key="id",
)
def get_comments(
    updated_at=dlt.sources.incremental("updated_at", initial_value="1970-01-01T00:00:00Z")
):
    for page in paginate(
        "https://api.github.com/repos/dlt-hub/dlt/comments",
        params={"per_page": 100}
    ):
        yield page

```

We can load this resource separately from the issues resource; however, loading both issues and comments in one go is more efficient. To do that, we'll use the `@dlt.source` decorator on a function that returns a list of resources:

```codeBlockLines_RjmQ
@dlt.source
def github_source():
    return [get_issues, get_comments]

```

`github_source()` groups resources into a [source](https://dlthub.com/docs/general-usage/source). A dlt source is a logical grouping of resources. You use it to group resources that belong together, for example, to load data from the same API. Loading data from a source can be run in a single pipeline. Here's what our updated script looks like:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers.rest_client import paginate

@dlt.resource(
    table_name="issues",
    write_disposition="merge",
    primary_key="id",
)
def get_issues(
    updated_at=dlt.sources.incremental("updated_at", initial_value="1970-01-01T00:00:00Z")
):
    for page in paginate(
        "https://api.github.com/repos/dlt-hub/dlt/issues",
        params={
            "since": updated_at.last_value,
            "per_page": 100,
            "sort": "updated",
            "direction": "desc",
            "state": "open",
        }
    ):
        yield page

@dlt.resource(
    table_name="comments",
    write_disposition="merge",
    primary_key="id",
)
def get_comments(
    updated_at=dlt.sources.incremental("updated_at", initial_value="1970-01-01T00:00:00Z")
):
    for page in paginate(
        "https://api.github.com/repos/dlt-hub/dlt/comments",
        params={
            "since": updated_at.last_value,
            "per_page": 100,
        }
    ):
        yield page

@dlt.source
def github_source():
    return [get_issues, get_comments]

pipeline = dlt.pipeline(
    pipeline_name='github_with_source',
    destination='duckdb',
    dataset_name='github_data',
)

load_info = pipeline.run(github_source())
print(load_info)

```

### Dynamic resources [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#dynamic-resources "Direct link to Dynamic resources")

You've noticed that there's a lot of code duplication in the `get_issues` and `get_comments` functions. We can reduce that by extracting the common fetching code into a separate function and using it in both resources. Even better, we can use `dlt.resource` as a function and pass it the `fetch_github_data()` generator function directly. Here's the refactored code:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers.rest_client import paginate

BASE_GITHUB_URL = "https://api.github.com/repos/dlt-hub/dlt"

def fetch_github_data(endpoint, params={}):
    url = f"{BASE_GITHUB_URL}/{endpoint}"
    return paginate(url, params=params)

@dlt.source
def github_source():
    for endpoint in ["issues", "comments"]:
        params = {"per_page": 100}
        yield dlt.resource(
            fetch_github_data(endpoint, params),
            name=endpoint,
            write_disposition="merge",
            primary_key="id",
        )

pipeline = dlt.pipeline(
    pipeline_name='github_dynamic_source',
    destination='duckdb',
    dataset_name='github_data',
)
load_info = pipeline.run(github_source())
row_counts = pipeline.last_trace.last_normalize_info

```

## Handle secrets [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#handle-secrets "Direct link to Handle secrets")

For the next step, we'd want to get the [number of repository clones](https://docs.github.com/en/rest/metrics/traffic?apiVersion=2022-11-28#get-repository-clones) for our dlt repo from the GitHub API. However, the `traffic/clones` endpoint that returns the data requires [authentication](https://docs.github.com/en/rest/overview/authenticating-to-the-rest-api?apiVersion=2022-11-28).

Let's handle this by changing our `fetch_github_data()` function first:

```codeBlockLines_RjmQ
from dlt.sources.helpers.rest_client.auth import BearerTokenAuth

def fetch_github_data_with_token(endpoint, params={}, access_token=None):
    url = f"{BASE_GITHUB_URL}/{endpoint}"
    return paginate(
        url,
        params=params,
        auth=BearerTokenAuth(token=access_token) if access_token else None,
    )

@dlt.source
def github_source_with_token(access_token: str):
    for endpoint in ["issues", "comments", "traffic/clones"]:
        params = {"per_page": 100}
        yield dlt.resource(
            fetch_github_data_with_token(endpoint, params, access_token),
            name=endpoint,
            write_disposition="merge",
            primary_key="id",
        )

...

```

Here, we added an `access_token` parameter and now we can use it to pass the access token to the request:

```codeBlockLines_RjmQ
load_info = pipeline.run(github_source_with_token(access_token="ghp_XXXXX"))

```

It's a good start. But we'd want to follow the best practices and not hardcode the token in the script. One option is to set the token as an environment variable, load it with `os.getenv()`, and pass it around as a parameter. dlt offers a more convenient way to handle secrets and credentials: it lets you inject the arguments using a special `dlt.secrets.value` argument value.

To use it, change the `github_source()` function to:

```codeBlockLines_RjmQ
@dlt.source
def github_source_with_token(
    access_token: str = dlt.secrets.value,
):
    ...

```

When you add `dlt.secrets.value` as a default value for an argument, `dlt` will try to load and inject this value from different configuration sources in the following order:

1. Special environment variables.
2. `secrets.toml` file.

The `secrets.toml` file is located in the `~/.dlt` folder (for global configuration) or in the `.dlt` folder in the project folder (for project-specific configuration).

Let's add the token to the `~/.dlt/secrets.toml` file:

```codeBlockLines_RjmQ
[github_with_source_secrets]
access_token = "ghp_A...3aRY"

```

Now we can run the script and it will load the data from the `traffic/clones` endpoint:

```codeBlockLines_RjmQ
...

@dlt.source
def github_source_with_token(
    access_token: str = dlt.secrets.value,
):
    for endpoint in ["issues", "comments", "traffic/clones"]:
        params = {"per_page": 100}
        yield dlt.resource(
            fetch_github_data_with_token(endpoint, params, access_token),
            name=endpoint,
            write_disposition="merge",
            primary_key="id",
        )

pipeline = dlt.pipeline(
    pipeline_name="github_with_source_secrets",
    destination="duckdb",
    dataset_name="github_data",
)
load_info = pipeline.run(github_source())

```

## Configurable sources [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#configurable-sources "Direct link to Configurable sources")

The next step is to make our dlt GitHub source reusable so it can load data from any GitHub repo. We'll do that by changing both the `github_source()` and `fetch_github_data()` functions to accept the repo name as a parameter:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.helpers.rest_client import paginate

BASE_GITHUB_URL = "https://api.github.com/repos/{repo_name}"

def fetch_github_data_with_token_and_params(repo_name, endpoint, params={}, access_token=None):
    """Fetch data from the GitHub API based on repo_name, endpoint, and params."""
    url = BASE_GITHUB_URL.format(repo_name=repo_name) + f"/{endpoint}"
    return paginate(
        url,
        params=params,
        auth=BearerTokenAuth(token=access_token) if access_token else None,
    )

@dlt.source
def github_source_with_token_and_repo(
    repo_name: str = dlt.config.value,
    access_token: str = dlt.secrets.value,
):
    for endpoint in ["issues", "comments", "traffic/clones"]:
        params = {"per_page": 100}
        yield dlt.resource(
            fetch_github_data_with_token_and_params(repo_name, endpoint, params, access_token),
            name=endpoint,
            write_disposition="merge",
            primary_key="id",
        )

pipeline = dlt.pipeline(
    pipeline_name="github_with_source_secrets",
    destination="duckdb",
    dataset_name="github_data",
)
load_info = pipeline.run(github_source())

```

Next, create a `.dlt/config.toml` file in the project folder and add the `repo_name` parameter to it:

```codeBlockLines_RjmQ
[github_with_source_secrets]
repo_name = "dlt-hub/dlt"

```

That's it! Now you have a reusable source that can load data from any GitHub repo.

## What’s next [​](https://dlthub.com/docs/tutorial/load-data-from-an-api\#whats-next "Direct link to What’s next")

Congratulations on completing the tutorial! You've come a long way since the [getting started](https://dlthub.com/docs/intro) guide. By now, you've mastered loading data from various GitHub API endpoints, organizing resources into sources, managing secrets securely, and creating reusable sources. You can use these skills to build your own pipelines and load data from any source.

Interested in learning more? Here are some suggestions:

1. You've been running your pipelines locally. Learn how to [deploy and run them in the cloud](https://dlthub.com/docs/walkthroughs/deploy-a-pipeline/).
2. Dive deeper into how dlt works by reading the [Using dlt](https://dlthub.com/docs/general-usage) section. Some highlights:
   - [Set up "last value" incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor).
   - Learn about data loading strategies: append, [replace](https://dlthub.com/docs/general-usage/full-loading), and [merge](https://dlthub.com/docs/general-usage/merge-loading).
   - [Connect the transformers to the resources](https://dlthub.com/docs/general-usage/resource#process-resources-with-dlttransformer) to load additional data or enrich it.
   - [Customize your data schema—set primary and merge keys, define column nullability, and specify data types](https://dlthub.com/docs/general-usage/resource#define-schema).
   - [Create your resources dynamically from data](https://dlthub.com/docs/general-usage/source#create-resources-dynamically).
   - [Transform your data before loading](https://dlthub.com/docs/general-usage/resource#customize-resources) and see some [examples of customizations like column renames and anonymization](https://dlthub.com/docs/general-usage/customising-pipelines/renaming_columns).
   - Employ data transformations using [SQL](https://dlthub.com/docs/dlt-ecosystem/transformations/sql) or [Pandas](https://dlthub.com/docs/dlt-ecosystem/transformations/sql).
   - [Pass config and credentials into your sources and resources](https://dlthub.com/docs/general-usage/credentials).
   - [Run in production: inspecting, tracing, retry policies, and cleaning up](https://dlthub.com/docs/running-in-production/running).
   - [Run resources in parallel, optimize buffers, and local storage](https://dlthub.com/docs/reference/performance)
   - [Use REST API client helpers](https://dlthub.com/docs/general-usage/http/rest-client) to simplify working with REST APIs.
3. Explore [destinations](https://dlthub.com/docs/dlt-ecosystem/destinations/) and [sources](https://dlthub.com/docs/dlt-ecosystem/verified-sources/) provided by us and the community.
4. Explore the [Examples](https://dlthub.com/docs/examples) section to see how dlt can be used in real-world scenarios.

- [What you will learn](https://dlthub.com/docs/tutorial/load-data-from-an-api#what-you-will-learn)
- [Prerequisites](https://dlthub.com/docs/tutorial/load-data-from-an-api#prerequisites)
- [Installing dlt](https://dlthub.com/docs/tutorial/load-data-from-an-api#installing-dlt)
- [Quick start](https://dlthub.com/docs/tutorial/load-data-from-an-api#quick-start)
  - [Explore data in Python](https://dlthub.com/docs/tutorial/load-data-from-an-api#explore-data-in-python)
  - [Explore data in Streamlit](https://dlthub.com/docs/tutorial/load-data-from-an-api#explore-data-in-streamlit)
- [Create a pipeline](https://dlthub.com/docs/tutorial/load-data-from-an-api#create-a-pipeline)
- [Run the pipeline](https://dlthub.com/docs/tutorial/load-data-from-an-api#run-the-pipeline)
- [Append or replace your data](https://dlthub.com/docs/tutorial/load-data-from-an-api#append-or-replace-your-data)
- [Declare loading behavior](https://dlthub.com/docs/tutorial/load-data-from-an-api#declare-loading-behavior)
  - [Load only new data (incremental loading)](https://dlthub.com/docs/tutorial/load-data-from-an-api#load-only-new-data-incremental-loading)
  - [Update and deduplicate your data](https://dlthub.com/docs/tutorial/load-data-from-an-api#update-and-deduplicate-your-data)
- [Using pagination helper](https://dlthub.com/docs/tutorial/load-data-from-an-api#using-pagination-helper)
- [Use source decorator](https://dlthub.com/docs/tutorial/load-data-from-an-api#use-source-decorator)
  - [Dynamic resources](https://dlthub.com/docs/tutorial/load-data-from-an-api#dynamic-resources)
- [Handle secrets](https://dlthub.com/docs/tutorial/load-data-from-an-api#handle-secrets)
- [Configurable sources](https://dlthub.com/docs/tutorial/load-data-from-an-api#configurable-sources)
- [What’s next](https://dlthub.com/docs/tutorial/load-data-from-an-api#whats-next)