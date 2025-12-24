"""Custom exceptions for pagepool package."""


class PoolException(Exception):
    """Base exception for all pool-related errors."""
    pass


class NoHealthyEndpointsError(PoolException):
    """Raised when no healthy endpoints are available."""
    pass


class EndpointConnectionError(PoolException):
    """Raised when unable to connect to a CDP endpoint."""
    pass


class ContextAcquireError(PoolException):
    """Raised when unable to acquire a browser context."""
    pass


class PageAcquireError(PoolException):
    """Raised when unable to acquire a page."""
    pass
