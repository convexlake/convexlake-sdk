class ConvexLakeError(Exception):
    pass

class AuthError(ConvexLakeError):
    pass

class NotFoundError(ConvexLakeError):
    pass

class RateLimitError(ConvexLakeError):
    pass

class PlanError(ConvexLakeError):
    """Raised when the endpoint requires a higher subscription tier."""
    pass
