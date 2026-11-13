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

class MultipleChoicesError(ConvexLakeError):
    """Raised when a request matches multiple files; pass slug= to select one."""
    def __init__(self, message: str, slugs: list[str]):
        super().__init__(message)
        self.slugs = slugs
