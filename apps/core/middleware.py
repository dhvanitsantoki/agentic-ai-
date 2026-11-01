from django.core.exceptions import PermissionDenied

from .models import Role
from .permissions import role_of

SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
VIEWER_ALLOWED_POST_PATHS = ("/accounts/logout/", "/notifications/")


class ReadOnlyViewerMiddleware:
    """Users with the Viewer role can look at everything but change nothing."""

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if (
            request.method not in SAFE_METHODS
            and request.user.is_authenticated
            and role_of(request.user) == Role.VIEWER
            and not request.path.startswith(VIEWER_ALLOWED_POST_PATHS)
        ):
            raise PermissionDenied("Viewer accounts are read-only.")
        return self.get_response(request)
