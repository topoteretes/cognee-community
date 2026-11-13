"""Public, credential-safe failures raised by the Contentful connector."""


class ContentfulSourceError(RuntimeError):
    """The source cannot safely complete its synchronization."""


class ContentfulAPIError(ContentfulSourceError):
    """A Delivery request failed after bounded retries."""


class ContentfulAuthError(ContentfulAPIError):
    """The Delivery token was rejected or cannot access this content."""


class ContentfulRateLimitError(ContentfulAPIError):
    """The provider's rate limit exceeded the request retry budget."""


class ContentfulSyncTokenError(ContentfulAPIError):
    """A Sync checkpoint was rejected and requires a complete refresh."""


class ContentfulResponseError(ContentfulSourceError):
    """A provider response is malformed, inconsistent, or outside this source."""
