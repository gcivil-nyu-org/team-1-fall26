from django.db import connection
from django.http import HttpResponse


class HealthCheckMiddleware:
    """Answer the load balancer's health checks before host validation.

    The load balancer calls /healthz/ on the instance's private IP, which is
    never in ALLOWED_HOSTS, so this must run before CommonMiddleware.

    /healthz/ answers without the database, so Elastic Beanstalk does not restart a
    healthy app because Postgres is slow. /readyz/ runs SELECT 1 and answers 503 when
    the database fails, for monitoring and for a person checking a deploy.
    """

    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        if request.path == "/healthz/":
            return HttpResponse("ok", content_type="text/plain")
        if request.path == "/readyz/":
            return self.ready()
        return self.get_response(request)

    @staticmethod
    def ready():
        try:
            with connection.cursor() as cursor:
                cursor.execute("SELECT 1")
                cursor.fetchone()
        except Exception:
            return HttpResponse(
                "database unavailable", status=503, content_type="text/plain"
            )
        return HttpResponse("ok", content_type="text/plain")
