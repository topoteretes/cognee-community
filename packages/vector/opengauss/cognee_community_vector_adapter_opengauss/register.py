from cognee.infrastructure.databases.vector import use_vector_adapter

from .opengauss_adapter import OpenGaussAdapter

use_vector_adapter("opengauss", OpenGaussAdapter)
