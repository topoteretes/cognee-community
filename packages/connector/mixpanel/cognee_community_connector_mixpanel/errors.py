"""Connector errors expose actionable context without provider payloads or secrets."""


class MixpanelConnectorError(RuntimeError):
    """A sync failed without publishing an authoritative partial inventory."""


class ResponseContractError(MixpanelConnectorError):
    """The selected provider response is incomplete, inconsistent or unsupported."""


class RequestBudgetExceeded(MixpanelConnectorError):
    """The selected scope cannot complete within the configured request budget."""
