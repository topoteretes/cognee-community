----- https://dlthub.com/docs/intro -----

Version: 1.11.0 (latest)

On this page

![dlt pacman](https://dlthub.com/docs/assets/images/dlt-pacman-b67f01290996fde3a5a12edbde2186bc.gif)

## What is dlt? [​](https://dlthub.com/docs/intro\#what-is-dlt "Direct link to What is dlt?")

dlt is an open-source Python library that loads data from various, often messy data sources into well-structured, live datasets. It offers a lightweight interface for extracting data from [REST APIs](https://dlthub.com/docs/tutorial/rest-api), [SQL databases](https://dlthub.com/docs/tutorial/sql-database), [cloud storage](https://dlthub.com/docs/tutorial/filesystem), [Python data structures](https://dlthub.com/docs/tutorial/load-data-from-an-api), and [many more](https://dlthub.com/docs/dlt-ecosystem/verified-sources).

dlt is designed to be easy to use, flexible, and scalable:

- dlt infers [schemas](https://dlthub.com/docs/general-usage/schema) and [data types](https://dlthub.com/docs/general-usage/schema/#data-types), [normalizes the data](https://dlthub.com/docs/general-usage/schema/#data-normalizer), and handles nested data structures.
- dlt supports a variety of [popular destinations](https://dlthub.com/docs/dlt-ecosystem/destinations/) and has an interface to add [custom destinations](https://dlthub.com/docs/dlt-ecosystem/destinations/destination) to create reverse ETL pipelines.
- dlt can be deployed anywhere Python runs, be it on [Airflow](https://dlthub.com/docs/walkthroughs/deploy-a-pipeline/deploy-with-airflow-composer), [serverless functions](https://dlthub.com/docs/walkthroughs/deploy-a-pipeline/deploy-with-google-cloud-functions), or any other cloud deployment of your choice.
- dlt automates pipeline maintenance with [incremental loading](https://dlthub.com/docs/general-usage/incremental-loading), [schema evolution](https://dlthub.com/docs/general-usage/schema-evolution), and [schema and data contracts](https://dlthub.com/docs/general-usage/schema-contracts).

To get started with dlt, install the library using pip:

```codeBlockLines_RjmQ
pip install dlt

```

tip

We recommend using a clean virtual environment for your experiments! Read the [detailed instructions](https://dlthub.com/docs/reference/installation) on how to set up one.

## Load data with dlt from … [​](https://dlthub.com/docs/intro\#load-data-with-dlt-from- "Direct link to Load data with dlt from …")

- REST APIs
- SQL databases
- Cloud storages or files
- Python data structures

Use dlt's [REST API source](https://dlthub.com/docs/tutorial/rest-api) to extract data from any REST API. Define the API endpoints you'd like to fetch data from, the pagination method, and authentication, and dlt will handle the rest:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.rest_api import rest_api_source

source = rest_api_source({
    "client": {
        "base_url": "https://api.example.com/",
        "auth": {
            "token": dlt.secrets["your_api_token"],
        },
        "paginator": {
            "type": "json_link",
            "next_url_path": "paging.next",
        },
    },
    "resources": ["posts", "comments"],
})

pipeline = dlt.pipeline(
    pipeline_name="rest_api_example",
    destination="duckdb",
    dataset_name="rest_api_data",
)

load_info = pipeline.run(source)

# print load info and posts table as dataframe
print(load_info)
print(pipeline.dataset().posts.df())

```

Follow the [REST API source tutorial](https://dlthub.com/docs/tutorial/rest-api) to learn more about the source configuration and pagination methods.

tip

If you'd like to try out dlt without installing it on your machine, check out the [Google Colab demo](https://colab.research.google.com/drive/1NfSB1DpwbbHX9_t5vlalBTf13utwpMGx?usp=sharing).

## Join the dlt community [​](https://dlthub.com/docs/intro\#join-the-dlt-community "Direct link to Join the dlt community")

1. Give the library a ⭐ and check out the code on [GitHub](https://github.com/dlt-hub/dlt).
2. Ask questions and share how you use the library on [Slack](https://dlthub.com/community).
3. Report problems and make feature requests [here](https://github.com/dlt-hub/dlt/issues/new/choose).

- [What is dlt?](https://dlthub.com/docs/intro#what-is-dlt)
- [Load data with dlt from …](https://dlthub.com/docs/intro#load-data-with-dlt-from-)
- [Join the dlt community](https://dlthub.com/docs/intro#join-the-dlt-community)

----- https://dlthub.com/docs/tutorial/rest-api -----

Version: 1.11.0 (latest)

On this page

This tutorial demonstrates how to extract data from a REST API using dlt's REST API source and load it into a destination. You will learn how to build a data pipeline that loads data from the [Pokemon](https://pokeapi.co/) and the [GitHub API](https://docs.github.com/en/) into a local DuckDB database.

Extracting data from an API is straightforward with dlt: provide the base URL, define the resources you want to fetch, and dlt will handle the pagination, authentication, and data loading.

## What you will learn [​](https://dlthub.com/docs/tutorial/rest-api\#what-you-will-learn "Direct link to What you will learn")

- How to set up a REST API source
- Configuration basics for API endpoints
- Configuring the destination database
- Relationships between different resources
- How to append, replace, and merge data in the destination
- Loading data incrementally by fetching only new or updated data

## Prerequisites [​](https://dlthub.com/docs/tutorial/rest-api\#prerequisites "Direct link to Prerequisites")

- Python 3.9 or higher installed
- Virtual environment set up

## Installing dlt [​](https://dlthub.com/docs/tutorial/rest-api\#installing-dlt "Direct link to Installing dlt")

Before we start, make sure you have a Python virtual environment set up. Follow the instructions in the [installation guide](https://dlthub.com/docs/reference/installation) to create a new virtual environment and install dlt.

Verify that dlt is installed by running the following command in your terminal:

```codeBlockLines_RjmQ
dlt --version

```

If you see the version number (such as "dlt 0.5.3"), you're ready to proceed.

## Setting up a new project [​](https://dlthub.com/docs/tutorial/rest-api\#setting-up-a-new-project "Direct link to Setting up a new project")

Initialize a new dlt project with a REST API source and DuckDB destination:

```codeBlockLines_RjmQ
dlt init rest_api duckdb

```

`dlt init` creates multiple files and a directory for your project. Let's take a look at the project structure:

```codeBlockLines_RjmQ
rest_api_pipeline.py
requirements.txt
.dlt/
    config.toml
    secrets.toml

```

Here's what each file and directory contains:

- `rest_api_pipeline.py`: This is the main script where you'll define your data pipeline. It contains two basic pipeline examples for Pokemon and GitHub APIs. You can modify or rename this file as needed.
- `requirements.txt`: This file lists all the Python dependencies required for your project.
- `.dlt/`: This directory contains the [configuration files](https://dlthub.com/docs/general-usage/credentials/) for your project:
  - `secrets.toml`: This file stores your API keys, tokens, and other sensitive information.
  - `config.toml`: This file contains the configuration settings for your dlt project.

## Installing dependencies [​](https://dlthub.com/docs/tutorial/rest-api\#installing-dependencies "Direct link to Installing dependencies")

Before we proceed, let's install the required dependencies for this tutorial. Run the following command to install the dependencies listed in the `requirements.txt` file:

```codeBlockLines_RjmQ
pip install -r requirements.txt

```

## Running the pipeline [​](https://dlthub.com/docs/tutorial/rest-api\#running-the-pipeline "Direct link to Running the pipeline")

Let's verify that the pipeline is working as expected. Run the following command to execute the pipeline:

```codeBlockLines_RjmQ
python rest_api_pipeline.py

```

You should see the output of the pipeline execution in the terminal. The output will also display the location of the DuckDB database file where the data is stored:

```codeBlockLines_RjmQ
Pipeline rest_api_pokemon load step completed in 1.08 seconds
1 load package(s) were loaded to destination duckdb and into dataset rest_api_data
The duckdb destination used duckdb:////home/user-name/quick_start/rest_api_pokemon.duckdb location to store data
Load package 1692364844.9254808 is LOADED and contains no failed jobs

```

## Exploring the data [​](https://dlthub.com/docs/tutorial/rest-api\#exploring-the-data "Direct link to Exploring the data")

Now that the pipeline has run successfully, let's explore the data loaded into DuckDB. dlt comes with a built-in browser application that allows you to interact with the data. To enable it, run the following command:

```codeBlockLines_RjmQ
pip install streamlit

```

Next, run the following command to start the data browser:

```codeBlockLines_RjmQ
dlt pipeline rest_api_pokemon show

```

The command opens a new browser window with the data browser application. `rest_api_pokemon` is the name of the pipeline defined in the `rest_api_pipeline.py` file.
You can explore the loaded data, run queries, and see some pipeline execution details:

![Explore rest_api data in Streamlit App](https://dlt-static.s3.eu-central-1.amazonaws.com/images/docs-rest-api-tutorial-streamlit-screenshot.png)

## Configuring the REST API source [​](https://dlthub.com/docs/tutorial/rest-api\#configuring-the-rest-api-source "Direct link to Configuring the REST API source")

Now that your environment and the project are set up, let's take a closer look at the configuration of the REST API source. Open the `rest_api_pipeline.py` file in your code editor and locate the following code snippet:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.rest_api import rest_api_source

def load_pokemon() -> None:
    pipeline = dlt.pipeline(
        pipeline_name="rest_api_pokemon",
        destination="duckdb",
        dataset_name="rest_api_data",
    )

    pokemon_source = rest_api_source(
        {
            "client": {
                "base_url": "https://pokeapi.co/api/v2/"
            },
            "resource_defaults": {
                "endpoint": {
                    "params": {
                        "limit": 1000,
                    },
                },
            },
            "resources": [\
                "pokemon",\
                "berry",\
                "location",\
            ],
        }
    )

    ...

    load_info = pipeline.run(pokemon_source)
    print(load_info)

```

Here's what's happening in the code:

1. With `dlt.pipeline()`, we define a new pipeline named `rest_api_pokemon` with DuckDB as the destination and `rest_api_data` as the dataset name.
2. The `rest_api_source()` function creates a new REST API source object.
3. We pass this source object to the `pipeline.run()` method to start the pipeline execution. Inside the `run()` method, dlt will fetch data from the API and load it into the DuckDB database.
4. The `print(load_info)` outputs the pipeline execution details to the console.

Let's break down the configuration of the REST API source. It consists of three main parts: `client`, `resource_defaults`, and `resources`.

```codeBlockLines_RjmQ
config: RESTAPIConfig = {
    "client": {
        # ...
    },
    "resource_defaults": {
        # ...
    },
    "resources": [\
        # ...\
    ],
}

```

- The `client` configuration is used to connect to the web server and authenticate if necessary. For our simple example, we only need to specify the `base_url` of the API: `https://pokeapi.co/api/v2/`.
- The `resource_defaults` configuration allows you to set default parameters for all resources. Normally, you would set common parameters here, such as pagination limits. In our Pokemon API example, we set the `limit` parameter to 1000 for all resources to retrieve more data in a single request and reduce the number of HTTP API calls.
- The `resources` list contains the names of the resources you want to load from the API. REST API will use some conventions to determine the endpoint URL based on the resource name. For example, the resource name `pokemon` will be translated to the endpoint URL `https://pokeapi.co/api/v2/pokemon`.

note

### Pagination [​](https://dlthub.com/docs/tutorial/rest-api\#pagination "Direct link to Pagination")

You may have noticed that we didn't specify any pagination configuration in the `rest_api_source()` function. That's because for REST APIs that follow best practices, dlt can automatically detect and handle pagination. Read more about [configuring pagination](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#pagination) in the REST API source documentation.

## Appending, replacing, and merging loaded data [​](https://dlthub.com/docs/tutorial/rest-api\#appending-replacing-and-merging-loaded-data "Direct link to Appending, replacing, and merging loaded data")

Try running the pipeline again with `python rest_api_pipeline.py`. You will notice that all the tables have duplicated data. This happens because, by default, dlt appends the data to the destination table. In dlt, you can control how the data is loaded into the destination table by setting the `write_disposition` parameter in the resource configuration. The possible values are:

- `append`: Appends the data to the destination table. This is the default.
- `replace`: Replaces the data in the destination table with the new data.
- `merge`: Merges the new data with the existing data in the destination table based on the primary key.

### Replacing the data [​](https://dlthub.com/docs/tutorial/rest-api\#replacing-the-data "Direct link to Replacing the data")

In our case, we don't want to append the data every time we run the pipeline. Let's start with the simpler `replace` write disposition.

To change the write disposition to `replace`, update the `resource_defaults` configuration in the `rest_api_pipeline.py` file:

```codeBlockLines_RjmQ
...
pokemon_source = rest_api_source(
    {
        "client": {
            "base_url": "https://pokeapi.co/api/v2/",
        },
        "resource_defaults": {
            "endpoint": {
                "params": {
                    "limit": 1000,
                },
            },
            "write_disposition": "replace", # Setting the write disposition to `replace`
        },
        "resources": [\
            "pokemon",\
            "berry",\
            "location",\
        ],
    }
)
...

```

Run the pipeline again with `python rest_api_pipeline.py`. This time, the data will be replaced in the destination table instead of being appended.

### Merging the data [​](https://dlthub.com/docs/tutorial/rest-api\#merging-the-data "Direct link to Merging the data")

When you want to update the existing data as new data is loaded, you can use the `merge` write disposition. This requires specifying a primary key for the resource. The primary key is used to match the new data with the existing data in the destination table.

Let's update our example to use the `merge` write disposition. We need to specify the primary key for the `pokemon` resource and set the write disposition to `merge`:

```codeBlockLines_RjmQ
...
pokemon_source = rest_api_source(
    {
        "client": {
            "base_url": "https://pokeapi.co/api/v2/",
        },
        "resource_defaults": {
            "endpoint": {
                "params": {
                    "limit": 1000,
                },
            },
            # For the `berry` and `location` resources, we keep
            # the `replace` write disposition
            "write_disposition": "replace",
        },
        "resources": [\
            # We create a specific configuration for the `pokemon` resource\
            # using a dictionary instead of a string to configure\
            # the primary key and write disposition\
            {\
                "name": "pokemon",\
                "primary_key": "name",\
                "write_disposition": "merge",\
            },\
            # The `berry` and `location` resources will use the default\
            "berry",\
            "location",\
        ],
    }
)

```

Run the pipeline with `python rest_api_pipeline.py`, the data for the `pokemon` resource will be merged with the existing data in the destination table based on the `name` field.

## Loading data incrementally [​](https://dlthub.com/docs/tutorial/rest-api\#loading-data-incrementally "Direct link to Loading data incrementally")

When working with some APIs, you may need to load data incrementally to avoid fetching the entire dataset every time and to reduce the load time. APIs that support incremental loading usually provide a way to fetch only new or changed data (most often by using a timestamp field like `updated_at`, `created_at`, or incremental IDs).

To illustrate incremental loading, let's consider the GitHub API. In the `rest_api_pipeline.py` file, you can find an example of how to load data from the GitHub API incrementally. Let's take a look at the configuration:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.rest_api import rest_api_source

pipeline = dlt.pipeline(
    pipeline_name="rest_api_github",
    destination="duckdb",
    dataset_name="rest_api_data",
)

github_source = rest_api_source({
    "client": {
        "base_url": "https://api.github.com/repos/dlt-hub/dlt/",
    },
    "resource_defaults": {
        "primary_key": "id",
        "write_disposition": "merge",
        "endpoint": {
            "params": {
                "per_page": 100,
            },
        },
    },
    "resources": [\
        {\
            "name": "issues",\
            "endpoint": {\
                "path": "issues",\
                "params": {\
                    "sort": "updated",\
                    "direction": "desc",\
                    "state": "open",\
                    "since": {\
                        "type": "incremental",\
                        "cursor_path": "updated_at",\
                        "initial_value": "2024-01-25T11:21:28Z",\
                    },\
                },\
            },\
        },\
    ],
})

load_info = pipeline.run(github_source)
print(load_info)

```

In this configuration, the `since` parameter is defined as a special incremental parameter. The `cursor_path` field specifies the JSON path to the field that will be used to fetch the updated data, and we use the `initial_value` for the initial value for the incremental parameter. This value will be used in the first request to fetch the data.

When the pipeline runs, dlt will automatically update the `since` parameter with the latest value from the response data. This way, you can fetch only the new or updated data from the API.

Read more about [incremental loading](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#incremental-loading) in the REST API source documentation.

## What's next? [​](https://dlthub.com/docs/tutorial/rest-api\#whats-next "Direct link to What's next?")

Congratulations on completing the tutorial! You've learned how to set up a REST API source in dlt and run a data pipeline to load the data into DuckDB.

Interested in learning more about dlt? Here are some suggestions:

- Learn more about the REST API source configuration in the [REST API source documentation](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/)
- Learn how to [create a custom source](https://dlthub.com/docs/tutorial/load-data-from-an-api) in the advanced tutorial.

- [What you will learn](https://dlthub.com/docs/tutorial/rest-api#what-you-will-learn)
- [Prerequisites](https://dlthub.com/docs/tutorial/rest-api#prerequisites)
- [Installing dlt](https://dlthub.com/docs/tutorial/rest-api#installing-dlt)
- [Setting up a new project](https://dlthub.com/docs/tutorial/rest-api#setting-up-a-new-project)
- [Installing dependencies](https://dlthub.com/docs/tutorial/rest-api#installing-dependencies)
- [Running the pipeline](https://dlthub.com/docs/tutorial/rest-api#running-the-pipeline)
- [Exploring the data](https://dlthub.com/docs/tutorial/rest-api#exploring-the-data)
- [Configuring the REST API source](https://dlthub.com/docs/tutorial/rest-api#configuring-the-rest-api-source)
  - [Pagination](https://dlthub.com/docs/tutorial/rest-api#pagination)
- [Appending, replacing, and merging loaded data](https://dlthub.com/docs/tutorial/rest-api#appending-replacing-and-merging-loaded-data)
  - [Replacing the data](https://dlthub.com/docs/tutorial/rest-api#replacing-the-data)
  - [Merging the data](https://dlthub.com/docs/tutorial/rest-api#merging-the-data)
- [Loading data incrementally](https://dlthub.com/docs/tutorial/rest-api#loading-data-incrementally)
- [What's next?](https://dlthub.com/docs/tutorial/rest-api#whats-next)

----- https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#pagination -----

Version: 1.11.0 (latest)

On this page

Need help deploying these sources or figuring out how to run them in your data stack?

[Join our Slack community](https://dlthub.com/community) or [Get in touch](https://dlthub.com/contact) with the dltHub Customer Success team.

This is a dlt source you can use to extract data from any REST API. It uses [declarative configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#source-configuration) to define the API endpoints, their [relationships](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#define-resource-relationships), how to handle [pagination](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#pagination), and [authentication](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#authentication).

### Quick example [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#quick-example "Direct link to Quick example")

Here's an example of how to configure the REST API source to load posts and related comments from a hypothetical blog API:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.rest_api import rest_api_source

source = rest_api_source({
    "client": {
        "base_url": "https://api.example.com/",
        "auth": {
            "token": dlt.secrets["your_api_token"],
        },
        "paginator": {
            "type": "json_link",
            "next_url_path": "paging.next",
        },
    },
    "resources": [\
        # "posts" will be used as the endpoint path, the resource name,\
        # and the table name in the destination. The HTTP client will send\
        # a request to "https://api.example.com/posts".\
        "posts",\
\
        # The explicit configuration allows you to link resources\
        # and define query string parameters.\
        {\
            "name": "comments",\
            "endpoint": {\
                "path": "posts/{resources.posts.id}/comments",\
                "params": {\
                    "sort": "created_at",\
                },\
            },\
        },\
    ],
})

pipeline = dlt.pipeline(
    pipeline_name="rest_api_example",
    destination="duckdb",
    dataset_name="rest_api_data",
)

load_info = pipeline.run(source)

```

Running this pipeline will create two tables in DuckDB: `posts` and `comments` with the data from the respective API endpoints. The `comments` resource will fetch comments for each post by using the `id` field from the `posts` resource.

## Setup [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#setup "Direct link to Setup")

### Prerequisites [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#prerequisites "Direct link to Prerequisites")

Please make sure the `dlt` library is installed. Refer to the [installation guide](https://dlthub.com/docs/intro).

### Initialize the REST API source [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#initialize-the-rest-api-source "Direct link to Initialize the REST API source")

Enter the following command in your terminal:

```codeBlockLines_RjmQ
dlt init rest_api duckdb

```

[dlt init](https://dlthub.com/docs/reference/command-line-interface) will initialize the pipeline examples for REST API as the [source](https://dlthub.com/docs/general-usage/source) and [duckdb](https://dlthub.com/docs/dlt-ecosystem/destinations/duckdb) as the [destination](https://dlthub.com/docs/dlt-ecosystem/destinations).

Running `dlt init` creates the following in the current folder:

- `rest_api_pipeline.py` file with a sample pipelines definition:
  - GitHub API example
  - Pokemon API example
- `.dlt` folder with:
  - `secrets.toml` file to store your access tokens and other sensitive information
  - `config.toml` file to store the configuration settings
- `requirements.txt` file with the required dependencies

Change the REST API source to your needs by modifying the `rest_api_pipeline.py` file. See the detailed [source configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#source-configuration) section below.

note

For the rest of the guide, we will use the [GitHub API](https://docs.github.com/en/rest?apiVersion=2022-11-28) and [Pokemon API](https://pokeapi.co/) as example sources.

This source is based on the [RESTClient class](https://dlthub.com/docs/general-usage/http/rest-client).

### Add credentials [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#add-credentials "Direct link to Add credentials")

In the `.dlt` folder, you'll find a file called `secrets.toml`, where you can securely store your access tokens and other sensitive information. It's important to handle this file with care and keep it safe.

The GitHub API [requires an access token](https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api?apiVersion=2022-11-28) to access some of its endpoints and to increase the rate limit for the API calls. To get a GitHub token, follow the GitHub documentation on [managing your personal access tokens](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens).

After you get the token, add it to the `secrets.toml` file:

```codeBlockLines_RjmQ
[sources.rest_api_pipeline.github_source]
github_token = "your_github_token"

```

## Run the pipeline [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#run-the-pipeline "Direct link to Run the pipeline")

1. Install the required dependencies by running the following command:

```codeBlockLines_RjmQ
pip install -r requirements.txt

```

2. Run the pipeline:

```codeBlockLines_RjmQ
python rest_api_pipeline.py

```

3. Verify that everything loaded correctly by using the following command:

```codeBlockLines_RjmQ
dlt pipeline rest_api show

```

## Source configuration [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#source-configuration "Direct link to Source configuration")

### Quick example [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#quick-example-1 "Direct link to Quick example")

Let's take a look at the GitHub example in the `rest_api_pipeline.py` file:

```codeBlockLines_RjmQ
from dlt.sources.rest_api import RESTAPIConfig, rest_api_resources

@dlt.source
def github_source(github_token=dlt.secrets.value):
    config: RESTAPIConfig = {
        "client": {
            "base_url": "https://api.github.com/repos/dlt-hub/dlt/",
            "auth": {
                "token": github_token,
            },
        },
        "resource_defaults": {
            "primary_key": "id",
            "write_disposition": "merge",
            "endpoint": {
                "params": {
                    "per_page": 100,
                },
            },
        },
        "resources": [\
            {\
                "name": "issues",\
                "endpoint": {\
                    "path": "issues",\
                    "params": {\
                        "sort": "updated",\
                        "direction": "desc",\
                        "state": "open",\
                        "since": {\
                            "type": "incremental",\
                            "cursor_path": "updated_at",\
                            "initial_value": "2024-01-25T11:21:28Z",\
                        },\
                    },\
                },\
            },\
            {\
                "name": "issue_comments",\
                "endpoint": {\
                    "path": "issues/{resources.issues.number}/comments",\
                },\
                "include_from_parent": ["id"],\
            },\
        ],
    }

    yield from rest_api_resources(config)

def load_github() -> None:
    pipeline = dlt.pipeline(
        pipeline_name="rest_api_github",
        destination="duckdb",
        dataset_name="rest_api_data",
    )

    load_info = pipeline.run(github_source())
    print(load_info)

```

The declarative resource configuration is defined in the `config` dictionary. It contains the following key components:

1. `client`: Defines the base URL and authentication method for the API. In this case, it uses token-based authentication. The token is stored in the `secrets.toml` file.

2. `resource_defaults`: Contains default settings for all [resources](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#resource-configuration). In this example, we define that all resources:
   - Have `id` as the [primary key](https://dlthub.com/docs/general-usage/resource#define-schema)
   - Use the `merge` [write disposition](https://dlthub.com/docs/general-usage/incremental-loading#choosing-a-write-disposition) to merge the data with the existing data in the destination.
   - Send a `per_page=100` query parameter with each request to get more results per page.
3. `resources`: A list of [resources](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#resource-configuration) to be loaded. Here, we have two resources: `issues` and `issue_comments`, which correspond to the GitHub API endpoints for [repository issues](https://docs.github.com/en/rest/issues/issues?apiVersion=2022-11-28#list-repository-issues) and [issue comments](https://docs.github.com/en/rest/issues/comments?apiVersion=2022-11-28#list-issue-comments). Note that we need an issue number to fetch comments for each issue. This number is taken from the `issues` resource. More on this in the [resource relationships](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#define-resource-relationships) section.

Let's break down the configuration in more detail.

### Configuration structure [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#configuration-structure "Direct link to Configuration structure")

tip

Import the `RESTAPIConfig` type from the `rest_api` module to have convenient hints in your editor/IDE and use it to define the configuration object.

```codeBlockLines_RjmQ
from dlt.sources.rest_api import RESTAPIConfig

```

The configuration object passed to the REST API Generic Source has three main elements:

```codeBlockLines_RjmQ
config: RESTAPIConfig = {
    "client": {
        # ...
    },
    "resource_defaults": {
        # ...
    },
    "resources": [\
        # ...\
    ],
}

```

#### `client` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#client "Direct link to client")

The `client` configuration is used to connect to the API's endpoints. It includes the following fields:

- `base_url` (str): The base URL of the API. This string is prepended to all endpoint paths. For example, if the base URL is `https://api.example.com/v1/`, and the endpoint path is `users`, the full URL will be `https://api.example.com/v1/users`.
- `headers` (dict, optional): Additional headers that are sent with each request.
- `auth` (optional): Authentication configuration. This can be a simple token, an `AuthConfigBase` object, or a more complex authentication method.
- `paginator` (optional): Configuration for the default pagination used for resources that support pagination. Refer to the [pagination](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#pagination) section for more details.

#### `resource_defaults` (optional) [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#resource_defaults-optional "Direct link to resource_defaults-optional")

`resource_defaults` contains the default values to [configure the dlt resources](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#resource-configuration). This configuration is applied to all resources unless overridden by the resource-specific configuration.

For example, you can set the primary key, write disposition, and other default settings here:

```codeBlockLines_RjmQ
config = {
    "client": {
        # ...
    },
    "resource_defaults": {
        "primary_key": "id",
        "write_disposition": "merge",
        "endpoint": {
            "params": {
                "per_page": 100,
            },
        },
    },
    "resources": [\
        "resource1",\
        {\
            "name": "resource2_name",\
            "write_disposition": "append",\
            "endpoint": {\
                "params": {\
                    "param1": "value1",\
                },\
            },\
        }\
    ],
}

```

Above, all resources will have `primary_key` set to `id`, `resource1` will have `write_disposition` set to `merge`, and `resource2` will override the default `write_disposition` with `append`.
Both `resource1` and `resource2` will have the `per_page` parameter set to 100.

#### `resources` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#resources "Direct link to resources")

This is a list of resource configurations that define the API endpoints to be loaded. Each resource configuration can be:

- a dictionary with the [resource configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#resource-configuration).
- a string. In this case, the string is used as both the endpoint path and the resource name, and the resource configuration is taken from the `resource_defaults` configuration if it exists.

### Resource configuration [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#resource-configuration "Direct link to Resource configuration")

A resource configuration is used to define a [dlt resource](https://dlthub.com/docs/general-usage/resource) for the data to be loaded from an API endpoint. It contains the following key fields:

- `endpoint`: The endpoint configuration for the resource. It can be a string or a dict representing the endpoint settings. See the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) section for more details.
- `write_disposition`: The write disposition for the resource.
- `primary_key`: The primary key for the resource.
- `include_from_parent`: A list of fields from the parent resource to be included in the resource output. See the [resource relationships](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#include-fields-from-the-parent-resource) section for more details.
- `processing_steps`: A list of [processing steps](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#processing-steps-filter-and-transform-data) to filter and transform your data.
- `selected`: A flag to indicate if the resource is selected for loading. This could be useful when you want to load data only from child resources and not from the parent resource.
- `auth`: An optional `AuthConfig` instance. If passed, is used over the one defined in the [client](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#client) definition. Example:

```codeBlockLines_RjmQ
from dlt.sources.helpers.rest_client.auth import HttpBasicAuth

config = {
    "client": {
        "auth": {
            "type": "bearer",
            "token": dlt.secrets["your_api_token"],
        }
    },
    "resources": [\
        "resource-using-bearer-auth",\
        {\
            "name": "my-resource-with-special-auth",\
            "endpoint": {\
                # ...\
                "auth": HttpBasicAuth("user", dlt.secrets["your_basic_auth_password"])\
            },\
            # ...\
        }\
    ]
    # ...
}

```

This would use `Bearer` auth as defined in the `client` for `resource-using-bearer-auth` and `Http Basic` auth for `my-resource-with-special-auth`.

You can also pass additional resource parameters that will be used to configure the dlt resource. See [dlt resource API reference](https://dlthub.com/docs/api_reference/dlt/extract/decorators#resource) for more details.

### Endpoint configuration [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#endpoint-configuration "Direct link to Endpoint configuration")

The endpoint configuration defines how to query the API endpoint. Quick example:

```codeBlockLines_RjmQ
{
    "path": "issues",
    "method": "GET",
    "params": {
        "sort": "updated",
        "direction": "desc",
        "state": "open",
        "since": {
            "type": "incremental",
            "cursor_path": "updated_at",
            "initial_value": "2024-01-25T11:21:28Z",
        },
    },
    "data_selector": "results",
}

```

The fields in the endpoint configuration are:

- `path`: The path to the API endpoint. By default this path is appended to the given `base_url`. If this is a fully qualified URL starting with `http:` or `https:` it will be
used as-is and `base_url` will be ignored.
- `method`: The HTTP method to be used. The default is `GET`.
- `params`: Query parameters to be sent with each request. For example, `sort` to order the results or `since` to specify [incremental loading](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#incremental-loading). This is also may be used to define [resource relationships](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#define-resource-relationships).
- `json`: The JSON payload to be sent with the request (for POST and PUT requests).
- `paginator`: Pagination configuration for the endpoint. See the [pagination](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#pagination) section for more details.
- `data_selector`: A JSONPath to select the data from the response. See the [data selection](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#data-selection) section for more details.
- `response_actions`: A list of actions that define how to process the response data. See the [response actions](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/advanced#response-actions) section for more details.
- `incremental`: Configuration for [incremental loading](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#incremental-loading).

### Pagination [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#pagination "Direct link to Pagination")

The REST API source will try to automatically handle pagination for you. This works by detecting the pagination details from the first API response.

In some special cases, you may need to specify the pagination configuration explicitly.

To specify the pagination configuration, use the `paginator` field in the [client](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#client) or [endpoint](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) configurations. You may either use a dictionary with a string alias in the `type` field along with the required parameters, or use a [paginator class instance](https://dlthub.com/docs/general-usage/http/rest-client#paginators).

#### Example [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#example "Direct link to Example")

Suppose the API response for `https://api.example.com/posts` contains a `next` field with the URL to the next page:

```codeBlockLines_RjmQ
{
    "data": [\
        {"id": 1, "title": "Post 1"},\
        {"id": 2, "title": "Post 2"},\
        {"id": 3, "title": "Post 3"}\
    ],
    "pagination": {
        "next": "https://api.example.com/posts?page=2"
    }
}

```

You can configure the pagination for the `posts` resource like this:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "paginator": {
        "type": "json_link",
        "next_url_path": "pagination.next",
    }
}

```

Alternatively, you can use the paginator instance directly:

```codeBlockLines_RjmQ
from dlt.sources.helpers.rest_client.paginators import JSONLinkPaginator

# ...

{
    "path": "posts",
    "paginator": JSONLinkPaginator(
        next_url_path="pagination.next"
    ),
}

```

note

Currently, pagination is supported only for GET requests. To handle POST requests with pagination, you need to implement a [custom paginator](https://dlthub.com/docs/general-usage/http/rest-client#implementing-a-custom-paginator).

These are the available paginators:

| `type` | Paginator class | Description |
| --- | --- | --- |
| `json_link` | [JSONLinkPaginator](https://dlthub.com/docs/general-usage/http/rest-client#jsonlinkpaginator) | The link to the next page is in the body (JSON) of the response.<br>_Parameters:_ <br>- `next_url_path` (str) - the JSONPath to the next page URL |
| `header_link` | [HeaderLinkPaginator](https://dlthub.com/docs/general-usage/http/rest-client#headerlinkpaginator) | The links to the next page are in the response headers.<br>_Parameters:_ <br>- `links_next_key` (str) - the name of the header containing the links. Default is "next". |
| `offset` | [OffsetPaginator](https://dlthub.com/docs/general-usage/http/rest-client#offsetpaginator) | The pagination is based on an offset parameter, with the total items count either in the response body or explicitly provided.<br>_Parameters:_ <br>- `limit` (int) - the maximum number of items to retrieve in each request<br>- `offset` (int) - the initial offset for the first request. Defaults to `0`<br>- `offset_param` (str) - the name of the query parameter used to specify the offset. Defaults to "offset"<br>- `limit_param` (str) - the name of the query parameter used to specify the limit. Defaults to "limit"<br>- `total_path` (str) - a JSONPath expression for the total number of items. If not provided, pagination is controlled by `maximum_offset` and `stop_after_empty_page`<br>- `maximum_offset` (int) - optional maximum offset value. Limits pagination even without total count<br>- `stop_after_empty_page` (bool) - Whether pagination should stop when a page contains no result items. Defaults to `True` |
| `page_number` | [PageNumberPaginator](https://dlthub.com/docs/general-usage/http/rest-client#pagenumberpaginator) | The pagination is based on a page number parameter, with the total pages count either in the response body or explicitly provided.<br>_Parameters:_ <br>- `base_page` (int) - the starting page number. Defaults to `0`<br>- `page_param` (str) - the query parameter name for the page number. Defaults to "page"<br>- `total_path` (str) - a JSONPath expression for the total number of pages. If not provided, pagination is controlled by `maximum_page` and `stop_after_empty_page`<br>- `maximum_page` (int) - optional maximum page number. Stops pagination once this page is reached<br>- `stop_after_empty_page` (bool) - Whether pagination should stop when a page contains no result items. Defaults to `True` |
| `cursor` | [JSONResponseCursorPaginator](https://dlthub.com/docs/general-usage/http/rest-client#jsonresponsecursorpaginator) | The pagination is based on a cursor parameter, with the value of the cursor in the response body (JSON).<br>_Parameters:_ <br>- `cursor_path` (str) - the JSONPath to the cursor value. Defaults to "cursors.next"<br>- `cursor_param` (str) - the query parameter name for the cursor. Defaults to "cursor" if neither `cursor_param` nor `cursor_body_path` is provided.<br>- `cursor_body_path` (str, optional) - the JSONPath to place the cursor in the request body.<br>Note: You must provide either `cursor_param` or `cursor_body_path`, but not both. If neither is provided, `cursor_param` will default to "cursor". |
| `single_page` | SinglePagePaginator | The response will be interpreted as a single-page response, ignoring possible pagination metadata. |
| `auto` | `None` | Explicitly specify that the source should automatically detect the pagination method. |

For more complex pagination methods, you can implement a [custom paginator](https://dlthub.com/docs/general-usage/http/rest-client#implementing-a-custom-paginator), instantiate it, and use it in the configuration.

Alternatively, you can use the dictionary configuration syntax also for custom paginators. For this, you need to register your custom paginator:

```codeBlockLines_RjmQ
from dlt.sources.rest_api.config_setup import register_paginator

class CustomPaginator(SinglePagePaginator):
    # custom implementation of SinglePagePaginator
    pass

register_paginator("custom_paginator", CustomPaginator)

{
    # ...
    "paginator": {
        "type": "custom_paginator",
        "next_url_path": "paging.nextLink",
    }
}

```

### Data selection [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#data-selection "Direct link to Data selection")

The `data_selector` field in the endpoint configuration allows you to specify a JSONPath to select the data from the response. By default, the source will try to detect the locations of the data automatically.

Use this field when you need to specify the location of the data in the response explicitly.

For example, if the API response looks like this:

```codeBlockLines_RjmQ
{
    "posts": [\
        {"id": 1, "title": "Post 1"},\
        {"id": 2, "title": "Post 2"},\
        {"id": 3, "title": "Post 3"}\
    ]
}

```

You can use the following endpoint configuration:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "posts",
}

```

For a nested structure like this:

```codeBlockLines_RjmQ
{
    "results": {
        "posts": [\
            {"id": 1, "title": "Post 1"},\
            {"id": 2, "title": "Post 2"},\
            {"id": 3, "title": "Post 3"}\
        ]
    }
}

```

You can use the following endpoint configuration:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "results.posts",
}

```

Read more about [JSONPath syntax](https://github.com/h2non/jsonpath-ng?tab=readme-ov-file#jsonpath-syntax) to learn how to write selectors.

### Authentication [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#authentication "Direct link to Authentication")

For APIs that require authentication to access their endpoints, the REST API source supports various authentication methods, including token-based authentication, query parameters, basic authentication, and custom authentication. The authentication configuration is specified in the `auth` field of the [client](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#client) either as a dictionary or as an instance of the [authentication class](https://dlthub.com/docs/general-usage/http/rest-client#authentication).

#### Quick example [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#quick-example-2 "Direct link to Quick example")

Here's how to configure authentication using a bearer token:

```codeBlockLines_RjmQ
{
    "client": {
        # ...
        "auth": {
            "type": "bearer",
            "token": dlt.secrets["your_api_token"],
        },
        # ...
    },
}

```

Alternatively, you can use the authentication class directly:

```codeBlockLines_RjmQ
from dlt.sources.helpers.rest_client.auth import BearerTokenAuth

config = {
    "client": {
        "auth": BearerTokenAuth(dlt.secrets["your_api_token"]),
    },
    "resources": [\
    ]
    # ...
}

```

Since token-based authentication is one of the most common methods, you can use the following shortcut:

```codeBlockLines_RjmQ
{
    "client": {
        # ...
        "auth": {
            "token": dlt.secrets["your_api_token"],
        },
        # ...
    },
}

```

warning

Make sure to store your access tokens and other sensitive information in the `secrets.toml` file and never commit it to the version control system.

Available authentication types:

| `type` | Authentication class | Description |
| --- | --- | --- |
| `bearer` | [BearerTokenAuth](https://dlthub.com/docs/general-usage/http/rest-client#bearer-token-authentication) | Bearer token authentication.<br>Parameters:<br>- `token` (str) |
| `http_basic` | [HTTPBasicAuth](https://dlthub.com/docs/general-usage/http/rest-client#http-basic-authentication) | Basic HTTP authentication.<br>Parameters:<br>- `username` (str)<br>- `password` (str) |
| `api_key` | [APIKeyAuth](https://dlthub.com/docs/general-usage/http/rest-client#api-key-authentication) | API key authentication with key defined in the query parameters or in the headers. <br>Parameters:<br>- `name` (str) - the name of the query parameter or header<br>- `api_key` (str) - the API key value<br>- `location` (str, optional) - the location of the API key in the request. Can be `query` or `header`. Default is `header` |
| `oauth2_client_credentials` | [OAuth2ClientCredentials](https://dlthub.com/docs/general-usage/http/rest-client#oauth-20-authorization) | OAuth 2.0 Client Credentials authorization for server-to-server communication without user consent. <br>Parameters:<br>- `access_token` (str, optional) - the temporary token. Usually not provided here because it is automatically obtained from the server by exchanging `client_id` and `client_secret`. Default is `None`<br>- `access_token_url` (str) - the URL to request the `access_token` from<br>- `client_id` (str) - identifier for your app. Usually issued via a developer portal<br>- `client_secret` (str) - client credential to obtain authorization. Usually issued via a developer portal<br>- `access_token_request_data` (dict, optional) - A dictionary with data required by the authorization server apart from the `client_id`, `client_secret`, and `"grant_type": "client_credentials"`. Defaults to `None`<br>- `default_token_expiration` (int, optional) - The time in seconds after which the temporary access token expires. Defaults to 3600.<br>- `session` (requests.Session, optional) - a custom session object. Mostly used for testing |

For more complex authentication methods, you can implement a [custom authentication class](https://dlthub.com/docs/general-usage/http/rest-client#implementing-custom-authentication) and use it in the configuration.

You can use the dictionary configuration syntax also for custom authentication classes after registering them as follows:

```codeBlockLines_RjmQ
from dlt.sources.rest_api.config_setup import register_auth

class CustomAuth(AuthConfigBase):
    pass

register_auth("custom_auth", CustomAuth)

{
    # ...
    "auth": {
        "type": "custom_auth",
        "api_key": dlt.secrets["sources.my_source.my_api_key"],
    }
}

```

### Define resource relationships [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#define-resource-relationships "Direct link to Define resource relationships")

When you have a resource that depends on another resource (for example, you must fetch a parent resource to get an ID needed to fetch the child), you can reference fields in the parent resource using special placeholders.
This allows you to link one or more [path](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#via-request-path), [query string](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#via-query-string-parameters) or [JSON body](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#via-json-body) parameters in the child resource to fields in the parent resource's data.

#### Via request path [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#via-request-path "Direct link to Via request path")

In the GitHub example, the `issue_comments` resource depends on the `issues` resource. The `resources.issues.number` placeholder links the `number` field in the `issues` resource data to the current request's path parameter.

```codeBlockLines_RjmQ
{
    "resources": [\
        {\
            "name": "issues",\
            "endpoint": {\
                "path": "issues",\
                # ...\
            },\
        },\
        {\
            "name": "issue_comments",\
            "endpoint": {\
                "path": "issues/{resources.issues.number}/comments",\
            },\
            "include_from_parent": ["id"],\
        },\
    ],
}

```

This configuration tells the source to get issue numbers from the `issues` resource data and use them to fetch comments for each issue number. So for each issue item, `"{resources.issues.number}"` is replaced by the issue number in the request path.
For example, if the `issues` resource yields the following data:

```codeBlockLines_RjmQ
[\
    {"id": 1, "number": 123},\
    {"id": 2, "number": 124},\
    {"id": 3, "number": 125}\
]

```

The `issue_comments` resource will make requests to the following endpoints:

- `issues/123/comments`
- `issues/124/comments`
- `issues/125/comments`

The syntax for the placeholder is `resources.<parent_resource_name>.<field_name>`.

#### Via query string parameters [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#via-query-string-parameters "Direct link to Via query string parameters")

The placeholder syntax can also be used in the query string parameters. For example, in an API which lets you fetch a blog posts (via `/posts`) and their comments (via `/comments?post_id=<post_id>`), you can define a resource `posts` and a resource `post_comments` which depends on the `posts` resource. You can then reference the `id` field from the `posts` resource in the `post_comments` resource:

```codeBlockLines_RjmQ
{
    "resources": [\
        "posts",\
        {\
            "name": "post_comments",\
            "endpoint": {\
                "path": "comments",\
                "params": {\
                    "post_id": "{resources.posts.id}",\
                },\
            },\
        },\
    ],
}

```

Similar to the GitHub example above, if the `posts` resource yields the following data:

```codeBlockLines_RjmQ
[\
    {"id": 1, "title": "Post 1"},\
    {"id": 2, "title": "Post 2"},\
    {"id": 3, "title": "Post 3"}\
]

```

The `post_comments` resource will make requests to the following endpoints:

- `comments?post_id=1`
- `comments?post_id=2`
- `comments?post_id=3`

#### Via JSON body [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#via-json-body "Direct link to Via JSON body")

In many APIs, you can send a complex query or configuration through a POST request's JSON body rather than in the request path or query parameters. For example, consider an imaginary `/search` endpoint that supports multiple filters and settings. You might have a parent resource `posts` with each post's `id` and a second resource, `post_details`, that uses `id` to perform a custom search.

In the example below we reference the `posts` resource's `id` field in the JSON body via placeholders:

```codeBlockLines_RjmQ
{
    "resources": [\
        "posts",\
        {\
            "name": "post_details",\
            "endpoint": {\
                "path": "search",\
                "method": "POST",\
                "json": {\
                    "filters": {\
                        "id": "{resources.posts.id}",\
                    },\
                    "order": "desc",\
                    "limit": 5,\
                }\
            },\
        },\
    ],
}

```

#### Legacy syntax: `resolve` field in parameter configuration [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#legacy-syntax-resolve-field-in-parameter-configuration "Direct link to legacy-syntax-resolve-field-in-parameter-configuration")

warning

`resolve` works only for path parameters. The new placeholder syntax is more flexible and recommended for new configurations.

An alternative, legacy way to define resource relationships is to use the `resolve` field in the parameter configuration.
Here's the same example as above that uses the `resolve` field:

```codeBlockLines_RjmQ
{
    "resources": [\
        {\
            "name": "issues",\
            "endpoint": {\
                "path": "issues",\
                # ...\
            },\
        },\
        {\
            "name": "issue_comments",\
            "endpoint": {\
                "path": "issues/{issue_number}/comments",\
                "params": {\
                    "issue_number": {\
                        "type": "resolve",\
                        "resource": "issues",\
                        "field": "number",\
                    }\
                },\
            },\
            "include_from_parent": ["id"],\
        },\
    ],
}

```

The syntax for the `resolve` field in parameter configuration is:

```codeBlockLines_RjmQ
{
    "<parameter_name>": {
        "type": "resolve",
        "resource": "<parent_resource_name>",
        "field": "<parent_resource_field_name_or_jsonpath>",
    }
}

```

The `field` value can be specified as a [JSONPath](https://github.com/h2non/jsonpath-ng?tab=readme-ov-file#jsonpath-syntax) to select a nested field in the parent resource data. For example: `"field": "items[0].id"`.

#### Resolving multiple path parameters from a parent resource [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#resolving-multiple-path-parameters-from-a-parent-resource "Direct link to Resolving multiple path parameters from a parent resource")

When a child resource depends on multiple fields from a single parent resource, you can define multiple `resolve` parameters in the endpoint configuration. For example:

```codeBlockLines_RjmQ
{
    "resources": [\
        "groups",\
        {\
            "name": "users",\
            "endpoint": {\
                "path": "groups/{group_id}/users",\
                "params": {\
                    "group_id": {\
                        "type": "resolve",\
                        "resource": "groups",\
                        "field": "id",\
                    },\
                },\
            },\
        },\
        {\
            "name": "user_details",\
            "endpoint": {\
                "path": "groups/{group_id}/users/{user_id}/details",\
                "params": {\
                    "group_id": {\
                        "type": "resolve",\
                        "resource": "users",\
                        "field": "group_id",\
                    },\
                    "user_id": {\
                        "type": "resolve",\
                        "resource": "users",\
                        "field": "id",\
                    },\
                },\
            },\
        },\
    ],
}

```

In the configuration above:

- The `users` resource depends on the `groups` resource, resolving the `group_id` parameter from the `id` field in `groups`.
- The `user_details` resource depends on the `users` resource, resolving both `group_id` and `user_id` parameters from fields in `users`.

#### Include fields from the parent resource [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#include-fields-from-the-parent-resource "Direct link to Include fields from the parent resource")

You can include data from the parent resource in the child resource by using the `include_from_parent` field in the resource configuration. For example:

```codeBlockLines_RjmQ
{
    "name": "issue_comments",
    "endpoint": {
        ...
    },
    "include_from_parent": ["id", "title", "created_at"],
}

```

This will include the `id`, `title`, and `created_at` fields from the `issues` resource in the `issue_comments` resource data. The names of the included fields will be prefixed with the parent resource name and an underscore ( `_`) like so: `_issues_id`, `_issues_title`, `_issues_created_at`.

### Define a resource which is not a REST endpoint [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#define-a-resource-which-is-not-a-rest-endpoint "Direct link to Define a resource which is not a REST endpoint")

Sometimes, we want to request endpoints with specific values that are not returned by another endpoint.
Thus, you can also include arbitrary dlt resources in your `RESTAPIConfig` instead of defining a resource for every path!

In the following example, we want to load the issues belonging to three repositories.
Instead of defining three different issues resources, one for each of the paths `dlt-hub/dlt/issues/`, `dlt-hub/verified-sources/issues/`, `dlt-hub/dlthub-education/issues/`, we have a resource `repositories` which yields a list of repository names that will be fetched by the dependent resource `issues`.

```codeBlockLines_RjmQ
from dlt.sources.rest_api import RESTAPIConfig

@dlt.resource()
def repositories() -> Generator[List[Dict[str, Any]], Any, Any]:
    """A seed list of repositories to fetch"""
    yield [{"name": "dlt"}, {"name": "verified-sources"}, {"name": "dlthub-education"}]

config: RESTAPIConfig = {
    "client": {"base_url": "https://github.com/api/v2"},
    "resources": [\
        {\
            "name": "issues",\
            "endpoint": {\
                "path": "dlt-hub/{repository}/issues/",\
                "params": {\
                    "repository": {\
                        "type": "resolve",\
                        "resource": "repositories",\
                        "field": "name",\
                    },\
                },\
            },\
        },\
        repositories(),\
    ],
}

```

Be careful that the parent resource needs to return `Generator[List[Dict[str, Any]]]`. Thus, the following will NOT work:

```codeBlockLines_RjmQ
@dlt.resource
def repositories() -> Generator[Dict[str, Any], Any, Any]:
    """Not working seed list of repositories to fetch"""
    yield from [{"name": "dlt"}, {"name": "verified-sources"}, {"name": "dlthub-education"}]

```

### Processing steps: filter and transform data [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#processing-steps-filter-and-transform-data "Direct link to Processing steps: filter and transform data")

The `processing_steps` field in the resource configuration allows you to apply transformations to the data fetched from the API before it is loaded into your destination. This is useful when you need to filter out certain records, modify the data structure, or anonymize sensitive information.

Each processing step is a dictionary specifying the type of operation ( `filter` or `map`) and the function to apply. Steps apply in the order they are listed.

#### Quick example [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#quick-example-3 "Direct link to Quick example")

```codeBlockLines_RjmQ
def lower_title(record):
    record["title"] = record["title"].lower()
    return record

config: RESTAPIConfig = {
    "client": {
        "base_url": "https://api.example.com",
    },
    "resources": [\
        {\
            "name": "posts",\
            "processing_steps": [\
                {"filter": lambda x: x["id"] < 10},\
                {"map": lower_title},\
            ],\
        },\
    ],
}

```

In the example above:

- First, the `filter` step uses a lambda function to include only records where `id` is less than 10.
- Thereafter, the `map` step applies the `lower_title` function to each remaining record.

#### Using `filter` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#using-filter "Direct link to using-filter")

The `filter` step allows you to exclude records that do not meet certain criteria. The provided function should return `True` to keep the record or `False` to exclude it:

```codeBlockLines_RjmQ
{
    "name": "posts",
    "endpoint": "posts",
    "processing_steps": [\
        {"filter": lambda x: x["id"] in [10, 20, 30]},\
    ],
}

```

In this example, only records with `id` equal to 10, 20, or 30 will be included.

#### Using `map` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#using-map "Direct link to using-map")

The `map` step allows you to modify the records fetched from the API. The provided function should take a record as an argument and return the modified record. For example, to anonymize the `email` field:

```codeBlockLines_RjmQ
def anonymize_email(record):
    record["email"] = "REDACTED"
    return record

config: RESTAPIConfig = {
    "client": {
        "base_url": "https://api.example.com",
    },
    "resources": [\
        {\
            "name": "users",\
            "processing_steps": [\
                {"map": anonymize_email},\
            ],\
        },\
    ],
}

```

#### Combining `filter` and `map` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#combining-filter-and-map "Direct link to combining-filter-and-map")

You can combine multiple processing steps to achieve complex transformations:

```codeBlockLines_RjmQ
{
    "name": "posts",
    "endpoint": "posts",
    "processing_steps": [\
        {"filter": lambda x: x["id"] < 10},\
        {"map": lower_title},\
        {"filter": lambda x: "important" in x["title"]},\
    ],
}

```

tip

#### Best practices [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#best-practices "Direct link to Best practices")

1. Order matters: Processing steps are applied in the order they are listed. Be mindful of the sequence, especially when combining `map` and `filter`.
2. Function definition: Define your filter and map functions separately for clarity and reuse.
3. Use `filter` to exclude records early in the process to reduce the amount of data that needs to be processed.
4. Combine consecutive `map` steps into a single function for faster execution.

## Incremental loading [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#incremental-loading "Direct link to Incremental loading")

Some APIs provide a way to fetch only new or changed data (most often by using a timestamp field like `updated_at`, `created_at`, or incremental IDs).
This is called [incremental loading](https://dlthub.com/docs/general-usage/incremental-loading) and is very useful as it allows you to reduce the load time and the amount of data transferred.

Let's continue with our imaginary blog API example to understand incremental loading with query parameters.

Imagine we have the following endpoint `https://api.example.com/posts` and it:

1. Accepts a `created_since` query parameter to fetch blog posts created after a certain date.
2. Returns a list of posts with the `created_at` field for each post.

For example, if we query the endpoint with GET request `https://api.example.com/posts?created_since=2024-01-25`, we get the following response:

```codeBlockLines_RjmQ
{
    "results": [\
        {"id": 1, "title": "Post 1", "created_at": "2024-01-26"},\
        {"id": 2, "title": "Post 2", "created_at": "2024-01-27"},\
        {"id": 3, "title": "Post 3", "created_at": "2024-01-28"}\
    ]
}

```

When the API endpoint supports incremental loading, you can configure dlt to load only the new or changed data using these three methods:

1. Using [placeholders for incremental loading](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#using-placeholders-for-incremental-loading)
2. Defining a special parameter in the `params` section of the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) (DEPRECATED)
3. Using the `incremental` field in the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) with the `start_param` field (DEPRECATED)

caution

The last two methods are deprecated and will be removed in a future dlt version.

### Using placeholders for incremental loading [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#using-placeholders-for-incremental-loading "Direct link to Using placeholders for incremental loading")

The most flexible way to configure incremental loading is to use placeholders in the request configuration along with the `incremental` section.
Here's how it works:

1. Define the `incremental` section in the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) to specify the cursor path (where to find the incremental value in the response) and initial value (the value to start the incremental loading from).
2. Use the placeholder `{incremental.start_value}` in the request configuration to reference the incremental value.

Let's take the example from the previous section and configure it using placeholders:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "results",
    "params": {
        "created_since": "{incremental.start_value}",  # Uses cursor value in query parameter
    },
    "incremental": {
        "cursor_path": "created_at",
        "initial_value": "2024-01-25T00:00:00Z",
    },
}

```

When you first run this pipeline, dlt will:

1. Replace `{incremental.start_value}` with `2024-01-25T00:00:00Z` (the initial value)
2. Make a GET request to `https://api.example.com/posts?created_since=2024-01-25T00:00:00Z`
3. Parse the response (e.g., posts with created\_at values like "2024-01-26", "2024-01-27", "2024-01-28")
4. Track the maximum value found in the "created\_at" field (in this case, "2024-01-28")

On the next pipeline run, dlt will:

1. Replace `{incremental.start_value}` with "2024-01-28" (the last seen maximum value)
2. Make a GET request to `https://api.example.com/posts?created_since=2024-01-28`
3. The API will only return posts created on or after January 28th

Let's break down the configuration:

1. We explicitly set `data_selector` to `"results"` to select the list of posts from the response. This is optional; if not set, dlt will try to auto-detect the data location.
2. We define the `created_since` parameter in `params` section and use the placeholder `{incremental.start_value}` to reference the incremental value.

Placeholders are versatile and can be used in various request components. Here are some examples:

#### In JSON body (for POST requests) [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#in-json-body-for-post-requests "Direct link to In JSON body (for POST requests)")

If the API lets you filter the data by a range of dates (e.g. `fromDate` and `toDate`), you can use the placeholder in the JSON body:

```codeBlockLines_RjmQ
{
    "path": "posts/search",
    "method": "POST",
    "json": {
        "filters": {
            "fromDate": "{incremental.start_value}",  # In JSON body
            "toDate": "2024-03-25"
        },
        "limit": 1000
    },
    "incremental": {
        "cursor_path": "created_at",
        "initial_value": "2024-01-25T00:00:00Z",
    },
}

```

#### In path parameters [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#in-path-parameters "Direct link to In path parameters")

Some APIs use path parameters to filter the data:

```codeBlockLines_RjmQ
{
    "path": "posts/since/{incremental.start_value}/list",  # In URL path
    "incremental": {
        "cursor_path": "created_at",
        "initial_value": "2024-01-25",
    },
}

```

#### In request headers [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#in-request-headers "Direct link to In request headers")

It's not so common, but you can also use placeholders in the request headers:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "headers": {
        "X-Since-Timestamp": "{incremental.start_value}"  # In custom header
    },
    "incremental": {
        "cursor_path": "created_at",
        "initial_value": "2024-01-25T00:00:00Z",
    },
}

```

You can also use different placeholder variants depending on your needs:

| Placeholder | Description |
| --- | --- |
| `{incremental.start_value}` | The value to use as the starting point for this request (either the initial value or the last tracked maximum value) |
| `{incremental.initial_value}` | Always uses the initial value specified in the configuration |
| `{incremental.last_value}` | The last seen value (same as start\_value in most cases, see the [incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor) guide for more details) |
| `{incremental.end_value}` | The end value if specified in the configuration |

### Legacy method: Incremental loading in `params` (DEPRECATED) [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#legacy-method-incremental-loading-in-params-deprecated "Direct link to legacy-method-incremental-loading-in-params-deprecated")

caution

DEPRECATED: This method is deprecated and will be removed in a future version. Use the [placeholder method](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#using-placeholders-for-incremental-loading) instead.

note

This method only works for query string parameters. For other request parts (path, JSON body, headers), use the [placeholder method](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#using-placeholders-for-incremental-loading).

For query string parameters, you can also specify incremental loading directly in the `params` section:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "results",  # Optional JSONPath to select the list of posts
    "params": {
        "created_since": {
            "type": "incremental",
            "cursor_path": "created_at", # The JSONPath to the field we want to track in each post
            "initial_value": "2024-01-25",
        },
    },
}

```

Above we define the `created_since` parameter as an incremental parameter as:

```codeBlockLines_RjmQ
{
    "created_since": {
        "type": "incremental",
        "cursor_path": "created_at",
        "initial_value": "2024-01-25",
    },
}

```

The fields are:

- `type`: The type of the parameter definition. In this case, it must be set to `incremental`.
- `cursor_path`: The JSONPath to the field within each item in the list. The value of this field will be used in the next request. In the example above, our items look like `{"id": 1, "title": "Post 1", "created_at": "2024-01-26"}` so to track the created time, we set `cursor_path` to `"created_at"`. Note that the JSONPath starts from the root of the item (dict) and not from the root of the response.
- `initial_value`: The initial value for the cursor. This is the value that will initialize the state of incremental loading. In this case, it's `2024-01-25`. The value type should match the type of the field in the data item.

### Incremental loading using the `incremental` field (DEPRECATED) [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#incremental-loading-using-the-incremental-field-deprecated "Direct link to incremental-loading-using-the-incremental-field-deprecated")

caution

DEPRECATED: This method is deprecated and will be removed in a future dlt version. Use the [placeholder method](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#using-placeholders-for-incremental-loading) instead.

Another alternative method is to use the `incremental` field in the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) while specifying names of the query string parameters to be used as start and end conditions.

Let's take the same example as above and configure it using the `incremental` field:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "results",
    "incremental": {
        "start_param": "created_since",
        "cursor_path": "created_at",
        "initial_value": "2024-01-25",
    },
}

```

The full available configuration for the `incremental` field is:

```codeBlockLines_RjmQ
{
    "incremental": {
        "start_param": "<start_parameter_name>",
        "end_param": "<end_parameter_name>",
        "cursor_path": "<path_to_cursor_field>",
        "initial_value": "<initial_value>",
        "end_value": "<end_value>",
        "convert": my_callable,
    }
}

```

The fields are:

- `start_param` (str): The name of the query parameter to be used as the start condition. If we use the example above, it would be `"created_since"`.
- `end_param` (str): The name of the query parameter to be used as the end condition. This is optional and can be omitted if you only need to track the start condition. This is useful when you need to fetch data within a specific range and the API supports end conditions (like the `created_before` query parameter).
- `cursor_path` (str): The JSONPath to the field within each item in the list. This is the field that will be used to track the incremental loading. In the example above, it's `"created_at"`.
- `initial_value` (str): The initial value for the cursor. This is the value that will initialize the state of incremental loading.
- `end_value` (str): The end value for the cursor to stop the incremental loading. This is optional and can be omitted if you only need to track the start condition. If you set this field, `initial_value` needs to be set as well.
- `convert` (callable): A callable that converts the cursor value into the format that the query parameter requires. For example, a UNIX timestamp can be converted into an ISO 8601 date or a date can be converted into `created_at+gt+{date}`.

See the [incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor) guide for more details.

If you encounter issues with incremental loading, see the [troubleshooting section](https://dlthub.com/docs/general-usage/incremental/troubleshooting) in the incremental loading guide.

### Convert the incremental value before calling the API [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#convert-the-incremental-value-before-calling-the-api "Direct link to Convert the incremental value before calling the API")

If you need to transform the values in the cursor field before passing them to the API endpoint, you can specify a callable under the key `convert`. For example, the API might return UNIX epoch timestamps but expects to be queried with an ISO 8601 date. To achieve that, we can specify a function that converts from the date format returned by the API to the date format required for API requests.

In the following examples, `1704067200` is returned from the API in the field `updated_at`, but the API will be called with `?created_since=2024-01-01`.

Incremental loading using the `params` field:

```codeBlockLines_RjmQ
{
    "created_since": {
        "type": "incremental",
        "cursor_path": "updated_at",
        "initial_value": "1704067200",
        "convert": lambda epoch: pendulum.from_timestamp(int(epoch)).to_date_string(),
    }
}

```

Incremental loading using the `incremental` field:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "results",
    "incremental": {
        "start_param": "created_since",
        "cursor_path": "updated_at",
        "initial_value": "1704067200",
        "convert": lambda epoch: pendulum.from_timestamp(int(epoch)).to_date_string(),
    },
}

```

## Troubleshooting [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#troubleshooting "Direct link to Troubleshooting")

If you encounter issues while running the pipeline, enable [logging](https://dlthub.com/docs/running-in-production/running#set-the-log-level-and-format) for detailed information about the execution:

```codeBlockLines_RjmQ
RUNTIME__LOG_LEVEL=INFO python my_script.py

```

This also provides details on the HTTP requests.

### Configuration issues [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#configuration-issues "Direct link to Configuration issues")

#### Getting validation errors [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#getting-validation-errors "Direct link to Getting validation errors")

When you are running the pipeline and getting a `DictValidationException`, it means that the [source configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#source-configuration) is incorrect. The error message provides details on the issue, including the path to the field and the expected type.

For example, if you have a source configuration like this:

```codeBlockLines_RjmQ
config: RESTAPIConfig = {
    "client": {
        # ...
    },
    "resources": [\
        {\
            "name": "issues",\
            "params": {             # <- Wrong: this should be inside\
                "sort": "updated",  #    the endpoint field below\
            },\
            "endpoint": {\
                "path": "issues",\
                # "params": {       # <- Correct configuration\
                #     "sort": "updated",\
                # },\
            },\
        },\
        # ...\
    ],
}

```

You will get an error like this:

```codeBlockLines_RjmQ
dlt.common.exceptions.DictValidationException: In path .: field 'resources[0]'
expects the following types: str, EndpointResource. Provided value {'name': 'issues', 'params': {'sort': 'updated'},
'endpoint': {'path': 'issues', ... }} with type 'dict' is invalid with the following errors:
For EndpointResource: In path ./resources[0]: following fields are unexpected {'params'}

```

It means that in the first resource configuration ( `resources[0]`), the `params` field should be inside the `endpoint` field.

tip

Import the `RESTAPIConfig` type from the `rest_api` module to have convenient hints in your editor/IDE and use it to define the configuration object.

```codeBlockLines_RjmQ
from dlt.sources.rest_api import RESTAPIConfig

```

#### Getting wrong data or no data [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#getting-wrong-data-or-no-data "Direct link to Getting wrong data or no data")

If incorrect data is received from an endpoint, check the `data_selector` field in the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration). Ensure the JSONPath is accurate and points to the correct data in the response body. `rest_api` attempts to auto-detect the data location, which may not always succeed. See the [data selection](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#data-selection) section for more details.

#### Getting insufficient data or incorrect pagination [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#getting-insufficient-data-or-incorrect-pagination "Direct link to Getting insufficient data or incorrect pagination")

Check the `paginator` field in the configuration. When not explicitly specified, the source tries to auto-detect the pagination method. If auto-detection fails, or the system is unsure, a warning is logged. For production environments, we recommend specifying an explicit paginator in the configuration. See the [pagination](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#pagination) section for more details. Some APIs may have non-standard pagination methods, and you may need to implement a [custom paginator](https://dlthub.com/docs/general-usage/http/rest-client#implementing-a-custom-paginator).

#### Incremental loading not working [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#incremental-loading-not-working "Direct link to Incremental loading not working")

See the [troubleshooting guide](https://dlthub.com/docs/general-usage/incremental/troubleshooting) for incremental loading issues.

#### Getting HTTP 404 errors [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#getting-http-404-errors "Direct link to Getting HTTP 404 errors")

Some APIs may return 404 errors for resources that do not exist or have no data. Manage these responses by configuring the `ignore` action in [response actions](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/advanced#response-actions).

### Authentication issues [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#authentication-issues "Direct link to Authentication issues")

If you are experiencing 401 (Unauthorized) errors, this could indicate:

- Incorrect authorization credentials. Verify credentials in the `secrets.toml`. Refer to [Secret and configs](https://dlthub.com/docs/general-usage/credentials/setup#troubleshoot-configuration-errors) for more information.
- An incorrect authentication type. Consult the API documentation for the proper method. See the [authentication](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#authentication) section for details. For some APIs, a [custom authentication method](https://dlthub.com/docs/general-usage/http/rest-client#implementing-custom-authentication) may be required.

### General guidelines [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#general-guidelines "Direct link to General guidelines")

The `rest_api` source uses the [RESTClient](https://dlthub.com/docs/general-usage/http/rest-client) class for HTTP requests. Refer to the RESTClient [troubleshooting guide](https://dlthub.com/docs/general-usage/http/rest-client#troubleshooting) for debugging tips.

For further assistance, join our [Slack community](https://dlthub.com/community). We're here to help!

- [Quick example](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#quick-example)
- [Setup](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#setup)
  - [Prerequisites](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#prerequisites)
  - [Initialize the REST API source](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#initialize-the-rest-api-source)
  - [Add credentials](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#add-credentials)
- [Run the pipeline](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#run-the-pipeline)
- [Source configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#source-configuration)
  - [Quick example](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#quick-example-1)
  - [Configuration structure](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#configuration-structure)
  - [Resource configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#resource-configuration)
  - [Endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration)
  - [Pagination](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#pagination)
  - [Data selection](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#data-selection)
  - [Authentication](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#authentication)
  - [Define resource relationships](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#define-resource-relationships)
  - [Define a resource which is not a REST endpoint](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#define-a-resource-which-is-not-a-rest-endpoint)
  - [Processing steps: filter and transform data](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#processing-steps-filter-and-transform-data)
- [Incremental loading](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#incremental-loading)
  - [Using placeholders for incremental loading](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#using-placeholders-for-incremental-loading)
  - [Legacy method: Incremental loading in `params` (DEPRECATED)](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#legacy-method-incremental-loading-in-params-deprecated)
  - [Incremental loading using the `incremental` field (DEPRECATED)](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#incremental-loading-using-the-incremental-field-deprecated)
  - [Convert the incremental value before calling the API](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#convert-the-incremental-value-before-calling-the-api)
- [Troubleshooting](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#troubleshooting)
  - [Configuration issues](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#configuration-issues)
  - [Authentication issues](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#authentication-issues)
  - [General guidelines](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#general-guidelines)

----- https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#incremental-loading -----

Version: 1.11.0 (latest)

On this page

Need help deploying these sources or figuring out how to run them in your data stack?

[Join our Slack community](https://dlthub.com/community) or [Get in touch](https://dlthub.com/contact) with the dltHub Customer Success team.

This is a dlt source you can use to extract data from any REST API. It uses [declarative configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#source-configuration) to define the API endpoints, their [relationships](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#define-resource-relationships), how to handle [pagination](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#pagination), and [authentication](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#authentication).

### Quick example [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#quick-example "Direct link to Quick example")

Here's an example of how to configure the REST API source to load posts and related comments from a hypothetical blog API:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.rest_api import rest_api_source

source = rest_api_source({
    "client": {
        "base_url": "https://api.example.com/",
        "auth": {
            "token": dlt.secrets["your_api_token"],
        },
        "paginator": {
            "type": "json_link",
            "next_url_path": "paging.next",
        },
    },
    "resources": [\
        # "posts" will be used as the endpoint path, the resource name,\
        # and the table name in the destination. The HTTP client will send\
        # a request to "https://api.example.com/posts".\
        "posts",\
\
        # The explicit configuration allows you to link resources\
        # and define query string parameters.\
        {\
            "name": "comments",\
            "endpoint": {\
                "path": "posts/{resources.posts.id}/comments",\
                "params": {\
                    "sort": "created_at",\
                },\
            },\
        },\
    ],
})

pipeline = dlt.pipeline(
    pipeline_name="rest_api_example",
    destination="duckdb",
    dataset_name="rest_api_data",
)

load_info = pipeline.run(source)

```

Running this pipeline will create two tables in DuckDB: `posts` and `comments` with the data from the respective API endpoints. The `comments` resource will fetch comments for each post by using the `id` field from the `posts` resource.

## Setup [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#setup "Direct link to Setup")

### Prerequisites [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#prerequisites "Direct link to Prerequisites")

Please make sure the `dlt` library is installed. Refer to the [installation guide](https://dlthub.com/docs/intro).

### Initialize the REST API source [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#initialize-the-rest-api-source "Direct link to Initialize the REST API source")

Enter the following command in your terminal:

```codeBlockLines_RjmQ
dlt init rest_api duckdb

```

[dlt init](https://dlthub.com/docs/reference/command-line-interface) will initialize the pipeline examples for REST API as the [source](https://dlthub.com/docs/general-usage/source) and [duckdb](https://dlthub.com/docs/dlt-ecosystem/destinations/duckdb) as the [destination](https://dlthub.com/docs/dlt-ecosystem/destinations).

Running `dlt init` creates the following in the current folder:

- `rest_api_pipeline.py` file with a sample pipelines definition:
  - GitHub API example
  - Pokemon API example
- `.dlt` folder with:
  - `secrets.toml` file to store your access tokens and other sensitive information
  - `config.toml` file to store the configuration settings
- `requirements.txt` file with the required dependencies

Change the REST API source to your needs by modifying the `rest_api_pipeline.py` file. See the detailed [source configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#source-configuration) section below.

note

For the rest of the guide, we will use the [GitHub API](https://docs.github.com/en/rest?apiVersion=2022-11-28) and [Pokemon API](https://pokeapi.co/) as example sources.

This source is based on the [RESTClient class](https://dlthub.com/docs/general-usage/http/rest-client).

### Add credentials [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#add-credentials "Direct link to Add credentials")

In the `.dlt` folder, you'll find a file called `secrets.toml`, where you can securely store your access tokens and other sensitive information. It's important to handle this file with care and keep it safe.

The GitHub API [requires an access token](https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api?apiVersion=2022-11-28) to access some of its endpoints and to increase the rate limit for the API calls. To get a GitHub token, follow the GitHub documentation on [managing your personal access tokens](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens).

After you get the token, add it to the `secrets.toml` file:

```codeBlockLines_RjmQ
[sources.rest_api_pipeline.github_source]
github_token = "your_github_token"

```

## Run the pipeline [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#run-the-pipeline "Direct link to Run the pipeline")

1. Install the required dependencies by running the following command:

```codeBlockLines_RjmQ
pip install -r requirements.txt

```

2. Run the pipeline:

```codeBlockLines_RjmQ
python rest_api_pipeline.py

```

3. Verify that everything loaded correctly by using the following command:

```codeBlockLines_RjmQ
dlt pipeline rest_api show

```

## Source configuration [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#source-configuration "Direct link to Source configuration")

### Quick example [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#quick-example-1 "Direct link to Quick example")

Let's take a look at the GitHub example in the `rest_api_pipeline.py` file:

```codeBlockLines_RjmQ
from dlt.sources.rest_api import RESTAPIConfig, rest_api_resources

@dlt.source
def github_source(github_token=dlt.secrets.value):
    config: RESTAPIConfig = {
        "client": {
            "base_url": "https://api.github.com/repos/dlt-hub/dlt/",
            "auth": {
                "token": github_token,
            },
        },
        "resource_defaults": {
            "primary_key": "id",
            "write_disposition": "merge",
            "endpoint": {
                "params": {
                    "per_page": 100,
                },
            },
        },
        "resources": [\
            {\
                "name": "issues",\
                "endpoint": {\
                    "path": "issues",\
                    "params": {\
                        "sort": "updated",\
                        "direction": "desc",\
                        "state": "open",\
                        "since": {\
                            "type": "incremental",\
                            "cursor_path": "updated_at",\
                            "initial_value": "2024-01-25T11:21:28Z",\
                        },\
                    },\
                },\
            },\
            {\
                "name": "issue_comments",\
                "endpoint": {\
                    "path": "issues/{resources.issues.number}/comments",\
                },\
                "include_from_parent": ["id"],\
            },\
        ],
    }

    yield from rest_api_resources(config)

def load_github() -> None:
    pipeline = dlt.pipeline(
        pipeline_name="rest_api_github",
        destination="duckdb",
        dataset_name="rest_api_data",
    )

    load_info = pipeline.run(github_source())
    print(load_info)

```

The declarative resource configuration is defined in the `config` dictionary. It contains the following key components:

1. `client`: Defines the base URL and authentication method for the API. In this case, it uses token-based authentication. The token is stored in the `secrets.toml` file.

2. `resource_defaults`: Contains default settings for all [resources](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#resource-configuration). In this example, we define that all resources:
   - Have `id` as the [primary key](https://dlthub.com/docs/general-usage/resource#define-schema)
   - Use the `merge` [write disposition](https://dlthub.com/docs/general-usage/incremental-loading#choosing-a-write-disposition) to merge the data with the existing data in the destination.
   - Send a `per_page=100` query parameter with each request to get more results per page.
3. `resources`: A list of [resources](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#resource-configuration) to be loaded. Here, we have two resources: `issues` and `issue_comments`, which correspond to the GitHub API endpoints for [repository issues](https://docs.github.com/en/rest/issues/issues?apiVersion=2022-11-28#list-repository-issues) and [issue comments](https://docs.github.com/en/rest/issues/comments?apiVersion=2022-11-28#list-issue-comments). Note that we need an issue number to fetch comments for each issue. This number is taken from the `issues` resource. More on this in the [resource relationships](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#define-resource-relationships) section.

Let's break down the configuration in more detail.

### Configuration structure [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#configuration-structure "Direct link to Configuration structure")

tip

Import the `RESTAPIConfig` type from the `rest_api` module to have convenient hints in your editor/IDE and use it to define the configuration object.

```codeBlockLines_RjmQ
from dlt.sources.rest_api import RESTAPIConfig

```

The configuration object passed to the REST API Generic Source has three main elements:

```codeBlockLines_RjmQ
config: RESTAPIConfig = {
    "client": {
        # ...
    },
    "resource_defaults": {
        # ...
    },
    "resources": [\
        # ...\
    ],
}

```

#### `client` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#client "Direct link to client")

The `client` configuration is used to connect to the API's endpoints. It includes the following fields:

- `base_url` (str): The base URL of the API. This string is prepended to all endpoint paths. For example, if the base URL is `https://api.example.com/v1/`, and the endpoint path is `users`, the full URL will be `https://api.example.com/v1/users`.
- `headers` (dict, optional): Additional headers that are sent with each request.
- `auth` (optional): Authentication configuration. This can be a simple token, an `AuthConfigBase` object, or a more complex authentication method.
- `paginator` (optional): Configuration for the default pagination used for resources that support pagination. Refer to the [pagination](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#pagination) section for more details.

#### `resource_defaults` (optional) [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#resource_defaults-optional "Direct link to resource_defaults-optional")

`resource_defaults` contains the default values to [configure the dlt resources](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#resource-configuration). This configuration is applied to all resources unless overridden by the resource-specific configuration.

For example, you can set the primary key, write disposition, and other default settings here:

```codeBlockLines_RjmQ
config = {
    "client": {
        # ...
    },
    "resource_defaults": {
        "primary_key": "id",
        "write_disposition": "merge",
        "endpoint": {
            "params": {
                "per_page": 100,
            },
        },
    },
    "resources": [\
        "resource1",\
        {\
            "name": "resource2_name",\
            "write_disposition": "append",\
            "endpoint": {\
                "params": {\
                    "param1": "value1",\
                },\
            },\
        }\
    ],
}

```

Above, all resources will have `primary_key` set to `id`, `resource1` will have `write_disposition` set to `merge`, and `resource2` will override the default `write_disposition` with `append`.
Both `resource1` and `resource2` will have the `per_page` parameter set to 100.

#### `resources` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#resources "Direct link to resources")

This is a list of resource configurations that define the API endpoints to be loaded. Each resource configuration can be:

- a dictionary with the [resource configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#resource-configuration).
- a string. In this case, the string is used as both the endpoint path and the resource name, and the resource configuration is taken from the `resource_defaults` configuration if it exists.

### Resource configuration [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#resource-configuration "Direct link to Resource configuration")

A resource configuration is used to define a [dlt resource](https://dlthub.com/docs/general-usage/resource) for the data to be loaded from an API endpoint. It contains the following key fields:

- `endpoint`: The endpoint configuration for the resource. It can be a string or a dict representing the endpoint settings. See the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) section for more details.
- `write_disposition`: The write disposition for the resource.
- `primary_key`: The primary key for the resource.
- `include_from_parent`: A list of fields from the parent resource to be included in the resource output. See the [resource relationships](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#include-fields-from-the-parent-resource) section for more details.
- `processing_steps`: A list of [processing steps](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#processing-steps-filter-and-transform-data) to filter and transform your data.
- `selected`: A flag to indicate if the resource is selected for loading. This could be useful when you want to load data only from child resources and not from the parent resource.
- `auth`: An optional `AuthConfig` instance. If passed, is used over the one defined in the [client](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#client) definition. Example:

```codeBlockLines_RjmQ
from dlt.sources.helpers.rest_client.auth import HttpBasicAuth

config = {
    "client": {
        "auth": {
            "type": "bearer",
            "token": dlt.secrets["your_api_token"],
        }
    },
    "resources": [\
        "resource-using-bearer-auth",\
        {\
            "name": "my-resource-with-special-auth",\
            "endpoint": {\
                # ...\
                "auth": HttpBasicAuth("user", dlt.secrets["your_basic_auth_password"])\
            },\
            # ...\
        }\
    ]
    # ...
}

```

This would use `Bearer` auth as defined in the `client` for `resource-using-bearer-auth` and `Http Basic` auth for `my-resource-with-special-auth`.

You can also pass additional resource parameters that will be used to configure the dlt resource. See [dlt resource API reference](https://dlthub.com/docs/api_reference/dlt/extract/decorators#resource) for more details.

### Endpoint configuration [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#endpoint-configuration "Direct link to Endpoint configuration")

The endpoint configuration defines how to query the API endpoint. Quick example:

```codeBlockLines_RjmQ
{
    "path": "issues",
    "method": "GET",
    "params": {
        "sort": "updated",
        "direction": "desc",
        "state": "open",
        "since": {
            "type": "incremental",
            "cursor_path": "updated_at",
            "initial_value": "2024-01-25T11:21:28Z",
        },
    },
    "data_selector": "results",
}

```

The fields in the endpoint configuration are:

- `path`: The path to the API endpoint. By default this path is appended to the given `base_url`. If this is a fully qualified URL starting with `http:` or `https:` it will be
used as-is and `base_url` will be ignored.
- `method`: The HTTP method to be used. The default is `GET`.
- `params`: Query parameters to be sent with each request. For example, `sort` to order the results or `since` to specify [incremental loading](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#incremental-loading). This is also may be used to define [resource relationships](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#define-resource-relationships).
- `json`: The JSON payload to be sent with the request (for POST and PUT requests).
- `paginator`: Pagination configuration for the endpoint. See the [pagination](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#pagination) section for more details.
- `data_selector`: A JSONPath to select the data from the response. See the [data selection](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#data-selection) section for more details.
- `response_actions`: A list of actions that define how to process the response data. See the [response actions](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/advanced#response-actions) section for more details.
- `incremental`: Configuration for [incremental loading](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#incremental-loading).

### Pagination [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#pagination "Direct link to Pagination")

The REST API source will try to automatically handle pagination for you. This works by detecting the pagination details from the first API response.

In some special cases, you may need to specify the pagination configuration explicitly.

To specify the pagination configuration, use the `paginator` field in the [client](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#client) or [endpoint](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) configurations. You may either use a dictionary with a string alias in the `type` field along with the required parameters, or use a [paginator class instance](https://dlthub.com/docs/general-usage/http/rest-client#paginators).

#### Example [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#example "Direct link to Example")

Suppose the API response for `https://api.example.com/posts` contains a `next` field with the URL to the next page:

```codeBlockLines_RjmQ
{
    "data": [\
        {"id": 1, "title": "Post 1"},\
        {"id": 2, "title": "Post 2"},\
        {"id": 3, "title": "Post 3"}\
    ],
    "pagination": {
        "next": "https://api.example.com/posts?page=2"
    }
}

```

You can configure the pagination for the `posts` resource like this:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "paginator": {
        "type": "json_link",
        "next_url_path": "pagination.next",
    }
}

```

Alternatively, you can use the paginator instance directly:

```codeBlockLines_RjmQ
from dlt.sources.helpers.rest_client.paginators import JSONLinkPaginator

# ...

{
    "path": "posts",
    "paginator": JSONLinkPaginator(
        next_url_path="pagination.next"
    ),
}

```

note

Currently, pagination is supported only for GET requests. To handle POST requests with pagination, you need to implement a [custom paginator](https://dlthub.com/docs/general-usage/http/rest-client#implementing-a-custom-paginator).

These are the available paginators:

| `type` | Paginator class | Description |
| --- | --- | --- |
| `json_link` | [JSONLinkPaginator](https://dlthub.com/docs/general-usage/http/rest-client#jsonlinkpaginator) | The link to the next page is in the body (JSON) of the response.<br>_Parameters:_ <br>- `next_url_path` (str) - the JSONPath to the next page URL |
| `header_link` | [HeaderLinkPaginator](https://dlthub.com/docs/general-usage/http/rest-client#headerlinkpaginator) | The links to the next page are in the response headers.<br>_Parameters:_ <br>- `links_next_key` (str) - the name of the header containing the links. Default is "next". |
| `offset` | [OffsetPaginator](https://dlthub.com/docs/general-usage/http/rest-client#offsetpaginator) | The pagination is based on an offset parameter, with the total items count either in the response body or explicitly provided.<br>_Parameters:_ <br>- `limit` (int) - the maximum number of items to retrieve in each request<br>- `offset` (int) - the initial offset for the first request. Defaults to `0`<br>- `offset_param` (str) - the name of the query parameter used to specify the offset. Defaults to "offset"<br>- `limit_param` (str) - the name of the query parameter used to specify the limit. Defaults to "limit"<br>- `total_path` (str) - a JSONPath expression for the total number of items. If not provided, pagination is controlled by `maximum_offset` and `stop_after_empty_page`<br>- `maximum_offset` (int) - optional maximum offset value. Limits pagination even without total count<br>- `stop_after_empty_page` (bool) - Whether pagination should stop when a page contains no result items. Defaults to `True` |
| `page_number` | [PageNumberPaginator](https://dlthub.com/docs/general-usage/http/rest-client#pagenumberpaginator) | The pagination is based on a page number parameter, with the total pages count either in the response body or explicitly provided.<br>_Parameters:_ <br>- `base_page` (int) - the starting page number. Defaults to `0`<br>- `page_param` (str) - the query parameter name for the page number. Defaults to "page"<br>- `total_path` (str) - a JSONPath expression for the total number of pages. If not provided, pagination is controlled by `maximum_page` and `stop_after_empty_page`<br>- `maximum_page` (int) - optional maximum page number. Stops pagination once this page is reached<br>- `stop_after_empty_page` (bool) - Whether pagination should stop when a page contains no result items. Defaults to `True` |
| `cursor` | [JSONResponseCursorPaginator](https://dlthub.com/docs/general-usage/http/rest-client#jsonresponsecursorpaginator) | The pagination is based on a cursor parameter, with the value of the cursor in the response body (JSON).<br>_Parameters:_ <br>- `cursor_path` (str) - the JSONPath to the cursor value. Defaults to "cursors.next"<br>- `cursor_param` (str) - the query parameter name for the cursor. Defaults to "cursor" if neither `cursor_param` nor `cursor_body_path` is provided.<br>- `cursor_body_path` (str, optional) - the JSONPath to place the cursor in the request body.<br>Note: You must provide either `cursor_param` or `cursor_body_path`, but not both. If neither is provided, `cursor_param` will default to "cursor". |
| `single_page` | SinglePagePaginator | The response will be interpreted as a single-page response, ignoring possible pagination metadata. |
| `auto` | `None` | Explicitly specify that the source should automatically detect the pagination method. |

For more complex pagination methods, you can implement a [custom paginator](https://dlthub.com/docs/general-usage/http/rest-client#implementing-a-custom-paginator), instantiate it, and use it in the configuration.

Alternatively, you can use the dictionary configuration syntax also for custom paginators. For this, you need to register your custom paginator:

```codeBlockLines_RjmQ
from dlt.sources.rest_api.config_setup import register_paginator

class CustomPaginator(SinglePagePaginator):
    # custom implementation of SinglePagePaginator
    pass

register_paginator("custom_paginator", CustomPaginator)

{
    # ...
    "paginator": {
        "type": "custom_paginator",
        "next_url_path": "paging.nextLink",
    }
}

```

### Data selection [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#data-selection "Direct link to Data selection")

The `data_selector` field in the endpoint configuration allows you to specify a JSONPath to select the data from the response. By default, the source will try to detect the locations of the data automatically.

Use this field when you need to specify the location of the data in the response explicitly.

For example, if the API response looks like this:

```codeBlockLines_RjmQ
{
    "posts": [\
        {"id": 1, "title": "Post 1"},\
        {"id": 2, "title": "Post 2"},\
        {"id": 3, "title": "Post 3"}\
    ]
}

```

You can use the following endpoint configuration:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "posts",
}

```

For a nested structure like this:

```codeBlockLines_RjmQ
{
    "results": {
        "posts": [\
            {"id": 1, "title": "Post 1"},\
            {"id": 2, "title": "Post 2"},\
            {"id": 3, "title": "Post 3"}\
        ]
    }
}

```

You can use the following endpoint configuration:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "results.posts",
}

```

Read more about [JSONPath syntax](https://github.com/h2non/jsonpath-ng?tab=readme-ov-file#jsonpath-syntax) to learn how to write selectors.

### Authentication [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#authentication "Direct link to Authentication")

For APIs that require authentication to access their endpoints, the REST API source supports various authentication methods, including token-based authentication, query parameters, basic authentication, and custom authentication. The authentication configuration is specified in the `auth` field of the [client](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#client) either as a dictionary or as an instance of the [authentication class](https://dlthub.com/docs/general-usage/http/rest-client#authentication).

#### Quick example [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#quick-example-2 "Direct link to Quick example")

Here's how to configure authentication using a bearer token:

```codeBlockLines_RjmQ
{
    "client": {
        # ...
        "auth": {
            "type": "bearer",
            "token": dlt.secrets["your_api_token"],
        },
        # ...
    },
}

```

Alternatively, you can use the authentication class directly:

```codeBlockLines_RjmQ
from dlt.sources.helpers.rest_client.auth import BearerTokenAuth

config = {
    "client": {
        "auth": BearerTokenAuth(dlt.secrets["your_api_token"]),
    },
    "resources": [\
    ]
    # ...
}

```

Since token-based authentication is one of the most common methods, you can use the following shortcut:

```codeBlockLines_RjmQ
{
    "client": {
        # ...
        "auth": {
            "token": dlt.secrets["your_api_token"],
        },
        # ...
    },
}

```

warning

Make sure to store your access tokens and other sensitive information in the `secrets.toml` file and never commit it to the version control system.

Available authentication types:

| `type` | Authentication class | Description |
| --- | --- | --- |
| `bearer` | [BearerTokenAuth](https://dlthub.com/docs/general-usage/http/rest-client#bearer-token-authentication) | Bearer token authentication.<br>Parameters:<br>- `token` (str) |
| `http_basic` | [HTTPBasicAuth](https://dlthub.com/docs/general-usage/http/rest-client#http-basic-authentication) | Basic HTTP authentication.<br>Parameters:<br>- `username` (str)<br>- `password` (str) |
| `api_key` | [APIKeyAuth](https://dlthub.com/docs/general-usage/http/rest-client#api-key-authentication) | API key authentication with key defined in the query parameters or in the headers. <br>Parameters:<br>- `name` (str) - the name of the query parameter or header<br>- `api_key` (str) - the API key value<br>- `location` (str, optional) - the location of the API key in the request. Can be `query` or `header`. Default is `header` |
| `oauth2_client_credentials` | [OAuth2ClientCredentials](https://dlthub.com/docs/general-usage/http/rest-client#oauth-20-authorization) | OAuth 2.0 Client Credentials authorization for server-to-server communication without user consent. <br>Parameters:<br>- `access_token` (str, optional) - the temporary token. Usually not provided here because it is automatically obtained from the server by exchanging `client_id` and `client_secret`. Default is `None`<br>- `access_token_url` (str) - the URL to request the `access_token` from<br>- `client_id` (str) - identifier for your app. Usually issued via a developer portal<br>- `client_secret` (str) - client credential to obtain authorization. Usually issued via a developer portal<br>- `access_token_request_data` (dict, optional) - A dictionary with data required by the authorization server apart from the `client_id`, `client_secret`, and `"grant_type": "client_credentials"`. Defaults to `None`<br>- `default_token_expiration` (int, optional) - The time in seconds after which the temporary access token expires. Defaults to 3600.<br>- `session` (requests.Session, optional) - a custom session object. Mostly used for testing |

For more complex authentication methods, you can implement a [custom authentication class](https://dlthub.com/docs/general-usage/http/rest-client#implementing-custom-authentication) and use it in the configuration.

You can use the dictionary configuration syntax also for custom authentication classes after registering them as follows:

```codeBlockLines_RjmQ
from dlt.sources.rest_api.config_setup import register_auth

class CustomAuth(AuthConfigBase):
    pass

register_auth("custom_auth", CustomAuth)

{
    # ...
    "auth": {
        "type": "custom_auth",
        "api_key": dlt.secrets["sources.my_source.my_api_key"],
    }
}

```

### Define resource relationships [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#define-resource-relationships "Direct link to Define resource relationships")

When you have a resource that depends on another resource (for example, you must fetch a parent resource to get an ID needed to fetch the child), you can reference fields in the parent resource using special placeholders.
This allows you to link one or more [path](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#via-request-path), [query string](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#via-query-string-parameters) or [JSON body](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#via-json-body) parameters in the child resource to fields in the parent resource's data.

#### Via request path [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#via-request-path "Direct link to Via request path")

In the GitHub example, the `issue_comments` resource depends on the `issues` resource. The `resources.issues.number` placeholder links the `number` field in the `issues` resource data to the current request's path parameter.

```codeBlockLines_RjmQ
{
    "resources": [\
        {\
            "name": "issues",\
            "endpoint": {\
                "path": "issues",\
                # ...\
            },\
        },\
        {\
            "name": "issue_comments",\
            "endpoint": {\
                "path": "issues/{resources.issues.number}/comments",\
            },\
            "include_from_parent": ["id"],\
        },\
    ],
}

```

This configuration tells the source to get issue numbers from the `issues` resource data and use them to fetch comments for each issue number. So for each issue item, `"{resources.issues.number}"` is replaced by the issue number in the request path.
For example, if the `issues` resource yields the following data:

```codeBlockLines_RjmQ
[\
    {"id": 1, "number": 123},\
    {"id": 2, "number": 124},\
    {"id": 3, "number": 125}\
]

```

The `issue_comments` resource will make requests to the following endpoints:

- `issues/123/comments`
- `issues/124/comments`
- `issues/125/comments`

The syntax for the placeholder is `resources.<parent_resource_name>.<field_name>`.

#### Via query string parameters [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#via-query-string-parameters "Direct link to Via query string parameters")

The placeholder syntax can also be used in the query string parameters. For example, in an API which lets you fetch a blog posts (via `/posts`) and their comments (via `/comments?post_id=<post_id>`), you can define a resource `posts` and a resource `post_comments` which depends on the `posts` resource. You can then reference the `id` field from the `posts` resource in the `post_comments` resource:

```codeBlockLines_RjmQ
{
    "resources": [\
        "posts",\
        {\
            "name": "post_comments",\
            "endpoint": {\
                "path": "comments",\
                "params": {\
                    "post_id": "{resources.posts.id}",\
                },\
            },\
        },\
    ],
}

```

Similar to the GitHub example above, if the `posts` resource yields the following data:

```codeBlockLines_RjmQ
[\
    {"id": 1, "title": "Post 1"},\
    {"id": 2, "title": "Post 2"},\
    {"id": 3, "title": "Post 3"}\
]

```

The `post_comments` resource will make requests to the following endpoints:

- `comments?post_id=1`
- `comments?post_id=2`
- `comments?post_id=3`

#### Via JSON body [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#via-json-body "Direct link to Via JSON body")

In many APIs, you can send a complex query or configuration through a POST request's JSON body rather than in the request path or query parameters. For example, consider an imaginary `/search` endpoint that supports multiple filters and settings. You might have a parent resource `posts` with each post's `id` and a second resource, `post_details`, that uses `id` to perform a custom search.

In the example below we reference the `posts` resource's `id` field in the JSON body via placeholders:

```codeBlockLines_RjmQ
{
    "resources": [\
        "posts",\
        {\
            "name": "post_details",\
            "endpoint": {\
                "path": "search",\
                "method": "POST",\
                "json": {\
                    "filters": {\
                        "id": "{resources.posts.id}",\
                    },\
                    "order": "desc",\
                    "limit": 5,\
                }\
            },\
        },\
    ],
}

```

#### Legacy syntax: `resolve` field in parameter configuration [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#legacy-syntax-resolve-field-in-parameter-configuration "Direct link to legacy-syntax-resolve-field-in-parameter-configuration")

warning

`resolve` works only for path parameters. The new placeholder syntax is more flexible and recommended for new configurations.

An alternative, legacy way to define resource relationships is to use the `resolve` field in the parameter configuration.
Here's the same example as above that uses the `resolve` field:

```codeBlockLines_RjmQ
{
    "resources": [\
        {\
            "name": "issues",\
            "endpoint": {\
                "path": "issues",\
                # ...\
            },\
        },\
        {\
            "name": "issue_comments",\
            "endpoint": {\
                "path": "issues/{issue_number}/comments",\
                "params": {\
                    "issue_number": {\
                        "type": "resolve",\
                        "resource": "issues",\
                        "field": "number",\
                    }\
                },\
            },\
            "include_from_parent": ["id"],\
        },\
    ],
}

```

The syntax for the `resolve` field in parameter configuration is:

```codeBlockLines_RjmQ
{
    "<parameter_name>": {
        "type": "resolve",
        "resource": "<parent_resource_name>",
        "field": "<parent_resource_field_name_or_jsonpath>",
    }
}

```

The `field` value can be specified as a [JSONPath](https://github.com/h2non/jsonpath-ng?tab=readme-ov-file#jsonpath-syntax) to select a nested field in the parent resource data. For example: `"field": "items[0].id"`.

#### Resolving multiple path parameters from a parent resource [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#resolving-multiple-path-parameters-from-a-parent-resource "Direct link to Resolving multiple path parameters from a parent resource")

When a child resource depends on multiple fields from a single parent resource, you can define multiple `resolve` parameters in the endpoint configuration. For example:

```codeBlockLines_RjmQ
{
    "resources": [\
        "groups",\
        {\
            "name": "users",\
            "endpoint": {\
                "path": "groups/{group_id}/users",\
                "params": {\
                    "group_id": {\
                        "type": "resolve",\
                        "resource": "groups",\
                        "field": "id",\
                    },\
                },\
            },\
        },\
        {\
            "name": "user_details",\
            "endpoint": {\
                "path": "groups/{group_id}/users/{user_id}/details",\
                "params": {\
                    "group_id": {\
                        "type": "resolve",\
                        "resource": "users",\
                        "field": "group_id",\
                    },\
                    "user_id": {\
                        "type": "resolve",\
                        "resource": "users",\
                        "field": "id",\
                    },\
                },\
            },\
        },\
    ],
}

```

In the configuration above:

- The `users` resource depends on the `groups` resource, resolving the `group_id` parameter from the `id` field in `groups`.
- The `user_details` resource depends on the `users` resource, resolving both `group_id` and `user_id` parameters from fields in `users`.

#### Include fields from the parent resource [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#include-fields-from-the-parent-resource "Direct link to Include fields from the parent resource")

You can include data from the parent resource in the child resource by using the `include_from_parent` field in the resource configuration. For example:

```codeBlockLines_RjmQ
{
    "name": "issue_comments",
    "endpoint": {
        ...
    },
    "include_from_parent": ["id", "title", "created_at"],
}

```

This will include the `id`, `title`, and `created_at` fields from the `issues` resource in the `issue_comments` resource data. The names of the included fields will be prefixed with the parent resource name and an underscore ( `_`) like so: `_issues_id`, `_issues_title`, `_issues_created_at`.

### Define a resource which is not a REST endpoint [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#define-a-resource-which-is-not-a-rest-endpoint "Direct link to Define a resource which is not a REST endpoint")

Sometimes, we want to request endpoints with specific values that are not returned by another endpoint.
Thus, you can also include arbitrary dlt resources in your `RESTAPIConfig` instead of defining a resource for every path!

In the following example, we want to load the issues belonging to three repositories.
Instead of defining three different issues resources, one for each of the paths `dlt-hub/dlt/issues/`, `dlt-hub/verified-sources/issues/`, `dlt-hub/dlthub-education/issues/`, we have a resource `repositories` which yields a list of repository names that will be fetched by the dependent resource `issues`.

```codeBlockLines_RjmQ
from dlt.sources.rest_api import RESTAPIConfig

@dlt.resource()
def repositories() -> Generator[List[Dict[str, Any]], Any, Any]:
    """A seed list of repositories to fetch"""
    yield [{"name": "dlt"}, {"name": "verified-sources"}, {"name": "dlthub-education"}]

config: RESTAPIConfig = {
    "client": {"base_url": "https://github.com/api/v2"},
    "resources": [\
        {\
            "name": "issues",\
            "endpoint": {\
                "path": "dlt-hub/{repository}/issues/",\
                "params": {\
                    "repository": {\
                        "type": "resolve",\
                        "resource": "repositories",\
                        "field": "name",\
                    },\
                },\
            },\
        },\
        repositories(),\
    ],
}

```

Be careful that the parent resource needs to return `Generator[List[Dict[str, Any]]]`. Thus, the following will NOT work:

```codeBlockLines_RjmQ
@dlt.resource
def repositories() -> Generator[Dict[str, Any], Any, Any]:
    """Not working seed list of repositories to fetch"""
    yield from [{"name": "dlt"}, {"name": "verified-sources"}, {"name": "dlthub-education"}]

```

### Processing steps: filter and transform data [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#processing-steps-filter-and-transform-data "Direct link to Processing steps: filter and transform data")

The `processing_steps` field in the resource configuration allows you to apply transformations to the data fetched from the API before it is loaded into your destination. This is useful when you need to filter out certain records, modify the data structure, or anonymize sensitive information.

Each processing step is a dictionary specifying the type of operation ( `filter` or `map`) and the function to apply. Steps apply in the order they are listed.

#### Quick example [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#quick-example-3 "Direct link to Quick example")

```codeBlockLines_RjmQ
def lower_title(record):
    record["title"] = record["title"].lower()
    return record

config: RESTAPIConfig = {
    "client": {
        "base_url": "https://api.example.com",
    },
    "resources": [\
        {\
            "name": "posts",\
            "processing_steps": [\
                {"filter": lambda x: x["id"] < 10},\
                {"map": lower_title},\
            ],\
        },\
    ],
}

```

In the example above:

- First, the `filter` step uses a lambda function to include only records where `id` is less than 10.
- Thereafter, the `map` step applies the `lower_title` function to each remaining record.

#### Using `filter` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#using-filter "Direct link to using-filter")

The `filter` step allows you to exclude records that do not meet certain criteria. The provided function should return `True` to keep the record or `False` to exclude it:

```codeBlockLines_RjmQ
{
    "name": "posts",
    "endpoint": "posts",
    "processing_steps": [\
        {"filter": lambda x: x["id"] in [10, 20, 30]},\
    ],
}

```

In this example, only records with `id` equal to 10, 20, or 30 will be included.

#### Using `map` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#using-map "Direct link to using-map")

The `map` step allows you to modify the records fetched from the API. The provided function should take a record as an argument and return the modified record. For example, to anonymize the `email` field:

```codeBlockLines_RjmQ
def anonymize_email(record):
    record["email"] = "REDACTED"
    return record

config: RESTAPIConfig = {
    "client": {
        "base_url": "https://api.example.com",
    },
    "resources": [\
        {\
            "name": "users",\
            "processing_steps": [\
                {"map": anonymize_email},\
            ],\
        },\
    ],
}

```

#### Combining `filter` and `map` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#combining-filter-and-map "Direct link to combining-filter-and-map")

You can combine multiple processing steps to achieve complex transformations:

```codeBlockLines_RjmQ
{
    "name": "posts",
    "endpoint": "posts",
    "processing_steps": [\
        {"filter": lambda x: x["id"] < 10},\
        {"map": lower_title},\
        {"filter": lambda x: "important" in x["title"]},\
    ],
}

```

tip

#### Best practices [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#best-practices "Direct link to Best practices")

1. Order matters: Processing steps are applied in the order they are listed. Be mindful of the sequence, especially when combining `map` and `filter`.
2. Function definition: Define your filter and map functions separately for clarity and reuse.
3. Use `filter` to exclude records early in the process to reduce the amount of data that needs to be processed.
4. Combine consecutive `map` steps into a single function for faster execution.

## Incremental loading [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#incremental-loading "Direct link to Incremental loading")

Some APIs provide a way to fetch only new or changed data (most often by using a timestamp field like `updated_at`, `created_at`, or incremental IDs).
This is called [incremental loading](https://dlthub.com/docs/general-usage/incremental-loading) and is very useful as it allows you to reduce the load time and the amount of data transferred.

Let's continue with our imaginary blog API example to understand incremental loading with query parameters.

Imagine we have the following endpoint `https://api.example.com/posts` and it:

1. Accepts a `created_since` query parameter to fetch blog posts created after a certain date.
2. Returns a list of posts with the `created_at` field for each post.

For example, if we query the endpoint with GET request `https://api.example.com/posts?created_since=2024-01-25`, we get the following response:

```codeBlockLines_RjmQ
{
    "results": [\
        {"id": 1, "title": "Post 1", "created_at": "2024-01-26"},\
        {"id": 2, "title": "Post 2", "created_at": "2024-01-27"},\
        {"id": 3, "title": "Post 3", "created_at": "2024-01-28"}\
    ]
}

```

When the API endpoint supports incremental loading, you can configure dlt to load only the new or changed data using these three methods:

1. Using [placeholders for incremental loading](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#using-placeholders-for-incremental-loading)
2. Defining a special parameter in the `params` section of the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) (DEPRECATED)
3. Using the `incremental` field in the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) with the `start_param` field (DEPRECATED)

caution

The last two methods are deprecated and will be removed in a future dlt version.

### Using placeholders for incremental loading [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#using-placeholders-for-incremental-loading "Direct link to Using placeholders for incremental loading")

The most flexible way to configure incremental loading is to use placeholders in the request configuration along with the `incremental` section.
Here's how it works:

1. Define the `incremental` section in the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) to specify the cursor path (where to find the incremental value in the response) and initial value (the value to start the incremental loading from).
2. Use the placeholder `{incremental.start_value}` in the request configuration to reference the incremental value.

Let's take the example from the previous section and configure it using placeholders:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "results",
    "params": {
        "created_since": "{incremental.start_value}",  # Uses cursor value in query parameter
    },
    "incremental": {
        "cursor_path": "created_at",
        "initial_value": "2024-01-25T00:00:00Z",
    },
}

```

When you first run this pipeline, dlt will:

1. Replace `{incremental.start_value}` with `2024-01-25T00:00:00Z` (the initial value)
2. Make a GET request to `https://api.example.com/posts?created_since=2024-01-25T00:00:00Z`
3. Parse the response (e.g., posts with created\_at values like "2024-01-26", "2024-01-27", "2024-01-28")
4. Track the maximum value found in the "created\_at" field (in this case, "2024-01-28")

On the next pipeline run, dlt will:

1. Replace `{incremental.start_value}` with "2024-01-28" (the last seen maximum value)
2. Make a GET request to `https://api.example.com/posts?created_since=2024-01-28`
3. The API will only return posts created on or after January 28th

Let's break down the configuration:

1. We explicitly set `data_selector` to `"results"` to select the list of posts from the response. This is optional; if not set, dlt will try to auto-detect the data location.
2. We define the `created_since` parameter in `params` section and use the placeholder `{incremental.start_value}` to reference the incremental value.

Placeholders are versatile and can be used in various request components. Here are some examples:

#### In JSON body (for POST requests) [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#in-json-body-for-post-requests "Direct link to In JSON body (for POST requests)")

If the API lets you filter the data by a range of dates (e.g. `fromDate` and `toDate`), you can use the placeholder in the JSON body:

```codeBlockLines_RjmQ
{
    "path": "posts/search",
    "method": "POST",
    "json": {
        "filters": {
            "fromDate": "{incremental.start_value}",  # In JSON body
            "toDate": "2024-03-25"
        },
        "limit": 1000
    },
    "incremental": {
        "cursor_path": "created_at",
        "initial_value": "2024-01-25T00:00:00Z",
    },
}

```

#### In path parameters [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#in-path-parameters "Direct link to In path parameters")

Some APIs use path parameters to filter the data:

```codeBlockLines_RjmQ
{
    "path": "posts/since/{incremental.start_value}/list",  # In URL path
    "incremental": {
        "cursor_path": "created_at",
        "initial_value": "2024-01-25",
    },
}

```

#### In request headers [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#in-request-headers "Direct link to In request headers")

It's not so common, but you can also use placeholders in the request headers:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "headers": {
        "X-Since-Timestamp": "{incremental.start_value}"  # In custom header
    },
    "incremental": {
        "cursor_path": "created_at",
        "initial_value": "2024-01-25T00:00:00Z",
    },
}

```

You can also use different placeholder variants depending on your needs:

| Placeholder | Description |
| --- | --- |
| `{incremental.start_value}` | The value to use as the starting point for this request (either the initial value or the last tracked maximum value) |
| `{incremental.initial_value}` | Always uses the initial value specified in the configuration |
| `{incremental.last_value}` | The last seen value (same as start\_value in most cases, see the [incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor) guide for more details) |
| `{incremental.end_value}` | The end value if specified in the configuration |

### Legacy method: Incremental loading in `params` (DEPRECATED) [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#legacy-method-incremental-loading-in-params-deprecated "Direct link to legacy-method-incremental-loading-in-params-deprecated")

caution

DEPRECATED: This method is deprecated and will be removed in a future version. Use the [placeholder method](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#using-placeholders-for-incremental-loading) instead.

note

This method only works for query string parameters. For other request parts (path, JSON body, headers), use the [placeholder method](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#using-placeholders-for-incremental-loading).

For query string parameters, you can also specify incremental loading directly in the `params` section:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "results",  # Optional JSONPath to select the list of posts
    "params": {
        "created_since": {
            "type": "incremental",
            "cursor_path": "created_at", # The JSONPath to the field we want to track in each post
            "initial_value": "2024-01-25",
        },
    },
}

```

Above we define the `created_since` parameter as an incremental parameter as:

```codeBlockLines_RjmQ
{
    "created_since": {
        "type": "incremental",
        "cursor_path": "created_at",
        "initial_value": "2024-01-25",
    },
}

```

The fields are:

- `type`: The type of the parameter definition. In this case, it must be set to `incremental`.
- `cursor_path`: The JSONPath to the field within each item in the list. The value of this field will be used in the next request. In the example above, our items look like `{"id": 1, "title": "Post 1", "created_at": "2024-01-26"}` so to track the created time, we set `cursor_path` to `"created_at"`. Note that the JSONPath starts from the root of the item (dict) and not from the root of the response.
- `initial_value`: The initial value for the cursor. This is the value that will initialize the state of incremental loading. In this case, it's `2024-01-25`. The value type should match the type of the field in the data item.

### Incremental loading using the `incremental` field (DEPRECATED) [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#incremental-loading-using-the-incremental-field-deprecated "Direct link to incremental-loading-using-the-incremental-field-deprecated")

caution

DEPRECATED: This method is deprecated and will be removed in a future dlt version. Use the [placeholder method](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#using-placeholders-for-incremental-loading) instead.

Another alternative method is to use the `incremental` field in the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) while specifying names of the query string parameters to be used as start and end conditions.

Let's take the same example as above and configure it using the `incremental` field:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "results",
    "incremental": {
        "start_param": "created_since",
        "cursor_path": "created_at",
        "initial_value": "2024-01-25",
    },
}

```

The full available configuration for the `incremental` field is:

```codeBlockLines_RjmQ
{
    "incremental": {
        "start_param": "<start_parameter_name>",
        "end_param": "<end_parameter_name>",
        "cursor_path": "<path_to_cursor_field>",
        "initial_value": "<initial_value>",
        "end_value": "<end_value>",
        "convert": my_callable,
    }
}

```

The fields are:

- `start_param` (str): The name of the query parameter to be used as the start condition. If we use the example above, it would be `"created_since"`.
- `end_param` (str): The name of the query parameter to be used as the end condition. This is optional and can be omitted if you only need to track the start condition. This is useful when you need to fetch data within a specific range and the API supports end conditions (like the `created_before` query parameter).
- `cursor_path` (str): The JSONPath to the field within each item in the list. This is the field that will be used to track the incremental loading. In the example above, it's `"created_at"`.
- `initial_value` (str): The initial value for the cursor. This is the value that will initialize the state of incremental loading.
- `end_value` (str): The end value for the cursor to stop the incremental loading. This is optional and can be omitted if you only need to track the start condition. If you set this field, `initial_value` needs to be set as well.
- `convert` (callable): A callable that converts the cursor value into the format that the query parameter requires. For example, a UNIX timestamp can be converted into an ISO 8601 date or a date can be converted into `created_at+gt+{date}`.

See the [incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor) guide for more details.

If you encounter issues with incremental loading, see the [troubleshooting section](https://dlthub.com/docs/general-usage/incremental/troubleshooting) in the incremental loading guide.

### Convert the incremental value before calling the API [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#convert-the-incremental-value-before-calling-the-api "Direct link to Convert the incremental value before calling the API")

If you need to transform the values in the cursor field before passing them to the API endpoint, you can specify a callable under the key `convert`. For example, the API might return UNIX epoch timestamps but expects to be queried with an ISO 8601 date. To achieve that, we can specify a function that converts from the date format returned by the API to the date format required for API requests.

In the following examples, `1704067200` is returned from the API in the field `updated_at`, but the API will be called with `?created_since=2024-01-01`.

Incremental loading using the `params` field:

```codeBlockLines_RjmQ
{
    "created_since": {
        "type": "incremental",
        "cursor_path": "updated_at",
        "initial_value": "1704067200",
        "convert": lambda epoch: pendulum.from_timestamp(int(epoch)).to_date_string(),
    }
}

```

Incremental loading using the `incremental` field:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "results",
    "incremental": {
        "start_param": "created_since",
        "cursor_path": "updated_at",
        "initial_value": "1704067200",
        "convert": lambda epoch: pendulum.from_timestamp(int(epoch)).to_date_string(),
    },
}

```

## Troubleshooting [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#troubleshooting "Direct link to Troubleshooting")

If you encounter issues while running the pipeline, enable [logging](https://dlthub.com/docs/running-in-production/running#set-the-log-level-and-format) for detailed information about the execution:

```codeBlockLines_RjmQ
RUNTIME__LOG_LEVEL=INFO python my_script.py

```

This also provides details on the HTTP requests.

### Configuration issues [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#configuration-issues "Direct link to Configuration issues")

#### Getting validation errors [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#getting-validation-errors "Direct link to Getting validation errors")

When you are running the pipeline and getting a `DictValidationException`, it means that the [source configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#source-configuration) is incorrect. The error message provides details on the issue, including the path to the field and the expected type.

For example, if you have a source configuration like this:

```codeBlockLines_RjmQ
config: RESTAPIConfig = {
    "client": {
        # ...
    },
    "resources": [\
        {\
            "name": "issues",\
            "params": {             # <- Wrong: this should be inside\
                "sort": "updated",  #    the endpoint field below\
            },\
            "endpoint": {\
                "path": "issues",\
                # "params": {       # <- Correct configuration\
                #     "sort": "updated",\
                # },\
            },\
        },\
        # ...\
    ],
}

```

You will get an error like this:

```codeBlockLines_RjmQ
dlt.common.exceptions.DictValidationException: In path .: field 'resources[0]'
expects the following types: str, EndpointResource. Provided value {'name': 'issues', 'params': {'sort': 'updated'},
'endpoint': {'path': 'issues', ... }} with type 'dict' is invalid with the following errors:
For EndpointResource: In path ./resources[0]: following fields are unexpected {'params'}

```

It means that in the first resource configuration ( `resources[0]`), the `params` field should be inside the `endpoint` field.

tip

Import the `RESTAPIConfig` type from the `rest_api` module to have convenient hints in your editor/IDE and use it to define the configuration object.

```codeBlockLines_RjmQ
from dlt.sources.rest_api import RESTAPIConfig

```

#### Getting wrong data or no data [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#getting-wrong-data-or-no-data "Direct link to Getting wrong data or no data")

If incorrect data is received from an endpoint, check the `data_selector` field in the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration). Ensure the JSONPath is accurate and points to the correct data in the response body. `rest_api` attempts to auto-detect the data location, which may not always succeed. See the [data selection](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#data-selection) section for more details.

#### Getting insufficient data or incorrect pagination [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#getting-insufficient-data-or-incorrect-pagination "Direct link to Getting insufficient data or incorrect pagination")

Check the `paginator` field in the configuration. When not explicitly specified, the source tries to auto-detect the pagination method. If auto-detection fails, or the system is unsure, a warning is logged. For production environments, we recommend specifying an explicit paginator in the configuration. See the [pagination](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#pagination) section for more details. Some APIs may have non-standard pagination methods, and you may need to implement a [custom paginator](https://dlthub.com/docs/general-usage/http/rest-client#implementing-a-custom-paginator).

#### Incremental loading not working [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#incremental-loading-not-working "Direct link to Incremental loading not working")

See the [troubleshooting guide](https://dlthub.com/docs/general-usage/incremental/troubleshooting) for incremental loading issues.

#### Getting HTTP 404 errors [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#getting-http-404-errors "Direct link to Getting HTTP 404 errors")

Some APIs may return 404 errors for resources that do not exist or have no data. Manage these responses by configuring the `ignore` action in [response actions](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/advanced#response-actions).

### Authentication issues [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#authentication-issues "Direct link to Authentication issues")

If you are experiencing 401 (Unauthorized) errors, this could indicate:

- Incorrect authorization credentials. Verify credentials in the `secrets.toml`. Refer to [Secret and configs](https://dlthub.com/docs/general-usage/credentials/setup#troubleshoot-configuration-errors) for more information.
- An incorrect authentication type. Consult the API documentation for the proper method. See the [authentication](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#authentication) section for details. For some APIs, a [custom authentication method](https://dlthub.com/docs/general-usage/http/rest-client#implementing-custom-authentication) may be required.

### General guidelines [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#general-guidelines "Direct link to General guidelines")

The `rest_api` source uses the [RESTClient](https://dlthub.com/docs/general-usage/http/rest-client) class for HTTP requests. Refer to the RESTClient [troubleshooting guide](https://dlthub.com/docs/general-usage/http/rest-client#troubleshooting) for debugging tips.

For further assistance, join our [Slack community](https://dlthub.com/community). We're here to help!

- [Quick example](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#quick-example)
- [Setup](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#setup)
  - [Prerequisites](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#prerequisites)
  - [Initialize the REST API source](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#initialize-the-rest-api-source)
  - [Add credentials](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#add-credentials)
- [Run the pipeline](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#run-the-pipeline)
- [Source configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#source-configuration)
  - [Quick example](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#quick-example-1)
  - [Configuration structure](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#configuration-structure)
  - [Resource configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#resource-configuration)
  - [Endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration)
  - [Pagination](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#pagination)
  - [Data selection](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#data-selection)
  - [Authentication](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#authentication)
  - [Define resource relationships](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#define-resource-relationships)
  - [Define a resource which is not a REST endpoint](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#define-a-resource-which-is-not-a-rest-endpoint)
  - [Processing steps: filter and transform data](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#processing-steps-filter-and-transform-data)
- [Incremental loading](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#incremental-loading)
  - [Using placeholders for incremental loading](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#using-placeholders-for-incremental-loading)
  - [Legacy method: Incremental loading in `params` (DEPRECATED)](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#legacy-method-incremental-loading-in-params-deprecated)
  - [Incremental loading using the `incremental` field (DEPRECATED)](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#incremental-loading-using-the-incremental-field-deprecated)
  - [Convert the incremental value before calling the API](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#convert-the-incremental-value-before-calling-the-api)
- [Troubleshooting](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#troubleshooting)
  - [Configuration issues](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#configuration-issues)
  - [Authentication issues](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#authentication-issues)
  - [General guidelines](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#general-guidelines)

----- https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/ -----

Version: 1.11.0 (latest)

You can use the REST API source to extract data from any REST API. Using a [declarative configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#source-configuration), you can define:

- the API endpoints to pull data from,
- their [relationships](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#define-resource-relationships),
- how to handle [pagination](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#pagination),
- [authentication](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#authentication).

dlt will take care of the rest: unnesting the data, inferring the schema, etc., and writing to the destination.

[**📄️REST API source** \\
Learn how to set up and configure](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic)[**📄️Advanced configuration** \\
Learn custom response processing](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/advanced)[**🗃️REST API helpers** \\
2 items](https://dlthub.com/docs/general-usage/http/overview)

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

----- https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic -----

Version: 1.11.0 (latest)

On this page

Need help deploying these sources or figuring out how to run them in your data stack?

[Join our Slack community](https://dlthub.com/community) or [Get in touch](https://dlthub.com/contact) with the dltHub Customer Success team.

This is a dlt source you can use to extract data from any REST API. It uses [declarative configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#source-configuration) to define the API endpoints, their [relationships](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#define-resource-relationships), how to handle [pagination](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#pagination), and [authentication](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#authentication).

### Quick example [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#quick-example "Direct link to Quick example")

Here's an example of how to configure the REST API source to load posts and related comments from a hypothetical blog API:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.rest_api import rest_api_source

source = rest_api_source({
    "client": {
        "base_url": "https://api.example.com/",
        "auth": {
            "token": dlt.secrets["your_api_token"],
        },
        "paginator": {
            "type": "json_link",
            "next_url_path": "paging.next",
        },
    },
    "resources": [\
        # "posts" will be used as the endpoint path, the resource name,\
        # and the table name in the destination. The HTTP client will send\
        # a request to "https://api.example.com/posts".\
        "posts",\
\
        # The explicit configuration allows you to link resources\
        # and define query string parameters.\
        {\
            "name": "comments",\
            "endpoint": {\
                "path": "posts/{resources.posts.id}/comments",\
                "params": {\
                    "sort": "created_at",\
                },\
            },\
        },\
    ],
})

pipeline = dlt.pipeline(
    pipeline_name="rest_api_example",
    destination="duckdb",
    dataset_name="rest_api_data",
)

load_info = pipeline.run(source)

```

Running this pipeline will create two tables in DuckDB: `posts` and `comments` with the data from the respective API endpoints. The `comments` resource will fetch comments for each post by using the `id` field from the `posts` resource.

## Setup [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#setup "Direct link to Setup")

### Prerequisites [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#prerequisites "Direct link to Prerequisites")

Please make sure the `dlt` library is installed. Refer to the [installation guide](https://dlthub.com/docs/intro).

### Initialize the REST API source [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#initialize-the-rest-api-source "Direct link to Initialize the REST API source")

Enter the following command in your terminal:

```codeBlockLines_RjmQ
dlt init rest_api duckdb

```

[dlt init](https://dlthub.com/docs/reference/command-line-interface) will initialize the pipeline examples for REST API as the [source](https://dlthub.com/docs/general-usage/source) and [duckdb](https://dlthub.com/docs/dlt-ecosystem/destinations/duckdb) as the [destination](https://dlthub.com/docs/dlt-ecosystem/destinations).

Running `dlt init` creates the following in the current folder:

- `rest_api_pipeline.py` file with a sample pipelines definition:
  - GitHub API example
  - Pokemon API example
- `.dlt` folder with:
  - `secrets.toml` file to store your access tokens and other sensitive information
  - `config.toml` file to store the configuration settings
- `requirements.txt` file with the required dependencies

Change the REST API source to your needs by modifying the `rest_api_pipeline.py` file. See the detailed [source configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#source-configuration) section below.

note

For the rest of the guide, we will use the [GitHub API](https://docs.github.com/en/rest?apiVersion=2022-11-28) and [Pokemon API](https://pokeapi.co/) as example sources.

This source is based on the [RESTClient class](https://dlthub.com/docs/general-usage/http/rest-client).

### Add credentials [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#add-credentials "Direct link to Add credentials")

In the `.dlt` folder, you'll find a file called `secrets.toml`, where you can securely store your access tokens and other sensitive information. It's important to handle this file with care and keep it safe.

The GitHub API [requires an access token](https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api?apiVersion=2022-11-28) to access some of its endpoints and to increase the rate limit for the API calls. To get a GitHub token, follow the GitHub documentation on [managing your personal access tokens](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens).

After you get the token, add it to the `secrets.toml` file:

```codeBlockLines_RjmQ
[sources.rest_api_pipeline.github_source]
github_token = "your_github_token"

```

## Run the pipeline [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#run-the-pipeline "Direct link to Run the pipeline")

1. Install the required dependencies by running the following command:

```codeBlockLines_RjmQ
pip install -r requirements.txt

```

2. Run the pipeline:

```codeBlockLines_RjmQ
python rest_api_pipeline.py

```

3. Verify that everything loaded correctly by using the following command:

```codeBlockLines_RjmQ
dlt pipeline rest_api show

```

## Source configuration [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#source-configuration "Direct link to Source configuration")

### Quick example [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#quick-example-1 "Direct link to Quick example")

Let's take a look at the GitHub example in the `rest_api_pipeline.py` file:

```codeBlockLines_RjmQ
from dlt.sources.rest_api import RESTAPIConfig, rest_api_resources

@dlt.source
def github_source(github_token=dlt.secrets.value):
    config: RESTAPIConfig = {
        "client": {
            "base_url": "https://api.github.com/repos/dlt-hub/dlt/",
            "auth": {
                "token": github_token,
            },
        },
        "resource_defaults": {
            "primary_key": "id",
            "write_disposition": "merge",
            "endpoint": {
                "params": {
                    "per_page": 100,
                },
            },
        },
        "resources": [\
            {\
                "name": "issues",\
                "endpoint": {\
                    "path": "issues",\
                    "params": {\
                        "sort": "updated",\
                        "direction": "desc",\
                        "state": "open",\
                        "since": {\
                            "type": "incremental",\
                            "cursor_path": "updated_at",\
                            "initial_value": "2024-01-25T11:21:28Z",\
                        },\
                    },\
                },\
            },\
            {\
                "name": "issue_comments",\
                "endpoint": {\
                    "path": "issues/{resources.issues.number}/comments",\
                },\
                "include_from_parent": ["id"],\
            },\
        ],
    }

    yield from rest_api_resources(config)

def load_github() -> None:
    pipeline = dlt.pipeline(
        pipeline_name="rest_api_github",
        destination="duckdb",
        dataset_name="rest_api_data",
    )

    load_info = pipeline.run(github_source())
    print(load_info)

```

The declarative resource configuration is defined in the `config` dictionary. It contains the following key components:

1. `client`: Defines the base URL and authentication method for the API. In this case, it uses token-based authentication. The token is stored in the `secrets.toml` file.

2. `resource_defaults`: Contains default settings for all [resources](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#resource-configuration). In this example, we define that all resources:
   - Have `id` as the [primary key](https://dlthub.com/docs/general-usage/resource#define-schema)
   - Use the `merge` [write disposition](https://dlthub.com/docs/general-usage/incremental-loading#choosing-a-write-disposition) to merge the data with the existing data in the destination.
   - Send a `per_page=100` query parameter with each request to get more results per page.
3. `resources`: A list of [resources](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#resource-configuration) to be loaded. Here, we have two resources: `issues` and `issue_comments`, which correspond to the GitHub API endpoints for [repository issues](https://docs.github.com/en/rest/issues/issues?apiVersion=2022-11-28#list-repository-issues) and [issue comments](https://docs.github.com/en/rest/issues/comments?apiVersion=2022-11-28#list-issue-comments). Note that we need an issue number to fetch comments for each issue. This number is taken from the `issues` resource. More on this in the [resource relationships](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#define-resource-relationships) section.

Let's break down the configuration in more detail.

### Configuration structure [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#configuration-structure "Direct link to Configuration structure")

tip

Import the `RESTAPIConfig` type from the `rest_api` module to have convenient hints in your editor/IDE and use it to define the configuration object.

```codeBlockLines_RjmQ
from dlt.sources.rest_api import RESTAPIConfig

```

The configuration object passed to the REST API Generic Source has three main elements:

```codeBlockLines_RjmQ
config: RESTAPIConfig = {
    "client": {
        # ...
    },
    "resource_defaults": {
        # ...
    },
    "resources": [\
        # ...\
    ],
}

```

#### `client` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#client "Direct link to client")

The `client` configuration is used to connect to the API's endpoints. It includes the following fields:

- `base_url` (str): The base URL of the API. This string is prepended to all endpoint paths. For example, if the base URL is `https://api.example.com/v1/`, and the endpoint path is `users`, the full URL will be `https://api.example.com/v1/users`.
- `headers` (dict, optional): Additional headers that are sent with each request.
- `auth` (optional): Authentication configuration. This can be a simple token, an `AuthConfigBase` object, or a more complex authentication method.
- `paginator` (optional): Configuration for the default pagination used for resources that support pagination. Refer to the [pagination](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#pagination) section for more details.

#### `resource_defaults` (optional) [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#resource_defaults-optional "Direct link to resource_defaults-optional")

`resource_defaults` contains the default values to [configure the dlt resources](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#resource-configuration). This configuration is applied to all resources unless overridden by the resource-specific configuration.

For example, you can set the primary key, write disposition, and other default settings here:

```codeBlockLines_RjmQ
config = {
    "client": {
        # ...
    },
    "resource_defaults": {
        "primary_key": "id",
        "write_disposition": "merge",
        "endpoint": {
            "params": {
                "per_page": 100,
            },
        },
    },
    "resources": [\
        "resource1",\
        {\
            "name": "resource2_name",\
            "write_disposition": "append",\
            "endpoint": {\
                "params": {\
                    "param1": "value1",\
                },\
            },\
        }\
    ],
}

```

Above, all resources will have `primary_key` set to `id`, `resource1` will have `write_disposition` set to `merge`, and `resource2` will override the default `write_disposition` with `append`.
Both `resource1` and `resource2` will have the `per_page` parameter set to 100.

#### `resources` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#resources "Direct link to resources")

This is a list of resource configurations that define the API endpoints to be loaded. Each resource configuration can be:

- a dictionary with the [resource configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#resource-configuration).
- a string. In this case, the string is used as both the endpoint path and the resource name, and the resource configuration is taken from the `resource_defaults` configuration if it exists.

### Resource configuration [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#resource-configuration "Direct link to Resource configuration")

A resource configuration is used to define a [dlt resource](https://dlthub.com/docs/general-usage/resource) for the data to be loaded from an API endpoint. It contains the following key fields:

- `endpoint`: The endpoint configuration for the resource. It can be a string or a dict representing the endpoint settings. See the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) section for more details.
- `write_disposition`: The write disposition for the resource.
- `primary_key`: The primary key for the resource.
- `include_from_parent`: A list of fields from the parent resource to be included in the resource output. See the [resource relationships](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#include-fields-from-the-parent-resource) section for more details.
- `processing_steps`: A list of [processing steps](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#processing-steps-filter-and-transform-data) to filter and transform your data.
- `selected`: A flag to indicate if the resource is selected for loading. This could be useful when you want to load data only from child resources and not from the parent resource.
- `auth`: An optional `AuthConfig` instance. If passed, is used over the one defined in the [client](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#client) definition. Example:

```codeBlockLines_RjmQ
from dlt.sources.helpers.rest_client.auth import HttpBasicAuth

config = {
    "client": {
        "auth": {
            "type": "bearer",
            "token": dlt.secrets["your_api_token"],
        }
    },
    "resources": [\
        "resource-using-bearer-auth",\
        {\
            "name": "my-resource-with-special-auth",\
            "endpoint": {\
                # ...\
                "auth": HttpBasicAuth("user", dlt.secrets["your_basic_auth_password"])\
            },\
            # ...\
        }\
    ]
    # ...
}

```

This would use `Bearer` auth as defined in the `client` for `resource-using-bearer-auth` and `Http Basic` auth for `my-resource-with-special-auth`.

You can also pass additional resource parameters that will be used to configure the dlt resource. See [dlt resource API reference](https://dlthub.com/docs/api_reference/dlt/extract/decorators#resource) for more details.

### Endpoint configuration [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#endpoint-configuration "Direct link to Endpoint configuration")

The endpoint configuration defines how to query the API endpoint. Quick example:

```codeBlockLines_RjmQ
{
    "path": "issues",
    "method": "GET",
    "params": {
        "sort": "updated",
        "direction": "desc",
        "state": "open",
        "since": {
            "type": "incremental",
            "cursor_path": "updated_at",
            "initial_value": "2024-01-25T11:21:28Z",
        },
    },
    "data_selector": "results",
}

```

The fields in the endpoint configuration are:

- `path`: The path to the API endpoint. By default this path is appended to the given `base_url`. If this is a fully qualified URL starting with `http:` or `https:` it will be
used as-is and `base_url` will be ignored.
- `method`: The HTTP method to be used. The default is `GET`.
- `params`: Query parameters to be sent with each request. For example, `sort` to order the results or `since` to specify [incremental loading](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#incremental-loading). This is also may be used to define [resource relationships](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#define-resource-relationships).
- `json`: The JSON payload to be sent with the request (for POST and PUT requests).
- `paginator`: Pagination configuration for the endpoint. See the [pagination](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#pagination) section for more details.
- `data_selector`: A JSONPath to select the data from the response. See the [data selection](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#data-selection) section for more details.
- `response_actions`: A list of actions that define how to process the response data. See the [response actions](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/advanced#response-actions) section for more details.
- `incremental`: Configuration for [incremental loading](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#incremental-loading).

### Pagination [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#pagination "Direct link to Pagination")

The REST API source will try to automatically handle pagination for you. This works by detecting the pagination details from the first API response.

In some special cases, you may need to specify the pagination configuration explicitly.

To specify the pagination configuration, use the `paginator` field in the [client](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#client) or [endpoint](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) configurations. You may either use a dictionary with a string alias in the `type` field along with the required parameters, or use a [paginator class instance](https://dlthub.com/docs/general-usage/http/rest-client#paginators).

#### Example [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#example "Direct link to Example")

Suppose the API response for `https://api.example.com/posts` contains a `next` field with the URL to the next page:

```codeBlockLines_RjmQ
{
    "data": [\
        {"id": 1, "title": "Post 1"},\
        {"id": 2, "title": "Post 2"},\
        {"id": 3, "title": "Post 3"}\
    ],
    "pagination": {
        "next": "https://api.example.com/posts?page=2"
    }
}

```

You can configure the pagination for the `posts` resource like this:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "paginator": {
        "type": "json_link",
        "next_url_path": "pagination.next",
    }
}

```

Alternatively, you can use the paginator instance directly:

```codeBlockLines_RjmQ
from dlt.sources.helpers.rest_client.paginators import JSONLinkPaginator

# ...

{
    "path": "posts",
    "paginator": JSONLinkPaginator(
        next_url_path="pagination.next"
    ),
}

```

note

Currently, pagination is supported only for GET requests. To handle POST requests with pagination, you need to implement a [custom paginator](https://dlthub.com/docs/general-usage/http/rest-client#implementing-a-custom-paginator).

These are the available paginators:

| `type` | Paginator class | Description |
| --- | --- | --- |
| `json_link` | [JSONLinkPaginator](https://dlthub.com/docs/general-usage/http/rest-client#jsonlinkpaginator) | The link to the next page is in the body (JSON) of the response.<br>_Parameters:_ <br>- `next_url_path` (str) - the JSONPath to the next page URL |
| `header_link` | [HeaderLinkPaginator](https://dlthub.com/docs/general-usage/http/rest-client#headerlinkpaginator) | The links to the next page are in the response headers.<br>_Parameters:_ <br>- `links_next_key` (str) - the name of the header containing the links. Default is "next". |
| `offset` | [OffsetPaginator](https://dlthub.com/docs/general-usage/http/rest-client#offsetpaginator) | The pagination is based on an offset parameter, with the total items count either in the response body or explicitly provided.<br>_Parameters:_ <br>- `limit` (int) - the maximum number of items to retrieve in each request<br>- `offset` (int) - the initial offset for the first request. Defaults to `0`<br>- `offset_param` (str) - the name of the query parameter used to specify the offset. Defaults to "offset"<br>- `limit_param` (str) - the name of the query parameter used to specify the limit. Defaults to "limit"<br>- `total_path` (str) - a JSONPath expression for the total number of items. If not provided, pagination is controlled by `maximum_offset` and `stop_after_empty_page`<br>- `maximum_offset` (int) - optional maximum offset value. Limits pagination even without total count<br>- `stop_after_empty_page` (bool) - Whether pagination should stop when a page contains no result items. Defaults to `True` |
| `page_number` | [PageNumberPaginator](https://dlthub.com/docs/general-usage/http/rest-client#pagenumberpaginator) | The pagination is based on a page number parameter, with the total pages count either in the response body or explicitly provided.<br>_Parameters:_ <br>- `base_page` (int) - the starting page number. Defaults to `0`<br>- `page_param` (str) - the query parameter name for the page number. Defaults to "page"<br>- `total_path` (str) - a JSONPath expression for the total number of pages. If not provided, pagination is controlled by `maximum_page` and `stop_after_empty_page`<br>- `maximum_page` (int) - optional maximum page number. Stops pagination once this page is reached<br>- `stop_after_empty_page` (bool) - Whether pagination should stop when a page contains no result items. Defaults to `True` |
| `cursor` | [JSONResponseCursorPaginator](https://dlthub.com/docs/general-usage/http/rest-client#jsonresponsecursorpaginator) | The pagination is based on a cursor parameter, with the value of the cursor in the response body (JSON).<br>_Parameters:_ <br>- `cursor_path` (str) - the JSONPath to the cursor value. Defaults to "cursors.next"<br>- `cursor_param` (str) - the query parameter name for the cursor. Defaults to "cursor" if neither `cursor_param` nor `cursor_body_path` is provided.<br>- `cursor_body_path` (str, optional) - the JSONPath to place the cursor in the request body.<br>Note: You must provide either `cursor_param` or `cursor_body_path`, but not both. If neither is provided, `cursor_param` will default to "cursor". |
| `single_page` | SinglePagePaginator | The response will be interpreted as a single-page response, ignoring possible pagination metadata. |
| `auto` | `None` | Explicitly specify that the source should automatically detect the pagination method. |

For more complex pagination methods, you can implement a [custom paginator](https://dlthub.com/docs/general-usage/http/rest-client#implementing-a-custom-paginator), instantiate it, and use it in the configuration.

Alternatively, you can use the dictionary configuration syntax also for custom paginators. For this, you need to register your custom paginator:

```codeBlockLines_RjmQ
from dlt.sources.rest_api.config_setup import register_paginator

class CustomPaginator(SinglePagePaginator):
    # custom implementation of SinglePagePaginator
    pass

register_paginator("custom_paginator", CustomPaginator)

{
    # ...
    "paginator": {
        "type": "custom_paginator",
        "next_url_path": "paging.nextLink",
    }
}

```

### Data selection [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#data-selection "Direct link to Data selection")

The `data_selector` field in the endpoint configuration allows you to specify a JSONPath to select the data from the response. By default, the source will try to detect the locations of the data automatically.

Use this field when you need to specify the location of the data in the response explicitly.

For example, if the API response looks like this:

```codeBlockLines_RjmQ
{
    "posts": [\
        {"id": 1, "title": "Post 1"},\
        {"id": 2, "title": "Post 2"},\
        {"id": 3, "title": "Post 3"}\
    ]
}

```

You can use the following endpoint configuration:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "posts",
}

```

For a nested structure like this:

```codeBlockLines_RjmQ
{
    "results": {
        "posts": [\
            {"id": 1, "title": "Post 1"},\
            {"id": 2, "title": "Post 2"},\
            {"id": 3, "title": "Post 3"}\
        ]
    }
}

```

You can use the following endpoint configuration:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "results.posts",
}

```

Read more about [JSONPath syntax](https://github.com/h2non/jsonpath-ng?tab=readme-ov-file#jsonpath-syntax) to learn how to write selectors.

### Authentication [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#authentication "Direct link to Authentication")

For APIs that require authentication to access their endpoints, the REST API source supports various authentication methods, including token-based authentication, query parameters, basic authentication, and custom authentication. The authentication configuration is specified in the `auth` field of the [client](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#client) either as a dictionary or as an instance of the [authentication class](https://dlthub.com/docs/general-usage/http/rest-client#authentication).

#### Quick example [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#quick-example-2 "Direct link to Quick example")

Here's how to configure authentication using a bearer token:

```codeBlockLines_RjmQ
{
    "client": {
        # ...
        "auth": {
            "type": "bearer",
            "token": dlt.secrets["your_api_token"],
        },
        # ...
    },
}

```

Alternatively, you can use the authentication class directly:

```codeBlockLines_RjmQ
from dlt.sources.helpers.rest_client.auth import BearerTokenAuth

config = {
    "client": {
        "auth": BearerTokenAuth(dlt.secrets["your_api_token"]),
    },
    "resources": [\
    ]
    # ...
}

```

Since token-based authentication is one of the most common methods, you can use the following shortcut:

```codeBlockLines_RjmQ
{
    "client": {
        # ...
        "auth": {
            "token": dlt.secrets["your_api_token"],
        },
        # ...
    },
}

```

warning

Make sure to store your access tokens and other sensitive information in the `secrets.toml` file and never commit it to the version control system.

Available authentication types:

| `type` | Authentication class | Description |
| --- | --- | --- |
| `bearer` | [BearerTokenAuth](https://dlthub.com/docs/general-usage/http/rest-client#bearer-token-authentication) | Bearer token authentication.<br>Parameters:<br>- `token` (str) |
| `http_basic` | [HTTPBasicAuth](https://dlthub.com/docs/general-usage/http/rest-client#http-basic-authentication) | Basic HTTP authentication.<br>Parameters:<br>- `username` (str)<br>- `password` (str) |
| `api_key` | [APIKeyAuth](https://dlthub.com/docs/general-usage/http/rest-client#api-key-authentication) | API key authentication with key defined in the query parameters or in the headers. <br>Parameters:<br>- `name` (str) - the name of the query parameter or header<br>- `api_key` (str) - the API key value<br>- `location` (str, optional) - the location of the API key in the request. Can be `query` or `header`. Default is `header` |
| `oauth2_client_credentials` | [OAuth2ClientCredentials](https://dlthub.com/docs/general-usage/http/rest-client#oauth-20-authorization) | OAuth 2.0 Client Credentials authorization for server-to-server communication without user consent. <br>Parameters:<br>- `access_token` (str, optional) - the temporary token. Usually not provided here because it is automatically obtained from the server by exchanging `client_id` and `client_secret`. Default is `None`<br>- `access_token_url` (str) - the URL to request the `access_token` from<br>- `client_id` (str) - identifier for your app. Usually issued via a developer portal<br>- `client_secret` (str) - client credential to obtain authorization. Usually issued via a developer portal<br>- `access_token_request_data` (dict, optional) - A dictionary with data required by the authorization server apart from the `client_id`, `client_secret`, and `"grant_type": "client_credentials"`. Defaults to `None`<br>- `default_token_expiration` (int, optional) - The time in seconds after which the temporary access token expires. Defaults to 3600.<br>- `session` (requests.Session, optional) - a custom session object. Mostly used for testing |

For more complex authentication methods, you can implement a [custom authentication class](https://dlthub.com/docs/general-usage/http/rest-client#implementing-custom-authentication) and use it in the configuration.

You can use the dictionary configuration syntax also for custom authentication classes after registering them as follows:

```codeBlockLines_RjmQ
from dlt.sources.rest_api.config_setup import register_auth

class CustomAuth(AuthConfigBase):
    pass

register_auth("custom_auth", CustomAuth)

{
    # ...
    "auth": {
        "type": "custom_auth",
        "api_key": dlt.secrets["sources.my_source.my_api_key"],
    }
}

```

### Define resource relationships [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#define-resource-relationships "Direct link to Define resource relationships")

When you have a resource that depends on another resource (for example, you must fetch a parent resource to get an ID needed to fetch the child), you can reference fields in the parent resource using special placeholders.
This allows you to link one or more [path](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#via-request-path), [query string](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#via-query-string-parameters) or [JSON body](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#via-json-body) parameters in the child resource to fields in the parent resource's data.

#### Via request path [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#via-request-path "Direct link to Via request path")

In the GitHub example, the `issue_comments` resource depends on the `issues` resource. The `resources.issues.number` placeholder links the `number` field in the `issues` resource data to the current request's path parameter.

```codeBlockLines_RjmQ
{
    "resources": [\
        {\
            "name": "issues",\
            "endpoint": {\
                "path": "issues",\
                # ...\
            },\
        },\
        {\
            "name": "issue_comments",\
            "endpoint": {\
                "path": "issues/{resources.issues.number}/comments",\
            },\
            "include_from_parent": ["id"],\
        },\
    ],
}

```

This configuration tells the source to get issue numbers from the `issues` resource data and use them to fetch comments for each issue number. So for each issue item, `"{resources.issues.number}"` is replaced by the issue number in the request path.
For example, if the `issues` resource yields the following data:

```codeBlockLines_RjmQ
[\
    {"id": 1, "number": 123},\
    {"id": 2, "number": 124},\
    {"id": 3, "number": 125}\
]

```

The `issue_comments` resource will make requests to the following endpoints:

- `issues/123/comments`
- `issues/124/comments`
- `issues/125/comments`

The syntax for the placeholder is `resources.<parent_resource_name>.<field_name>`.

#### Via query string parameters [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#via-query-string-parameters "Direct link to Via query string parameters")

The placeholder syntax can also be used in the query string parameters. For example, in an API which lets you fetch a blog posts (via `/posts`) and their comments (via `/comments?post_id=<post_id>`), you can define a resource `posts` and a resource `post_comments` which depends on the `posts` resource. You can then reference the `id` field from the `posts` resource in the `post_comments` resource:

```codeBlockLines_RjmQ
{
    "resources": [\
        "posts",\
        {\
            "name": "post_comments",\
            "endpoint": {\
                "path": "comments",\
                "params": {\
                    "post_id": "{resources.posts.id}",\
                },\
            },\
        },\
    ],
}

```

Similar to the GitHub example above, if the `posts` resource yields the following data:

```codeBlockLines_RjmQ
[\
    {"id": 1, "title": "Post 1"},\
    {"id": 2, "title": "Post 2"},\
    {"id": 3, "title": "Post 3"}\
]

```

The `post_comments` resource will make requests to the following endpoints:

- `comments?post_id=1`
- `comments?post_id=2`
- `comments?post_id=3`

#### Via JSON body [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#via-json-body "Direct link to Via JSON body")

In many APIs, you can send a complex query or configuration through a POST request's JSON body rather than in the request path or query parameters. For example, consider an imaginary `/search` endpoint that supports multiple filters and settings. You might have a parent resource `posts` with each post's `id` and a second resource, `post_details`, that uses `id` to perform a custom search.

In the example below we reference the `posts` resource's `id` field in the JSON body via placeholders:

```codeBlockLines_RjmQ
{
    "resources": [\
        "posts",\
        {\
            "name": "post_details",\
            "endpoint": {\
                "path": "search",\
                "method": "POST",\
                "json": {\
                    "filters": {\
                        "id": "{resources.posts.id}",\
                    },\
                    "order": "desc",\
                    "limit": 5,\
                }\
            },\
        },\
    ],
}

```

#### Legacy syntax: `resolve` field in parameter configuration [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#legacy-syntax-resolve-field-in-parameter-configuration "Direct link to legacy-syntax-resolve-field-in-parameter-configuration")

warning

`resolve` works only for path parameters. The new placeholder syntax is more flexible and recommended for new configurations.

An alternative, legacy way to define resource relationships is to use the `resolve` field in the parameter configuration.
Here's the same example as above that uses the `resolve` field:

```codeBlockLines_RjmQ
{
    "resources": [\
        {\
            "name": "issues",\
            "endpoint": {\
                "path": "issues",\
                # ...\
            },\
        },\
        {\
            "name": "issue_comments",\
            "endpoint": {\
                "path": "issues/{issue_number}/comments",\
                "params": {\
                    "issue_number": {\
                        "type": "resolve",\
                        "resource": "issues",\
                        "field": "number",\
                    }\
                },\
            },\
            "include_from_parent": ["id"],\
        },\
    ],
}

```

The syntax for the `resolve` field in parameter configuration is:

```codeBlockLines_RjmQ
{
    "<parameter_name>": {
        "type": "resolve",
        "resource": "<parent_resource_name>",
        "field": "<parent_resource_field_name_or_jsonpath>",
    }
}

```

The `field` value can be specified as a [JSONPath](https://github.com/h2non/jsonpath-ng?tab=readme-ov-file#jsonpath-syntax) to select a nested field in the parent resource data. For example: `"field": "items[0].id"`.

#### Resolving multiple path parameters from a parent resource [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#resolving-multiple-path-parameters-from-a-parent-resource "Direct link to Resolving multiple path parameters from a parent resource")

When a child resource depends on multiple fields from a single parent resource, you can define multiple `resolve` parameters in the endpoint configuration. For example:

```codeBlockLines_RjmQ
{
    "resources": [\
        "groups",\
        {\
            "name": "users",\
            "endpoint": {\
                "path": "groups/{group_id}/users",\
                "params": {\
                    "group_id": {\
                        "type": "resolve",\
                        "resource": "groups",\
                        "field": "id",\
                    },\
                },\
            },\
        },\
        {\
            "name": "user_details",\
            "endpoint": {\
                "path": "groups/{group_id}/users/{user_id}/details",\
                "params": {\
                    "group_id": {\
                        "type": "resolve",\
                        "resource": "users",\
                        "field": "group_id",\
                    },\
                    "user_id": {\
                        "type": "resolve",\
                        "resource": "users",\
                        "field": "id",\
                    },\
                },\
            },\
        },\
    ],
}

```

In the configuration above:

- The `users` resource depends on the `groups` resource, resolving the `group_id` parameter from the `id` field in `groups`.
- The `user_details` resource depends on the `users` resource, resolving both `group_id` and `user_id` parameters from fields in `users`.

#### Include fields from the parent resource [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#include-fields-from-the-parent-resource "Direct link to Include fields from the parent resource")

You can include data from the parent resource in the child resource by using the `include_from_parent` field in the resource configuration. For example:

```codeBlockLines_RjmQ
{
    "name": "issue_comments",
    "endpoint": {
        ...
    },
    "include_from_parent": ["id", "title", "created_at"],
}

```

This will include the `id`, `title`, and `created_at` fields from the `issues` resource in the `issue_comments` resource data. The names of the included fields will be prefixed with the parent resource name and an underscore ( `_`) like so: `_issues_id`, `_issues_title`, `_issues_created_at`.

### Define a resource which is not a REST endpoint [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#define-a-resource-which-is-not-a-rest-endpoint "Direct link to Define a resource which is not a REST endpoint")

Sometimes, we want to request endpoints with specific values that are not returned by another endpoint.
Thus, you can also include arbitrary dlt resources in your `RESTAPIConfig` instead of defining a resource for every path!

In the following example, we want to load the issues belonging to three repositories.
Instead of defining three different issues resources, one for each of the paths `dlt-hub/dlt/issues/`, `dlt-hub/verified-sources/issues/`, `dlt-hub/dlthub-education/issues/`, we have a resource `repositories` which yields a list of repository names that will be fetched by the dependent resource `issues`.

```codeBlockLines_RjmQ
from dlt.sources.rest_api import RESTAPIConfig

@dlt.resource()
def repositories() -> Generator[List[Dict[str, Any]], Any, Any]:
    """A seed list of repositories to fetch"""
    yield [{"name": "dlt"}, {"name": "verified-sources"}, {"name": "dlthub-education"}]

config: RESTAPIConfig = {
    "client": {"base_url": "https://github.com/api/v2"},
    "resources": [\
        {\
            "name": "issues",\
            "endpoint": {\
                "path": "dlt-hub/{repository}/issues/",\
                "params": {\
                    "repository": {\
                        "type": "resolve",\
                        "resource": "repositories",\
                        "field": "name",\
                    },\
                },\
            },\
        },\
        repositories(),\
    ],
}

```

Be careful that the parent resource needs to return `Generator[List[Dict[str, Any]]]`. Thus, the following will NOT work:

```codeBlockLines_RjmQ
@dlt.resource
def repositories() -> Generator[Dict[str, Any], Any, Any]:
    """Not working seed list of repositories to fetch"""
    yield from [{"name": "dlt"}, {"name": "verified-sources"}, {"name": "dlthub-education"}]

```

### Processing steps: filter and transform data [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#processing-steps-filter-and-transform-data "Direct link to Processing steps: filter and transform data")

The `processing_steps` field in the resource configuration allows you to apply transformations to the data fetched from the API before it is loaded into your destination. This is useful when you need to filter out certain records, modify the data structure, or anonymize sensitive information.

Each processing step is a dictionary specifying the type of operation ( `filter` or `map`) and the function to apply. Steps apply in the order they are listed.

#### Quick example [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#quick-example-3 "Direct link to Quick example")

```codeBlockLines_RjmQ
def lower_title(record):
    record["title"] = record["title"].lower()
    return record

config: RESTAPIConfig = {
    "client": {
        "base_url": "https://api.example.com",
    },
    "resources": [\
        {\
            "name": "posts",\
            "processing_steps": [\
                {"filter": lambda x: x["id"] < 10},\
                {"map": lower_title},\
            ],\
        },\
    ],
}

```

In the example above:

- First, the `filter` step uses a lambda function to include only records where `id` is less than 10.
- Thereafter, the `map` step applies the `lower_title` function to each remaining record.

#### Using `filter` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#using-filter "Direct link to using-filter")

The `filter` step allows you to exclude records that do not meet certain criteria. The provided function should return `True` to keep the record or `False` to exclude it:

```codeBlockLines_RjmQ
{
    "name": "posts",
    "endpoint": "posts",
    "processing_steps": [\
        {"filter": lambda x: x["id"] in [10, 20, 30]},\
    ],
}

```

In this example, only records with `id` equal to 10, 20, or 30 will be included.

#### Using `map` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#using-map "Direct link to using-map")

The `map` step allows you to modify the records fetched from the API. The provided function should take a record as an argument and return the modified record. For example, to anonymize the `email` field:

```codeBlockLines_RjmQ
def anonymize_email(record):
    record["email"] = "REDACTED"
    return record

config: RESTAPIConfig = {
    "client": {
        "base_url": "https://api.example.com",
    },
    "resources": [\
        {\
            "name": "users",\
            "processing_steps": [\
                {"map": anonymize_email},\
            ],\
        },\
    ],
}

```

#### Combining `filter` and `map` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#combining-filter-and-map "Direct link to combining-filter-and-map")

You can combine multiple processing steps to achieve complex transformations:

```codeBlockLines_RjmQ
{
    "name": "posts",
    "endpoint": "posts",
    "processing_steps": [\
        {"filter": lambda x: x["id"] < 10},\
        {"map": lower_title},\
        {"filter": lambda x: "important" in x["title"]},\
    ],
}

```

tip

#### Best practices [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#best-practices "Direct link to Best practices")

1. Order matters: Processing steps are applied in the order they are listed. Be mindful of the sequence, especially when combining `map` and `filter`.
2. Function definition: Define your filter and map functions separately for clarity and reuse.
3. Use `filter` to exclude records early in the process to reduce the amount of data that needs to be processed.
4. Combine consecutive `map` steps into a single function for faster execution.

## Incremental loading [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#incremental-loading "Direct link to Incremental loading")

Some APIs provide a way to fetch only new or changed data (most often by using a timestamp field like `updated_at`, `created_at`, or incremental IDs).
This is called [incremental loading](https://dlthub.com/docs/general-usage/incremental-loading) and is very useful as it allows you to reduce the load time and the amount of data transferred.

Let's continue with our imaginary blog API example to understand incremental loading with query parameters.

Imagine we have the following endpoint `https://api.example.com/posts` and it:

1. Accepts a `created_since` query parameter to fetch blog posts created after a certain date.
2. Returns a list of posts with the `created_at` field for each post.

For example, if we query the endpoint with GET request `https://api.example.com/posts?created_since=2024-01-25`, we get the following response:

```codeBlockLines_RjmQ
{
    "results": [\
        {"id": 1, "title": "Post 1", "created_at": "2024-01-26"},\
        {"id": 2, "title": "Post 2", "created_at": "2024-01-27"},\
        {"id": 3, "title": "Post 3", "created_at": "2024-01-28"}\
    ]
}

```

When the API endpoint supports incremental loading, you can configure dlt to load only the new or changed data using these three methods:

1. Using [placeholders for incremental loading](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#using-placeholders-for-incremental-loading)
2. Defining a special parameter in the `params` section of the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) (DEPRECATED)
3. Using the `incremental` field in the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) with the `start_param` field (DEPRECATED)

caution

The last two methods are deprecated and will be removed in a future dlt version.

### Using placeholders for incremental loading [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#using-placeholders-for-incremental-loading "Direct link to Using placeholders for incremental loading")

The most flexible way to configure incremental loading is to use placeholders in the request configuration along with the `incremental` section.
Here's how it works:

1. Define the `incremental` section in the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) to specify the cursor path (where to find the incremental value in the response) and initial value (the value to start the incremental loading from).
2. Use the placeholder `{incremental.start_value}` in the request configuration to reference the incremental value.

Let's take the example from the previous section and configure it using placeholders:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "results",
    "params": {
        "created_since": "{incremental.start_value}",  # Uses cursor value in query parameter
    },
    "incremental": {
        "cursor_path": "created_at",
        "initial_value": "2024-01-25T00:00:00Z",
    },
}

```

When you first run this pipeline, dlt will:

1. Replace `{incremental.start_value}` with `2024-01-25T00:00:00Z` (the initial value)
2. Make a GET request to `https://api.example.com/posts?created_since=2024-01-25T00:00:00Z`
3. Parse the response (e.g., posts with created\_at values like "2024-01-26", "2024-01-27", "2024-01-28")
4. Track the maximum value found in the "created\_at" field (in this case, "2024-01-28")

On the next pipeline run, dlt will:

1. Replace `{incremental.start_value}` with "2024-01-28" (the last seen maximum value)
2. Make a GET request to `https://api.example.com/posts?created_since=2024-01-28`
3. The API will only return posts created on or after January 28th

Let's break down the configuration:

1. We explicitly set `data_selector` to `"results"` to select the list of posts from the response. This is optional; if not set, dlt will try to auto-detect the data location.
2. We define the `created_since` parameter in `params` section and use the placeholder `{incremental.start_value}` to reference the incremental value.

Placeholders are versatile and can be used in various request components. Here are some examples:

#### In JSON body (for POST requests) [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#in-json-body-for-post-requests "Direct link to In JSON body (for POST requests)")

If the API lets you filter the data by a range of dates (e.g. `fromDate` and `toDate`), you can use the placeholder in the JSON body:

```codeBlockLines_RjmQ
{
    "path": "posts/search",
    "method": "POST",
    "json": {
        "filters": {
            "fromDate": "{incremental.start_value}",  # In JSON body
            "toDate": "2024-03-25"
        },
        "limit": 1000
    },
    "incremental": {
        "cursor_path": "created_at",
        "initial_value": "2024-01-25T00:00:00Z",
    },
}

```

#### In path parameters [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#in-path-parameters "Direct link to In path parameters")

Some APIs use path parameters to filter the data:

```codeBlockLines_RjmQ
{
    "path": "posts/since/{incremental.start_value}/list",  # In URL path
    "incremental": {
        "cursor_path": "created_at",
        "initial_value": "2024-01-25",
    },
}

```

#### In request headers [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#in-request-headers "Direct link to In request headers")

It's not so common, but you can also use placeholders in the request headers:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "headers": {
        "X-Since-Timestamp": "{incremental.start_value}"  # In custom header
    },
    "incremental": {
        "cursor_path": "created_at",
        "initial_value": "2024-01-25T00:00:00Z",
    },
}

```

You can also use different placeholder variants depending on your needs:

| Placeholder | Description |
| --- | --- |
| `{incremental.start_value}` | The value to use as the starting point for this request (either the initial value or the last tracked maximum value) |
| `{incremental.initial_value}` | Always uses the initial value specified in the configuration |
| `{incremental.last_value}` | The last seen value (same as start\_value in most cases, see the [incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor) guide for more details) |
| `{incremental.end_value}` | The end value if specified in the configuration |

### Legacy method: Incremental loading in `params` (DEPRECATED) [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#legacy-method-incremental-loading-in-params-deprecated "Direct link to legacy-method-incremental-loading-in-params-deprecated")

caution

DEPRECATED: This method is deprecated and will be removed in a future version. Use the [placeholder method](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#using-placeholders-for-incremental-loading) instead.

note

This method only works for query string parameters. For other request parts (path, JSON body, headers), use the [placeholder method](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#using-placeholders-for-incremental-loading).

For query string parameters, you can also specify incremental loading directly in the `params` section:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "results",  # Optional JSONPath to select the list of posts
    "params": {
        "created_since": {
            "type": "incremental",
            "cursor_path": "created_at", # The JSONPath to the field we want to track in each post
            "initial_value": "2024-01-25",
        },
    },
}

```

Above we define the `created_since` parameter as an incremental parameter as:

```codeBlockLines_RjmQ
{
    "created_since": {
        "type": "incremental",
        "cursor_path": "created_at",
        "initial_value": "2024-01-25",
    },
}

```

The fields are:

- `type`: The type of the parameter definition. In this case, it must be set to `incremental`.
- `cursor_path`: The JSONPath to the field within each item in the list. The value of this field will be used in the next request. In the example above, our items look like `{"id": 1, "title": "Post 1", "created_at": "2024-01-26"}` so to track the created time, we set `cursor_path` to `"created_at"`. Note that the JSONPath starts from the root of the item (dict) and not from the root of the response.
- `initial_value`: The initial value for the cursor. This is the value that will initialize the state of incremental loading. In this case, it's `2024-01-25`. The value type should match the type of the field in the data item.

### Incremental loading using the `incremental` field (DEPRECATED) [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#incremental-loading-using-the-incremental-field-deprecated "Direct link to incremental-loading-using-the-incremental-field-deprecated")

caution

DEPRECATED: This method is deprecated and will be removed in a future dlt version. Use the [placeholder method](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#using-placeholders-for-incremental-loading) instead.

Another alternative method is to use the `incremental` field in the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) while specifying names of the query string parameters to be used as start and end conditions.

Let's take the same example as above and configure it using the `incremental` field:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "results",
    "incremental": {
        "start_param": "created_since",
        "cursor_path": "created_at",
        "initial_value": "2024-01-25",
    },
}

```

The full available configuration for the `incremental` field is:

```codeBlockLines_RjmQ
{
    "incremental": {
        "start_param": "<start_parameter_name>",
        "end_param": "<end_parameter_name>",
        "cursor_path": "<path_to_cursor_field>",
        "initial_value": "<initial_value>",
        "end_value": "<end_value>",
        "convert": my_callable,
    }
}

```

The fields are:

- `start_param` (str): The name of the query parameter to be used as the start condition. If we use the example above, it would be `"created_since"`.
- `end_param` (str): The name of the query parameter to be used as the end condition. This is optional and can be omitted if you only need to track the start condition. This is useful when you need to fetch data within a specific range and the API supports end conditions (like the `created_before` query parameter).
- `cursor_path` (str): The JSONPath to the field within each item in the list. This is the field that will be used to track the incremental loading. In the example above, it's `"created_at"`.
- `initial_value` (str): The initial value for the cursor. This is the value that will initialize the state of incremental loading.
- `end_value` (str): The end value for the cursor to stop the incremental loading. This is optional and can be omitted if you only need to track the start condition. If you set this field, `initial_value` needs to be set as well.
- `convert` (callable): A callable that converts the cursor value into the format that the query parameter requires. For example, a UNIX timestamp can be converted into an ISO 8601 date or a date can be converted into `created_at+gt+{date}`.

See the [incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor) guide for more details.

If you encounter issues with incremental loading, see the [troubleshooting section](https://dlthub.com/docs/general-usage/incremental/troubleshooting) in the incremental loading guide.

### Convert the incremental value before calling the API [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#convert-the-incremental-value-before-calling-the-api "Direct link to Convert the incremental value before calling the API")

If you need to transform the values in the cursor field before passing them to the API endpoint, you can specify a callable under the key `convert`. For example, the API might return UNIX epoch timestamps but expects to be queried with an ISO 8601 date. To achieve that, we can specify a function that converts from the date format returned by the API to the date format required for API requests.

In the following examples, `1704067200` is returned from the API in the field `updated_at`, but the API will be called with `?created_since=2024-01-01`.

Incremental loading using the `params` field:

```codeBlockLines_RjmQ
{
    "created_since": {
        "type": "incremental",
        "cursor_path": "updated_at",
        "initial_value": "1704067200",
        "convert": lambda epoch: pendulum.from_timestamp(int(epoch)).to_date_string(),
    }
}

```

Incremental loading using the `incremental` field:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "results",
    "incremental": {
        "start_param": "created_since",
        "cursor_path": "updated_at",
        "initial_value": "1704067200",
        "convert": lambda epoch: pendulum.from_timestamp(int(epoch)).to_date_string(),
    },
}

```

## Troubleshooting [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#troubleshooting "Direct link to Troubleshooting")

If you encounter issues while running the pipeline, enable [logging](https://dlthub.com/docs/running-in-production/running#set-the-log-level-and-format) for detailed information about the execution:

```codeBlockLines_RjmQ
RUNTIME__LOG_LEVEL=INFO python my_script.py

```

This also provides details on the HTTP requests.

### Configuration issues [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#configuration-issues "Direct link to Configuration issues")

#### Getting validation errors [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#getting-validation-errors "Direct link to Getting validation errors")

When you are running the pipeline and getting a `DictValidationException`, it means that the [source configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#source-configuration) is incorrect. The error message provides details on the issue, including the path to the field and the expected type.

For example, if you have a source configuration like this:

```codeBlockLines_RjmQ
config: RESTAPIConfig = {
    "client": {
        # ...
    },
    "resources": [\
        {\
            "name": "issues",\
            "params": {             # <- Wrong: this should be inside\
                "sort": "updated",  #    the endpoint field below\
            },\
            "endpoint": {\
                "path": "issues",\
                # "params": {       # <- Correct configuration\
                #     "sort": "updated",\
                # },\
            },\
        },\
        # ...\
    ],
}

```

You will get an error like this:

```codeBlockLines_RjmQ
dlt.common.exceptions.DictValidationException: In path .: field 'resources[0]'
expects the following types: str, EndpointResource. Provided value {'name': 'issues', 'params': {'sort': 'updated'},
'endpoint': {'path': 'issues', ... }} with type 'dict' is invalid with the following errors:
For EndpointResource: In path ./resources[0]: following fields are unexpected {'params'}

```

It means that in the first resource configuration ( `resources[0]`), the `params` field should be inside the `endpoint` field.

tip

Import the `RESTAPIConfig` type from the `rest_api` module to have convenient hints in your editor/IDE and use it to define the configuration object.

```codeBlockLines_RjmQ
from dlt.sources.rest_api import RESTAPIConfig

```

#### Getting wrong data or no data [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#getting-wrong-data-or-no-data "Direct link to Getting wrong data or no data")

If incorrect data is received from an endpoint, check the `data_selector` field in the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration). Ensure the JSONPath is accurate and points to the correct data in the response body. `rest_api` attempts to auto-detect the data location, which may not always succeed. See the [data selection](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#data-selection) section for more details.

#### Getting insufficient data or incorrect pagination [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#getting-insufficient-data-or-incorrect-pagination "Direct link to Getting insufficient data or incorrect pagination")

Check the `paginator` field in the configuration. When not explicitly specified, the source tries to auto-detect the pagination method. If auto-detection fails, or the system is unsure, a warning is logged. For production environments, we recommend specifying an explicit paginator in the configuration. See the [pagination](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#pagination) section for more details. Some APIs may have non-standard pagination methods, and you may need to implement a [custom paginator](https://dlthub.com/docs/general-usage/http/rest-client#implementing-a-custom-paginator).

#### Incremental loading not working [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#incremental-loading-not-working "Direct link to Incremental loading not working")

See the [troubleshooting guide](https://dlthub.com/docs/general-usage/incremental/troubleshooting) for incremental loading issues.

#### Getting HTTP 404 errors [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#getting-http-404-errors "Direct link to Getting HTTP 404 errors")

Some APIs may return 404 errors for resources that do not exist or have no data. Manage these responses by configuring the `ignore` action in [response actions](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/advanced#response-actions).

### Authentication issues [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#authentication-issues "Direct link to Authentication issues")

If you are experiencing 401 (Unauthorized) errors, this could indicate:

- Incorrect authorization credentials. Verify credentials in the `secrets.toml`. Refer to [Secret and configs](https://dlthub.com/docs/general-usage/credentials/setup#troubleshoot-configuration-errors) for more information.
- An incorrect authentication type. Consult the API documentation for the proper method. See the [authentication](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#authentication) section for details. For some APIs, a [custom authentication method](https://dlthub.com/docs/general-usage/http/rest-client#implementing-custom-authentication) may be required.

### General guidelines [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#general-guidelines "Direct link to General guidelines")

The `rest_api` source uses the [RESTClient](https://dlthub.com/docs/general-usage/http/rest-client) class for HTTP requests. Refer to the RESTClient [troubleshooting guide](https://dlthub.com/docs/general-usage/http/rest-client#troubleshooting) for debugging tips.

For further assistance, join our [Slack community](https://dlthub.com/community). We're here to help!

- [Quick example](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#quick-example)
- [Setup](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#setup)
  - [Prerequisites](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#prerequisites)
  - [Initialize the REST API source](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#initialize-the-rest-api-source)
  - [Add credentials](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#add-credentials)
- [Run the pipeline](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#run-the-pipeline)
- [Source configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#source-configuration)
  - [Quick example](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#quick-example-1)
  - [Configuration structure](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#configuration-structure)
  - [Resource configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#resource-configuration)
  - [Endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration)
  - [Pagination](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#pagination)
  - [Data selection](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#data-selection)
  - [Authentication](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#authentication)
  - [Define resource relationships](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#define-resource-relationships)
  - [Define a resource which is not a REST endpoint](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#define-a-resource-which-is-not-a-rest-endpoint)
  - [Processing steps: filter and transform data](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#processing-steps-filter-and-transform-data)
- [Incremental loading](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#incremental-loading)
  - [Using placeholders for incremental loading](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#using-placeholders-for-incremental-loading)
  - [Legacy method: Incremental loading in `params` (DEPRECATED)](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#legacy-method-incremental-loading-in-params-deprecated)
  - [Incremental loading using the `incremental` field (DEPRECATED)](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#incremental-loading-using-the-incremental-field-deprecated)
  - [Convert the incremental value before calling the API](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#convert-the-incremental-value-before-calling-the-api)
- [Troubleshooting](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#troubleshooting)
  - [Configuration issues](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#configuration-issues)
  - [Authentication issues](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#authentication-issues)
  - [General guidelines](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#general-guidelines)

----- https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/advanced -----

Version: 1.11.0 (latest)

On this page

`rest_api_source()` function creates the [dlt source](https://dlthub.com/docs/general-usage/source) and lets you configure the following parameters:

- `config`: The REST API configuration dictionary.
- `name`: An optional name for the source.
- `section`: An optional section name in the configuration file.
- `max_table_nesting`: Sets the maximum depth of nested tables above which the remaining nodes are loaded as structs or JSON.
- `root_key` (bool): Enables merging on all resources by propagating the root foreign key to nested tables. This option is most useful if you plan to change the write disposition of a resource to disable/enable merge. Defaults to False.
- `schema_contract`: Schema contract settings that will be applied to this resource.
- `spec`: A specification of configuration and secret values required by the source.

### Response actions [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/advanced\#response-actions "Direct link to Response actions")

The `response_actions` field in the endpoint configuration allows you to specify how to handle specific responses or all responses from the API. For example, responses with specific status codes or content substrings can be ignored.
Additionally, all responses or only responses with specific status codes or content substrings can be transformed with a custom callable, such as a function. This callable is passed on to the requests library as a [response hook](https://requests.readthedocs.io/en/latest/user/advanced/#event-hooks). The callable can modify the response object and must return it for the modifications to take effect.

Experimental Feature

This is an experimental feature and may change in future releases.

**Fields:**

- `status_code` (int, optional): The HTTP status code to match.
- `content` (str, optional): A substring to search for in the response content.
- `action` (str or Callable or List\[Callable\], optional): The action to take when the condition is met. Currently supported actions:
  - `"ignore"`: Ignore the response.
  - a callable accepting and returning the response object.
  - a list of callables, each accepting and returning the response object.

#### Example A [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/advanced\#example-a "Direct link to Example A")

```codeBlockLines_RjmQ
{
    "path": "issues",
    "response_actions": [\
        {"status_code": 404, "action": "ignore"},\
        {"content": "Not found", "action": "ignore"},\
        {"status_code": 200, "content": "some text", "action": "ignore"},\
    ],
}

```

In this example, the source will ignore responses with a status code of 404, responses with the content "Not found", and responses with a status code of 200 _and_ content "some text".

#### Example B [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/advanced\#example-b "Direct link to Example B")

```codeBlockLines_RjmQ
from requests.models import Response
from dlt.common import json

def set_encoding(response, *args, **kwargs):
    # Sets the encoding in case it's not correctly detected
    response.encoding = 'windows-1252'
    return response

def add_and_remove_fields(response: Response, *args, **kwargs) -> Response:
    payload = response.json()
    for record in payload["data"]:
        record["custom_field"] = "foobar"
        record.pop("email", None)
    modified_content: bytes = json.dumps(payload).encode("utf-8")
    response._content = modified_content
    return response

source_config = {
    "client": {
        # ...
    },
    "resources": [\
        {\
            "name": "issues",\
            "endpoint": {\
                "path": "issues",\
                "response_actions": [\
                    set_encoding,\
                    {\
                        "status_code": 200,\
                        "content": "some text",\
                        "action": add_and_remove_fields,\
                    },\
                ],\
            },\
        },\
    ],
}

```

In this example, the resource will set the correct encoding for all responses first. Thereafter, for all responses that have the status code 200, we will add a field `custom_field` and remove the field `email`.

#### Example C [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/advanced\#example-c "Direct link to Example C")

```codeBlockLines_RjmQ
def set_encoding(response, *args, **kwargs):
    # Sets the encoding in case it's not correctly detected
    response.encoding = 'windows-1252'
    return response

source_config = {
    "client": {
        # ...
    },
    "resources": [\
        {\
            "name": "issues",\
            "endpoint": {\
                "path": "issues",\
                "response_actions": [\
                    set_encoding,\
                ],\
            },\
        },\
    ],
}

```

In this example, the resource will set the correct encoding for all responses. More callables can be added to the list of response\_actions.

- [Response actions](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/advanced#response-actions)

----- https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/advanced#response-actions -----

Version: 1.11.0 (latest)

On this page

`rest_api_source()` function creates the [dlt source](https://dlthub.com/docs/general-usage/source) and lets you configure the following parameters:

- `config`: The REST API configuration dictionary.
- `name`: An optional name for the source.
- `section`: An optional section name in the configuration file.
- `max_table_nesting`: Sets the maximum depth of nested tables above which the remaining nodes are loaded as structs or JSON.
- `root_key` (bool): Enables merging on all resources by propagating the root foreign key to nested tables. This option is most useful if you plan to change the write disposition of a resource to disable/enable merge. Defaults to False.
- `schema_contract`: Schema contract settings that will be applied to this resource.
- `spec`: A specification of configuration and secret values required by the source.

### Response actions [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/advanced\#response-actions "Direct link to Response actions")

The `response_actions` field in the endpoint configuration allows you to specify how to handle specific responses or all responses from the API. For example, responses with specific status codes or content substrings can be ignored.
Additionally, all responses or only responses with specific status codes or content substrings can be transformed with a custom callable, such as a function. This callable is passed on to the requests library as a [response hook](https://requests.readthedocs.io/en/latest/user/advanced/#event-hooks). The callable can modify the response object and must return it for the modifications to take effect.

Experimental Feature

This is an experimental feature and may change in future releases.

**Fields:**

- `status_code` (int, optional): The HTTP status code to match.
- `content` (str, optional): A substring to search for in the response content.
- `action` (str or Callable or List\[Callable\], optional): The action to take when the condition is met. Currently supported actions:
  - `"ignore"`: Ignore the response.
  - a callable accepting and returning the response object.
  - a list of callables, each accepting and returning the response object.

#### Example A [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/advanced\#example-a "Direct link to Example A")

```codeBlockLines_RjmQ
{
    "path": "issues",
    "response_actions": [\
        {"status_code": 404, "action": "ignore"},\
        {"content": "Not found", "action": "ignore"},\
        {"status_code": 200, "content": "some text", "action": "ignore"},\
    ],
}

```

In this example, the source will ignore responses with a status code of 404, responses with the content "Not found", and responses with a status code of 200 _and_ content "some text".

#### Example B [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/advanced\#example-b "Direct link to Example B")

```codeBlockLines_RjmQ
from requests.models import Response
from dlt.common import json

def set_encoding(response, *args, **kwargs):
    # Sets the encoding in case it's not correctly detected
    response.encoding = 'windows-1252'
    return response

def add_and_remove_fields(response: Response, *args, **kwargs) -> Response:
    payload = response.json()
    for record in payload["data"]:
        record["custom_field"] = "foobar"
        record.pop("email", None)
    modified_content: bytes = json.dumps(payload).encode("utf-8")
    response._content = modified_content
    return response

source_config = {
    "client": {
        # ...
    },
    "resources": [\
        {\
            "name": "issues",\
            "endpoint": {\
                "path": "issues",\
                "response_actions": [\
                    set_encoding,\
                    {\
                        "status_code": 200,\
                        "content": "some text",\
                        "action": add_and_remove_fields,\
                    },\
                ],\
            },\
        },\
    ],
}

```

In this example, the resource will set the correct encoding for all responses first. Thereafter, for all responses that have the status code 200, we will add a field `custom_field` and remove the field `email`.

#### Example C [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/advanced\#example-c "Direct link to Example C")

```codeBlockLines_RjmQ
def set_encoding(response, *args, **kwargs):
    # Sets the encoding in case it's not correctly detected
    response.encoding = 'windows-1252'
    return response

source_config = {
    "client": {
        # ...
    },
    "resources": [\
        {\
            "name": "issues",\
            "endpoint": {\
                "path": "issues",\
                "response_actions": [\
                    set_encoding,\
                ],\
            },\
        },\
    ],
}

```

In this example, the resource will set the correct encoding for all responses. More callables can be added to the list of response\_actions.

- [Response actions](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/advanced#response-actions)

----- https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#source-configuration -----

Version: 1.11.0 (latest)

On this page

Need help deploying these sources or figuring out how to run them in your data stack?

[Join our Slack community](https://dlthub.com/community) or [Get in touch](https://dlthub.com/contact) with the dltHub Customer Success team.

This is a dlt source you can use to extract data from any REST API. It uses [declarative configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#source-configuration) to define the API endpoints, their [relationships](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#define-resource-relationships), how to handle [pagination](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#pagination), and [authentication](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#authentication).

### Quick example [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#quick-example "Direct link to Quick example")

Here's an example of how to configure the REST API source to load posts and related comments from a hypothetical blog API:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.rest_api import rest_api_source

source = rest_api_source({
    "client": {
        "base_url": "https://api.example.com/",
        "auth": {
            "token": dlt.secrets["your_api_token"],
        },
        "paginator": {
            "type": "json_link",
            "next_url_path": "paging.next",
        },
    },
    "resources": [\
        # "posts" will be used as the endpoint path, the resource name,\
        # and the table name in the destination. The HTTP client will send\
        # a request to "https://api.example.com/posts".\
        "posts",\
\
        # The explicit configuration allows you to link resources\
        # and define query string parameters.\
        {\
            "name": "comments",\
            "endpoint": {\
                "path": "posts/{resources.posts.id}/comments",\
                "params": {\
                    "sort": "created_at",\
                },\
            },\
        },\
    ],
})

pipeline = dlt.pipeline(
    pipeline_name="rest_api_example",
    destination="duckdb",
    dataset_name="rest_api_data",
)

load_info = pipeline.run(source)

```

Running this pipeline will create two tables in DuckDB: `posts` and `comments` with the data from the respective API endpoints. The `comments` resource will fetch comments for each post by using the `id` field from the `posts` resource.

## Setup [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#setup "Direct link to Setup")

### Prerequisites [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#prerequisites "Direct link to Prerequisites")

Please make sure the `dlt` library is installed. Refer to the [installation guide](https://dlthub.com/docs/intro).

### Initialize the REST API source [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#initialize-the-rest-api-source "Direct link to Initialize the REST API source")

Enter the following command in your terminal:

```codeBlockLines_RjmQ
dlt init rest_api duckdb

```

[dlt init](https://dlthub.com/docs/reference/command-line-interface) will initialize the pipeline examples for REST API as the [source](https://dlthub.com/docs/general-usage/source) and [duckdb](https://dlthub.com/docs/dlt-ecosystem/destinations/duckdb) as the [destination](https://dlthub.com/docs/dlt-ecosystem/destinations).

Running `dlt init` creates the following in the current folder:

- `rest_api_pipeline.py` file with a sample pipelines definition:
  - GitHub API example
  - Pokemon API example
- `.dlt` folder with:
  - `secrets.toml` file to store your access tokens and other sensitive information
  - `config.toml` file to store the configuration settings
- `requirements.txt` file with the required dependencies

Change the REST API source to your needs by modifying the `rest_api_pipeline.py` file. See the detailed [source configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#source-configuration) section below.

note

For the rest of the guide, we will use the [GitHub API](https://docs.github.com/en/rest?apiVersion=2022-11-28) and [Pokemon API](https://pokeapi.co/) as example sources.

This source is based on the [RESTClient class](https://dlthub.com/docs/general-usage/http/rest-client).

### Add credentials [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#add-credentials "Direct link to Add credentials")

In the `.dlt` folder, you'll find a file called `secrets.toml`, where you can securely store your access tokens and other sensitive information. It's important to handle this file with care and keep it safe.

The GitHub API [requires an access token](https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api?apiVersion=2022-11-28) to access some of its endpoints and to increase the rate limit for the API calls. To get a GitHub token, follow the GitHub documentation on [managing your personal access tokens](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens).

After you get the token, add it to the `secrets.toml` file:

```codeBlockLines_RjmQ
[sources.rest_api_pipeline.github_source]
github_token = "your_github_token"

```

## Run the pipeline [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#run-the-pipeline "Direct link to Run the pipeline")

1. Install the required dependencies by running the following command:

```codeBlockLines_RjmQ
pip install -r requirements.txt

```

2. Run the pipeline:

```codeBlockLines_RjmQ
python rest_api_pipeline.py

```

3. Verify that everything loaded correctly by using the following command:

```codeBlockLines_RjmQ
dlt pipeline rest_api show

```

## Source configuration [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#source-configuration "Direct link to Source configuration")

### Quick example [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#quick-example-1 "Direct link to Quick example")

Let's take a look at the GitHub example in the `rest_api_pipeline.py` file:

```codeBlockLines_RjmQ
from dlt.sources.rest_api import RESTAPIConfig, rest_api_resources

@dlt.source
def github_source(github_token=dlt.secrets.value):
    config: RESTAPIConfig = {
        "client": {
            "base_url": "https://api.github.com/repos/dlt-hub/dlt/",
            "auth": {
                "token": github_token,
            },
        },
        "resource_defaults": {
            "primary_key": "id",
            "write_disposition": "merge",
            "endpoint": {
                "params": {
                    "per_page": 100,
                },
            },
        },
        "resources": [\
            {\
                "name": "issues",\
                "endpoint": {\
                    "path": "issues",\
                    "params": {\
                        "sort": "updated",\
                        "direction": "desc",\
                        "state": "open",\
                        "since": {\
                            "type": "incremental",\
                            "cursor_path": "updated_at",\
                            "initial_value": "2024-01-25T11:21:28Z",\
                        },\
                    },\
                },\
            },\
            {\
                "name": "issue_comments",\
                "endpoint": {\
                    "path": "issues/{resources.issues.number}/comments",\
                },\
                "include_from_parent": ["id"],\
            },\
        ],
    }

    yield from rest_api_resources(config)

def load_github() -> None:
    pipeline = dlt.pipeline(
        pipeline_name="rest_api_github",
        destination="duckdb",
        dataset_name="rest_api_data",
    )

    load_info = pipeline.run(github_source())
    print(load_info)

```

The declarative resource configuration is defined in the `config` dictionary. It contains the following key components:

1. `client`: Defines the base URL and authentication method for the API. In this case, it uses token-based authentication. The token is stored in the `secrets.toml` file.

2. `resource_defaults`: Contains default settings for all [resources](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#resource-configuration). In this example, we define that all resources:
   - Have `id` as the [primary key](https://dlthub.com/docs/general-usage/resource#define-schema)
   - Use the `merge` [write disposition](https://dlthub.com/docs/general-usage/incremental-loading#choosing-a-write-disposition) to merge the data with the existing data in the destination.
   - Send a `per_page=100` query parameter with each request to get more results per page.
3. `resources`: A list of [resources](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#resource-configuration) to be loaded. Here, we have two resources: `issues` and `issue_comments`, which correspond to the GitHub API endpoints for [repository issues](https://docs.github.com/en/rest/issues/issues?apiVersion=2022-11-28#list-repository-issues) and [issue comments](https://docs.github.com/en/rest/issues/comments?apiVersion=2022-11-28#list-issue-comments). Note that we need an issue number to fetch comments for each issue. This number is taken from the `issues` resource. More on this in the [resource relationships](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#define-resource-relationships) section.

Let's break down the configuration in more detail.

### Configuration structure [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#configuration-structure "Direct link to Configuration structure")

tip

Import the `RESTAPIConfig` type from the `rest_api` module to have convenient hints in your editor/IDE and use it to define the configuration object.

```codeBlockLines_RjmQ
from dlt.sources.rest_api import RESTAPIConfig

```

The configuration object passed to the REST API Generic Source has three main elements:

```codeBlockLines_RjmQ
config: RESTAPIConfig = {
    "client": {
        # ...
    },
    "resource_defaults": {
        # ...
    },
    "resources": [\
        # ...\
    ],
}

```

#### `client` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#client "Direct link to client")

The `client` configuration is used to connect to the API's endpoints. It includes the following fields:

- `base_url` (str): The base URL of the API. This string is prepended to all endpoint paths. For example, if the base URL is `https://api.example.com/v1/`, and the endpoint path is `users`, the full URL will be `https://api.example.com/v1/users`.
- `headers` (dict, optional): Additional headers that are sent with each request.
- `auth` (optional): Authentication configuration. This can be a simple token, an `AuthConfigBase` object, or a more complex authentication method.
- `paginator` (optional): Configuration for the default pagination used for resources that support pagination. Refer to the [pagination](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#pagination) section for more details.

#### `resource_defaults` (optional) [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#resource_defaults-optional "Direct link to resource_defaults-optional")

`resource_defaults` contains the default values to [configure the dlt resources](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#resource-configuration). This configuration is applied to all resources unless overridden by the resource-specific configuration.

For example, you can set the primary key, write disposition, and other default settings here:

```codeBlockLines_RjmQ
config = {
    "client": {
        # ...
    },
    "resource_defaults": {
        "primary_key": "id",
        "write_disposition": "merge",
        "endpoint": {
            "params": {
                "per_page": 100,
            },
        },
    },
    "resources": [\
        "resource1",\
        {\
            "name": "resource2_name",\
            "write_disposition": "append",\
            "endpoint": {\
                "params": {\
                    "param1": "value1",\
                },\
            },\
        }\
    ],
}

```

Above, all resources will have `primary_key` set to `id`, `resource1` will have `write_disposition` set to `merge`, and `resource2` will override the default `write_disposition` with `append`.
Both `resource1` and `resource2` will have the `per_page` parameter set to 100.

#### `resources` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#resources "Direct link to resources")

This is a list of resource configurations that define the API endpoints to be loaded. Each resource configuration can be:

- a dictionary with the [resource configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#resource-configuration).
- a string. In this case, the string is used as both the endpoint path and the resource name, and the resource configuration is taken from the `resource_defaults` configuration if it exists.

### Resource configuration [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#resource-configuration "Direct link to Resource configuration")

A resource configuration is used to define a [dlt resource](https://dlthub.com/docs/general-usage/resource) for the data to be loaded from an API endpoint. It contains the following key fields:

- `endpoint`: The endpoint configuration for the resource. It can be a string or a dict representing the endpoint settings. See the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) section for more details.
- `write_disposition`: The write disposition for the resource.
- `primary_key`: The primary key for the resource.
- `include_from_parent`: A list of fields from the parent resource to be included in the resource output. See the [resource relationships](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#include-fields-from-the-parent-resource) section for more details.
- `processing_steps`: A list of [processing steps](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#processing-steps-filter-and-transform-data) to filter and transform your data.
- `selected`: A flag to indicate if the resource is selected for loading. This could be useful when you want to load data only from child resources and not from the parent resource.
- `auth`: An optional `AuthConfig` instance. If passed, is used over the one defined in the [client](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#client) definition. Example:

```codeBlockLines_RjmQ
from dlt.sources.helpers.rest_client.auth import HttpBasicAuth

config = {
    "client": {
        "auth": {
            "type": "bearer",
            "token": dlt.secrets["your_api_token"],
        }
    },
    "resources": [\
        "resource-using-bearer-auth",\
        {\
            "name": "my-resource-with-special-auth",\
            "endpoint": {\
                # ...\
                "auth": HttpBasicAuth("user", dlt.secrets["your_basic_auth_password"])\
            },\
            # ...\
        }\
    ]
    # ...
}

```

This would use `Bearer` auth as defined in the `client` for `resource-using-bearer-auth` and `Http Basic` auth for `my-resource-with-special-auth`.

You can also pass additional resource parameters that will be used to configure the dlt resource. See [dlt resource API reference](https://dlthub.com/docs/api_reference/dlt/extract/decorators#resource) for more details.

### Endpoint configuration [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#endpoint-configuration "Direct link to Endpoint configuration")

The endpoint configuration defines how to query the API endpoint. Quick example:

```codeBlockLines_RjmQ
{
    "path": "issues",
    "method": "GET",
    "params": {
        "sort": "updated",
        "direction": "desc",
        "state": "open",
        "since": {
            "type": "incremental",
            "cursor_path": "updated_at",
            "initial_value": "2024-01-25T11:21:28Z",
        },
    },
    "data_selector": "results",
}

```

The fields in the endpoint configuration are:

- `path`: The path to the API endpoint. By default this path is appended to the given `base_url`. If this is a fully qualified URL starting with `http:` or `https:` it will be
used as-is and `base_url` will be ignored.
- `method`: The HTTP method to be used. The default is `GET`.
- `params`: Query parameters to be sent with each request. For example, `sort` to order the results or `since` to specify [incremental loading](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#incremental-loading). This is also may be used to define [resource relationships](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#define-resource-relationships).
- `json`: The JSON payload to be sent with the request (for POST and PUT requests).
- `paginator`: Pagination configuration for the endpoint. See the [pagination](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#pagination) section for more details.
- `data_selector`: A JSONPath to select the data from the response. See the [data selection](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#data-selection) section for more details.
- `response_actions`: A list of actions that define how to process the response data. See the [response actions](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/advanced#response-actions) section for more details.
- `incremental`: Configuration for [incremental loading](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#incremental-loading).

### Pagination [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#pagination "Direct link to Pagination")

The REST API source will try to automatically handle pagination for you. This works by detecting the pagination details from the first API response.

In some special cases, you may need to specify the pagination configuration explicitly.

To specify the pagination configuration, use the `paginator` field in the [client](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#client) or [endpoint](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) configurations. You may either use a dictionary with a string alias in the `type` field along with the required parameters, or use a [paginator class instance](https://dlthub.com/docs/general-usage/http/rest-client#paginators).

#### Example [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#example "Direct link to Example")

Suppose the API response for `https://api.example.com/posts` contains a `next` field with the URL to the next page:

```codeBlockLines_RjmQ
{
    "data": [\
        {"id": 1, "title": "Post 1"},\
        {"id": 2, "title": "Post 2"},\
        {"id": 3, "title": "Post 3"}\
    ],
    "pagination": {
        "next": "https://api.example.com/posts?page=2"
    }
}

```

You can configure the pagination for the `posts` resource like this:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "paginator": {
        "type": "json_link",
        "next_url_path": "pagination.next",
    }
}

```

Alternatively, you can use the paginator instance directly:

```codeBlockLines_RjmQ
from dlt.sources.helpers.rest_client.paginators import JSONLinkPaginator

# ...

{
    "path": "posts",
    "paginator": JSONLinkPaginator(
        next_url_path="pagination.next"
    ),
}

```

note

Currently, pagination is supported only for GET requests. To handle POST requests with pagination, you need to implement a [custom paginator](https://dlthub.com/docs/general-usage/http/rest-client#implementing-a-custom-paginator).

These are the available paginators:

| `type` | Paginator class | Description |
| --- | --- | --- |
| `json_link` | [JSONLinkPaginator](https://dlthub.com/docs/general-usage/http/rest-client#jsonlinkpaginator) | The link to the next page is in the body (JSON) of the response.<br>_Parameters:_ <br>- `next_url_path` (str) - the JSONPath to the next page URL |
| `header_link` | [HeaderLinkPaginator](https://dlthub.com/docs/general-usage/http/rest-client#headerlinkpaginator) | The links to the next page are in the response headers.<br>_Parameters:_ <br>- `links_next_key` (str) - the name of the header containing the links. Default is "next". |
| `offset` | [OffsetPaginator](https://dlthub.com/docs/general-usage/http/rest-client#offsetpaginator) | The pagination is based on an offset parameter, with the total items count either in the response body or explicitly provided.<br>_Parameters:_ <br>- `limit` (int) - the maximum number of items to retrieve in each request<br>- `offset` (int) - the initial offset for the first request. Defaults to `0`<br>- `offset_param` (str) - the name of the query parameter used to specify the offset. Defaults to "offset"<br>- `limit_param` (str) - the name of the query parameter used to specify the limit. Defaults to "limit"<br>- `total_path` (str) - a JSONPath expression for the total number of items. If not provided, pagination is controlled by `maximum_offset` and `stop_after_empty_page`<br>- `maximum_offset` (int) - optional maximum offset value. Limits pagination even without total count<br>- `stop_after_empty_page` (bool) - Whether pagination should stop when a page contains no result items. Defaults to `True` |
| `page_number` | [PageNumberPaginator](https://dlthub.com/docs/general-usage/http/rest-client#pagenumberpaginator) | The pagination is based on a page number parameter, with the total pages count either in the response body or explicitly provided.<br>_Parameters:_ <br>- `base_page` (int) - the starting page number. Defaults to `0`<br>- `page_param` (str) - the query parameter name for the page number. Defaults to "page"<br>- `total_path` (str) - a JSONPath expression for the total number of pages. If not provided, pagination is controlled by `maximum_page` and `stop_after_empty_page`<br>- `maximum_page` (int) - optional maximum page number. Stops pagination once this page is reached<br>- `stop_after_empty_page` (bool) - Whether pagination should stop when a page contains no result items. Defaults to `True` |
| `cursor` | [JSONResponseCursorPaginator](https://dlthub.com/docs/general-usage/http/rest-client#jsonresponsecursorpaginator) | The pagination is based on a cursor parameter, with the value of the cursor in the response body (JSON).<br>_Parameters:_ <br>- `cursor_path` (str) - the JSONPath to the cursor value. Defaults to "cursors.next"<br>- `cursor_param` (str) - the query parameter name for the cursor. Defaults to "cursor" if neither `cursor_param` nor `cursor_body_path` is provided.<br>- `cursor_body_path` (str, optional) - the JSONPath to place the cursor in the request body.<br>Note: You must provide either `cursor_param` or `cursor_body_path`, but not both. If neither is provided, `cursor_param` will default to "cursor". |
| `single_page` | SinglePagePaginator | The response will be interpreted as a single-page response, ignoring possible pagination metadata. |
| `auto` | `None` | Explicitly specify that the source should automatically detect the pagination method. |

For more complex pagination methods, you can implement a [custom paginator](https://dlthub.com/docs/general-usage/http/rest-client#implementing-a-custom-paginator), instantiate it, and use it in the configuration.

Alternatively, you can use the dictionary configuration syntax also for custom paginators. For this, you need to register your custom paginator:

```codeBlockLines_RjmQ
from dlt.sources.rest_api.config_setup import register_paginator

class CustomPaginator(SinglePagePaginator):
    # custom implementation of SinglePagePaginator
    pass

register_paginator("custom_paginator", CustomPaginator)

{
    # ...
    "paginator": {
        "type": "custom_paginator",
        "next_url_path": "paging.nextLink",
    }
}

```

### Data selection [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#data-selection "Direct link to Data selection")

The `data_selector` field in the endpoint configuration allows you to specify a JSONPath to select the data from the response. By default, the source will try to detect the locations of the data automatically.

Use this field when you need to specify the location of the data in the response explicitly.

For example, if the API response looks like this:

```codeBlockLines_RjmQ
{
    "posts": [\
        {"id": 1, "title": "Post 1"},\
        {"id": 2, "title": "Post 2"},\
        {"id": 3, "title": "Post 3"}\
    ]
}

```

You can use the following endpoint configuration:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "posts",
}

```

For a nested structure like this:

```codeBlockLines_RjmQ
{
    "results": {
        "posts": [\
            {"id": 1, "title": "Post 1"},\
            {"id": 2, "title": "Post 2"},\
            {"id": 3, "title": "Post 3"}\
        ]
    }
}

```

You can use the following endpoint configuration:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "results.posts",
}

```

Read more about [JSONPath syntax](https://github.com/h2non/jsonpath-ng?tab=readme-ov-file#jsonpath-syntax) to learn how to write selectors.

### Authentication [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#authentication "Direct link to Authentication")

For APIs that require authentication to access their endpoints, the REST API source supports various authentication methods, including token-based authentication, query parameters, basic authentication, and custom authentication. The authentication configuration is specified in the `auth` field of the [client](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#client) either as a dictionary or as an instance of the [authentication class](https://dlthub.com/docs/general-usage/http/rest-client#authentication).

#### Quick example [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#quick-example-2 "Direct link to Quick example")

Here's how to configure authentication using a bearer token:

```codeBlockLines_RjmQ
{
    "client": {
        # ...
        "auth": {
            "type": "bearer",
            "token": dlt.secrets["your_api_token"],
        },
        # ...
    },
}

```

Alternatively, you can use the authentication class directly:

```codeBlockLines_RjmQ
from dlt.sources.helpers.rest_client.auth import BearerTokenAuth

config = {
    "client": {
        "auth": BearerTokenAuth(dlt.secrets["your_api_token"]),
    },
    "resources": [\
    ]
    # ...
}

```

Since token-based authentication is one of the most common methods, you can use the following shortcut:

```codeBlockLines_RjmQ
{
    "client": {
        # ...
        "auth": {
            "token": dlt.secrets["your_api_token"],
        },
        # ...
    },
}

```

warning

Make sure to store your access tokens and other sensitive information in the `secrets.toml` file and never commit it to the version control system.

Available authentication types:

| `type` | Authentication class | Description |
| --- | --- | --- |
| `bearer` | [BearerTokenAuth](https://dlthub.com/docs/general-usage/http/rest-client#bearer-token-authentication) | Bearer token authentication.<br>Parameters:<br>- `token` (str) |
| `http_basic` | [HTTPBasicAuth](https://dlthub.com/docs/general-usage/http/rest-client#http-basic-authentication) | Basic HTTP authentication.<br>Parameters:<br>- `username` (str)<br>- `password` (str) |
| `api_key` | [APIKeyAuth](https://dlthub.com/docs/general-usage/http/rest-client#api-key-authentication) | API key authentication with key defined in the query parameters or in the headers. <br>Parameters:<br>- `name` (str) - the name of the query parameter or header<br>- `api_key` (str) - the API key value<br>- `location` (str, optional) - the location of the API key in the request. Can be `query` or `header`. Default is `header` |
| `oauth2_client_credentials` | [OAuth2ClientCredentials](https://dlthub.com/docs/general-usage/http/rest-client#oauth-20-authorization) | OAuth 2.0 Client Credentials authorization for server-to-server communication without user consent. <br>Parameters:<br>- `access_token` (str, optional) - the temporary token. Usually not provided here because it is automatically obtained from the server by exchanging `client_id` and `client_secret`. Default is `None`<br>- `access_token_url` (str) - the URL to request the `access_token` from<br>- `client_id` (str) - identifier for your app. Usually issued via a developer portal<br>- `client_secret` (str) - client credential to obtain authorization. Usually issued via a developer portal<br>- `access_token_request_data` (dict, optional) - A dictionary with data required by the authorization server apart from the `client_id`, `client_secret`, and `"grant_type": "client_credentials"`. Defaults to `None`<br>- `default_token_expiration` (int, optional) - The time in seconds after which the temporary access token expires. Defaults to 3600.<br>- `session` (requests.Session, optional) - a custom session object. Mostly used for testing |

For more complex authentication methods, you can implement a [custom authentication class](https://dlthub.com/docs/general-usage/http/rest-client#implementing-custom-authentication) and use it in the configuration.

You can use the dictionary configuration syntax also for custom authentication classes after registering them as follows:

```codeBlockLines_RjmQ
from dlt.sources.rest_api.config_setup import register_auth

class CustomAuth(AuthConfigBase):
    pass

register_auth("custom_auth", CustomAuth)

{
    # ...
    "auth": {
        "type": "custom_auth",
        "api_key": dlt.secrets["sources.my_source.my_api_key"],
    }
}

```

### Define resource relationships [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#define-resource-relationships "Direct link to Define resource relationships")

When you have a resource that depends on another resource (for example, you must fetch a parent resource to get an ID needed to fetch the child), you can reference fields in the parent resource using special placeholders.
This allows you to link one or more [path](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#via-request-path), [query string](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#via-query-string-parameters) or [JSON body](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#via-json-body) parameters in the child resource to fields in the parent resource's data.

#### Via request path [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#via-request-path "Direct link to Via request path")

In the GitHub example, the `issue_comments` resource depends on the `issues` resource. The `resources.issues.number` placeholder links the `number` field in the `issues` resource data to the current request's path parameter.

```codeBlockLines_RjmQ
{
    "resources": [\
        {\
            "name": "issues",\
            "endpoint": {\
                "path": "issues",\
                # ...\
            },\
        },\
        {\
            "name": "issue_comments",\
            "endpoint": {\
                "path": "issues/{resources.issues.number}/comments",\
            },\
            "include_from_parent": ["id"],\
        },\
    ],
}

```

This configuration tells the source to get issue numbers from the `issues` resource data and use them to fetch comments for each issue number. So for each issue item, `"{resources.issues.number}"` is replaced by the issue number in the request path.
For example, if the `issues` resource yields the following data:

```codeBlockLines_RjmQ
[\
    {"id": 1, "number": 123},\
    {"id": 2, "number": 124},\
    {"id": 3, "number": 125}\
]

```

The `issue_comments` resource will make requests to the following endpoints:

- `issues/123/comments`
- `issues/124/comments`
- `issues/125/comments`

The syntax for the placeholder is `resources.<parent_resource_name>.<field_name>`.

#### Via query string parameters [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#via-query-string-parameters "Direct link to Via query string parameters")

The placeholder syntax can also be used in the query string parameters. For example, in an API which lets you fetch a blog posts (via `/posts`) and their comments (via `/comments?post_id=<post_id>`), you can define a resource `posts` and a resource `post_comments` which depends on the `posts` resource. You can then reference the `id` field from the `posts` resource in the `post_comments` resource:

```codeBlockLines_RjmQ
{
    "resources": [\
        "posts",\
        {\
            "name": "post_comments",\
            "endpoint": {\
                "path": "comments",\
                "params": {\
                    "post_id": "{resources.posts.id}",\
                },\
            },\
        },\
    ],
}

```

Similar to the GitHub example above, if the `posts` resource yields the following data:

```codeBlockLines_RjmQ
[\
    {"id": 1, "title": "Post 1"},\
    {"id": 2, "title": "Post 2"},\
    {"id": 3, "title": "Post 3"}\
]

```

The `post_comments` resource will make requests to the following endpoints:

- `comments?post_id=1`
- `comments?post_id=2`
- `comments?post_id=3`

#### Via JSON body [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#via-json-body "Direct link to Via JSON body")

In many APIs, you can send a complex query or configuration through a POST request's JSON body rather than in the request path or query parameters. For example, consider an imaginary `/search` endpoint that supports multiple filters and settings. You might have a parent resource `posts` with each post's `id` and a second resource, `post_details`, that uses `id` to perform a custom search.

In the example below we reference the `posts` resource's `id` field in the JSON body via placeholders:

```codeBlockLines_RjmQ
{
    "resources": [\
        "posts",\
        {\
            "name": "post_details",\
            "endpoint": {\
                "path": "search",\
                "method": "POST",\
                "json": {\
                    "filters": {\
                        "id": "{resources.posts.id}",\
                    },\
                    "order": "desc",\
                    "limit": 5,\
                }\
            },\
        },\
    ],
}

```

#### Legacy syntax: `resolve` field in parameter configuration [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#legacy-syntax-resolve-field-in-parameter-configuration "Direct link to legacy-syntax-resolve-field-in-parameter-configuration")

warning

`resolve` works only for path parameters. The new placeholder syntax is more flexible and recommended for new configurations.

An alternative, legacy way to define resource relationships is to use the `resolve` field in the parameter configuration.
Here's the same example as above that uses the `resolve` field:

```codeBlockLines_RjmQ
{
    "resources": [\
        {\
            "name": "issues",\
            "endpoint": {\
                "path": "issues",\
                # ...\
            },\
        },\
        {\
            "name": "issue_comments",\
            "endpoint": {\
                "path": "issues/{issue_number}/comments",\
                "params": {\
                    "issue_number": {\
                        "type": "resolve",\
                        "resource": "issues",\
                        "field": "number",\
                    }\
                },\
            },\
            "include_from_parent": ["id"],\
        },\
    ],
}

```

The syntax for the `resolve` field in parameter configuration is:

```codeBlockLines_RjmQ
{
    "<parameter_name>": {
        "type": "resolve",
        "resource": "<parent_resource_name>",
        "field": "<parent_resource_field_name_or_jsonpath>",
    }
}

```

The `field` value can be specified as a [JSONPath](https://github.com/h2non/jsonpath-ng?tab=readme-ov-file#jsonpath-syntax) to select a nested field in the parent resource data. For example: `"field": "items[0].id"`.

#### Resolving multiple path parameters from a parent resource [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#resolving-multiple-path-parameters-from-a-parent-resource "Direct link to Resolving multiple path parameters from a parent resource")

When a child resource depends on multiple fields from a single parent resource, you can define multiple `resolve` parameters in the endpoint configuration. For example:

```codeBlockLines_RjmQ
{
    "resources": [\
        "groups",\
        {\
            "name": "users",\
            "endpoint": {\
                "path": "groups/{group_id}/users",\
                "params": {\
                    "group_id": {\
                        "type": "resolve",\
                        "resource": "groups",\
                        "field": "id",\
                    },\
                },\
            },\
        },\
        {\
            "name": "user_details",\
            "endpoint": {\
                "path": "groups/{group_id}/users/{user_id}/details",\
                "params": {\
                    "group_id": {\
                        "type": "resolve",\
                        "resource": "users",\
                        "field": "group_id",\
                    },\
                    "user_id": {\
                        "type": "resolve",\
                        "resource": "users",\
                        "field": "id",\
                    },\
                },\
            },\
        },\
    ],
}

```

In the configuration above:

- The `users` resource depends on the `groups` resource, resolving the `group_id` parameter from the `id` field in `groups`.
- The `user_details` resource depends on the `users` resource, resolving both `group_id` and `user_id` parameters from fields in `users`.

#### Include fields from the parent resource [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#include-fields-from-the-parent-resource "Direct link to Include fields from the parent resource")

You can include data from the parent resource in the child resource by using the `include_from_parent` field in the resource configuration. For example:

```codeBlockLines_RjmQ
{
    "name": "issue_comments",
    "endpoint": {
        ...
    },
    "include_from_parent": ["id", "title", "created_at"],
}

```

This will include the `id`, `title`, and `created_at` fields from the `issues` resource in the `issue_comments` resource data. The names of the included fields will be prefixed with the parent resource name and an underscore ( `_`) like so: `_issues_id`, `_issues_title`, `_issues_created_at`.

### Define a resource which is not a REST endpoint [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#define-a-resource-which-is-not-a-rest-endpoint "Direct link to Define a resource which is not a REST endpoint")

Sometimes, we want to request endpoints with specific values that are not returned by another endpoint.
Thus, you can also include arbitrary dlt resources in your `RESTAPIConfig` instead of defining a resource for every path!

In the following example, we want to load the issues belonging to three repositories.
Instead of defining three different issues resources, one for each of the paths `dlt-hub/dlt/issues/`, `dlt-hub/verified-sources/issues/`, `dlt-hub/dlthub-education/issues/`, we have a resource `repositories` which yields a list of repository names that will be fetched by the dependent resource `issues`.

```codeBlockLines_RjmQ
from dlt.sources.rest_api import RESTAPIConfig

@dlt.resource()
def repositories() -> Generator[List[Dict[str, Any]], Any, Any]:
    """A seed list of repositories to fetch"""
    yield [{"name": "dlt"}, {"name": "verified-sources"}, {"name": "dlthub-education"}]

config: RESTAPIConfig = {
    "client": {"base_url": "https://github.com/api/v2"},
    "resources": [\
        {\
            "name": "issues",\
            "endpoint": {\
                "path": "dlt-hub/{repository}/issues/",\
                "params": {\
                    "repository": {\
                        "type": "resolve",\
                        "resource": "repositories",\
                        "field": "name",\
                    },\
                },\
            },\
        },\
        repositories(),\
    ],
}

```

Be careful that the parent resource needs to return `Generator[List[Dict[str, Any]]]`. Thus, the following will NOT work:

```codeBlockLines_RjmQ
@dlt.resource
def repositories() -> Generator[Dict[str, Any], Any, Any]:
    """Not working seed list of repositories to fetch"""
    yield from [{"name": "dlt"}, {"name": "verified-sources"}, {"name": "dlthub-education"}]

```

### Processing steps: filter and transform data [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#processing-steps-filter-and-transform-data "Direct link to Processing steps: filter and transform data")

The `processing_steps` field in the resource configuration allows you to apply transformations to the data fetched from the API before it is loaded into your destination. This is useful when you need to filter out certain records, modify the data structure, or anonymize sensitive information.

Each processing step is a dictionary specifying the type of operation ( `filter` or `map`) and the function to apply. Steps apply in the order they are listed.

#### Quick example [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#quick-example-3 "Direct link to Quick example")

```codeBlockLines_RjmQ
def lower_title(record):
    record["title"] = record["title"].lower()
    return record

config: RESTAPIConfig = {
    "client": {
        "base_url": "https://api.example.com",
    },
    "resources": [\
        {\
            "name": "posts",\
            "processing_steps": [\
                {"filter": lambda x: x["id"] < 10},\
                {"map": lower_title},\
            ],\
        },\
    ],
}

```

In the example above:

- First, the `filter` step uses a lambda function to include only records where `id` is less than 10.
- Thereafter, the `map` step applies the `lower_title` function to each remaining record.

#### Using `filter` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#using-filter "Direct link to using-filter")

The `filter` step allows you to exclude records that do not meet certain criteria. The provided function should return `True` to keep the record or `False` to exclude it:

```codeBlockLines_RjmQ
{
    "name": "posts",
    "endpoint": "posts",
    "processing_steps": [\
        {"filter": lambda x: x["id"] in [10, 20, 30]},\
    ],
}

```

In this example, only records with `id` equal to 10, 20, or 30 will be included.

#### Using `map` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#using-map "Direct link to using-map")

The `map` step allows you to modify the records fetched from the API. The provided function should take a record as an argument and return the modified record. For example, to anonymize the `email` field:

```codeBlockLines_RjmQ
def anonymize_email(record):
    record["email"] = "REDACTED"
    return record

config: RESTAPIConfig = {
    "client": {
        "base_url": "https://api.example.com",
    },
    "resources": [\
        {\
            "name": "users",\
            "processing_steps": [\
                {"map": anonymize_email},\
            ],\
        },\
    ],
}

```

#### Combining `filter` and `map` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#combining-filter-and-map "Direct link to combining-filter-and-map")

You can combine multiple processing steps to achieve complex transformations:

```codeBlockLines_RjmQ
{
    "name": "posts",
    "endpoint": "posts",
    "processing_steps": [\
        {"filter": lambda x: x["id"] < 10},\
        {"map": lower_title},\
        {"filter": lambda x: "important" in x["title"]},\
    ],
}

```

tip

#### Best practices [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#best-practices "Direct link to Best practices")

1. Order matters: Processing steps are applied in the order they are listed. Be mindful of the sequence, especially when combining `map` and `filter`.
2. Function definition: Define your filter and map functions separately for clarity and reuse.
3. Use `filter` to exclude records early in the process to reduce the amount of data that needs to be processed.
4. Combine consecutive `map` steps into a single function for faster execution.

## Incremental loading [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#incremental-loading "Direct link to Incremental loading")

Some APIs provide a way to fetch only new or changed data (most often by using a timestamp field like `updated_at`, `created_at`, or incremental IDs).
This is called [incremental loading](https://dlthub.com/docs/general-usage/incremental-loading) and is very useful as it allows you to reduce the load time and the amount of data transferred.

Let's continue with our imaginary blog API example to understand incremental loading with query parameters.

Imagine we have the following endpoint `https://api.example.com/posts` and it:

1. Accepts a `created_since` query parameter to fetch blog posts created after a certain date.
2. Returns a list of posts with the `created_at` field for each post.

For example, if we query the endpoint with GET request `https://api.example.com/posts?created_since=2024-01-25`, we get the following response:

```codeBlockLines_RjmQ
{
    "results": [\
        {"id": 1, "title": "Post 1", "created_at": "2024-01-26"},\
        {"id": 2, "title": "Post 2", "created_at": "2024-01-27"},\
        {"id": 3, "title": "Post 3", "created_at": "2024-01-28"}\
    ]
}

```

When the API endpoint supports incremental loading, you can configure dlt to load only the new or changed data using these three methods:

1. Using [placeholders for incremental loading](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#using-placeholders-for-incremental-loading)
2. Defining a special parameter in the `params` section of the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) (DEPRECATED)
3. Using the `incremental` field in the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) with the `start_param` field (DEPRECATED)

caution

The last two methods are deprecated and will be removed in a future dlt version.

### Using placeholders for incremental loading [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#using-placeholders-for-incremental-loading "Direct link to Using placeholders for incremental loading")

The most flexible way to configure incremental loading is to use placeholders in the request configuration along with the `incremental` section.
Here's how it works:

1. Define the `incremental` section in the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) to specify the cursor path (where to find the incremental value in the response) and initial value (the value to start the incremental loading from).
2. Use the placeholder `{incremental.start_value}` in the request configuration to reference the incremental value.

Let's take the example from the previous section and configure it using placeholders:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "results",
    "params": {
        "created_since": "{incremental.start_value}",  # Uses cursor value in query parameter
    },
    "incremental": {
        "cursor_path": "created_at",
        "initial_value": "2024-01-25T00:00:00Z",
    },
}

```

When you first run this pipeline, dlt will:

1. Replace `{incremental.start_value}` with `2024-01-25T00:00:00Z` (the initial value)
2. Make a GET request to `https://api.example.com/posts?created_since=2024-01-25T00:00:00Z`
3. Parse the response (e.g., posts with created\_at values like "2024-01-26", "2024-01-27", "2024-01-28")
4. Track the maximum value found in the "created\_at" field (in this case, "2024-01-28")

On the next pipeline run, dlt will:

1. Replace `{incremental.start_value}` with "2024-01-28" (the last seen maximum value)
2. Make a GET request to `https://api.example.com/posts?created_since=2024-01-28`
3. The API will only return posts created on or after January 28th

Let's break down the configuration:

1. We explicitly set `data_selector` to `"results"` to select the list of posts from the response. This is optional; if not set, dlt will try to auto-detect the data location.
2. We define the `created_since` parameter in `params` section and use the placeholder `{incremental.start_value}` to reference the incremental value.

Placeholders are versatile and can be used in various request components. Here are some examples:

#### In JSON body (for POST requests) [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#in-json-body-for-post-requests "Direct link to In JSON body (for POST requests)")

If the API lets you filter the data by a range of dates (e.g. `fromDate` and `toDate`), you can use the placeholder in the JSON body:

```codeBlockLines_RjmQ
{
    "path": "posts/search",
    "method": "POST",
    "json": {
        "filters": {
            "fromDate": "{incremental.start_value}",  # In JSON body
            "toDate": "2024-03-25"
        },
        "limit": 1000
    },
    "incremental": {
        "cursor_path": "created_at",
        "initial_value": "2024-01-25T00:00:00Z",
    },
}

```

#### In path parameters [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#in-path-parameters "Direct link to In path parameters")

Some APIs use path parameters to filter the data:

```codeBlockLines_RjmQ
{
    "path": "posts/since/{incremental.start_value}/list",  # In URL path
    "incremental": {
        "cursor_path": "created_at",
        "initial_value": "2024-01-25",
    },
}

```

#### In request headers [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#in-request-headers "Direct link to In request headers")

It's not so common, but you can also use placeholders in the request headers:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "headers": {
        "X-Since-Timestamp": "{incremental.start_value}"  # In custom header
    },
    "incremental": {
        "cursor_path": "created_at",
        "initial_value": "2024-01-25T00:00:00Z",
    },
}

```

You can also use different placeholder variants depending on your needs:

| Placeholder | Description |
| --- | --- |
| `{incremental.start_value}` | The value to use as the starting point for this request (either the initial value or the last tracked maximum value) |
| `{incremental.initial_value}` | Always uses the initial value specified in the configuration |
| `{incremental.last_value}` | The last seen value (same as start\_value in most cases, see the [incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor) guide for more details) |
| `{incremental.end_value}` | The end value if specified in the configuration |

### Legacy method: Incremental loading in `params` (DEPRECATED) [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#legacy-method-incremental-loading-in-params-deprecated "Direct link to legacy-method-incremental-loading-in-params-deprecated")

caution

DEPRECATED: This method is deprecated and will be removed in a future version. Use the [placeholder method](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#using-placeholders-for-incremental-loading) instead.

note

This method only works for query string parameters. For other request parts (path, JSON body, headers), use the [placeholder method](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#using-placeholders-for-incremental-loading).

For query string parameters, you can also specify incremental loading directly in the `params` section:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "results",  # Optional JSONPath to select the list of posts
    "params": {
        "created_since": {
            "type": "incremental",
            "cursor_path": "created_at", # The JSONPath to the field we want to track in each post
            "initial_value": "2024-01-25",
        },
    },
}

```

Above we define the `created_since` parameter as an incremental parameter as:

```codeBlockLines_RjmQ
{
    "created_since": {
        "type": "incremental",
        "cursor_path": "created_at",
        "initial_value": "2024-01-25",
    },
}

```

The fields are:

- `type`: The type of the parameter definition. In this case, it must be set to `incremental`.
- `cursor_path`: The JSONPath to the field within each item in the list. The value of this field will be used in the next request. In the example above, our items look like `{"id": 1, "title": "Post 1", "created_at": "2024-01-26"}` so to track the created time, we set `cursor_path` to `"created_at"`. Note that the JSONPath starts from the root of the item (dict) and not from the root of the response.
- `initial_value`: The initial value for the cursor. This is the value that will initialize the state of incremental loading. In this case, it's `2024-01-25`. The value type should match the type of the field in the data item.

### Incremental loading using the `incremental` field (DEPRECATED) [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#incremental-loading-using-the-incremental-field-deprecated "Direct link to incremental-loading-using-the-incremental-field-deprecated")

caution

DEPRECATED: This method is deprecated and will be removed in a future dlt version. Use the [placeholder method](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#using-placeholders-for-incremental-loading) instead.

Another alternative method is to use the `incremental` field in the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) while specifying names of the query string parameters to be used as start and end conditions.

Let's take the same example as above and configure it using the `incremental` field:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "results",
    "incremental": {
        "start_param": "created_since",
        "cursor_path": "created_at",
        "initial_value": "2024-01-25",
    },
}

```

The full available configuration for the `incremental` field is:

```codeBlockLines_RjmQ
{
    "incremental": {
        "start_param": "<start_parameter_name>",
        "end_param": "<end_parameter_name>",
        "cursor_path": "<path_to_cursor_field>",
        "initial_value": "<initial_value>",
        "end_value": "<end_value>",
        "convert": my_callable,
    }
}

```

The fields are:

- `start_param` (str): The name of the query parameter to be used as the start condition. If we use the example above, it would be `"created_since"`.
- `end_param` (str): The name of the query parameter to be used as the end condition. This is optional and can be omitted if you only need to track the start condition. This is useful when you need to fetch data within a specific range and the API supports end conditions (like the `created_before` query parameter).
- `cursor_path` (str): The JSONPath to the field within each item in the list. This is the field that will be used to track the incremental loading. In the example above, it's `"created_at"`.
- `initial_value` (str): The initial value for the cursor. This is the value that will initialize the state of incremental loading.
- `end_value` (str): The end value for the cursor to stop the incremental loading. This is optional and can be omitted if you only need to track the start condition. If you set this field, `initial_value` needs to be set as well.
- `convert` (callable): A callable that converts the cursor value into the format that the query parameter requires. For example, a UNIX timestamp can be converted into an ISO 8601 date or a date can be converted into `created_at+gt+{date}`.

See the [incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor) guide for more details.

If you encounter issues with incremental loading, see the [troubleshooting section](https://dlthub.com/docs/general-usage/incremental/troubleshooting) in the incremental loading guide.

### Convert the incremental value before calling the API [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#convert-the-incremental-value-before-calling-the-api "Direct link to Convert the incremental value before calling the API")

If you need to transform the values in the cursor field before passing them to the API endpoint, you can specify a callable under the key `convert`. For example, the API might return UNIX epoch timestamps but expects to be queried with an ISO 8601 date. To achieve that, we can specify a function that converts from the date format returned by the API to the date format required for API requests.

In the following examples, `1704067200` is returned from the API in the field `updated_at`, but the API will be called with `?created_since=2024-01-01`.

Incremental loading using the `params` field:

```codeBlockLines_RjmQ
{
    "created_since": {
        "type": "incremental",
        "cursor_path": "updated_at",
        "initial_value": "1704067200",
        "convert": lambda epoch: pendulum.from_timestamp(int(epoch)).to_date_string(),
    }
}

```

Incremental loading using the `incremental` field:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "results",
    "incremental": {
        "start_param": "created_since",
        "cursor_path": "updated_at",
        "initial_value": "1704067200",
        "convert": lambda epoch: pendulum.from_timestamp(int(epoch)).to_date_string(),
    },
}

```

## Troubleshooting [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#troubleshooting "Direct link to Troubleshooting")

If you encounter issues while running the pipeline, enable [logging](https://dlthub.com/docs/running-in-production/running#set-the-log-level-and-format) for detailed information about the execution:

```codeBlockLines_RjmQ
RUNTIME__LOG_LEVEL=INFO python my_script.py

```

This also provides details on the HTTP requests.

### Configuration issues [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#configuration-issues "Direct link to Configuration issues")

#### Getting validation errors [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#getting-validation-errors "Direct link to Getting validation errors")

When you are running the pipeline and getting a `DictValidationException`, it means that the [source configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#source-configuration) is incorrect. The error message provides details on the issue, including the path to the field and the expected type.

For example, if you have a source configuration like this:

```codeBlockLines_RjmQ
config: RESTAPIConfig = {
    "client": {
        # ...
    },
    "resources": [\
        {\
            "name": "issues",\
            "params": {             # <- Wrong: this should be inside\
                "sort": "updated",  #    the endpoint field below\
            },\
            "endpoint": {\
                "path": "issues",\
                # "params": {       # <- Correct configuration\
                #     "sort": "updated",\
                # },\
            },\
        },\
        # ...\
    ],
}

```

You will get an error like this:

```codeBlockLines_RjmQ
dlt.common.exceptions.DictValidationException: In path .: field 'resources[0]'
expects the following types: str, EndpointResource. Provided value {'name': 'issues', 'params': {'sort': 'updated'},
'endpoint': {'path': 'issues', ... }} with type 'dict' is invalid with the following errors:
For EndpointResource: In path ./resources[0]: following fields are unexpected {'params'}

```

It means that in the first resource configuration ( `resources[0]`), the `params` field should be inside the `endpoint` field.

tip

Import the `RESTAPIConfig` type from the `rest_api` module to have convenient hints in your editor/IDE and use it to define the configuration object.

```codeBlockLines_RjmQ
from dlt.sources.rest_api import RESTAPIConfig

```

#### Getting wrong data or no data [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#getting-wrong-data-or-no-data "Direct link to Getting wrong data or no data")

If incorrect data is received from an endpoint, check the `data_selector` field in the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration). Ensure the JSONPath is accurate and points to the correct data in the response body. `rest_api` attempts to auto-detect the data location, which may not always succeed. See the [data selection](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#data-selection) section for more details.

#### Getting insufficient data or incorrect pagination [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#getting-insufficient-data-or-incorrect-pagination "Direct link to Getting insufficient data or incorrect pagination")

Check the `paginator` field in the configuration. When not explicitly specified, the source tries to auto-detect the pagination method. If auto-detection fails, or the system is unsure, a warning is logged. For production environments, we recommend specifying an explicit paginator in the configuration. See the [pagination](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#pagination) section for more details. Some APIs may have non-standard pagination methods, and you may need to implement a [custom paginator](https://dlthub.com/docs/general-usage/http/rest-client#implementing-a-custom-paginator).

#### Incremental loading not working [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#incremental-loading-not-working "Direct link to Incremental loading not working")

See the [troubleshooting guide](https://dlthub.com/docs/general-usage/incremental/troubleshooting) for incremental loading issues.

#### Getting HTTP 404 errors [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#getting-http-404-errors "Direct link to Getting HTTP 404 errors")

Some APIs may return 404 errors for resources that do not exist or have no data. Manage these responses by configuring the `ignore` action in [response actions](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/advanced#response-actions).

### Authentication issues [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#authentication-issues "Direct link to Authentication issues")

If you are experiencing 401 (Unauthorized) errors, this could indicate:

- Incorrect authorization credentials. Verify credentials in the `secrets.toml`. Refer to [Secret and configs](https://dlthub.com/docs/general-usage/credentials/setup#troubleshoot-configuration-errors) for more information.
- An incorrect authentication type. Consult the API documentation for the proper method. See the [authentication](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#authentication) section for details. For some APIs, a [custom authentication method](https://dlthub.com/docs/general-usage/http/rest-client#implementing-custom-authentication) may be required.

### General guidelines [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#general-guidelines "Direct link to General guidelines")

The `rest_api` source uses the [RESTClient](https://dlthub.com/docs/general-usage/http/rest-client) class for HTTP requests. Refer to the RESTClient [troubleshooting guide](https://dlthub.com/docs/general-usage/http/rest-client#troubleshooting) for debugging tips.

For further assistance, join our [Slack community](https://dlthub.com/community). We're here to help!

- [Quick example](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#quick-example)
- [Setup](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#setup)
  - [Prerequisites](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#prerequisites)
  - [Initialize the REST API source](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#initialize-the-rest-api-source)
  - [Add credentials](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#add-credentials)
- [Run the pipeline](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#run-the-pipeline)
- [Source configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#source-configuration)
  - [Quick example](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#quick-example-1)
  - [Configuration structure](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#configuration-structure)
  - [Resource configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#resource-configuration)
  - [Endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration)
  - [Pagination](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#pagination)
  - [Data selection](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#data-selection)
  - [Authentication](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#authentication)
  - [Define resource relationships](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#define-resource-relationships)
  - [Define a resource which is not a REST endpoint](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#define-a-resource-which-is-not-a-rest-endpoint)
  - [Processing steps: filter and transform data](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#processing-steps-filter-and-transform-data)
- [Incremental loading](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#incremental-loading)
  - [Using placeholders for incremental loading](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#using-placeholders-for-incremental-loading)
  - [Legacy method: Incremental loading in `params` (DEPRECATED)](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#legacy-method-incremental-loading-in-params-deprecated)
  - [Incremental loading using the `incremental` field (DEPRECATED)](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#incremental-loading-using-the-incremental-field-deprecated)
  - [Convert the incremental value before calling the API](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#convert-the-incremental-value-before-calling-the-api)
- [Troubleshooting](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#troubleshooting)
  - [Configuration issues](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#configuration-issues)
  - [Authentication issues](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#authentication-issues)
  - [General guidelines](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#general-guidelines)

[iframe](https://

----- https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#authentication -----

Version: 1.11.0 (latest)

On this page

Need help deploying these sources or figuring out how to run them in your data stack?

[Join our Slack community](https://dlthub.com/community) or [Get in touch](https://dlthub.com/contact) with the dltHub Customer Success team.

This is a dlt source you can use to extract data from any REST API. It uses [declarative configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#source-configuration) to define the API endpoints, their [relationships](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#define-resource-relationships), how to handle [pagination](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#pagination), and [authentication](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#authentication).

### Quick example [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#quick-example "Direct link to Quick example")

Here's an example of how to configure the REST API source to load posts and related comments from a hypothetical blog API:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.rest_api import rest_api_source

source = rest_api_source({
    "client": {
        "base_url": "https://api.example.com/",
        "auth": {
            "token": dlt.secrets["your_api_token"],
        },
        "paginator": {
            "type": "json_link",
            "next_url_path": "paging.next",
        },
    },
    "resources": [\
        # "posts" will be used as the endpoint path, the resource name,\
        # and the table name in the destination. The HTTP client will send\
        # a request to "https://api.example.com/posts".\
        "posts",\
\
        # The explicit configuration allows you to link resources\
        # and define query string parameters.\
        {\
            "name": "comments",\
            "endpoint": {\
                "path": "posts/{resources.posts.id}/comments",\
                "params": {\
                    "sort": "created_at",\
                },\
            },\
        },\
    ],
})

pipeline = dlt.pipeline(
    pipeline_name="rest_api_example",
    destination="duckdb",
    dataset_name="rest_api_data",
)

load_info = pipeline.run(source)

```

Running this pipeline will create two tables in DuckDB: `posts` and `comments` with the data from the respective API endpoints. The `comments` resource will fetch comments for each post by using the `id` field from the `posts` resource.

## Setup [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#setup "Direct link to Setup")

### Prerequisites [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#prerequisites "Direct link to Prerequisites")

Please make sure the `dlt` library is installed. Refer to the [installation guide](https://dlthub.com/docs/intro).

### Initialize the REST API source [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#initialize-the-rest-api-source "Direct link to Initialize the REST API source")

Enter the following command in your terminal:

```codeBlockLines_RjmQ
dlt init rest_api duckdb

```

[dlt init](https://dlthub.com/docs/reference/command-line-interface) will initialize the pipeline examples for REST API as the [source](https://dlthub.com/docs/general-usage/source) and [duckdb](https://dlthub.com/docs/dlt-ecosystem/destinations/duckdb) as the [destination](https://dlthub.com/docs/dlt-ecosystem/destinations).

Running `dlt init` creates the following in the current folder:

- `rest_api_pipeline.py` file with a sample pipelines definition:
  - GitHub API example
  - Pokemon API example
- `.dlt` folder with:
  - `secrets.toml` file to store your access tokens and other sensitive information
  - `config.toml` file to store the configuration settings
- `requirements.txt` file with the required dependencies

Change the REST API source to your needs by modifying the `rest_api_pipeline.py` file. See the detailed [source configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#source-configuration) section below.

note

For the rest of the guide, we will use the [GitHub API](https://docs.github.com/en/rest?apiVersion=2022-11-28) and [Pokemon API](https://pokeapi.co/) as example sources.

This source is based on the [RESTClient class](https://dlthub.com/docs/general-usage/http/rest-client).

### Add credentials [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#add-credentials "Direct link to Add credentials")

In the `.dlt` folder, you'll find a file called `secrets.toml`, where you can securely store your access tokens and other sensitive information. It's important to handle this file with care and keep it safe.

The GitHub API [requires an access token](https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api?apiVersion=2022-11-28) to access some of its endpoints and to increase the rate limit for the API calls. To get a GitHub token, follow the GitHub documentation on [managing your personal access tokens](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens).

After you get the token, add it to the `secrets.toml` file:

```codeBlockLines_RjmQ
[sources.rest_api_pipeline.github_source]
github_token = "your_github_token"

```

## Run the pipeline [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#run-the-pipeline "Direct link to Run the pipeline")

1. Install the required dependencies by running the following command:

```codeBlockLines_RjmQ
pip install -r requirements.txt

```

2. Run the pipeline:

```codeBlockLines_RjmQ
python rest_api_pipeline.py

```

3. Verify that everything loaded correctly by using the following command:

```codeBlockLines_RjmQ
dlt pipeline rest_api show

```

## Source configuration [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#source-configuration "Direct link to Source configuration")

### Quick example [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#quick-example-1 "Direct link to Quick example")

Let's take a look at the GitHub example in the `rest_api_pipeline.py` file:

```codeBlockLines_RjmQ
from dlt.sources.rest_api import RESTAPIConfig, rest_api_resources

@dlt.source
def github_source(github_token=dlt.secrets.value):
    config: RESTAPIConfig = {
        "client": {
            "base_url": "https://api.github.com/repos/dlt-hub/dlt/",
            "auth": {
                "token": github_token,
            },
        },
        "resource_defaults": {
            "primary_key": "id",
            "write_disposition": "merge",
            "endpoint": {
                "params": {
                    "per_page": 100,
                },
            },
        },
        "resources": [\
            {\
                "name": "issues",\
                "endpoint": {\
                    "path": "issues",\
                    "params": {\
                        "sort": "updated",\
                        "direction": "desc",\
                        "state": "open",\
                        "since": {\
                            "type": "incremental",\
                            "cursor_path": "updated_at",\
                            "initial_value": "2024-01-25T11:21:28Z",\
                        },\
                    },\
                },\
            },\
            {\
                "name": "issue_comments",\
                "endpoint": {\
                    "path": "issues/{resources.issues.number}/comments",\
                },\
                "include_from_parent": ["id"],\
            },\
        ],
    }

    yield from rest_api_resources(config)

def load_github() -> None:
    pipeline = dlt.pipeline(
        pipeline_name="rest_api_github",
        destination="duckdb",
        dataset_name="rest_api_data",
    )

    load_info = pipeline.run(github_source())
    print(load_info)

```

The declarative resource configuration is defined in the `config` dictionary. It contains the following key components:

1. `client`: Defines the base URL and authentication method for the API. In this case, it uses token-based authentication. The token is stored in the `secrets.toml` file.

2. `resource_defaults`: Contains default settings for all [resources](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#resource-configuration). In this example, we define that all resources:
   - Have `id` as the [primary key](https://dlthub.com/docs/general-usage/resource#define-schema)
   - Use the `merge` [write disposition](https://dlthub.com/docs/general-usage/incremental-loading#choosing-a-write-disposition) to merge the data with the existing data in the destination.
   - Send a `per_page=100` query parameter with each request to get more results per page.
3. `resources`: A list of [resources](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#resource-configuration) to be loaded. Here, we have two resources: `issues` and `issue_comments`, which correspond to the GitHub API endpoints for [repository issues](https://docs.github.com/en/rest/issues/issues?apiVersion=2022-11-28#list-repository-issues) and [issue comments](https://docs.github.com/en/rest/issues/comments?apiVersion=2022-11-28#list-issue-comments). Note that we need an issue number to fetch comments for each issue. This number is taken from the `issues` resource. More on this in the [resource relationships](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#define-resource-relationships) section.

Let's break down the configuration in more detail.

### Configuration structure [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#configuration-structure "Direct link to Configuration structure")

tip

Import the `RESTAPIConfig` type from the `rest_api` module to have convenient hints in your editor/IDE and use it to define the configuration object.

```codeBlockLines_RjmQ
from dlt.sources.rest_api import RESTAPIConfig

```

The configuration object passed to the REST API Generic Source has three main elements:

```codeBlockLines_RjmQ
config: RESTAPIConfig = {
    "client": {
        # ...
    },
    "resource_defaults": {
        # ...
    },
    "resources": [\
        # ...\
    ],
}

```

#### `client` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#client "Direct link to client")

The `client` configuration is used to connect to the API's endpoints. It includes the following fields:

- `base_url` (str): The base URL of the API. This string is prepended to all endpoint paths. For example, if the base URL is `https://api.example.com/v1/`, and the endpoint path is `users`, the full URL will be `https://api.example.com/v1/users`.
- `headers` (dict, optional): Additional headers that are sent with each request.
- `auth` (optional): Authentication configuration. This can be a simple token, an `AuthConfigBase` object, or a more complex authentication method.
- `paginator` (optional): Configuration for the default pagination used for resources that support pagination. Refer to the [pagination](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#pagination) section for more details.

#### `resource_defaults` (optional) [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#resource_defaults-optional "Direct link to resource_defaults-optional")

`resource_defaults` contains the default values to [configure the dlt resources](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#resource-configuration). This configuration is applied to all resources unless overridden by the resource-specific configuration.

For example, you can set the primary key, write disposition, and other default settings here:

```codeBlockLines_RjmQ
config = {
    "client": {
        # ...
    },
    "resource_defaults": {
        "primary_key": "id",
        "write_disposition": "merge",
        "endpoint": {
            "params": {
                "per_page": 100,
            },
        },
    },
    "resources": [\
        "resource1",\
        {\
            "name": "resource2_name",\
            "write_disposition": "append",\
            "endpoint": {\
                "params": {\
                    "param1": "value1",\
                },\
            },\
        }\
    ],
}

```

Above, all resources will have `primary_key` set to `id`, `resource1` will have `write_disposition` set to `merge`, and `resource2` will override the default `write_disposition` with `append`.
Both `resource1` and `resource2` will have the `per_page` parameter set to 100.

#### `resources` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#resources "Direct link to resources")

This is a list of resource configurations that define the API endpoints to be loaded. Each resource configuration can be:

- a dictionary with the [resource configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#resource-configuration).
- a string. In this case, the string is used as both the endpoint path and the resource name, and the resource configuration is taken from the `resource_defaults` configuration if it exists.

### Resource configuration [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#resource-configuration "Direct link to Resource configuration")

A resource configuration is used to define a [dlt resource](https://dlthub.com/docs/general-usage/resource) for the data to be loaded from an API endpoint. It contains the following key fields:

- `endpoint`: The endpoint configuration for the resource. It can be a string or a dict representing the endpoint settings. See the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) section for more details.
- `write_disposition`: The write disposition for the resource.
- `primary_key`: The primary key for the resource.
- `include_from_parent`: A list of fields from the parent resource to be included in the resource output. See the [resource relationships](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#include-fields-from-the-parent-resource) section for more details.
- `processing_steps`: A list of [processing steps](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#processing-steps-filter-and-transform-data) to filter and transform your data.
- `selected`: A flag to indicate if the resource is selected for loading. This could be useful when you want to load data only from child resources and not from the parent resource.
- `auth`: An optional `AuthConfig` instance. If passed, is used over the one defined in the [client](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#client) definition. Example:

```codeBlockLines_RjmQ
from dlt.sources.helpers.rest_client.auth import HttpBasicAuth

config = {
    "client": {
        "auth": {
            "type": "bearer",
            "token": dlt.secrets["your_api_token"],
        }
    },
    "resources": [\
        "resource-using-bearer-auth",\
        {\
            "name": "my-resource-with-special-auth",\
            "endpoint": {\
                # ...\
                "auth": HttpBasicAuth("user", dlt.secrets["your_basic_auth_password"])\
            },\
            # ...\
        }\
    ]
    # ...
}

```

This would use `Bearer` auth as defined in the `client` for `resource-using-bearer-auth` and `Http Basic` auth for `my-resource-with-special-auth`.

You can also pass additional resource parameters that will be used to configure the dlt resource. See [dlt resource API reference](https://dlthub.com/docs/api_reference/dlt/extract/decorators#resource) for more details.

### Endpoint configuration [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#endpoint-configuration "Direct link to Endpoint configuration")

The endpoint configuration defines how to query the API endpoint. Quick example:

```codeBlockLines_RjmQ
{
    "path": "issues",
    "method": "GET",
    "params": {
        "sort": "updated",
        "direction": "desc",
        "state": "open",
        "since": {
            "type": "incremental",
            "cursor_path": "updated_at",
            "initial_value": "2024-01-25T11:21:28Z",
        },
    },
    "data_selector": "results",
}

```

The fields in the endpoint configuration are:

- `path`: The path to the API endpoint. By default this path is appended to the given `base_url`. If this is a fully qualified URL starting with `http:` or `https:` it will be
used as-is and `base_url` will be ignored.
- `method`: The HTTP method to be used. The default is `GET`.
- `params`: Query parameters to be sent with each request. For example, `sort` to order the results or `since` to specify [incremental loading](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#incremental-loading). This is also may be used to define [resource relationships](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#define-resource-relationships).
- `json`: The JSON payload to be sent with the request (for POST and PUT requests).
- `paginator`: Pagination configuration for the endpoint. See the [pagination](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#pagination) section for more details.
- `data_selector`: A JSONPath to select the data from the response. See the [data selection](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#data-selection) section for more details.
- `response_actions`: A list of actions that define how to process the response data. See the [response actions](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/advanced#response-actions) section for more details.
- `incremental`: Configuration for [incremental loading](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#incremental-loading).

### Pagination [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#pagination "Direct link to Pagination")

The REST API source will try to automatically handle pagination for you. This works by detecting the pagination details from the first API response.

In some special cases, you may need to specify the pagination configuration explicitly.

To specify the pagination configuration, use the `paginator` field in the [client](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#client) or [endpoint](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) configurations. You may either use a dictionary with a string alias in the `type` field along with the required parameters, or use a [paginator class instance](https://dlthub.com/docs/general-usage/http/rest-client#paginators).

#### Example [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#example "Direct link to Example")

Suppose the API response for `https://api.example.com/posts` contains a `next` field with the URL to the next page:

```codeBlockLines_RjmQ
{
    "data": [\
        {"id": 1, "title": "Post 1"},\
        {"id": 2, "title": "Post 2"},\
        {"id": 3, "title": "Post 3"}\
    ],
    "pagination": {
        "next": "https://api.example.com/posts?page=2"
    }
}

```

You can configure the pagination for the `posts` resource like this:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "paginator": {
        "type": "json_link",
        "next_url_path": "pagination.next",
    }
}

```

Alternatively, you can use the paginator instance directly:

```codeBlockLines_RjmQ
from dlt.sources.helpers.rest_client.paginators import JSONLinkPaginator

# ...

{
    "path": "posts",
    "paginator": JSONLinkPaginator(
        next_url_path="pagination.next"
    ),
}

```

note

Currently, pagination is supported only for GET requests. To handle POST requests with pagination, you need to implement a [custom paginator](https://dlthub.com/docs/general-usage/http/rest-client#implementing-a-custom-paginator).

These are the available paginators:

| `type` | Paginator class | Description |
| --- | --- | --- |
| `json_link` | [JSONLinkPaginator](https://dlthub.com/docs/general-usage/http/rest-client#jsonlinkpaginator) | The link to the next page is in the body (JSON) of the response.<br>_Parameters:_ <br>- `next_url_path` (str) - the JSONPath to the next page URL |
| `header_link` | [HeaderLinkPaginator](https://dlthub.com/docs/general-usage/http/rest-client#headerlinkpaginator) | The links to the next page are in the response headers.<br>_Parameters:_ <br>- `links_next_key` (str) - the name of the header containing the links. Default is "next". |
| `offset` | [OffsetPaginator](https://dlthub.com/docs/general-usage/http/rest-client#offsetpaginator) | The pagination is based on an offset parameter, with the total items count either in the response body or explicitly provided.<br>_Parameters:_ <br>- `limit` (int) - the maximum number of items to retrieve in each request<br>- `offset` (int) - the initial offset for the first request. Defaults to `0`<br>- `offset_param` (str) - the name of the query parameter used to specify the offset. Defaults to "offset"<br>- `limit_param` (str) - the name of the query parameter used to specify the limit. Defaults to "limit"<br>- `total_path` (str) - a JSONPath expression for the total number of items. If not provided, pagination is controlled by `maximum_offset` and `stop_after_empty_page`<br>- `maximum_offset` (int) - optional maximum offset value. Limits pagination even without total count<br>- `stop_after_empty_page` (bool) - Whether pagination should stop when a page contains no result items. Defaults to `True` |
| `page_number` | [PageNumberPaginator](https://dlthub.com/docs/general-usage/http/rest-client#pagenumberpaginator) | The pagination is based on a page number parameter, with the total pages count either in the response body or explicitly provided.<br>_Parameters:_ <br>- `base_page` (int) - the starting page number. Defaults to `0`<br>- `page_param` (str) - the query parameter name for the page number. Defaults to "page"<br>- `total_path` (str) - a JSONPath expression for the total number of pages. If not provided, pagination is controlled by `maximum_page` and `stop_after_empty_page`<br>- `maximum_page` (int) - optional maximum page number. Stops pagination once this page is reached<br>- `stop_after_empty_page` (bool) - Whether pagination should stop when a page contains no result items. Defaults to `True` |
| `cursor` | [JSONResponseCursorPaginator](https://dlthub.com/docs/general-usage/http/rest-client#jsonresponsecursorpaginator) | The pagination is based on a cursor parameter, with the value of the cursor in the response body (JSON).<br>_Parameters:_ <br>- `cursor_path` (str) - the JSONPath to the cursor value. Defaults to "cursors.next"<br>- `cursor_param` (str) - the query parameter name for the cursor. Defaults to "cursor" if neither `cursor_param` nor `cursor_body_path` is provided.<br>- `cursor_body_path` (str, optional) - the JSONPath to place the cursor in the request body.<br>Note: You must provide either `cursor_param` or `cursor_body_path`, but not both. If neither is provided, `cursor_param` will default to "cursor". |
| `single_page` | SinglePagePaginator | The response will be interpreted as a single-page response, ignoring possible pagination metadata. |
| `auto` | `None` | Explicitly specify that the source should automatically detect the pagination method. |

For more complex pagination methods, you can implement a [custom paginator](https://dlthub.com/docs/general-usage/http/rest-client#implementing-a-custom-paginator), instantiate it, and use it in the configuration.

Alternatively, you can use the dictionary configuration syntax also for custom paginators. For this, you need to register your custom paginator:

```codeBlockLines_RjmQ
from dlt.sources.rest_api.config_setup import register_paginator

class CustomPaginator(SinglePagePaginator):
    # custom implementation of SinglePagePaginator
    pass

register_paginator("custom_paginator", CustomPaginator)

{
    # ...
    "paginator": {
        "type": "custom_paginator",
        "next_url_path": "paging.nextLink",
    }
}

```

### Data selection [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#data-selection "Direct link to Data selection")

The `data_selector` field in the endpoint configuration allows you to specify a JSONPath to select the data from the response. By default, the source will try to detect the locations of the data automatically.

Use this field when you need to specify the location of the data in the response explicitly.

For example, if the API response looks like this:

```codeBlockLines_RjmQ
{
    "posts": [\
        {"id": 1, "title": "Post 1"},\
        {"id": 2, "title": "Post 2"},\
        {"id": 3, "title": "Post 3"}\
    ]
}

```

You can use the following endpoint configuration:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "posts",
}

```

For a nested structure like this:

```codeBlockLines_RjmQ
{
    "results": {
        "posts": [\
            {"id": 1, "title": "Post 1"},\
            {"id": 2, "title": "Post 2"},\
            {"id": 3, "title": "Post 3"}\
        ]
    }
}

```

You can use the following endpoint configuration:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "results.posts",
}

```

Read more about [JSONPath syntax](https://github.com/h2non/jsonpath-ng?tab=readme-ov-file#jsonpath-syntax) to learn how to write selectors.

### Authentication [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#authentication "Direct link to Authentication")

For APIs that require authentication to access their endpoints, the REST API source supports various authentication methods, including token-based authentication, query parameters, basic authentication, and custom authentication. The authentication configuration is specified in the `auth` field of the [client](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#client) either as a dictionary or as an instance of the [authentication class](https://dlthub.com/docs/general-usage/http/rest-client#authentication).

#### Quick example [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#quick-example-2 "Direct link to Quick example")

Here's how to configure authentication using a bearer token:

```codeBlockLines_RjmQ
{
    "client": {
        # ...
        "auth": {
            "type": "bearer",
            "token": dlt.secrets["your_api_token"],
        },
        # ...
    },
}

```

Alternatively, you can use the authentication class directly:

```codeBlockLines_RjmQ
from dlt.sources.helpers.rest_client.auth import BearerTokenAuth

config = {
    "client": {
        "auth": BearerTokenAuth(dlt.secrets["your_api_token"]),
    },
    "resources": [\
    ]
    # ...
}

```

Since token-based authentication is one of the most common methods, you can use the following shortcut:

```codeBlockLines_RjmQ
{
    "client": {
        # ...
        "auth": {
            "token": dlt.secrets["your_api_token"],
        },
        # ...
    },
}

```

warning

Make sure to store your access tokens and other sensitive information in the `secrets.toml` file and never commit it to the version control system.

Available authentication types:

| `type` | Authentication class | Description |
| --- | --- | --- |
| `bearer` | [BearerTokenAuth](https://dlthub.com/docs/general-usage/http/rest-client#bearer-token-authentication) | Bearer token authentication.<br>Parameters:<br>- `token` (str) |
| `http_basic` | [HTTPBasicAuth](https://dlthub.com/docs/general-usage/http/rest-client#http-basic-authentication) | Basic HTTP authentication.<br>Parameters:<br>- `username` (str)<br>- `password` (str) |
| `api_key` | [APIKeyAuth](https://dlthub.com/docs/general-usage/http/rest-client#api-key-authentication) | API key authentication with key defined in the query parameters or in the headers. <br>Parameters:<br>- `name` (str) - the name of the query parameter or header<br>- `api_key` (str) - the API key value<br>- `location` (str, optional) - the location of the API key in the request. Can be `query` or `header`. Default is `header` |
| `oauth2_client_credentials` | [OAuth2ClientCredentials](https://dlthub.com/docs/general-usage/http/rest-client#oauth-20-authorization) | OAuth 2.0 Client Credentials authorization for server-to-server communication without user consent. <br>Parameters:<br>- `access_token` (str, optional) - the temporary token. Usually not provided here because it is automatically obtained from the server by exchanging `client_id` and `client_secret`. Default is `None`<br>- `access_token_url` (str) - the URL to request the `access_token` from<br>- `client_id` (str) - identifier for your app. Usually issued via a developer portal<br>- `client_secret` (str) - client credential to obtain authorization. Usually issued via a developer portal<br>- `access_token_request_data` (dict, optional) - A dictionary with data required by the authorization server apart from the `client_id`, `client_secret`, and `"grant_type": "client_credentials"`. Defaults to `None`<br>- `default_token_expiration` (int, optional) - The time in seconds after which the temporary access token expires. Defaults to 3600.<br>- `session` (requests.Session, optional) - a custom session object. Mostly used for testing |

For more complex authentication methods, you can implement a [custom authentication class](https://dlthub.com/docs/general-usage/http/rest-client#implementing-custom-authentication) and use it in the configuration.

You can use the dictionary configuration syntax also for custom authentication classes after registering them as follows:

```codeBlockLines_RjmQ
from dlt.sources.rest_api.config_setup import register_auth

class CustomAuth(AuthConfigBase):
    pass

register_auth("custom_auth", CustomAuth)

{
    # ...
    "auth": {
        "type": "custom_auth",
        "api_key": dlt.secrets["sources.my_source.my_api_key"],
    }
}

```

### Define resource relationships [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#define-resource-relationships "Direct link to Define resource relationships")

When you have a resource that depends on another resource (for example, you must fetch a parent resource to get an ID needed to fetch the child), you can reference fields in the parent resource using special placeholders.
This allows you to link one or more [path](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#via-request-path), [query string](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#via-query-string-parameters) or [JSON body](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#via-json-body) parameters in the child resource to fields in the parent resource's data.

#### Via request path [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#via-request-path "Direct link to Via request path")

In the GitHub example, the `issue_comments` resource depends on the `issues` resource. The `resources.issues.number` placeholder links the `number` field in the `issues` resource data to the current request's path parameter.

```codeBlockLines_RjmQ
{
    "resources": [\
        {\
            "name": "issues",\
            "endpoint": {\
                "path": "issues",\
                # ...\
            },\
        },\
        {\
            "name": "issue_comments",\
            "endpoint": {\
                "path": "issues/{resources.issues.number}/comments",\
            },\
            "include_from_parent": ["id"],\
        },\
    ],
}

```

This configuration tells the source to get issue numbers from the `issues` resource data and use them to fetch comments for each issue number. So for each issue item, `"{resources.issues.number}"` is replaced by the issue number in the request path.
For example, if the `issues` resource yields the following data:

```codeBlockLines_RjmQ
[\
    {"id": 1, "number": 123},\
    {"id": 2, "number": 124},\
    {"id": 3, "number": 125}\
]

```

The `issue_comments` resource will make requests to the following endpoints:

- `issues/123/comments`
- `issues/124/comments`
- `issues/125/comments`

The syntax for the placeholder is `resources.<parent_resource_name>.<field_name>`.

#### Via query string parameters [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#via-query-string-parameters "Direct link to Via query string parameters")

The placeholder syntax can also be used in the query string parameters. For example, in an API which lets you fetch a blog posts (via `/posts`) and their comments (via `/comments?post_id=<post_id>`), you can define a resource `posts` and a resource `post_comments` which depends on the `posts` resource. You can then reference the `id` field from the `posts` resource in the `post_comments` resource:

```codeBlockLines_RjmQ
{
    "resources": [\
        "posts",\
        {\
            "name": "post_comments",\
            "endpoint": {\
                "path": "comments",\
                "params": {\
                    "post_id": "{resources.posts.id}",\
                },\
            },\
        },\
    ],
}

```

Similar to the GitHub example above, if the `posts` resource yields the following data:

```codeBlockLines_RjmQ
[\
    {"id": 1, "title": "Post 1"},\
    {"id": 2, "title": "Post 2"},\
    {"id": 3, "title": "Post 3"}\
]

```

The `post_comments` resource will make requests to the following endpoints:

- `comments?post_id=1`
- `comments?post_id=2`
- `comments?post_id=3`

#### Via JSON body [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#via-json-body "Direct link to Via JSON body")

In many APIs, you can send a complex query or configuration through a POST request's JSON body rather than in the request path or query parameters. For example, consider an imaginary `/search` endpoint that supports multiple filters and settings. You might have a parent resource `posts` with each post's `id` and a second resource, `post_details`, that uses `id` to perform a custom search.

In the example below we reference the `posts` resource's `id` field in the JSON body via placeholders:

```codeBlockLines_RjmQ
{
    "resources": [\
        "posts",\
        {\
            "name": "post_details",\
            "endpoint": {\
                "path": "search",\
                "method": "POST",\
                "json": {\
                    "filters": {\
                        "id": "{resources.posts.id}",\
                    },\
                    "order": "desc",\
                    "limit": 5,\
                }\
            },\
        },\
    ],
}

```

#### Legacy syntax: `resolve` field in parameter configuration [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#legacy-syntax-resolve-field-in-parameter-configuration "Direct link to legacy-syntax-resolve-field-in-parameter-configuration")

warning

`resolve` works only for path parameters. The new placeholder syntax is more flexible and recommended for new configurations.

An alternative, legacy way to define resource relationships is to use the `resolve` field in the parameter configuration.
Here's the same example as above that uses the `resolve` field:

```codeBlockLines_RjmQ
{
    "resources": [\
        {\
            "name": "issues",\
            "endpoint": {\
                "path": "issues",\
                # ...\
            },\
        },\
        {\
            "name": "issue_comments",\
            "endpoint": {\
                "path": "issues/{issue_number}/comments",\
                "params": {\
                    "issue_number": {\
                        "type": "resolve",\
                        "resource": "issues",\
                        "field": "number",\
                    }\
                },\
            },\
            "include_from_parent": ["id"],\
        },\
    ],
}

```

The syntax for the `resolve` field in parameter configuration is:

```codeBlockLines_RjmQ
{
    "<parameter_name>": {
        "type": "resolve",
        "resource": "<parent_resource_name>",
        "field": "<parent_resource_field_name_or_jsonpath>",
    }
}

```

The `field` value can be specified as a [JSONPath](https://github.com/h2non/jsonpath-ng?tab=readme-ov-file#jsonpath-syntax) to select a nested field in the parent resource data. For example: `"field": "items[0].id"`.

#### Resolving multiple path parameters from a parent resource [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#resolving-multiple-path-parameters-from-a-parent-resource "Direct link to Resolving multiple path parameters from a parent resource")

When a child resource depends on multiple fields from a single parent resource, you can define multiple `resolve` parameters in the endpoint configuration. For example:

```codeBlockLines_RjmQ
{
    "resources": [\
        "groups",\
        {\
            "name": "users",\
            "endpoint": {\
                "path": "groups/{group_id}/users",\
                "params": {\
                    "group_id": {\
                        "type": "resolve",\
                        "resource": "groups",\
                        "field": "id",\
                    },\
                },\
            },\
        },\
        {\
            "name": "user_details",\
            "endpoint": {\
                "path": "groups/{group_id}/users/{user_id}/details",\
                "params": {\
                    "group_id": {\
                        "type": "resolve",\
                        "resource": "users",\
                        "field": "group_id",\
                    },\
                    "user_id": {\
                        "type": "resolve",\
                        "resource": "users",\
                        "field": "id",\
                    },\
                },\
            },\
        },\
    ],
}

```

In the configuration above:

- The `users` resource depends on the `groups` resource, resolving the `group_id` parameter from the `id` field in `groups`.
- The `user_details` resource depends on the `users` resource, resolving both `group_id` and `user_id` parameters from fields in `users`.

#### Include fields from the parent resource [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#include-fields-from-the-parent-resource "Direct link to Include fields from the parent resource")

You can include data from the parent resource in the child resource by using the `include_from_parent` field in the resource configuration. For example:

```codeBlockLines_RjmQ
{
    "name": "issue_comments",
    "endpoint": {
        ...
    },
    "include_from_parent": ["id", "title", "created_at"],
}

```

This will include the `id`, `title`, and `created_at` fields from the `issues` resource in the `issue_comments` resource data. The names of the included fields will be prefixed with the parent resource name and an underscore ( `_`) like so: `_issues_id`, `_issues_title`, `_issues_created_at`.

### Define a resource which is not a REST endpoint [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#define-a-resource-which-is-not-a-rest-endpoint "Direct link to Define a resource which is not a REST endpoint")

Sometimes, we want to request endpoints with specific values that are not returned by another endpoint.
Thus, you can also include arbitrary dlt resources in your `RESTAPIConfig` instead of defining a resource for every path!

In the following example, we want to load the issues belonging to three repositories.
Instead of defining three different issues resources, one for each of the paths `dlt-hub/dlt/issues/`, `dlt-hub/verified-sources/issues/`, `dlt-hub/dlthub-education/issues/`, we have a resource `repositories` which yields a list of repository names that will be fetched by the dependent resource `issues`.

```codeBlockLines_RjmQ
from dlt.sources.rest_api import RESTAPIConfig

@dlt.resource()
def repositories() -> Generator[List[Dict[str, Any]], Any, Any]:
    """A seed list of repositories to fetch"""
    yield [{"name": "dlt"}, {"name": "verified-sources"}, {"name": "dlthub-education"}]

config: RESTAPIConfig = {
    "client": {"base_url": "https://github.com/api/v2"},
    "resources": [\
        {\
            "name": "issues",\
            "endpoint": {\
                "path": "dlt-hub/{repository}/issues/",\
                "params": {\
                    "repository": {\
                        "type": "resolve",\
                        "resource": "repositories",\
                        "field": "name",\
                    },\
                },\
            },\
        },\
        repositories(),\
    ],
}

```

Be careful that the parent resource needs to return `Generator[List[Dict[str, Any]]]`. Thus, the following will NOT work:

```codeBlockLines_RjmQ
@dlt.resource
def repositories() -> Generator[Dict[str, Any], Any, Any]:
    """Not working seed list of repositories to fetch"""
    yield from [{"name": "dlt"}, {"name": "verified-sources"}, {"name": "dlthub-education"}]

```

### Processing steps: filter and transform data [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#processing-steps-filter-and-transform-data "Direct link to Processing steps: filter and transform data")

The `processing_steps` field in the resource configuration allows you to apply transformations to the data fetched from the API before it is loaded into your destination. This is useful when you need to filter out certain records, modify the data structure, or anonymize sensitive information.

Each processing step is a dictionary specifying the type of operation ( `filter` or `map`) and the function to apply. Steps apply in the order they are listed.

#### Quick example [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#quick-example-3 "Direct link to Quick example")

```codeBlockLines_RjmQ
def lower_title(record):
    record["title"] = record["title"].lower()
    return record

config: RESTAPIConfig = {
    "client": {
        "base_url": "https://api.example.com",
    },
    "resources": [\
        {\
            "name": "posts",\
            "processing_steps": [\
                {"filter": lambda x: x["id"] < 10},\
                {"map": lower_title},\
            ],\
        },\
    ],
}

```

In the example above:

- First, the `filter` step uses a lambda function to include only records where `id` is less than 10.
- Thereafter, the `map` step applies the `lower_title` function to each remaining record.

#### Using `filter` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#using-filter "Direct link to using-filter")

The `filter` step allows you to exclude records that do not meet certain criteria. The provided function should return `True` to keep the record or `False` to exclude it:

```codeBlockLines_RjmQ
{
    "name": "posts",
    "endpoint": "posts",
    "processing_steps": [\
        {"filter": lambda x: x["id"] in [10, 20, 30]},\
    ],
}

```

In this example, only records with `id` equal to 10, 20, or 30 will be included.

#### Using `map` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#using-map "Direct link to using-map")

The `map` step allows you to modify the records fetched from the API. The provided function should take a record as an argument and return the modified record. For example, to anonymize the `email` field:

```codeBlockLines_RjmQ
def anonymize_email(record):
    record["email"] = "REDACTED"
    return record

config: RESTAPIConfig = {
    "client": {
        "base_url": "https://api.example.com",
    },
    "resources": [\
        {\
            "name": "users",\
            "processing_steps": [\
                {"map": anonymize_email},\
            ],\
        },\
    ],
}

```

#### Combining `filter` and `map` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#combining-filter-and-map "Direct link to combining-filter-and-map")

You can combine multiple processing steps to achieve complex transformations:

```codeBlockLines_RjmQ
{
    "name": "posts",
    "endpoint": "posts",
    "processing_steps": [\
        {"filter": lambda x: x["id"] < 10},\
        {"map": lower_title},\
        {"filter": lambda x: "important" in x["title"]},\
    ],
}

```

tip

#### Best practices [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#best-practices "Direct link to Best practices")

1. Order matters: Processing steps are applied in the order they are listed. Be mindful of the sequence, especially when combining `map` and `filter`.
2. Function definition: Define your filter and map functions separately for clarity and reuse.
3. Use `filter` to exclude records early in the process to reduce the amount of data that needs to be processed.
4. Combine consecutive `map` steps into a single function for faster execution.

## Incremental loading [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#incremental-loading "Direct link to Incremental loading")

Some APIs provide a way to fetch only new or changed data (most often by using a timestamp field like `updated_at`, `created_at`, or incremental IDs).
This is called [incremental loading](https://dlthub.com/docs/general-usage/incremental-loading) and is very useful as it allows you to reduce the load time and the amount of data transferred.

Let's continue with our imaginary blog API example to understand incremental loading with query parameters.

Imagine we have the following endpoint `https://api.example.com/posts` and it:

1. Accepts a `created_since` query parameter to fetch blog posts created after a certain date.
2. Returns a list of posts with the `created_at` field for each post.

For example, if we query the endpoint with GET request `https://api.example.com/posts?created_since=2024-01-25`, we get the following response:

```codeBlockLines_RjmQ
{
    "results": [\
        {"id": 1, "title": "Post 1", "created_at": "2024-01-26"},\
        {"id": 2, "title": "Post 2", "created_at": "2024-01-27"},\
        {"id": 3, "title": "Post 3", "created_at": "2024-01-28"}\
    ]
}

```

When the API endpoint supports incremental loading, you can configure dlt to load only the new or changed data using these three methods:

1. Using [placeholders for incremental loading](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#using-placeholders-for-incremental-loading)
2. Defining a special parameter in the `params` section of the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) (DEPRECATED)
3. Using the `incremental` field in the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) with the `start_param` field (DEPRECATED)

caution

The last two methods are deprecated and will be removed in a future dlt version.

### Using placeholders for incremental loading [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#using-placeholders-for-incremental-loading "Direct link to Using placeholders for incremental loading")

The most flexible way to configure incremental loading is to use placeholders in the request configuration along with the `incremental` section.
Here's how it works:

1. Define the `incremental` section in the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) to specify the cursor path (where to find the incremental value in the response) and initial value (the value to start the incremental loading from).
2. Use the placeholder `{incremental.start_value}` in the request configuration to reference the incremental value.

Let's take the example from the previous section and configure it using placeholders:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "results",
    "params": {
        "created_since": "{incremental.start_value}",  # Uses cursor value in query parameter
    },
    "incremental": {
        "cursor_path": "created_at",
        "initial_value": "2024-01-25T00:00:00Z",
    },
}

```

When you first run this pipeline, dlt will:

1. Replace `{incremental.start_value}` with `2024-01-25T00:00:00Z` (the initial value)
2. Make a GET request to `https://api.example.com/posts?created_since=2024-01-25T00:00:00Z`
3. Parse the response (e.g., posts with created\_at values like "2024-01-26", "2024-01-27", "2024-01-28")
4. Track the maximum value found in the "created\_at" field (in this case, "2024-01-28")

On the next pipeline run, dlt will:

1. Replace `{incremental.start_value}` with "2024-01-28" (the last seen maximum value)
2. Make a GET request to `https://api.example.com/posts?created_since=2024-01-28`
3. The API will only return posts created on or after January 28th

Let's break down the configuration:

1. We explicitly set `data_selector` to `"results"` to select the list of posts from the response. This is optional; if not set, dlt will try to auto-detect the data location.
2. We define the `created_since` parameter in `params` section and use the placeholder `{incremental.start_value}` to reference the incremental value.

Placeholders are versatile and can be used in various request components. Here are some examples:

#### In JSON body (for POST requests) [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#in-json-body-for-post-requests "Direct link to In JSON body (for POST requests)")

If the API lets you filter the data by a range of dates (e.g. `fromDate` and `toDate`), you can use the placeholder in the JSON body:

```codeBlockLines_RjmQ
{
    "path": "posts/search",
    "method": "POST",
    "json": {
        "filters": {
            "fromDate": "{incremental.start_value}",  # In JSON body
            "toDate": "2024-03-25"
        },
        "limit": 1000
    },
    "incremental": {
        "cursor_path": "created_at",
        "initial_value": "2024-01-25T00:00:00Z",
    },
}

```

#### In path parameters [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#in-path-parameters "Direct link to In path parameters")

Some APIs use path parameters to filter the data:

```codeBlockLines_RjmQ
{
    "path": "posts/since/{incremental.start_value}/list",  # In URL path
    "incremental": {
        "cursor_path": "created_at",
        "initial_value": "2024-01-25",
    },
}

```

#### In request headers [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#in-request-headers "Direct link to In request headers")

It's not so common, but you can also use placeholders in the request headers:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "headers": {
        "X-Since-Timestamp": "{incremental.start_value}"  # In custom header
    },
    "incremental": {
        "cursor_path": "created_at",
        "initial_value": "2024-01-25T00:00:00Z",
    },
}

```

You can also use different placeholder variants depending on your needs:

| Placeholder | Description |
| --- | --- |
| `{incremental.start_value}` | The value to use as the starting point for this request (either the initial value or the last tracked maximum value) |
| `{incremental.initial_value}` | Always uses the initial value specified in the configuration |
| `{incremental.last_value}` | The last seen value (same as start\_value in most cases, see the [incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor) guide for more details) |
| `{incremental.end_value}` | The end value if specified in the configuration |

### Legacy method: Incremental loading in `params` (DEPRECATED) [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#legacy-method-incremental-loading-in-params-deprecated "Direct link to legacy-method-incremental-loading-in-params-deprecated")

caution

DEPRECATED: This method is deprecated and will be removed in a future version. Use the [placeholder method](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#using-placeholders-for-incremental-loading) instead.

note

This method only works for query string parameters. For other request parts (path, JSON body, headers), use the [placeholder method](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#using-placeholders-for-incremental-loading).

For query string parameters, you can also specify incremental loading directly in the `params` section:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "results",  # Optional JSONPath to select the list of posts
    "params": {
        "created_since": {
            "type": "incremental",
            "cursor_path": "created_at", # The JSONPath to the field we want to track in each post
            "initial_value": "2024-01-25",
        },
    },
}

```

Above we define the `created_since` parameter as an incremental parameter as:

```codeBlockLines_RjmQ
{
    "created_since": {
        "type": "incremental",
        "cursor_path": "created_at",
        "initial_value": "2024-01-25",
    },
}

```

The fields are:

- `type`: The type of the parameter definition. In this case, it must be set to `incremental`.
- `cursor_path`: The JSONPath to the field within each item in the list. The value of this field will be used in the next request. In the example above, our items look like `{"id": 1, "title": "Post 1", "created_at": "2024-01-26"}` so to track the created time, we set `cursor_path` to `"created_at"`. Note that the JSONPath starts from the root of the item (dict) and not from the root of the response.
- `initial_value`: The initial value for the cursor. This is the value that will initialize the state of incremental loading. In this case, it's `2024-01-25`. The value type should match the type of the field in the data item.

### Incremental loading using the `incremental` field (DEPRECATED) [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#incremental-loading-using-the-incremental-field-deprecated "Direct link to incremental-loading-using-the-incremental-field-deprecated")

caution

DEPRECATED: This method is deprecated and will be removed in a future dlt version. Use the [placeholder method](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#using-placeholders-for-incremental-loading) instead.

Another alternative method is to use the `incremental` field in the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) while specifying names of the query string parameters to be used as start and end conditions.

Let's take the same example as above and configure it using the `incremental` field:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "results",
    "incremental": {
        "start_param": "created_since",
        "cursor_path": "created_at",
        "initial_value": "2024-01-25",
    },
}

```

The full available configuration for the `incremental` field is:

```codeBlockLines_RjmQ
{
    "incremental": {
        "start_param": "<start_parameter_name>",
        "end_param": "<end_parameter_name>",
        "cursor_path": "<path_to_cursor_field>",
        "initial_value": "<initial_value>",
        "end_value": "<end_value>",
        "convert": my_callable,
    }
}

```

The fields are:

- `start_param` (str): The name of the query parameter to be used as the start condition. If we use the example above, it would be `"created_since"`.
- `end_param` (str): The name of the query parameter to be used as the end condition. This is optional and can be omitted if you only need to track the start condition. This is useful when you need to fetch data within a specific range and the API supports end conditions (like the `created_before` query parameter).
- `cursor_path` (str): The JSONPath to the field within each item in the list. This is the field that will be used to track the incremental loading. In the example above, it's `"created_at"`.
- `initial_value` (str): The initial value for the cursor. This is the value that will initialize the state of incremental loading.
- `end_value` (str): The end value for the cursor to stop the incremental loading. This is optional and can be omitted if you only need to track the start condition. If you set this field, `initial_value` needs to be set as well.
- `convert` (callable): A callable that converts the cursor value into the format that the query parameter requires. For example, a UNIX timestamp can be converted into an ISO 8601 date or a date can be converted into `created_at+gt+{date}`.

See the [incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor) guide for more details.

If you encounter issues with incremental loading, see the [troubleshooting section](https://dlthub.com/docs/general-usage/incremental/troubleshooting) in the incremental loading guide.

### Convert the incremental value before calling the API [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#convert-the-incremental-value-before-calling-the-api "Direct link to Convert the incremental value before calling the API")

If you need to transform the values in the cursor field before passing them to the API endpoint, you can specify a callable under the key `convert`. For example, the API might return UNIX epoch timestamps but expects to be queried with an ISO 8601 date. To achieve that, we can specify a function that converts from the date format returned by the API to the date format required for API requests.

In the following examples, `1704067200` is returned from the API in the field `updated_at`, but the API will be called with `?created_since=2024-01-01`.

Incremental loading using the `params` field:

```codeBlockLines_RjmQ
{
    "created_since": {
        "type": "incremental",
        "cursor_path": "updated_at",
        "initial_value": "1704067200",
        "convert": lambda epoch: pendulum.from_timestamp(int(epoch)).to_date_string(),
    }
}

```

Incremental loading using the `incremental` field:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "results",
    "incremental": {
        "start_param": "created_since",
        "cursor_path": "updated_at",
        "initial_value": "1704067200",
        "convert": lambda epoch: pendulum.from_timestamp(int(epoch)).to_date_string(),
    },
}

```

## Troubleshooting [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#troubleshooting "Direct link to Troubleshooting")

If you encounter issues while running the pipeline, enable [logging](https://dlthub.com/docs/running-in-production/running#set-the-log-level-and-format) for detailed information about the execution:

```codeBlockLines_RjmQ
RUNTIME__LOG_LEVEL=INFO python my_script.py

```

This also provides details on the HTTP requests.

### Configuration issues [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#configuration-issues "Direct link to Configuration issues")

#### Getting validation errors [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#getting-validation-errors "Direct link to Getting validation errors")

When you are running the pipeline and getting a `DictValidationException`, it means that the [source configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#source-configuration) is incorrect. The error message provides details on the issue, including the path to the field and the expected type.

For example, if you have a source configuration like this:

```codeBlockLines_RjmQ
config: RESTAPIConfig = {
    "client": {
        # ...
    },
    "resources": [\
        {\
            "name": "issues",\
            "params": {             # <- Wrong: this should be inside\
                "sort": "updated",  #    the endpoint field below\
            },\
            "endpoint": {\
                "path": "issues",\
                # "params": {       # <- Correct configuration\
                #     "sort": "updated",\
                # },\
            },\
        },\
        # ...\
    ],
}

```

You will get an error like this:

```codeBlockLines_RjmQ
dlt.common.exceptions.DictValidationException: In path .: field 'resources[0]'
expects the following types: str, EndpointResource. Provided value {'name': 'issues', 'params': {'sort': 'updated'},
'endpoint': {'path': 'issues', ... }} with type 'dict' is invalid with the following errors:
For EndpointResource: In path ./resources[0]: following fields are unexpected {'params'}

```

It means that in the first resource configuration ( `resources[0]`), the `params` field should be inside the `endpoint` field.

tip

Import the `RESTAPIConfig` type from the `rest_api` module to have convenient hints in your editor/IDE and use it to define the configuration object.

```codeBlockLines_RjmQ
from dlt.sources.rest_api import RESTAPIConfig

```

#### Getting wrong data or no data [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#getting-wrong-data-or-no-data "Direct link to Getting wrong data or no data")

If incorrect data is received from an endpoint, check the `data_selector` field in the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration). Ensure the JSONPath is accurate and points to the correct data in the response body. `rest_api` attempts to auto-detect the data location, which may not always succeed. See the [data selection](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#data-selection) section for more details.

#### Getting insufficient data or incorrect pagination [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#getting-insufficient-data-or-incorrect-pagination "Direct link to Getting insufficient data or incorrect pagination")

Check the `paginator` field in the configuration. When not explicitly specified, the source tries to auto-detect the pagination method. If auto-detection fails, or the system is unsure, a warning is logged. For production environments, we recommend specifying an explicit paginator in the configuration. See the [pagination](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#pagination) section for more details. Some APIs may have non-standard pagination methods, and you may need to implement a [custom paginator](https://dlthub.com/docs/general-usage/http/rest-client#implementing-a-custom-paginator).

#### Incremental loading not working [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#incremental-loading-not-working "Direct link to Incremental loading not working")

See the [troubleshooting guide](https://dlthub.com/docs/general-usage/incremental/troubleshooting) for incremental loading issues.

#### Getting HTTP 404 errors [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#getting-http-404-errors "Direct link to Getting HTTP 404 errors")

Some APIs may return 404 errors for resources that do not exist or have no data. Manage these responses by configuring the `ignore` action in [response actions](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/advanced#response-actions).

### Authentication issues [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#authentication-issues "Direct link to Authentication issues")

If you are experiencing 401 (Unauthorized) errors, this could indicate:

- Incorrect authorization credentials. Verify credentials in the `secrets.toml`. Refer to [Secret and configs](https://dlthub.com/docs/general-usage/credentials/setup#troubleshoot-configuration-errors) for more information.
- An incorrect authentication type. Consult the API documentation for the proper method. See the [authentication](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#authentication) section for details. For some APIs, a [custom authentication method](https://dlthub.com/docs/general-usage/http/rest-client#implementing-custom-authentication) may be required.

### General guidelines [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#general-guidelines "Direct link to General guidelines")

The `rest_api` source uses the [RESTClient](https://dlthub.com/docs/general-usage/http/rest-client) class for HTTP requests. Refer to the RESTClient [troubleshooting guide](https://dlthub.com/docs/general-usage/http/rest-client#troubleshooting) for debugging tips.

For further assistance, join our [Slack community](https://dlthub.com/community). We're here to help!

- [Quick example](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#quick-example)
- [Setup](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#setup)
  - [Prerequisites](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#prerequisites)
  - [Initialize the REST API source](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#initialize-the-rest-api-source)
  - [Add credentials](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#add-credentials)
- [Run the pipeline](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#run-the-pipeline)
- [Source configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#source-configuration)
  - [Quick example](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#quick-example-1)
  - [Configuration structure](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#configuration-structure)
  - [Resource configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#resource-configuration)
  - [Endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration)
  - [Pagination](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#pagination)
  - [Data selection](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#data-selection)
  - [Authentication](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#authentication)
  - [Define resource relationships](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#define-resource-relationships)
  - [Define a resource which is not a REST endpoint](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#define-a-resource-which-is-not-a-rest-endpoint)
  - [Processing steps: filter and transform data](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#processing-steps-filter-and-transform-data)
- [Incremental loading](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#incremental-loading)
  - [Using placeholders for incremental loading](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#using-placeholders-for-incremental-loading)
  - [Legacy method: Incremental loading in `params` (DEPRECATED)](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#legacy-method-incremental-loading-in-params-deprecated)
  - [Incremental loading using the `incremental` field (DEPRECATED)](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#incremental-loading-using-the-incremental-field-deprecated)
  - [Convert the incremental value before calling the API](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#convert-the-incremental-value-before-calling-the-api)
- [Troubleshooting](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#troubleshooting)
  - [Configuration issues](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#configuration-issues)
  - [Authentication issues](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#authentication-issues)
  - [General guidelines](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#general-guidelines)

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

----- https://dlthub.com/docs/dlt-ecosystem/llm-tooling/cursor-restapi -----

Version: 1.11.0 (latest)

On this page

## Overview [​](https://dlthub.com/docs/dlt-ecosystem/llm-tooling/cursor-restapi\#overview "Direct link to Overview")

The purpose of this document is to explain how to build REST API sources with Cursor and dlt. While the focus here is on REST APIs, this approach can be generalized to other source types such as Python imperative/full code dlthub sources. We choose REST APIs because they are ideal for this process, they are inherently self-documenting, exposing endpoints and methods in a way that enables us to easily troubleshoot and refine our vibe coding.

With REST API sources being configuration-driven and vibe coding based on prompts, users don't necessarily need to know how to code. However, it is important to understand how an API is represented and how data should be structured at the destination, such as managing incremental loading configurations. This foundational knowledge ensures that even non-developers can effectively contribute to building and maintaining these data pipelines.

We also introduce experimental [dlt ai](https://dlthub.com/docs/reference/command-line-interface#dlt-ai-setup) command that distributes relevant cursor rules.

## 1\. Problem definition & feature extraction [​](https://dlthub.com/docs/dlt-ecosystem/llm-tooling/cursor-restapi\#1-problem-definition--feature-extraction "Direct link to 1. Problem definition & feature extraction")

Building a data pipeline can be separated into two distinct problems, each with their own challenges:

1. **Extraction:** Identifying and gathering key configuration details from various sources.
2. **Pipeline construction:** Using those details to build a robust data ingestion pipeline.

Consider these steps separately, as this will aid you in troubleshooting.

![image](https://storage.googleapis.com/dlt-blog-images/dlt-cursor-restapi.drawio.png)

### 1.1 Extracting features from information sources [​](https://dlthub.com/docs/dlt-ecosystem/llm-tooling/cursor-restapi\#11-extracting-features-from-information-sources "Direct link to 1.1 Extracting features from information sources")

The best source of information for building a pipeline is another working pipeline. This is because many documentations do not contain all the information required to build a top-notch pipeline. For example, API docs often do not contain all the information needed for creating an incremental strategy - meaning you can still build a pipeline, but it will not work as efficiently as it could.

So here is the ranking of what sources you could use

1. **Other pipelines**: Legacy pipelines, sources and connectors from other languages or frameworks, or any code that shows how to request, such as API wrappers.
2. **OpenAPI spec**: Contains very detailed specification of all endpoints and authentication, but does not provide info on pagination, incremental loading or primary keys for data entities.
3. **Docs + HTTP responses**. Reading the docs, creating an initial working pipeline and then requesting from the API data so we can infer the rest of the missing info.
4. **Scraped code, LLM memory**: When nothing is available but a public API exists, such as APIs that power public websites, we can try inferring how it is called from other websites’ code that calls it.

### 1.2 Understanding the parameters [​](https://dlthub.com/docs/dlt-ecosystem/llm-tooling/cursor-restapi\#12-understanding-the-parameters "Direct link to 1.2 Understanding the parameters")

Since there are only partial naming standards for these parameters, we describe the ones we use here.

- **Top-level client settings:**
  - **client.base\_url:** The API’s root URL.
  - **client.auth:** Authentication details (token/credentials, reference to dlthub secrets).
  - **client.headers:** Required custom headers.
  - **client.paginator:** Pagination configuration (type such as "cursor", and associated parameters like `next_url_path`, `offset_param`, `limit_param`).
- **Per-resource settings:**
  - **name:** The resource/table name.
  - **endpoint.path:** The REST API endpoint’s path.
  - **endpoint.method:** HTTP method (defaults to GET if unspecified).
  - **endpoint.params:** Query parameters including defaults and dynamic placeholders.
  - **endpoint.json:** POST payload (with support for placeholders) when applicable.
  - **endpoint.data\_selector:** JSONPath or equivalent to extract the actual data.
  - **endpoint.paginator:** Resource-specific pagination settings if differing from client-level.
  - **endpoint.response\_actions:** Handlers for varied HTTP status codes or responses.
  - **write\_disposition:** Options like append, replace, or merge.
  - **primary\_key:** Field(s) used for deduplication.
  - **incremental:** Configuration parameters for incremental loading ( `start_param`, `end_param`, `cursor_path`, etc.).
  - **include\_from\_parent:** Inheriting fields from parent resources.
  - **processing\_steps:** Transformations (filters, mappings) applied to the records.
- **Resource relationships:**
  - Define parent-child relationships and how to reference fields using placeholders (for example when requesting details of an entity by ID).

## 2\. Setting up Cursor for REST API source generation [​](https://dlthub.com/docs/dlt-ecosystem/llm-tooling/cursor-restapi\#2-setting-up-cursor-for-rest-api-source-generation "Direct link to 2. Setting up Cursor for REST API source generation")

### 2.1 Understanding Cursor [​](https://dlthub.com/docs/dlt-ecosystem/llm-tooling/cursor-restapi\#21-understanding-cursor "Direct link to 2.1 Understanding Cursor")

Cursor is an AI-powered IDE built on Visual Studio Code that accelerates your workflow by integrating intelligent features into a familiar editor. This enables you to build REST API sources with dlt through prompt-based code generation, clear handling of parameters (such as endpoints, authentication, and pagination), and real-time code validation.

This produces self-documenting, self-maintaining, and easy to troubleshoot sources.

### 2.2 Adding rules to cursor [​](https://dlthub.com/docs/dlt-ecosystem/llm-tooling/cursor-restapi\#22-adding-rules-to-cursor "Direct link to 2.2 Adding rules to cursor")

We bundled a set of cursor rules that help build REST API pipelines with `dlt`. To use them, you should start by [initializing a new `dlt` project](https://dlthub.com/docs/walkthroughs/create-a-pipeline):

```codeBlockLines_RjmQ
dlt init rest_api duckdb

```

This will add `rest_api_pipeline.py` to your current folder with pretty detailed usage examples. We see improvements in generated code if this file is
added to the cursor agent context.

Now you can add a set of project rules we use to develop REST API sources:

```codeBlockLines_RjmQ
dlt ai setup cursor

```

They are pretty useful when converting OpenAPI specs or legacy source code into `dlt` pipelines. Some rules are always included,
others are triggered by the agent based on their descriptions. You can review your project rules by looking in `.cursor/rules` folder
or in Cursor Settings >> Rules.

note

🚧 This command is a work in progress and currently only adds rules focused on REST API Source. We plan for more code editors,
`dlt` use cases and mcp tools to be added this way so command options will surely change.

We are also happy to get improvements. Make a PR with an update [here](https://github.com/dlt-hub/verified-sources/ai).

### 2.3 Configuring Cursor with documentation [​](https://dlthub.com/docs/dlt-ecosystem/llm-tooling/cursor-restapi\#23-configuring-cursor-with-documentation "Direct link to 2.3 Configuring Cursor with documentation")

Under `Cursor Settings` > `Features` > `Docs`, you will see the docs you have added. You can edit, delete, or add new docs here.

We recommend adding documentation scoped for a specific task. Here you may try:

- REST API Source documentation: [https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest\_api/](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/)
- Core `dlt` concepts & usage: [https://dlthub.com/docs/general-usage/resource](https://dlthub.com/docs/general-usage/resource) (optional)

We observed that cursor is not able to ingest full `dlt` docs (there are bug reports in cursor about their docs crawler). Also putting too much information into the agent context will prevent generation of correct code.

### 2.4 Integrating local docs [​](https://dlthub.com/docs/dlt-ecosystem/llm-tooling/cursor-restapi\#24-integrating-local-docs "Direct link to 2.4 Integrating local docs")

If you have local docs in a folder in your codebase, Cursor will automatically index them unless they are added to `.gitignore` or `.cursorignore` files.

To improve accuracy, make sure any files or docs that could confound the search are ignored.

### 2.5 Documentation augmentation [​](https://dlthub.com/docs/dlt-ecosystem/llm-tooling/cursor-restapi\#25-documentation-augmentation "Direct link to 2.5 Documentation augmentation")

If existing documentation for a task is regularly ignored by LLMs, consider using reverse prompts outside of cursor to create LLM optimised docs, and then make those available to cursor as local documentation. You could use a RAG or something like DeepResearch for this task. You can then ask cursor to improve the cursor rules with this documentation.

### 2.6 Model and context selection [​](https://dlthub.com/docs/dlt-ecosystem/llm-tooling/cursor-restapi\#26-model-and-context-selection "Direct link to 2.6 Model and context selection")

We had the best results with Claude 3.7-sonnet (which requires paid version of Cursor). Weaker models were not able to comprehend the required
context in full and were not able to use tools and follow workflows consistently.

We typically put the following in the context:

1. docs: REST API Source documentation
2. example `rest_api_pipeline.py`
3. yaml definitions, OpenAPI specs or references to files implementing legacy data source
4. we've noticed that it makes sense to copy initial section from `build-rest-api` rule, which Claude follows pretty well.

## 3\. Running the workflow [​](https://dlthub.com/docs/dlt-ecosystem/llm-tooling/cursor-restapi\#3-running-the-workflow "Direct link to 3. Running the workflow")

Before you start, it might be a good idea to scaffold a dlt pipeline by running dlt init with the REST API source:

`dlt init rest_api duckdb`

Consider prompting for instructions how to make credentials for the source and add them to the project.

1. **Initial prompt:**
   - Make sure your cursor rule is added, dlt REST API docs are added and your source for information is added.

   - Prompt something along the lines:

     "Please build a REST API (dlthub) pipeline using the details from the documentation you added in context. Please include all endpoints you find and try to capture the incremental logic you can find. Use the build REST API cursor rule. Build it in a python file called “my\_pipeline.py"

   - If it builds what looks like a sensible REST API source, go on with inspection. If not and if the LLM ended up writing random code, just start over.
2. **Inspect the generated code:**
   - Look into the endpoints that were added. Often the LLM will stop after adding a bunch, so you might need to prompt “Did you add all the endpoints? Please do”.
   - Look into incremental loading configs, does it align with documentation, is it sensible?
3. **Run and test the code:**
   - Execute the code in a controlled test environment.
   - If it succeeds, double check the outputs and make double sure your incremental and pagination are correct - both of them could cause silent failures, where only some pages are loaded, or pagination never finishes (some APIs re-start from page 0 once they finish pages), or the wrong incremental strategy removes records based on the wrong key, or the wrong data is extracted. Writing some tests is probably a good idea at this point.
4. **Error handling and recovery:**
   - If an error occurs, share the error message with Cursor’s LLM agent chat.
   - Let the LLM attempt to recover the error and repeat this process until the issue is resolved.
5. **Iterative debugging:**
   - If errors persist after several attempts, review the error details manually.
     - Extraction issue: Identify if any configuration details are missing from the feature extraction, and try to manually locate the missing information and provide it in the chat along with the error message.
     - Info in responses: Currently the REST API does not return full information about the API calls on failure, to prevent accidental leakage of sensitive information. We are considering adding a dev mode for enabling full responses in a future version. If you want to pass the full responses, consider building the pipeline in pure python first to expose the API responses (you can prompt for it).
     - Implementation incorrect: If the extracted details seem correct but the REST API source still isn’t implemented properly, ask the LLM to re-check the REST API documentation and locate the missing information.

## 4\. Best practices and vibe coding tips [​](https://dlthub.com/docs/dlt-ecosystem/llm-tooling/cursor-restapi\#4-best-practices-and-vibe-coding-tips "Direct link to 4. Best practices and vibe coding tips")

Below, find some tips that might help when vibe coding REST API or python pipelines.

### 4.1 General best practices [​](https://dlthub.com/docs/dlt-ecosystem/llm-tooling/cursor-restapi\#41-general-best-practices "Direct link to 4.1 General best practices")

- **Clarity over cleverness**
  - Use explicit names for variables, configs, and pipeline steps.

    `source_config = {...}` is better than `cfg = {...}`.

  - Version your code and config files. Use Git branches per integration.

  - Include comments only when the code isn’t self-explanatory, avoid noise.

    Good: `# Required by API to avoid pagination bug`.

    Bad: `# Set the page size`.
- **Use checklists, no, really.**

Before publishing any new pipeline or source, confirm:
  - [ ]  All `client` configs (auth keys, tokens, endpoints) are present and correct.
  - [ ] `resource` settings match the expected schema and entity structure.
  - [ ]  Incremental fields ( `updated_at`, `id`, etc.) are configured correctly.
  - [ ]  Destination settings (dataset names, schema names, write disposition) are reviewed.
- **Break work into small, testable chunks**
  - Don’t process all endpoints at once. Implement one `resource` at a time, test it, and then layer more.
  - Ingestion pipelines should run in under 10 minutes locally, if not, split the logic.
  - Use fixtures or mocks when testing APIs with rate limits or unstable responses.

### 4.2 LLM-Specific tips & common pitfalls [​](https://dlthub.com/docs/dlt-ecosystem/llm-tooling/cursor-restapi\#42-llm-specific-tips--common-pitfalls "Direct link to 4.2 LLM-Specific tips & common pitfalls")

- **Prompt design tips**
  - Give context: include 1-2 lines about the task, data structure, and output format.

    Example: _"You are generating Python code to parse a paginated REST API. Output one function per endpoint."_

  - Be explicit: always define structure expectations.

    Example: _"Return a list of dictionaries with keys `id`, `name`, `timestamp`."_

  - Avoid ambiguity: never say “optimize” or “improve” unless you define what “better” means.
- **Validate LLM output like you would PRs**
  - Run the code it generates.
  - Compare schema definitions, type hints, and transformations with the actual API/data.
  - If your docs say a field is optional, the prompt should include that.
- **Pitfalls to avoid**
  - Session sprawl: Keep LLM sessions under 15-20 interactions. Split into sub-tasks if needed.

  - Misinterpreting placeholders: For dynamic variables like `{{timestamp}}` or `{{page}}`, wrap them in extra explanation, e.g.,

    _"Use `{{timestamp}}` as an ISO 8601 string from the last run time."_

  - **Model drift**: GPT-4, Claude, and other models don’t behave the same. Outputs vary in subtle ways. Always test prompts across models if switching.
- **When changing models or seeing weird behavior:**
  - Reduce the scope: instead of asking “write the full source,” say “write the pagination logic.”
  - Switch from creative mode (broad prompts) to constrained mode (e.g., give a template and ask it to fill it in).
  - Run diffs between old and new LLM outputs to detect accidental regressions, especially if you fine-tuned prompts.

## Closing words [​](https://dlthub.com/docs/dlt-ecosystem/llm-tooling/cursor-restapi\#closing-words "Direct link to Closing words")

This domain is new, and the document on this page will evolve as we will be better able to provide better instructions. We are actively working on pushing the envelope. For advanced usage, see our [MCP server docs.](https://dlthub.com/docs/dlt-ecosystem/llm-tooling/mcp-server)

- [Overview](https://dlthub.com/docs/dlt-ecosystem/llm-tooling/cursor-restapi#overview)
- [1\. Problem definition & feature extraction](https://dlthub.com/docs/dlt-ecosystem/llm-tooling/cursor-restapi#1-problem-definition--feature-extraction)
  - [1.1 Extracting features from information sources](https://dlthub.com/docs/dlt-ecosystem/llm-tooling/cursor-restapi#11-extracting-features-from-information-sources)
  - [1.2 Understanding the parameters](https://dlthub.com/docs/dlt-ecosystem/llm-tooling/cursor-restapi#12-understanding-the-parameters)
- [2\. Setting up Cursor for REST API source generation](https://dlthub.com/docs/dlt-ecosystem/llm-tooling/cursor-restapi#2-setting-up-cursor-for-rest-api-source-generation)
  - [2.1 Understanding Cursor](https://dlthub.com/docs/dlt-ecosystem/llm-tooling/cursor-restapi#21-understanding-cursor)
  - [2.2 Adding rules to cursor](https://dlthub.com/docs/dlt-ecosystem/llm-tooling/cursor-restapi#22-adding-rules-to-cursor)
  - [2.3 Configuring Cursor with documentation](https://dlthub.com/docs/dlt-ecosystem/llm-tooling/cursor-restapi#23-configuring-cursor-with-documentation)
  - [2.4 Integrating local docs](https://dlthub.com/docs/dlt-ecosystem/llm-tooling/cursor-restapi#24-integrating-local-docs)
  - [2.5 Documentation augmentation](https://dlthub.com/docs/dlt-ecosystem/llm-tooling/cursor-restapi#25-documentation-augmentation)
  - [2.6 Model and context selection](https://dlthub.com/docs/dlt-ecosystem/llm-tooling/cursor-restapi#26-model-and-context-selection)
- [3\. Running the workflow](https://dlthub.com/docs/dlt-ecosystem/llm-tooling/cursor-restapi#3-running-the-workflow)
- [4\. Best practices and vibe coding tips](https://dlthub.com/docs/dlt-ecosystem/llm-tooling/cursor-restapi#4-best-practices-and-vibe-coding-tips)
  - [4.1 General best practices](https://dlthub.com/docs/dlt-ecosystem/llm-tooling/cursor-restapi#41-general-best-practices)
  - [4.2 LLM-Specific tips & common pitfalls](https://dlthub.com/docs/dlt-ecosystem/llm-tooling/cursor-restapi#42-llm-specific-tips--common-pitfalls)
- [Closing words](https://dlthub.com/docs/dlt-ecosystem/llm-tooling/cursor-restapi#closing-words)

----- https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#data-selection -----

Version: 1.11.0 (latest)

On this page

Need help deploying these sources or figuring out how to run them in your data stack?

[Join our Slack community](https://dlthub.com/community) or [Get in touch](https://dlthub.com/contact) with the dltHub Customer Success team.

This is a dlt source you can use to extract data from any REST API. It uses [declarative configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#source-configuration) to define the API endpoints, their [relationships](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#define-resource-relationships), how to handle [pagination](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#pagination), and [authentication](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#authentication).

### Quick example [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#quick-example "Direct link to Quick example")

Here's an example of how to configure the REST API source to load posts and related comments from a hypothetical blog API:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.rest_api import rest_api_source

source = rest_api_source({
    "client": {
        "base_url": "https://api.example.com/",
        "auth": {
            "token": dlt.secrets["your_api_token"],
        },
        "paginator": {
            "type": "json_link",
            "next_url_path": "paging.next",
        },
    },
    "resources": [\
        # "posts" will be used as the endpoint path, the resource name,\
        # and the table name in the destination. The HTTP client will send\
        # a request to "https://api.example.com/posts".\
        "posts",\
\
        # The explicit configuration allows you to link resources\
        # and define query string parameters.\
        {\
            "name": "comments",\
            "endpoint": {\
                "path": "posts/{resources.posts.id}/comments",\
                "params": {\
                    "sort": "created_at",\
                },\
            },\
        },\
    ],
})

pipeline = dlt.pipeline(
    pipeline_name="rest_api_example",
    destination="duckdb",
    dataset_name="rest_api_data",
)

load_info = pipeline.run(source)

```

Running this pipeline will create two tables in DuckDB: `posts` and `comments` with the data from the respective API endpoints. The `comments` resource will fetch comments for each post by using the `id` field from the `posts` resource.

## Setup [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#setup "Direct link to Setup")

### Prerequisites [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#prerequisites "Direct link to Prerequisites")

Please make sure the `dlt` library is installed. Refer to the [installation guide](https://dlthub.com/docs/intro).

### Initialize the REST API source [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#initialize-the-rest-api-source "Direct link to Initialize the REST API source")

Enter the following command in your terminal:

```codeBlockLines_RjmQ
dlt init rest_api duckdb

```

[dlt init](https://dlthub.com/docs/reference/command-line-interface) will initialize the pipeline examples for REST API as the [source](https://dlthub.com/docs/general-usage/source) and [duckdb](https://dlthub.com/docs/dlt-ecosystem/destinations/duckdb) as the [destination](https://dlthub.com/docs/dlt-ecosystem/destinations).

Running `dlt init` creates the following in the current folder:

- `rest_api_pipeline.py` file with a sample pipelines definition:
  - GitHub API example
  - Pokemon API example
- `.dlt` folder with:
  - `secrets.toml` file to store your access tokens and other sensitive information
  - `config.toml` file to store the configuration settings
- `requirements.txt` file with the required dependencies

Change the REST API source to your needs by modifying the `rest_api_pipeline.py` file. See the detailed [source configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#source-configuration) section below.

note

For the rest of the guide, we will use the [GitHub API](https://docs.github.com/en/rest?apiVersion=2022-11-28) and [Pokemon API](https://pokeapi.co/) as example sources.

This source is based on the [RESTClient class](https://dlthub.com/docs/general-usage/http/rest-client).

### Add credentials [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#add-credentials "Direct link to Add credentials")

In the `.dlt` folder, you'll find a file called `secrets.toml`, where you can securely store your access tokens and other sensitive information. It's important to handle this file with care and keep it safe.

The GitHub API [requires an access token](https://docs.github.com/en/rest/authentication/authenticating-to-the-rest-api?apiVersion=2022-11-28) to access some of its endpoints and to increase the rate limit for the API calls. To get a GitHub token, follow the GitHub documentation on [managing your personal access tokens](https://docs.github.com/en/authentication/keeping-your-account-and-data-secure/managing-your-personal-access-tokens).

After you get the token, add it to the `secrets.toml` file:

```codeBlockLines_RjmQ
[sources.rest_api_pipeline.github_source]
github_token = "your_github_token"

```

## Run the pipeline [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#run-the-pipeline "Direct link to Run the pipeline")

1. Install the required dependencies by running the following command:

```codeBlockLines_RjmQ
pip install -r requirements.txt

```

2. Run the pipeline:

```codeBlockLines_RjmQ
python rest_api_pipeline.py

```

3. Verify that everything loaded correctly by using the following command:

```codeBlockLines_RjmQ
dlt pipeline rest_api show

```

## Source configuration [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#source-configuration "Direct link to Source configuration")

### Quick example [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#quick-example-1 "Direct link to Quick example")

Let's take a look at the GitHub example in the `rest_api_pipeline.py` file:

```codeBlockLines_RjmQ
from dlt.sources.rest_api import RESTAPIConfig, rest_api_resources

@dlt.source
def github_source(github_token=dlt.secrets.value):
    config: RESTAPIConfig = {
        "client": {
            "base_url": "https://api.github.com/repos/dlt-hub/dlt/",
            "auth": {
                "token": github_token,
            },
        },
        "resource_defaults": {
            "primary_key": "id",
            "write_disposition": "merge",
            "endpoint": {
                "params": {
                    "per_page": 100,
                },
            },
        },
        "resources": [\
            {\
                "name": "issues",\
                "endpoint": {\
                    "path": "issues",\
                    "params": {\
                        "sort": "updated",\
                        "direction": "desc",\
                        "state": "open",\
                        "since": {\
                            "type": "incremental",\
                            "cursor_path": "updated_at",\
                            "initial_value": "2024-01-25T11:21:28Z",\
                        },\
                    },\
                },\
            },\
            {\
                "name": "issue_comments",\
                "endpoint": {\
                    "path": "issues/{resources.issues.number}/comments",\
                },\
                "include_from_parent": ["id"],\
            },\
        ],
    }

    yield from rest_api_resources(config)

def load_github() -> None:
    pipeline = dlt.pipeline(
        pipeline_name="rest_api_github",
        destination="duckdb",
        dataset_name="rest_api_data",
    )

    load_info = pipeline.run(github_source())
    print(load_info)

```

The declarative resource configuration is defined in the `config` dictionary. It contains the following key components:

1. `client`: Defines the base URL and authentication method for the API. In this case, it uses token-based authentication. The token is stored in the `secrets.toml` file.

2. `resource_defaults`: Contains default settings for all [resources](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#resource-configuration). In this example, we define that all resources:
   - Have `id` as the [primary key](https://dlthub.com/docs/general-usage/resource#define-schema)
   - Use the `merge` [write disposition](https://dlthub.com/docs/general-usage/incremental-loading#choosing-a-write-disposition) to merge the data with the existing data in the destination.
   - Send a `per_page=100` query parameter with each request to get more results per page.
3. `resources`: A list of [resources](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#resource-configuration) to be loaded. Here, we have two resources: `issues` and `issue_comments`, which correspond to the GitHub API endpoints for [repository issues](https://docs.github.com/en/rest/issues/issues?apiVersion=2022-11-28#list-repository-issues) and [issue comments](https://docs.github.com/en/rest/issues/comments?apiVersion=2022-11-28#list-issue-comments). Note that we need an issue number to fetch comments for each issue. This number is taken from the `issues` resource. More on this in the [resource relationships](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#define-resource-relationships) section.

Let's break down the configuration in more detail.

### Configuration structure [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#configuration-structure "Direct link to Configuration structure")

tip

Import the `RESTAPIConfig` type from the `rest_api` module to have convenient hints in your editor/IDE and use it to define the configuration object.

```codeBlockLines_RjmQ
from dlt.sources.rest_api import RESTAPIConfig

```

The configuration object passed to the REST API Generic Source has three main elements:

```codeBlockLines_RjmQ
config: RESTAPIConfig = {
    "client": {
        # ...
    },
    "resource_defaults": {
        # ...
    },
    "resources": [\
        # ...\
    ],
}

```

#### `client` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#client "Direct link to client")

The `client` configuration is used to connect to the API's endpoints. It includes the following fields:

- `base_url` (str): The base URL of the API. This string is prepended to all endpoint paths. For example, if the base URL is `https://api.example.com/v1/`, and the endpoint path is `users`, the full URL will be `https://api.example.com/v1/users`.
- `headers` (dict, optional): Additional headers that are sent with each request.
- `auth` (optional): Authentication configuration. This can be a simple token, an `AuthConfigBase` object, or a more complex authentication method.
- `paginator` (optional): Configuration for the default pagination used for resources that support pagination. Refer to the [pagination](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#pagination) section for more details.

#### `resource_defaults` (optional) [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#resource_defaults-optional "Direct link to resource_defaults-optional")

`resource_defaults` contains the default values to [configure the dlt resources](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#resource-configuration). This configuration is applied to all resources unless overridden by the resource-specific configuration.

For example, you can set the primary key, write disposition, and other default settings here:

```codeBlockLines_RjmQ
config = {
    "client": {
        # ...
    },
    "resource_defaults": {
        "primary_key": "id",
        "write_disposition": "merge",
        "endpoint": {
            "params": {
                "per_page": 100,
            },
        },
    },
    "resources": [\
        "resource1",\
        {\
            "name": "resource2_name",\
            "write_disposition": "append",\
            "endpoint": {\
                "params": {\
                    "param1": "value1",\
                },\
            },\
        }\
    ],
}

```

Above, all resources will have `primary_key` set to `id`, `resource1` will have `write_disposition` set to `merge`, and `resource2` will override the default `write_disposition` with `append`.
Both `resource1` and `resource2` will have the `per_page` parameter set to 100.

#### `resources` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#resources "Direct link to resources")

This is a list of resource configurations that define the API endpoints to be loaded. Each resource configuration can be:

- a dictionary with the [resource configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#resource-configuration).
- a string. In this case, the string is used as both the endpoint path and the resource name, and the resource configuration is taken from the `resource_defaults` configuration if it exists.

### Resource configuration [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#resource-configuration "Direct link to Resource configuration")

A resource configuration is used to define a [dlt resource](https://dlthub.com/docs/general-usage/resource) for the data to be loaded from an API endpoint. It contains the following key fields:

- `endpoint`: The endpoint configuration for the resource. It can be a string or a dict representing the endpoint settings. See the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) section for more details.
- `write_disposition`: The write disposition for the resource.
- `primary_key`: The primary key for the resource.
- `include_from_parent`: A list of fields from the parent resource to be included in the resource output. See the [resource relationships](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#include-fields-from-the-parent-resource) section for more details.
- `processing_steps`: A list of [processing steps](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#processing-steps-filter-and-transform-data) to filter and transform your data.
- `selected`: A flag to indicate if the resource is selected for loading. This could be useful when you want to load data only from child resources and not from the parent resource.
- `auth`: An optional `AuthConfig` instance. If passed, is used over the one defined in the [client](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#client) definition. Example:

```codeBlockLines_RjmQ
from dlt.sources.helpers.rest_client.auth import HttpBasicAuth

config = {
    "client": {
        "auth": {
            "type": "bearer",
            "token": dlt.secrets["your_api_token"],
        }
    },
    "resources": [\
        "resource-using-bearer-auth",\
        {\
            "name": "my-resource-with-special-auth",\
            "endpoint": {\
                # ...\
                "auth": HttpBasicAuth("user", dlt.secrets["your_basic_auth_password"])\
            },\
            # ...\
        }\
    ]
    # ...
}

```

This would use `Bearer` auth as defined in the `client` for `resource-using-bearer-auth` and `Http Basic` auth for `my-resource-with-special-auth`.

You can also pass additional resource parameters that will be used to configure the dlt resource. See [dlt resource API reference](https://dlthub.com/docs/api_reference/dlt/extract/decorators#resource) for more details.

### Endpoint configuration [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#endpoint-configuration "Direct link to Endpoint configuration")

The endpoint configuration defines how to query the API endpoint. Quick example:

```codeBlockLines_RjmQ
{
    "path": "issues",
    "method": "GET",
    "params": {
        "sort": "updated",
        "direction": "desc",
        "state": "open",
        "since": {
            "type": "incremental",
            "cursor_path": "updated_at",
            "initial_value": "2024-01-25T11:21:28Z",
        },
    },
    "data_selector": "results",
}

```

The fields in the endpoint configuration are:

- `path`: The path to the API endpoint. By default this path is appended to the given `base_url`. If this is a fully qualified URL starting with `http:` or `https:` it will be
used as-is and `base_url` will be ignored.
- `method`: The HTTP method to be used. The default is `GET`.
- `params`: Query parameters to be sent with each request. For example, `sort` to order the results or `since` to specify [incremental loading](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#incremental-loading). This is also may be used to define [resource relationships](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#define-resource-relationships).
- `json`: The JSON payload to be sent with the request (for POST and PUT requests).
- `paginator`: Pagination configuration for the endpoint. See the [pagination](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#pagination) section for more details.
- `data_selector`: A JSONPath to select the data from the response. See the [data selection](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#data-selection) section for more details.
- `response_actions`: A list of actions that define how to process the response data. See the [response actions](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/advanced#response-actions) section for more details.
- `incremental`: Configuration for [incremental loading](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#incremental-loading).

### Pagination [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#pagination "Direct link to Pagination")

The REST API source will try to automatically handle pagination for you. This works by detecting the pagination details from the first API response.

In some special cases, you may need to specify the pagination configuration explicitly.

To specify the pagination configuration, use the `paginator` field in the [client](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#client) or [endpoint](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) configurations. You may either use a dictionary with a string alias in the `type` field along with the required parameters, or use a [paginator class instance](https://dlthub.com/docs/general-usage/http/rest-client#paginators).

#### Example [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#example "Direct link to Example")

Suppose the API response for `https://api.example.com/posts` contains a `next` field with the URL to the next page:

```codeBlockLines_RjmQ
{
    "data": [\
        {"id": 1, "title": "Post 1"},\
        {"id": 2, "title": "Post 2"},\
        {"id": 3, "title": "Post 3"}\
    ],
    "pagination": {
        "next": "https://api.example.com/posts?page=2"
    }
}

```

You can configure the pagination for the `posts` resource like this:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "paginator": {
        "type": "json_link",
        "next_url_path": "pagination.next",
    }
}

```

Alternatively, you can use the paginator instance directly:

```codeBlockLines_RjmQ
from dlt.sources.helpers.rest_client.paginators import JSONLinkPaginator

# ...

{
    "path": "posts",
    "paginator": JSONLinkPaginator(
        next_url_path="pagination.next"
    ),
}

```

note

Currently, pagination is supported only for GET requests. To handle POST requests with pagination, you need to implement a [custom paginator](https://dlthub.com/docs/general-usage/http/rest-client#implementing-a-custom-paginator).

These are the available paginators:

| `type` | Paginator class | Description |
| --- | --- | --- |
| `json_link` | [JSONLinkPaginator](https://dlthub.com/docs/general-usage/http/rest-client#jsonlinkpaginator) | The link to the next page is in the body (JSON) of the response.<br>_Parameters:_ <br>- `next_url_path` (str) - the JSONPath to the next page URL |
| `header_link` | [HeaderLinkPaginator](https://dlthub.com/docs/general-usage/http/rest-client#headerlinkpaginator) | The links to the next page are in the response headers.<br>_Parameters:_ <br>- `links_next_key` (str) - the name of the header containing the links. Default is "next". |
| `offset` | [OffsetPaginator](https://dlthub.com/docs/general-usage/http/rest-client#offsetpaginator) | The pagination is based on an offset parameter, with the total items count either in the response body or explicitly provided.<br>_Parameters:_ <br>- `limit` (int) - the maximum number of items to retrieve in each request<br>- `offset` (int) - the initial offset for the first request. Defaults to `0`<br>- `offset_param` (str) - the name of the query parameter used to specify the offset. Defaults to "offset"<br>- `limit_param` (str) - the name of the query parameter used to specify the limit. Defaults to "limit"<br>- `total_path` (str) - a JSONPath expression for the total number of items. If not provided, pagination is controlled by `maximum_offset` and `stop_after_empty_page`<br>- `maximum_offset` (int) - optional maximum offset value. Limits pagination even without total count<br>- `stop_after_empty_page` (bool) - Whether pagination should stop when a page contains no result items. Defaults to `True` |
| `page_number` | [PageNumberPaginator](https://dlthub.com/docs/general-usage/http/rest-client#pagenumberpaginator) | The pagination is based on a page number parameter, with the total pages count either in the response body or explicitly provided.<br>_Parameters:_ <br>- `base_page` (int) - the starting page number. Defaults to `0`<br>- `page_param` (str) - the query parameter name for the page number. Defaults to "page"<br>- `total_path` (str) - a JSONPath expression for the total number of pages. If not provided, pagination is controlled by `maximum_page` and `stop_after_empty_page`<br>- `maximum_page` (int) - optional maximum page number. Stops pagination once this page is reached<br>- `stop_after_empty_page` (bool) - Whether pagination should stop when a page contains no result items. Defaults to `True` |
| `cursor` | [JSONResponseCursorPaginator](https://dlthub.com/docs/general-usage/http/rest-client#jsonresponsecursorpaginator) | The pagination is based on a cursor parameter, with the value of the cursor in the response body (JSON).<br>_Parameters:_ <br>- `cursor_path` (str) - the JSONPath to the cursor value. Defaults to "cursors.next"<br>- `cursor_param` (str) - the query parameter name for the cursor. Defaults to "cursor" if neither `cursor_param` nor `cursor_body_path` is provided.<br>- `cursor_body_path` (str, optional) - the JSONPath to place the cursor in the request body.<br>Note: You must provide either `cursor_param` or `cursor_body_path`, but not both. If neither is provided, `cursor_param` will default to "cursor". |
| `single_page` | SinglePagePaginator | The response will be interpreted as a single-page response, ignoring possible pagination metadata. |
| `auto` | `None` | Explicitly specify that the source should automatically detect the pagination method. |

For more complex pagination methods, you can implement a [custom paginator](https://dlthub.com/docs/general-usage/http/rest-client#implementing-a-custom-paginator), instantiate it, and use it in the configuration.

Alternatively, you can use the dictionary configuration syntax also for custom paginators. For this, you need to register your custom paginator:

```codeBlockLines_RjmQ
from dlt.sources.rest_api.config_setup import register_paginator

class CustomPaginator(SinglePagePaginator):
    # custom implementation of SinglePagePaginator
    pass

register_paginator("custom_paginator", CustomPaginator)

{
    # ...
    "paginator": {
        "type": "custom_paginator",
        "next_url_path": "paging.nextLink",
    }
}

```

### Data selection [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#data-selection "Direct link to Data selection")

The `data_selector` field in the endpoint configuration allows you to specify a JSONPath to select the data from the response. By default, the source will try to detect the locations of the data automatically.

Use this field when you need to specify the location of the data in the response explicitly.

For example, if the API response looks like this:

```codeBlockLines_RjmQ
{
    "posts": [\
        {"id": 1, "title": "Post 1"},\
        {"id": 2, "title": "Post 2"},\
        {"id": 3, "title": "Post 3"}\
    ]
}

```

You can use the following endpoint configuration:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "posts",
}

```

For a nested structure like this:

```codeBlockLines_RjmQ
{
    "results": {
        "posts": [\
            {"id": 1, "title": "Post 1"},\
            {"id": 2, "title": "Post 2"},\
            {"id": 3, "title": "Post 3"}\
        ]
    }
}

```

You can use the following endpoint configuration:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "results.posts",
}

```

Read more about [JSONPath syntax](https://github.com/h2non/jsonpath-ng?tab=readme-ov-file#jsonpath-syntax) to learn how to write selectors.

### Authentication [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#authentication "Direct link to Authentication")

For APIs that require authentication to access their endpoints, the REST API source supports various authentication methods, including token-based authentication, query parameters, basic authentication, and custom authentication. The authentication configuration is specified in the `auth` field of the [client](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#client) either as a dictionary or as an instance of the [authentication class](https://dlthub.com/docs/general-usage/http/rest-client#authentication).

#### Quick example [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#quick-example-2 "Direct link to Quick example")

Here's how to configure authentication using a bearer token:

```codeBlockLines_RjmQ
{
    "client": {
        # ...
        "auth": {
            "type": "bearer",
            "token": dlt.secrets["your_api_token"],
        },
        # ...
    },
}

```

Alternatively, you can use the authentication class directly:

```codeBlockLines_RjmQ
from dlt.sources.helpers.rest_client.auth import BearerTokenAuth

config = {
    "client": {
        "auth": BearerTokenAuth(dlt.secrets["your_api_token"]),
    },
    "resources": [\
    ]
    # ...
}

```

Since token-based authentication is one of the most common methods, you can use the following shortcut:

```codeBlockLines_RjmQ
{
    "client": {
        # ...
        "auth": {
            "token": dlt.secrets["your_api_token"],
        },
        # ...
    },
}

```

warning

Make sure to store your access tokens and other sensitive information in the `secrets.toml` file and never commit it to the version control system.

Available authentication types:

| `type` | Authentication class | Description |
| --- | --- | --- |
| `bearer` | [BearerTokenAuth](https://dlthub.com/docs/general-usage/http/rest-client#bearer-token-authentication) | Bearer token authentication.<br>Parameters:<br>- `token` (str) |
| `http_basic` | [HTTPBasicAuth](https://dlthub.com/docs/general-usage/http/rest-client#http-basic-authentication) | Basic HTTP authentication.<br>Parameters:<br>- `username` (str)<br>- `password` (str) |
| `api_key` | [APIKeyAuth](https://dlthub.com/docs/general-usage/http/rest-client#api-key-authentication) | API key authentication with key defined in the query parameters or in the headers. <br>Parameters:<br>- `name` (str) - the name of the query parameter or header<br>- `api_key` (str) - the API key value<br>- `location` (str, optional) - the location of the API key in the request. Can be `query` or `header`. Default is `header` |
| `oauth2_client_credentials` | [OAuth2ClientCredentials](https://dlthub.com/docs/general-usage/http/rest-client#oauth-20-authorization) | OAuth 2.0 Client Credentials authorization for server-to-server communication without user consent. <br>Parameters:<br>- `access_token` (str, optional) - the temporary token. Usually not provided here because it is automatically obtained from the server by exchanging `client_id` and `client_secret`. Default is `None`<br>- `access_token_url` (str) - the URL to request the `access_token` from<br>- `client_id` (str) - identifier for your app. Usually issued via a developer portal<br>- `client_secret` (str) - client credential to obtain authorization. Usually issued via a developer portal<br>- `access_token_request_data` (dict, optional) - A dictionary with data required by the authorization server apart from the `client_id`, `client_secret`, and `"grant_type": "client_credentials"`. Defaults to `None`<br>- `default_token_expiration` (int, optional) - The time in seconds after which the temporary access token expires. Defaults to 3600.<br>- `session` (requests.Session, optional) - a custom session object. Mostly used for testing |

For more complex authentication methods, you can implement a [custom authentication class](https://dlthub.com/docs/general-usage/http/rest-client#implementing-custom-authentication) and use it in the configuration.

You can use the dictionary configuration syntax also for custom authentication classes after registering them as follows:

```codeBlockLines_RjmQ
from dlt.sources.rest_api.config_setup import register_auth

class CustomAuth(AuthConfigBase):
    pass

register_auth("custom_auth", CustomAuth)

{
    # ...
    "auth": {
        "type": "custom_auth",
        "api_key": dlt.secrets["sources.my_source.my_api_key"],
    }
}

```

### Define resource relationships [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#define-resource-relationships "Direct link to Define resource relationships")

When you have a resource that depends on another resource (for example, you must fetch a parent resource to get an ID needed to fetch the child), you can reference fields in the parent resource using special placeholders.
This allows you to link one or more [path](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#via-request-path), [query string](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#via-query-string-parameters) or [JSON body](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#via-json-body) parameters in the child resource to fields in the parent resource's data.

#### Via request path [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#via-request-path "Direct link to Via request path")

In the GitHub example, the `issue_comments` resource depends on the `issues` resource. The `resources.issues.number` placeholder links the `number` field in the `issues` resource data to the current request's path parameter.

```codeBlockLines_RjmQ
{
    "resources": [\
        {\
            "name": "issues",\
            "endpoint": {\
                "path": "issues",\
                # ...\
            },\
        },\
        {\
            "name": "issue_comments",\
            "endpoint": {\
                "path": "issues/{resources.issues.number}/comments",\
            },\
            "include_from_parent": ["id"],\
        },\
    ],
}

```

This configuration tells the source to get issue numbers from the `issues` resource data and use them to fetch comments for each issue number. So for each issue item, `"{resources.issues.number}"` is replaced by the issue number in the request path.
For example, if the `issues` resource yields the following data:

```codeBlockLines_RjmQ
[\
    {"id": 1, "number": 123},\
    {"id": 2, "number": 124},\
    {"id": 3, "number": 125}\
]

```

The `issue_comments` resource will make requests to the following endpoints:

- `issues/123/comments`
- `issues/124/comments`
- `issues/125/comments`

The syntax for the placeholder is `resources.<parent_resource_name>.<field_name>`.

#### Via query string parameters [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#via-query-string-parameters "Direct link to Via query string parameters")

The placeholder syntax can also be used in the query string parameters. For example, in an API which lets you fetch a blog posts (via `/posts`) and their comments (via `/comments?post_id=<post_id>`), you can define a resource `posts` and a resource `post_comments` which depends on the `posts` resource. You can then reference the `id` field from the `posts` resource in the `post_comments` resource:

```codeBlockLines_RjmQ
{
    "resources": [\
        "posts",\
        {\
            "name": "post_comments",\
            "endpoint": {\
                "path": "comments",\
                "params": {\
                    "post_id": "{resources.posts.id}",\
                },\
            },\
        },\
    ],
}

```

Similar to the GitHub example above, if the `posts` resource yields the following data:

```codeBlockLines_RjmQ
[\
    {"id": 1, "title": "Post 1"},\
    {"id": 2, "title": "Post 2"},\
    {"id": 3, "title": "Post 3"}\
]

```

The `post_comments` resource will make requests to the following endpoints:

- `comments?post_id=1`
- `comments?post_id=2`
- `comments?post_id=3`

#### Via JSON body [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#via-json-body "Direct link to Via JSON body")

In many APIs, you can send a complex query or configuration through a POST request's JSON body rather than in the request path or query parameters. For example, consider an imaginary `/search` endpoint that supports multiple filters and settings. You might have a parent resource `posts` with each post's `id` and a second resource, `post_details`, that uses `id` to perform a custom search.

In the example below we reference the `posts` resource's `id` field in the JSON body via placeholders:

```codeBlockLines_RjmQ
{
    "resources": [\
        "posts",\
        {\
            "name": "post_details",\
            "endpoint": {\
                "path": "search",\
                "method": "POST",\
                "json": {\
                    "filters": {\
                        "id": "{resources.posts.id}",\
                    },\
                    "order": "desc",\
                    "limit": 5,\
                }\
            },\
        },\
    ],
}

```

#### Legacy syntax: `resolve` field in parameter configuration [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#legacy-syntax-resolve-field-in-parameter-configuration "Direct link to legacy-syntax-resolve-field-in-parameter-configuration")

warning

`resolve` works only for path parameters. The new placeholder syntax is more flexible and recommended for new configurations.

An alternative, legacy way to define resource relationships is to use the `resolve` field in the parameter configuration.
Here's the same example as above that uses the `resolve` field:

```codeBlockLines_RjmQ
{
    "resources": [\
        {\
            "name": "issues",\
            "endpoint": {\
                "path": "issues",\
                # ...\
            },\
        },\
        {\
            "name": "issue_comments",\
            "endpoint": {\
                "path": "issues/{issue_number}/comments",\
                "params": {\
                    "issue_number": {\
                        "type": "resolve",\
                        "resource": "issues",\
                        "field": "number",\
                    }\
                },\
            },\
            "include_from_parent": ["id"],\
        },\
    ],
}

```

The syntax for the `resolve` field in parameter configuration is:

```codeBlockLines_RjmQ
{
    "<parameter_name>": {
        "type": "resolve",
        "resource": "<parent_resource_name>",
        "field": "<parent_resource_field_name_or_jsonpath>",
    }
}

```

The `field` value can be specified as a [JSONPath](https://github.com/h2non/jsonpath-ng?tab=readme-ov-file#jsonpath-syntax) to select a nested field in the parent resource data. For example: `"field": "items[0].id"`.

#### Resolving multiple path parameters from a parent resource [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#resolving-multiple-path-parameters-from-a-parent-resource "Direct link to Resolving multiple path parameters from a parent resource")

When a child resource depends on multiple fields from a single parent resource, you can define multiple `resolve` parameters in the endpoint configuration. For example:

```codeBlockLines_RjmQ
{
    "resources": [\
        "groups",\
        {\
            "name": "users",\
            "endpoint": {\
                "path": "groups/{group_id}/users",\
                "params": {\
                    "group_id": {\
                        "type": "resolve",\
                        "resource": "groups",\
                        "field": "id",\
                    },\
                },\
            },\
        },\
        {\
            "name": "user_details",\
            "endpoint": {\
                "path": "groups/{group_id}/users/{user_id}/details",\
                "params": {\
                    "group_id": {\
                        "type": "resolve",\
                        "resource": "users",\
                        "field": "group_id",\
                    },\
                    "user_id": {\
                        "type": "resolve",\
                        "resource": "users",\
                        "field": "id",\
                    },\
                },\
            },\
        },\
    ],
}

```

In the configuration above:

- The `users` resource depends on the `groups` resource, resolving the `group_id` parameter from the `id` field in `groups`.
- The `user_details` resource depends on the `users` resource, resolving both `group_id` and `user_id` parameters from fields in `users`.

#### Include fields from the parent resource [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#include-fields-from-the-parent-resource "Direct link to Include fields from the parent resource")

You can include data from the parent resource in the child resource by using the `include_from_parent` field in the resource configuration. For example:

```codeBlockLines_RjmQ
{
    "name": "issue_comments",
    "endpoint": {
        ...
    },
    "include_from_parent": ["id", "title", "created_at"],
}

```

This will include the `id`, `title`, and `created_at` fields from the `issues` resource in the `issue_comments` resource data. The names of the included fields will be prefixed with the parent resource name and an underscore ( `_`) like so: `_issues_id`, `_issues_title`, `_issues_created_at`.

### Define a resource which is not a REST endpoint [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#define-a-resource-which-is-not-a-rest-endpoint "Direct link to Define a resource which is not a REST endpoint")

Sometimes, we want to request endpoints with specific values that are not returned by another endpoint.
Thus, you can also include arbitrary dlt resources in your `RESTAPIConfig` instead of defining a resource for every path!

In the following example, we want to load the issues belonging to three repositories.
Instead of defining three different issues resources, one for each of the paths `dlt-hub/dlt/issues/`, `dlt-hub/verified-sources/issues/`, `dlt-hub/dlthub-education/issues/`, we have a resource `repositories` which yields a list of repository names that will be fetched by the dependent resource `issues`.

```codeBlockLines_RjmQ
from dlt.sources.rest_api import RESTAPIConfig

@dlt.resource()
def repositories() -> Generator[List[Dict[str, Any]], Any, Any]:
    """A seed list of repositories to fetch"""
    yield [{"name": "dlt"}, {"name": "verified-sources"}, {"name": "dlthub-education"}]

config: RESTAPIConfig = {
    "client": {"base_url": "https://github.com/api/v2"},
    "resources": [\
        {\
            "name": "issues",\
            "endpoint": {\
                "path": "dlt-hub/{repository}/issues/",\
                "params": {\
                    "repository": {\
                        "type": "resolve",\
                        "resource": "repositories",\
                        "field": "name",\
                    },\
                },\
            },\
        },\
        repositories(),\
    ],
}

```

Be careful that the parent resource needs to return `Generator[List[Dict[str, Any]]]`. Thus, the following will NOT work:

```codeBlockLines_RjmQ
@dlt.resource
def repositories() -> Generator[Dict[str, Any], Any, Any]:
    """Not working seed list of repositories to fetch"""
    yield from [{"name": "dlt"}, {"name": "verified-sources"}, {"name": "dlthub-education"}]

```

### Processing steps: filter and transform data [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#processing-steps-filter-and-transform-data "Direct link to Processing steps: filter and transform data")

The `processing_steps` field in the resource configuration allows you to apply transformations to the data fetched from the API before it is loaded into your destination. This is useful when you need to filter out certain records, modify the data structure, or anonymize sensitive information.

Each processing step is a dictionary specifying the type of operation ( `filter` or `map`) and the function to apply. Steps apply in the order they are listed.

#### Quick example [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#quick-example-3 "Direct link to Quick example")

```codeBlockLines_RjmQ
def lower_title(record):
    record["title"] = record["title"].lower()
    return record

config: RESTAPIConfig = {
    "client": {
        "base_url": "https://api.example.com",
    },
    "resources": [\
        {\
            "name": "posts",\
            "processing_steps": [\
                {"filter": lambda x: x["id"] < 10},\
                {"map": lower_title},\
            ],\
        },\
    ],
}

```

In the example above:

- First, the `filter` step uses a lambda function to include only records where `id` is less than 10.
- Thereafter, the `map` step applies the `lower_title` function to each remaining record.

#### Using `filter` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#using-filter "Direct link to using-filter")

The `filter` step allows you to exclude records that do not meet certain criteria. The provided function should return `True` to keep the record or `False` to exclude it:

```codeBlockLines_RjmQ
{
    "name": "posts",
    "endpoint": "posts",
    "processing_steps": [\
        {"filter": lambda x: x["id"] in [10, 20, 30]},\
    ],
}

```

In this example, only records with `id` equal to 10, 20, or 30 will be included.

#### Using `map` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#using-map "Direct link to using-map")

The `map` step allows you to modify the records fetched from the API. The provided function should take a record as an argument and return the modified record. For example, to anonymize the `email` field:

```codeBlockLines_RjmQ
def anonymize_email(record):
    record["email"] = "REDACTED"
    return record

config: RESTAPIConfig = {
    "client": {
        "base_url": "https://api.example.com",
    },
    "resources": [\
        {\
            "name": "users",\
            "processing_steps": [\
                {"map": anonymize_email},\
            ],\
        },\
    ],
}

```

#### Combining `filter` and `map` [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#combining-filter-and-map "Direct link to combining-filter-and-map")

You can combine multiple processing steps to achieve complex transformations:

```codeBlockLines_RjmQ
{
    "name": "posts",
    "endpoint": "posts",
    "processing_steps": [\
        {"filter": lambda x: x["id"] < 10},\
        {"map": lower_title},\
        {"filter": lambda x: "important" in x["title"]},\
    ],
}

```

tip

#### Best practices [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#best-practices "Direct link to Best practices")

1. Order matters: Processing steps are applied in the order they are listed. Be mindful of the sequence, especially when combining `map` and `filter`.
2. Function definition: Define your filter and map functions separately for clarity and reuse.
3. Use `filter` to exclude records early in the process to reduce the amount of data that needs to be processed.
4. Combine consecutive `map` steps into a single function for faster execution.

## Incremental loading [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#incremental-loading "Direct link to Incremental loading")

Some APIs provide a way to fetch only new or changed data (most often by using a timestamp field like `updated_at`, `created_at`, or incremental IDs).
This is called [incremental loading](https://dlthub.com/docs/general-usage/incremental-loading) and is very useful as it allows you to reduce the load time and the amount of data transferred.

Let's continue with our imaginary blog API example to understand incremental loading with query parameters.

Imagine we have the following endpoint `https://api.example.com/posts` and it:

1. Accepts a `created_since` query parameter to fetch blog posts created after a certain date.
2. Returns a list of posts with the `created_at` field for each post.

For example, if we query the endpoint with GET request `https://api.example.com/posts?created_since=2024-01-25`, we get the following response:

```codeBlockLines_RjmQ
{
    "results": [\
        {"id": 1, "title": "Post 1", "created_at": "2024-01-26"},\
        {"id": 2, "title": "Post 2", "created_at": "2024-01-27"},\
        {"id": 3, "title": "Post 3", "created_at": "2024-01-28"}\
    ]
}

```

When the API endpoint supports incremental loading, you can configure dlt to load only the new or changed data using these three methods:

1. Using [placeholders for incremental loading](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#using-placeholders-for-incremental-loading)
2. Defining a special parameter in the `params` section of the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) (DEPRECATED)
3. Using the `incremental` field in the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) with the `start_param` field (DEPRECATED)

caution

The last two methods are deprecated and will be removed in a future dlt version.

### Using placeholders for incremental loading [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#using-placeholders-for-incremental-loading "Direct link to Using placeholders for incremental loading")

The most flexible way to configure incremental loading is to use placeholders in the request configuration along with the `incremental` section.
Here's how it works:

1. Define the `incremental` section in the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) to specify the cursor path (where to find the incremental value in the response) and initial value (the value to start the incremental loading from).
2. Use the placeholder `{incremental.start_value}` in the request configuration to reference the incremental value.

Let's take the example from the previous section and configure it using placeholders:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "results",
    "params": {
        "created_since": "{incremental.start_value}",  # Uses cursor value in query parameter
    },
    "incremental": {
        "cursor_path": "created_at",
        "initial_value": "2024-01-25T00:00:00Z",
    },
}

```

When you first run this pipeline, dlt will:

1. Replace `{incremental.start_value}` with `2024-01-25T00:00:00Z` (the initial value)
2. Make a GET request to `https://api.example.com/posts?created_since=2024-01-25T00:00:00Z`
3. Parse the response (e.g., posts with created\_at values like "2024-01-26", "2024-01-27", "2024-01-28")
4. Track the maximum value found in the "created\_at" field (in this case, "2024-01-28")

On the next pipeline run, dlt will:

1. Replace `{incremental.start_value}` with "2024-01-28" (the last seen maximum value)
2. Make a GET request to `https://api.example.com/posts?created_since=2024-01-28`
3. The API will only return posts created on or after January 28th

Let's break down the configuration:

1. We explicitly set `data_selector` to `"results"` to select the list of posts from the response. This is optional; if not set, dlt will try to auto-detect the data location.
2. We define the `created_since` parameter in `params` section and use the placeholder `{incremental.start_value}` to reference the incremental value.

Placeholders are versatile and can be used in various request components. Here are some examples:

#### In JSON body (for POST requests) [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#in-json-body-for-post-requests "Direct link to In JSON body (for POST requests)")

If the API lets you filter the data by a range of dates (e.g. `fromDate` and `toDate`), you can use the placeholder in the JSON body:

```codeBlockLines_RjmQ
{
    "path": "posts/search",
    "method": "POST",
    "json": {
        "filters": {
            "fromDate": "{incremental.start_value}",  # In JSON body
            "toDate": "2024-03-25"
        },
        "limit": 1000
    },
    "incremental": {
        "cursor_path": "created_at",
        "initial_value": "2024-01-25T00:00:00Z",
    },
}

```

#### In path parameters [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#in-path-parameters "Direct link to In path parameters")

Some APIs use path parameters to filter the data:

```codeBlockLines_RjmQ
{
    "path": "posts/since/{incremental.start_value}/list",  # In URL path
    "incremental": {
        "cursor_path": "created_at",
        "initial_value": "2024-01-25",
    },
}

```

#### In request headers [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#in-request-headers "Direct link to In request headers")

It's not so common, but you can also use placeholders in the request headers:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "headers": {
        "X-Since-Timestamp": "{incremental.start_value}"  # In custom header
    },
    "incremental": {
        "cursor_path": "created_at",
        "initial_value": "2024-01-25T00:00:00Z",
    },
}

```

You can also use different placeholder variants depending on your needs:

| Placeholder | Description |
| --- | --- |
| `{incremental.start_value}` | The value to use as the starting point for this request (either the initial value or the last tracked maximum value) |
| `{incremental.initial_value}` | Always uses the initial value specified in the configuration |
| `{incremental.last_value}` | The last seen value (same as start\_value in most cases, see the [incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor) guide for more details) |
| `{incremental.end_value}` | The end value if specified in the configuration |

### Legacy method: Incremental loading in `params` (DEPRECATED) [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#legacy-method-incremental-loading-in-params-deprecated "Direct link to legacy-method-incremental-loading-in-params-deprecated")

caution

DEPRECATED: This method is deprecated and will be removed in a future version. Use the [placeholder method](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#using-placeholders-for-incremental-loading) instead.

note

This method only works for query string parameters. For other request parts (path, JSON body, headers), use the [placeholder method](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#using-placeholders-for-incremental-loading).

For query string parameters, you can also specify incremental loading directly in the `params` section:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "results",  # Optional JSONPath to select the list of posts
    "params": {
        "created_since": {
            "type": "incremental",
            "cursor_path": "created_at", # The JSONPath to the field we want to track in each post
            "initial_value": "2024-01-25",
        },
    },
}

```

Above we define the `created_since` parameter as an incremental parameter as:

```codeBlockLines_RjmQ
{
    "created_since": {
        "type": "incremental",
        "cursor_path": "created_at",
        "initial_value": "2024-01-25",
    },
}

```

The fields are:

- `type`: The type of the parameter definition. In this case, it must be set to `incremental`.
- `cursor_path`: The JSONPath to the field within each item in the list. The value of this field will be used in the next request. In the example above, our items look like `{"id": 1, "title": "Post 1", "created_at": "2024-01-26"}` so to track the created time, we set `cursor_path` to `"created_at"`. Note that the JSONPath starts from the root of the item (dict) and not from the root of the response.
- `initial_value`: The initial value for the cursor. This is the value that will initialize the state of incremental loading. In this case, it's `2024-01-25`. The value type should match the type of the field in the data item.

### Incremental loading using the `incremental` field (DEPRECATED) [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#incremental-loading-using-the-incremental-field-deprecated "Direct link to incremental-loading-using-the-incremental-field-deprecated")

caution

DEPRECATED: This method is deprecated and will be removed in a future dlt version. Use the [placeholder method](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#using-placeholders-for-incremental-loading) instead.

Another alternative method is to use the `incremental` field in the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration) while specifying names of the query string parameters to be used as start and end conditions.

Let's take the same example as above and configure it using the `incremental` field:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "results",
    "incremental": {
        "start_param": "created_since",
        "cursor_path": "created_at",
        "initial_value": "2024-01-25",
    },
}

```

The full available configuration for the `incremental` field is:

```codeBlockLines_RjmQ
{
    "incremental": {
        "start_param": "<start_parameter_name>",
        "end_param": "<end_parameter_name>",
        "cursor_path": "<path_to_cursor_field>",
        "initial_value": "<initial_value>",
        "end_value": "<end_value>",
        "convert": my_callable,
    }
}

```

The fields are:

- `start_param` (str): The name of the query parameter to be used as the start condition. If we use the example above, it would be `"created_since"`.
- `end_param` (str): The name of the query parameter to be used as the end condition. This is optional and can be omitted if you only need to track the start condition. This is useful when you need to fetch data within a specific range and the API supports end conditions (like the `created_before` query parameter).
- `cursor_path` (str): The JSONPath to the field within each item in the list. This is the field that will be used to track the incremental loading. In the example above, it's `"created_at"`.
- `initial_value` (str): The initial value for the cursor. This is the value that will initialize the state of incremental loading.
- `end_value` (str): The end value for the cursor to stop the incremental loading. This is optional and can be omitted if you only need to track the start condition. If you set this field, `initial_value` needs to be set as well.
- `convert` (callable): A callable that converts the cursor value into the format that the query parameter requires. For example, a UNIX timestamp can be converted into an ISO 8601 date or a date can be converted into `created_at+gt+{date}`.

See the [incremental loading](https://dlthub.com/docs/general-usage/incremental/cursor) guide for more details.

If you encounter issues with incremental loading, see the [troubleshooting section](https://dlthub.com/docs/general-usage/incremental/troubleshooting) in the incremental loading guide.

### Convert the incremental value before calling the API [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#convert-the-incremental-value-before-calling-the-api "Direct link to Convert the incremental value before calling the API")

If you need to transform the values in the cursor field before passing them to the API endpoint, you can specify a callable under the key `convert`. For example, the API might return UNIX epoch timestamps but expects to be queried with an ISO 8601 date. To achieve that, we can specify a function that converts from the date format returned by the API to the date format required for API requests.

In the following examples, `1704067200` is returned from the API in the field `updated_at`, but the API will be called with `?created_since=2024-01-01`.

Incremental loading using the `params` field:

```codeBlockLines_RjmQ
{
    "created_since": {
        "type": "incremental",
        "cursor_path": "updated_at",
        "initial_value": "1704067200",
        "convert": lambda epoch: pendulum.from_timestamp(int(epoch)).to_date_string(),
    }
}

```

Incremental loading using the `incremental` field:

```codeBlockLines_RjmQ
{
    "path": "posts",
    "data_selector": "results",
    "incremental": {
        "start_param": "created_since",
        "cursor_path": "updated_at",
        "initial_value": "1704067200",
        "convert": lambda epoch: pendulum.from_timestamp(int(epoch)).to_date_string(),
    },
}

```

## Troubleshooting [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#troubleshooting "Direct link to Troubleshooting")

If you encounter issues while running the pipeline, enable [logging](https://dlthub.com/docs/running-in-production/running#set-the-log-level-and-format) for detailed information about the execution:

```codeBlockLines_RjmQ
RUNTIME__LOG_LEVEL=INFO python my_script.py

```

This also provides details on the HTTP requests.

### Configuration issues [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#configuration-issues "Direct link to Configuration issues")

#### Getting validation errors [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#getting-validation-errors "Direct link to Getting validation errors")

When you are running the pipeline and getting a `DictValidationException`, it means that the [source configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#source-configuration) is incorrect. The error message provides details on the issue, including the path to the field and the expected type.

For example, if you have a source configuration like this:

```codeBlockLines_RjmQ
config: RESTAPIConfig = {
    "client": {
        # ...
    },
    "resources": [\
        {\
            "name": "issues",\
            "params": {             # <- Wrong: this should be inside\
                "sort": "updated",  #    the endpoint field below\
            },\
            "endpoint": {\
                "path": "issues",\
                # "params": {       # <- Correct configuration\
                #     "sort": "updated",\
                # },\
            },\
        },\
        # ...\
    ],
}

```

You will get an error like this:

```codeBlockLines_RjmQ
dlt.common.exceptions.DictValidationException: In path .: field 'resources[0]'
expects the following types: str, EndpointResource. Provided value {'name': 'issues', 'params': {'sort': 'updated'},
'endpoint': {'path': 'issues', ... }} with type 'dict' is invalid with the following errors:
For EndpointResource: In path ./resources[0]: following fields are unexpected {'params'}

```

It means that in the first resource configuration ( `resources[0]`), the `params` field should be inside the `endpoint` field.

tip

Import the `RESTAPIConfig` type from the `rest_api` module to have convenient hints in your editor/IDE and use it to define the configuration object.

```codeBlockLines_RjmQ
from dlt.sources.rest_api import RESTAPIConfig

```

#### Getting wrong data or no data [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#getting-wrong-data-or-no-data "Direct link to Getting wrong data or no data")

If incorrect data is received from an endpoint, check the `data_selector` field in the [endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration). Ensure the JSONPath is accurate and points to the correct data in the response body. `rest_api` attempts to auto-detect the data location, which may not always succeed. See the [data selection](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#data-selection) section for more details.

#### Getting insufficient data or incorrect pagination [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#getting-insufficient-data-or-incorrect-pagination "Direct link to Getting insufficient data or incorrect pagination")

Check the `paginator` field in the configuration. When not explicitly specified, the source tries to auto-detect the pagination method. If auto-detection fails, or the system is unsure, a warning is logged. For production environments, we recommend specifying an explicit paginator in the configuration. See the [pagination](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#pagination) section for more details. Some APIs may have non-standard pagination methods, and you may need to implement a [custom paginator](https://dlthub.com/docs/general-usage/http/rest-client#implementing-a-custom-paginator).

#### Incremental loading not working [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#incremental-loading-not-working "Direct link to Incremental loading not working")

See the [troubleshooting guide](https://dlthub.com/docs/general-usage/incremental/troubleshooting) for incremental loading issues.

#### Getting HTTP 404 errors [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#getting-http-404-errors "Direct link to Getting HTTP 404 errors")

Some APIs may return 404 errors for resources that do not exist or have no data. Manage these responses by configuring the `ignore` action in [response actions](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/advanced#response-actions).

### Authentication issues [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#authentication-issues "Direct link to Authentication issues")

If you are experiencing 401 (Unauthorized) errors, this could indicate:

- Incorrect authorization credentials. Verify credentials in the `secrets.toml`. Refer to [Secret and configs](https://dlthub.com/docs/general-usage/credentials/setup#troubleshoot-configuration-errors) for more information.
- An incorrect authentication type. Consult the API documentation for the proper method. See the [authentication](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#authentication) section for details. For some APIs, a [custom authentication method](https://dlthub.com/docs/general-usage/http/rest-client#implementing-custom-authentication) may be required.

### General guidelines [​](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic\#general-guidelines "Direct link to General guidelines")

The `rest_api` source uses the [RESTClient](https://dlthub.com/docs/general-usage/http/rest-client) class for HTTP requests. Refer to the RESTClient [troubleshooting guide](https://dlthub.com/docs/general-usage/http/rest-client#troubleshooting) for debugging tips.

For further assistance, join our [Slack community](https://dlthub.com/community). We're here to help!

- [Quick example](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#quick-example)
- [Setup](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#setup)
  - [Prerequisites](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#prerequisites)
  - [Initialize the REST API source](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#initialize-the-rest-api-source)
  - [Add credentials](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#add-credentials)
- [Run the pipeline](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#run-the-pipeline)
- [Source configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#source-configuration)
  - [Quick example](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#quick-example-1)
  - [Configuration structure](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#configuration-structure)
  - [Resource configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#resource-configuration)
  - [Endpoint configuration](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#endpoint-configuration)
  - [Pagination](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#pagination)
  - [Data selection](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#data-selection)
  - [Authentication](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#authentication)
  - [Define resource relationships](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#define-resource-relationships)
  - [Define a resource which is not a REST endpoint](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#define-a-resource-which-is-not-a-rest-endpoint)
  - [Processing steps: filter and transform data](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#processing-steps-filter-and-transform-data)
- [Incremental loading](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#incremental-loading)
  - [Using placeholders for incremental loading](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#using-placeholders-for-incremental-loading)
  - [Legacy method: Incremental loading in `params` (DEPRECATED)](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#legacy-method-incremental-loading-in-params-deprecated)
  - [Incremental loading using the `incremental` field (DEPRECATED)](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#incremental-loading-using-the-incremental-field-deprecated)
  - [Convert the incremental value before calling the API](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#convert-the-incremental-value-before-calling-the-api)
- [Troubleshooting](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#troubleshooting)
  - [Configuration issues](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#configuration-issues)
  - [Authentication issues](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#authentication-issues)
  - [General guidelines](https://dlthub.com/docs/dlt-ecosystem/verified-sources/rest_api/basic#general-guidelines)

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

----- https://dlthub.com/docs/intro#getting-started-with-dlt -----

Version: 1.11.0 (latest)

On this page

![dlt pacman](https://dlthub.com/docs/assets/images/dlt-pacman-b67f01290996fde3a5a12edbde2186bc.gif)

## What is dlt? [​](https://dlthub.com/docs/intro\#what-is-dlt "Direct link to What is dlt?")

dlt is an open-source Python library that loads data from various, often messy data sources into well-structured, live datasets. It offers a lightweight interface for extracting data from [REST APIs](https://dlthub.com/docs/tutorial/rest-api), [SQL databases](https://dlthub.com/docs/tutorial/sql-database), [cloud storage](https://dlthub.com/docs/tutorial/filesystem), [Python data structures](https://dlthub.com/docs/tutorial/load-data-from-an-api), and [many more](https://dlthub.com/docs/dlt-ecosystem/verified-sources).

dlt is designed to be easy to use, flexible, and scalable:

- dlt infers [schemas](https://dlthub.com/docs/general-usage/schema) and [data types](https://dlthub.com/docs/general-usage/schema/#data-types), [normalizes the data](https://dlthub.com/docs/general-usage/schema/#data-normalizer), and handles nested data structures.
- dlt supports a variety of [popular destinations](https://dlthub.com/docs/dlt-ecosystem/destinations/) and has an interface to add [custom destinations](https://dlthub.com/docs/dlt-ecosystem/destinations/destination) to create reverse ETL pipelines.
- dlt can be deployed anywhere Python runs, be it on [Airflow](https://dlthub.com/docs/walkthroughs/deploy-a-pipeline/deploy-with-airflow-composer), [serverless functions](https://dlthub.com/docs/walkthroughs/deploy-a-pipeline/deploy-with-google-cloud-functions), or any other cloud deployment of your choice.
- dlt automates pipeline maintenance with [incremental loading](https://dlthub.com/docs/general-usage/incremental-loading), [schema evolution](https://dlthub.com/docs/general-usage/schema-evolution), and [schema and data contracts](https://dlthub.com/docs/general-usage/schema-contracts).

To get started with dlt, install the library using pip:

```codeBlockLines_RjmQ
pip install dlt

```

tip

We recommend using a clean virtual environment for your experiments! Read the [detailed instructions](https://dlthub.com/docs/reference/installation) on how to set up one.

## Load data with dlt from … [​](https://dlthub.com/docs/intro\#load-data-with-dlt-from- "Direct link to Load data with dlt from …")

- REST APIs
- SQL databases
- Cloud storages or files
- Python data structures

Use dlt's [REST API source](https://dlthub.com/docs/tutorial/rest-api) to extract data from any REST API. Define the API endpoints you'd like to fetch data from, the pagination method, and authentication, and dlt will handle the rest:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.rest_api import rest_api_source

source = rest_api_source({
    "client": {
        "base_url": "https://api.example.com/",
        "auth": {
            "token": dlt.secrets["your_api_token"],
        },
        "paginator": {
            "type": "json_link",
            "next_url_path": "paging.next",
        },
    },
    "resources": ["posts", "comments"],
})

pipeline = dlt.pipeline(
    pipeline_name="rest_api_example",
    destination="duckdb",
    dataset_name="rest_api_data",
)

load_info = pipeline.run(source)

# print load info and posts table as dataframe
print(load_info)
print(pipeline.dataset().posts.df())

```

Follow the [REST API source tutorial](https://dlthub.com/docs/tutorial/rest-api) to learn more about the source configuration and pagination methods.

tip

If you'd like to try out dlt without installing it on your machine, check out the [Google Colab demo](https://colab.research.google.com/drive/1NfSB1DpwbbHX9_t5vlalBTf13utwpMGx?usp=sharing).

## Join the dlt community [​](https://dlthub.com/docs/intro\#join-the-dlt-community "Direct link to Join the dlt community")

1. Give the library a ⭐ and check out the code on [GitHub](https://github.com/dlt-hub/dlt).
2. Ask questions and share how you use the library on [Slack](https://dlthub.com/community).
3. Report problems and make feature requests [here](https://github.com/dlt-hub/dlt/issues/new/choose).

- [What is dlt?](https://dlthub.com/docs/intro#what-is-dlt)
- [Load data with dlt from …](https://dlthub.com/docs/intro#load-data-with-dlt-from-)
- [Join the dlt community](https://dlthub.com/docs/intro#join-the-dlt-community)

----- https://dlthub.com/docs/intro#join-the-dlt-community -----

PreferencesDeclineAccept

Version: 1.11.0 (latest)

On this page

![dlt pacman](https://dlthub.com/docs/assets/images/dlt-pacman-b67f01290996fde3a5a12edbde2186bc.gif)

## What is dlt? [​](https://dlthub.com/docs/intro\#what-is-dlt "Direct link to What is dlt?")

dlt is an open-source Python library that loads data from various, often messy data sources into well-structured, live datasets. It offers a lightweight interface for extracting data from [REST APIs](https://dlthub.com/docs/tutorial/rest-api), [SQL databases](https://dlthub.com/docs/tutorial/sql-database), [cloud storage](https://dlthub.com/docs/tutorial/filesystem), [Python data structures](https://dlthub.com/docs/tutorial/load-data-from-an-api), and [many more](https://dlthub.com/docs/dlt-ecosystem/verified-sources).

dlt is designed to be easy to use, flexible, and scalable:

- dlt infers [schemas](https://dlthub.com/docs/general-usage/schema) and [data types](https://dlthub.com/docs/general-usage/schema/#data-types), [normalizes the data](https://dlthub.com/docs/general-usage/schema/#data-normalizer), and handles nested data structures.
- dlt supports a variety of [popular destinations](https://dlthub.com/docs/dlt-ecosystem/destinations/) and has an interface to add [custom destinations](https://dlthub.com/docs/dlt-ecosystem/destinations/destination) to create reverse ETL pipelines.
- dlt can be deployed anywhere Python runs, be it on [Airflow](https://dlthub.com/docs/walkthroughs/deploy-a-pipeline/deploy-with-airflow-composer), [serverless functions](https://dlthub.com/docs/walkthroughs/deploy-a-pipeline/deploy-with-google-cloud-functions), or any other cloud deployment of your choice.
- dlt automates pipeline maintenance with [incremental loading](https://dlthub.com/docs/general-usage/incremental-loading), [schema evolution](https://dlthub.com/docs/general-usage/schema-evolution), and [schema and data contracts](https://dlthub.com/docs/general-usage/schema-contracts).

To get started with dlt, install the library using pip:

```codeBlockLines_RjmQ
pip install dlt

```

tip

We recommend using a clean virtual environment for your experiments! Read the [detailed instructions](https://dlthub.com/docs/reference/installation) on how to set up one.

## Load data with dlt from … [​](https://dlthub.com/docs/intro\#load-data-with-dlt-from- "Direct link to Load data with dlt from …")

- REST APIs
- SQL databases
- Cloud storages or files
- Python data structures

Use dlt's [REST API source](https://dlthub.com/docs/tutorial/rest-api) to extract data from any REST API. Define the API endpoints you'd like to fetch data from, the pagination method, and authentication, and dlt will handle the rest:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.rest_api import rest_api_source

source = rest_api_source({
    "client": {
        "base_url": "https://api.example.com/",
        "auth": {
            "token": dlt.secrets["your_api_token"],
        },
        "paginator": {
            "type": "json_link",
            "next_url_path": "paging.next",
        },
    },
    "resources": ["posts", "comments"],
})

pipeline = dlt.pipeline(
    pipeline_name="rest_api_example",
    destination="duckdb",
    dataset_name="rest_api_data",
)

load_info = pipeline.run(source)

# print load info and posts table as dataframe
print(load_info)
print(pipeline.dataset().posts.df())

```

Follow the [REST API source tutorial](https://dlthub.com/docs/tutorial/rest-api) to learn more about the source configuration and pagination methods.

tip

If you'd like to try out dlt without installing it on your machine, check out the [Google Colab demo](https://colab.research.google.com/drive/1NfSB1DpwbbHX9_t5vlalBTf13utwpMGx?usp=sharing).

## Join the dlt community [​](https://dlthub.com/docs/intro\#join-the-dlt-community "Direct link to Join the dlt community")

1. Give the library a ⭐ and check out the code on [GitHub](https://github.com/dlt-hub/dlt).
2. Ask questions and share how you use the library on [Slack](https://dlthub.com/community).
3. Report problems and make feature requests [here](https://github.com/dlt-hub/dlt/issues/new/choose).

- [What is dlt?](https://dlthub.com/docs/intro#what-is-dlt)
- [Load data with dlt from …](https://dlthub.com/docs/intro#load-data-with-dlt-from-)
- [Join the dlt community](https://dlthub.com/docs/intro#join-the-dlt-community)

----- https://dlthub.com/docs/intro#why-use-dlt -----

Version: 1.11.0 (latest)

On this page

![dlt pacman](https://dlthub.com/docs/assets/images/dlt-pacman-b67f01290996fde3a5a12edbde2186bc.gif)

## What is dlt? [​](https://dlthub.com/docs/intro\#what-is-dlt "Direct link to What is dlt?")

dlt is an open-source Python library that loads data from various, often messy data sources into well-structured, live datasets. It offers a lightweight interface for extracting data from [REST APIs](https://dlthub.com/docs/tutorial/rest-api), [SQL databases](https://dlthub.com/docs/tutorial/sql-database), [cloud storage](https://dlthub.com/docs/tutorial/filesystem), [Python data structures](https://dlthub.com/docs/tutorial/load-data-from-an-api), and [many more](https://dlthub.com/docs/dlt-ecosystem/verified-sources).

dlt is designed to be easy to use, flexible, and scalable:

- dlt infers [schemas](https://dlthub.com/docs/general-usage/schema) and [data types](https://dlthub.com/docs/general-usage/schema/#data-types), [normalizes the data](https://dlthub.com/docs/general-usage/schema/#data-normalizer), and handles nested data structures.
- dlt supports a variety of [popular destinations](https://dlthub.com/docs/dlt-ecosystem/destinations/) and has an interface to add [custom destinations](https://dlthub.com/docs/dlt-ecosystem/destinations/destination) to create reverse ETL pipelines.
- dlt can be deployed anywhere Python runs, be it on [Airflow](https://dlthub.com/docs/walkthroughs/deploy-a-pipeline/deploy-with-airflow-composer), [serverless functions](https://dlthub.com/docs/walkthroughs/deploy-a-pipeline/deploy-with-google-cloud-functions), or any other cloud deployment of your choice.
- dlt automates pipeline maintenance with [incremental loading](https://dlthub.com/docs/general-usage/incremental-loading), [schema evolution](https://dlthub.com/docs/general-usage/schema-evolution), and [schema and data contracts](https://dlthub.com/docs/general-usage/schema-contracts).

To get started with dlt, install the library using pip:

```codeBlockLines_RjmQ
pip install dlt

```

tip

We recommend using a clean virtual environment for your experiments! Read the [detailed instructions](https://dlthub.com/docs/reference/installation) on how to set up one.

## Load data with dlt from … [​](https://dlthub.com/docs/intro\#load-data-with-dlt-from- "Direct link to Load data with dlt from …")

- REST APIs
- SQL databases
- Cloud storages or files
- Python data structures

Use dlt's [REST API source](https://dlthub.com/docs/tutorial/rest-api) to extract data from any REST API. Define the API endpoints you'd like to fetch data from, the pagination method, and authentication, and dlt will handle the rest:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.rest_api import rest_api_source

source = rest_api_source({
    "client": {
        "base_url": "https://api.example.com/",
        "auth": {
            "token": dlt.secrets["your_api_token"],
        },
        "paginator": {
            "type": "json_link",
            "next_url_path": "paging.next",
        },
    },
    "resources": ["posts", "comments"],
})

pipeline = dlt.pipeline(
    pipeline_name="rest_api_example",
    destination="duckdb",
    dataset_name="rest_api_data",
)

load_info = pipeline.run(source)

# print load info and posts table as dataframe
print(load_info)
print(pipeline.dataset().posts.df())

```

Follow the [REST API source tutorial](https://dlthub.com/docs/tutorial/rest-api) to learn more about the source configuration and pagination methods.

tip

If you'd like to try out dlt without installing it on your machine, check out the [Google Colab demo](https://colab.research.google.com/drive/1NfSB1DpwbbHX9_t5vlalBTf13utwpMGx?usp=sharing).

## Join the dlt community [​](https://dlthub.com/docs/intro\#join-the-dlt-community "Direct link to Join the dlt community")

1. Give the library a ⭐ and check out the code on [GitHub](https://github.com/dlt-hub/dlt).
2. Ask questions and share how you use the library on [Slack](https://dlthub.com/community).
3. Report problems and make feature requests [here](https://github.com/dlt-hub/dlt/issues/new/choose).

- [What is dlt?](https://dlthub.com/docs/intro#what-is-dlt)
- [Load data with dlt from …](https://dlthub.com/docs/intro#load-data-with-dlt-from-)
- [Join the dlt community](https://dlthub.com/docs/intro#join-the-dlt-community)

----- https://dlthub.com/docs/intro#governance-support-in-dlt-pipelines -----

Version: 1.11.0 (latest)

On this page

![dlt pacman](https://dlthub.com/docs/assets/images/dlt-pacman-b67f01290996fde3a5a12edbde2186bc.gif)

## What is dlt? [​](https://dlthub.com/docs/intro\#what-is-dlt "Direct link to What is dlt?")

dlt is an open-source Python library that loads data from various, often messy data sources into well-structured, live datasets. It offers a lightweight interface for extracting data from [REST APIs](https://dlthub.com/docs/tutorial/rest-api), [SQL databases](https://dlthub.com/docs/tutorial/sql-database), [cloud storage](https://dlthub.com/docs/tutorial/filesystem), [Python data structures](https://dlthub.com/docs/tutorial/load-data-from-an-api), and [many more](https://dlthub.com/docs/dlt-ecosystem/verified-sources).

dlt is designed to be easy to use, flexible, and scalable:

- dlt infers [schemas](https://dlthub.com/docs/general-usage/schema) and [data types](https://dlthub.com/docs/general-usage/schema/#data-types), [normalizes the data](https://dlthub.com/docs/general-usage/schema/#data-normalizer), and handles nested data structures.
- dlt supports a variety of [popular destinations](https://dlthub.com/docs/dlt-ecosystem/destinations/) and has an interface to add [custom destinations](https://dlthub.com/docs/dlt-ecosystem/destinations/destination) to create reverse ETL pipelines.
- dlt can be deployed anywhere Python runs, be it on [Airflow](https://dlthub.com/docs/walkthroughs/deploy-a-pipeline/deploy-with-airflow-composer), [serverless functions](https://dlthub.com/docs/walkthroughs/deploy-a-pipeline/deploy-with-google-cloud-functions), or any other cloud deployment of your choice.
- dlt automates pipeline maintenance with [incremental loading](https://dlthub.com/docs/general-usage/incremental-loading), [schema evolution](https://dlthub.com/docs/general-usage/schema-evolution), and [schema and data contracts](https://dlthub.com/docs/general-usage/schema-contracts).

To get started with dlt, install the library using pip:

```codeBlockLines_RjmQ
pip install dlt

```

tip

We recommend using a clean virtual environment for your experiments! Read the [detailed instructions](https://dlthub.com/docs/reference/installation) on how to set up one.

## Load data with dlt from … [​](https://dlthub.com/docs/intro\#load-data-with-dlt-from- "Direct link to Load data with dlt from …")

- REST APIs
- SQL databases
- Cloud storages or files
- Python data structures

Use dlt's [REST API source](https://dlthub.com/docs/tutorial/rest-api) to extract data from any REST API. Define the API endpoints you'd like to fetch data from, the pagination method, and authentication, and dlt will handle the rest:

```codeBlockLines_RjmQ
import dlt
from dlt.sources.rest_api import rest_api_source

source = rest_api_source({
    "client": {
        "base_url": "https://api.example.com/",
        "auth": {
            "token": dlt.secrets["your_api_token"],
        },
        "paginator": {
            "type": "json_link",
            "next_url_path": "paging.next",
        },
    },
    "resources": ["posts", "comments"],
})

pipeline = dlt.pipeline(
    pipeline_name="rest_api_example",
    destination="duckdb",
    dataset_name="rest_api_data",
)

load_info = pipeline.run(source)

# print load info and posts table as dataframe
print(load_info)
print(pipeline.dataset().posts.df())

```

Follow the [REST API source tutorial](https://dlthub.com/docs/tutorial/rest-api) to learn more about the source configuration and pagination methods.

tip

If you'd like to try out dlt without installing it on your machine, check out the [Google Colab demo](https://colab.research.google.com/drive/1NfSB1DpwbbHX9_t5vlalBTf13utwpMGx?usp=sharing).

## Join the dlt community [​](https://dlthub.com/docs/intro\#join-the-dlt-community "Direct link to Join the dlt community")

1. Give the library a ⭐ and check out the code on [GitHub](https://github.com/dlt-hub/dlt).
2. Ask questions and share how you use the library on [Slack](https://dlthub.com/community).
3. Report problems and make feature requests [here](https://github.com/dlt-hub/dlt/issues/new/choose).

- [What is dlt?](https://dlthub.com/docs/intro#what-is-dlt)
- [Load data with dlt from …](https://dlthub.com/docs/intro#load-data-with-dlt-from-)
- [Join the dlt community](https://dlthub.com/docs/intro#join-the-dlt-community)