## Cognee Community Retrievers

This directory features custom community retrievers, where anyone can define new, custom retrievers.
Please check our [main repository](https://github.com/topoteretes/cognee) before adding new retrievers, and make sure that the functionality
you want to implement isn't already present in an existing retriever.

## Instructions

Any new retriever should inherit from the [base retriever](https://github.com/topoteretes/cognee/blob/main/cognee/modules/retrieval/base_retriever.py) class,
or another, more specific retriever, if applicable. Current existing retrievers can be found on 
cognee's [core repo](https://github.com/topoteretes/cognee/tree/main/cognee/modules/retrieval).

When adding a new retriever, one should follow these steps:

1) In the `retriever` directory, create a new directory with the name of the new retriever
    (such as `code_retriever`), and inside it, add the directory (which will be the package
    for the retriever), named `cognee_community_retriever_<your_retriever_name>`, e.g. `cognee_community_retriever_code`.
2) Add a `pyproject.toml` file, with all the necessary dependencies defined.
3) As you can see in the `code_retriever`, one must also define a new `SearchType` to go along
    with the new retriever. The way this is done now (as of December 8th 2025), is that one should
    add new values to the `SearchType` Enum imported from Cognee. More specifically, you should add the
    the SearchType value in the following manner:
```python
from aenum import extend_enum


# We define the search type as a class here to avoid hardcoding it in
# other modules and files. This will also help in reducing warnings from the compiler.
class CodeSearchType:
    name = "CODE"
    value = "CODE"


extend_enum(SearchType, CodeSearchType.name, CodeSearchType.value)
```
**Note** that your retriever will need `aenum` as one of its dependencies.

After defining the retriever and the search type, you must also create a `register.py` file, which calls the
registration function. This enables cognee to use the new retriever. 

```python
from cognee.modules.retrieval.register_retriever import use_retriever
from cognee.modules.search.types import SearchType

from cognee_community_retriever_code.code_retriever import CodeRetriever, CodeSearchType

use_retriever(SearchType[CodeSearchType.name], CodeRetriever)
```

An example of how to use this register function can be seen in the `codify_example.py` in the `pipeline/codify_pipeline` 
directory. It is enough to simply do something like this before using the retriever and search type:
```python
from cognee_community_retriever_code import register  # noqa: F401
```
4) Add at least one example and/or test, if necessary, to make sure the retriever works as intended.