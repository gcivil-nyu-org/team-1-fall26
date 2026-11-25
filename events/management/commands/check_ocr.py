from django.core.management.base import BaseCommand, CommandError

from events.services.event_ocr import ocr_runtime_status


class Command(BaseCommand):
    help = "Verify Tesseract binary, eng/osd tessdata, and end-to-end OCR readiness."

    def add_arguments(self, parser):
        parser.add_argument(
            "--no-smoke-test",
            action="store_true",
            help="Skip the synthetic image OCR extraction check.",
        )

    def handle(self, *args, **options):
        status = ocr_runtime_status(smoke_test=not options["no_smoke_test"])
        if not status["ready"]:
            self.stderr.write(f"OCR_READINESS=unavailable reason={status['error']}")
            raise CommandError(status["error"])

        langs = ",".join(status["languages"])
        self.stdout.write(
            f"OCR_READINESS=ready binary={status['binary']} "
            f"version={status['version']} languages={langs}"
        )
