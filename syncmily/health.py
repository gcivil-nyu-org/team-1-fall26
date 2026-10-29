from django.http import HttpResponse


class HealthCheckMiddleware:
    """Answer the load balancer's health check before host validation.

    The load balancer calls /healthz/ on the instance's private IP, which is
    never in ALLOWED_HOSTS, so this must run before CommonMiddleware.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == "/healthz/":
            return HttpResponse("ok", content_type="text/plain")
        return self.get_response(request)
