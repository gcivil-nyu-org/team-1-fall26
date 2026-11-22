"""
Security lane gap1: the OCR upload pipeline (POST /events/workspaces/<uuid>/scan-ocr/).

Code under test: events/views.py:936-989 (scan_event_ocr), events/services/event_ocr.py
(limits :37-45, validate_and_load_image :311-416, preprocess :419-545,
extract_text_from_image :559-652, scan_event_image :1768-1786).

Every test_poc_* test asserts the SECURE behaviour, so it FAILS while the gap exists.
test_control_* tests pin behaviour that is already correct, so a later fix cannot
regress it.  Nothing here installs or runs Tesseract: pytesseract.image_to_osd and
image_to_string are mocked and their calls counted.  Nothing touches the network.

Run (own database, never the shared one):
    cd /Users/simonsavine/syncmily-security-fixes
    set -a; . <scratchpad>/target/target.env; set +a
    POSTGRES_DB=syncmily_sec_gap1 /Users/simonsavine/team-1-fall26/.venv/bin/python \
        manage.py test syncmily.tests.security.test_gap1 --noinput -v 2

ASVS = OWASP ASVS 5.0.0.  Pitfall numbers are from the 111-pitfall list
(11 Timeouts, 44 Backpressure, 92 Throughput).
"""

import json
import os
import random
import struct
import subprocess
import sys
import tempfile
import threading
import time
import zlib
from io import BytesIO
from types import SimpleNamespace
from unittest import mock

from django.conf import settings
from django.contrib.auth import get_user_model
from django.core.files.uploadedfile import SimpleUploadedFile
from django.db import connections
from django.test import Client, TestCase, TransactionTestCase
from django.urls import reverse
from django.utils import timezone
from PIL import Image, ImageFile, WebPImagePlugin

from accounts.models import Membership, Workspace
from events.services import event_ocr

PASSWORD = "Correct-Horse-Battery-9"
OCR = "events.services.event_ocr"
GOOD_TEXT = (
    "Spring Soccer Practice\nSaturday March 14 2027\n3:00 PM - 4:30 PM\nLincoln Field"
)
OSD_FAIL = RuntimeError("Tesseract process timeout")


# ------------------------------------------------------------------ builders ---
def make_household(prefix):
    User = get_user_model()
    parent = User.objects.create_user(username=f"{prefix}_parent", password=PASSWORD)
    workspace = Workspace.objects.create(name=f"{prefix} family", primary_admin=parent)
    kid = User.objects.create_user(username=f"{prefix}_kid", password=PASSWORD)
    Membership.objects.create(
        user=kid,
        workspace=workspace,
        role=Membership.Role.KID,
        invited_by=parent,
        accepted_at=timezone.now(),
    )
    return workspace, parent, kid


def login(user):
    client = Client()
    assert client.login(username=user.get_username(), password=PASSWORD)
    return client


def png_bytes(size=(300, 200), mode="RGB", color="white", **save_kw):
    buf = BytesIO()
    Image.new(mode, size, color).save(buf, "PNG", **save_kw)
    return buf.getvalue()


def noisy_png(size=(400, 300), seed=7):
    rnd = random.Random(seed)
    img = Image.frombytes("RGB", size, rnd.randbytes(size[0] * size[1] * 3))
    buf = BytesIO()
    img.save(buf, "PNG")
    return buf.getvalue()


def png_chunks(data):
    pos, out = 8, []
    while pos < len(data):
        (length,) = struct.unpack(">I", data[pos : pos + 4])
        out.append((data[pos + 4 : pos + 8], pos, length))
        pos += 12 + length
    return out


def make_chunk(kind, payload):
    crc = zlib.crc32(kind + payload) & 0xFFFFFFFF
    return struct.pack(">I", len(payload)) + kind + payload + struct.pack(">I", crc)


def bomb_png():
    """3464x3464 RGBA, one solid colour: 11,999,296 px (< 12,000,000), ~52 KB on the wire,
    48 MB once decoded."""
    return png_bytes((3464, 3464), "RGBA", (255, 255, 255, 255), optimize=True)


class OCRViewMixin:
    """Posts a file to the scan view with pytesseract mocked and the calls counted."""

    def scan(self, client, ws, data, name="scan.png", ctype="image/png", **extra):
        url = reverse("events:scan_ocr", args=[str(ws.pk)])
        upload = SimpleUploadedFile(name, data, content_type=ctype)
        with mock.patch(f"{OCR}.pytesseract.image_to_osd", side_effect=OSD_FAIL) as osd:
            with mock.patch(
                f"{OCR}.pytesseract.image_to_string", return_value=GOOD_TEXT
            ) as s2s:
                resp = client.post(
                    url, {"document": upload, "timezone": "UTC", **extra}
                )
        self.osd, self.s2s = osd, s2s
        return resp


# ------------------------------------------------- (a) memory amplification ---
RSS_SCRIPT = r"""
import json, os, resource, sys
from io import BytesIO
from unittest import mock
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "syncmily.settings")
from events.services import event_ocr as eo
def rss_mb():
    v = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    return v / 1e6 if sys.platform == "darwin" else v / 1e3
class F:
    name = "scan.png"; content_type = "image/png"
    def __init__(self, b): self.b = BytesIO(b); self.size = len(b)
    def seek(self, n): self.b.seek(n)
    def read(self, n=-1): return self.b.read(n)
data = open(sys.argv[1], "rb").read()
base = rss_mb()
with mock.patch.object(eo.pytesseract, "image_to_osd", side_effect=RuntimeError("x")), \
     mock.patch.object(eo.pytesseract, "image_to_string", return_value=sys.argv[2]) as s2s:
    img = eo.validate_and_load_image(F(data))
    after_load = rss_mb()
    mode, size = img.mode, img.size
    eo.extract_text_from_image(img)
    img.close()
print(json.dumps({"base": base, "after_load": after_load, "peak": rss_mb(),
                  "mode": mode, "size": size, "s2s_calls": s2s.call_count,
                  "file_bytes": len(data)}))
"""


class MemoryAmplificationTests(TestCase):
    def test_poc_GAP1_001_one_scan_of_a_52kb_png_costs_over_128mb_rss(self):
        """GAP1-001 (ASVS V5.2.1, V5.2.3, V5.2.6, V15.2.2; pitfalls 44, 92).

        A 3464x3464 RGBA PNG is 11,999,296 px, so it passes the 12 MP header cap
        (event_ocr.py:38), and is ~52 KB on the wire, so it passes the 10 MiB cap. It decodes
        to 48 MB; validate_and_load_image keeps it, then _normalize_canvas_and_size
        (:385-416) makes exif_transpose copy + RGBA convert + RGB canvas + resize, and the
        preprocess chain adds grayscale/contrast/sharpen/median/binarize copies. Peak RSS
        measured in a fresh process (tracemalloc cannot see Pillow's C allocations) grows by
        ~250 MB per scan. OCR_SLOTS allows two at once, on a 1 GiB t3.micro that also runs
        Daphne, nginx and the OS. Secure behaviour: a scan stays under 128 MB RSS growth
        (2 slots <= 256 MB, a quarter of the box)."""
        data = bomb_png()
        img = Image.open(BytesIO(data))
        pixels = img.width * img.height
        self.assertLess(len(data), event_ocr.MAX_IMAGE_SIZE_BYTES)
        self.assertLess(pixels, event_ocr.MAX_IMAGE_PIXELS)
        self.assertGreater(pixels * 4, 36_000_000)  # RGBA decodes to ~48 MB
        img.close()
        with tempfile.NamedTemporaryFile(suffix=".png", delete=False) as handle:
            handle.write(data)
        try:
            proc = subprocess.run(
                [sys.executable, "-c", RSS_SCRIPT, handle.name, GOOD_TEXT],
                cwd=settings.BASE_DIR,
                env=os.environ.copy(),
                capture_output=True,
                text=True,
                timeout=120,
            )
        finally:
            os.unlink(handle.name)
        self.assertEqual(proc.returncode, 0, proc.stderr[-2000:])
        m = json.loads(proc.stdout.strip().splitlines()[-1])
        growth = m["peak"] - m["base"]
        print(
            f"\n[GAP1-001] file={m['file_bytes']} B decoded={m['mode']}{m['size']} "
            f"rss base={m['base']:.0f} MB after_load={m['after_load']:.0f} MB "
            f"peak={m['peak']:.0f} MB growth={growth:.0f} MB "
            f"(amplification ~{growth * 1e6 / m['file_bytes']:.0f}x file size)"
        )
        self.assertLessEqual(
            growth,
            128,
            f"one scan of a {m['file_bytes']} byte PNG grew RSS by {growth:.0f} MB; "
            "two parallel slots would take about twice that",
        )


# ------------------------------------------------- (b) animated WebP, (c) formats ---
class FormatTests(OCRViewMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ws, cls.parent, cls.kid = make_household("g1fmt")

    def setUp(self):
        self.client = login(self.parent)

    def test_control_GAP1_002_animated_webp_500_frames_decodes_one_frame(self):
        """GAP1-002 (ASVS V5.2.1, V5.2.3): a 500-frame animated WebP (a few tens of KB) is
        accepted but only frame 0 is decoded (WebPAnimDecoder.get_next called once)."""
        frames = [
            Image.new("RGB", (400, 400), (i % 256, (i * 7) % 256, (i * 13) % 256))
            for i in range(500)
        ]
        buf = BytesIO()
        frames[0].save(
            buf,
            "WEBP",
            save_all=True,
            append_images=frames[1:],
            duration=20,
            lossless=True,
            method=0,
        )
        data = buf.getvalue()
        self.assertLess(len(data), event_ocr.MAX_IMAGE_SIZE_BYTES)
        probe = Image.open(BytesIO(data))
        self.assertEqual(probe.n_frames, 500)
        probe.close()

        real = WebPImagePlugin._webp.WebPAnimDecoder
        counter = SimpleNamespace(next_calls=0)

        class Counting:
            def __init__(self, blob):
                self._d = real(blob)

            def get_next(self):
                counter.next_calls += 1
                return self._d.get_next()

            def __getattr__(self, name):
                return getattr(self._d, name)

        with mock.patch.object(WebPImagePlugin._webp, "WebPAnimDecoder", Counting):
            started = time.monotonic()
            resp = self.scan(self.client, self.ws, data, "anim.webp", "image/webp")
            took = time.monotonic() - started
        self.assertLessEqual(
            counter.next_calls, 1, "animated WebP decoded frame by frame"
        )
        self.assertLess(took, 5)
        self.assertIn(resp.status_code, (200, 400, 422), resp.content[:200])

    def _decode_counter(self):
        """Count real pixel decodes (ImageFile.load) so a refused file shows 0."""
        real = ImageFile.ImageFile.load
        calls = []

        def counting(inner_self, *a, **kw):
            if inner_self.tile:  # only a call that still has pixels to decode
                calls.append(inner_self.format)
            return real(inner_self, *a, **kw)

        return mock.patch.object(ImageFile.ImageFile, "load", counting), calls

    def test_poc_GAP1_003_mislabelled_gif_tiff_eps_refused_before_decode(self):
        """GAP1-003 (ASVS V5.2.2): a GIF, a TIFF and an EPS renamed .png (Content-Type
        image/png) must be refused with 4xx and never decoded or OCR'd. The format allowlist
        (event_ocr.py:329-331 / :367) should stop them after the header sniff."""
        blobs = {}
        for fmt in ("GIF", "TIFF", "EPS"):
            buf = BytesIO()
            Image.new("RGB", (120, 80), "white").save(buf, fmt)
            blobs[fmt] = buf.getvalue()
        failures = []
        for fmt, data in blobs.items():
            patcher, decodes = self._decode_counter()
            with patcher:
                resp = self.scan(self.client, self.ws, data, "scan.png", "image/png")
            body = resp.json()
            if (
                resp.status_code != 400
                or body.get("error_code") != "unsupported_format"
                or decodes
                or self.s2s.call_count
            ):
                failures.append(
                    (fmt, resp.status_code, body, decodes, self.s2s.call_count)
                )
        self.assertEqual(failures, [])

    def test_poc_GAP1_004_wrong_extension_and_mime_are_refused(self):
        """GAP1-004 (ASVS V5.2.2): real PNG bytes named .svg/.php/.html or typed text/html or
        image/svg+xml are refused (allowlist at :318-336). Checks both the extension and the
        Content-Type gate."""
        data = png_bytes()
        cases = [
            ("a.svg", "image/svg+xml"),
            ("a.php", "image/png"),
            ("a.png", "text/html"),
            ("a.html", "image/png"),
        ]
        failures = []
        for name, ctype in cases:
            resp = self.scan(self.client, self.ws, data, name, ctype)
            if resp.status_code != 400 or self.s2s.call_count:
                failures.append((name, ctype, resp.status_code))
        self.assertEqual(failures, [])

    def test_control_GAP1_005_no_extension_octet_stream_png_decoded_once(self):
        """GAP1-005 (ASVS V5.2.2): a file with no extension sent as application/octet-stream
        passes the name/MIME gate (empty extension and octet-stream are allowed at :321/:326)
        and is then accepted on its magic bytes. The pixels are decoded once (verify() then one
        load()); OCR runs once. Advisory only: the content check, not the label, decides.
        """
        patcher, decodes = self._decode_counter()
        with patcher:
            resp = self.scan(
                self.client, self.ws, png_bytes(), "upload", "application/octet-stream"
            )
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertEqual(len(decodes), 1, decodes)
        self.assertEqual(self.s2s.call_count, 1)

    def test_control_GAP1_006_jpeg_with_trailing_zip_decoded_once(self):
        """GAP1-006 (ASVS V5.2.2, V5.2.3): a valid JPEG with a ZIP appended (a classic polyglot)
        is read as a JPEG: one decode, one OCR pass, trailing bytes ignored and never stored or
        served (the view returns only parsed fields)."""
        import zipfile

        jpg = BytesIO()
        Image.new("RGB", (300, 200), "white").save(jpg, "JPEG")
        zbuf = BytesIO()
        with zipfile.ZipFile(zbuf, "w") as z:
            z.writestr("evil.txt", "A" * 100_000)
        data = jpg.getvalue() + zbuf.getvalue()
        patcher, decodes = self._decode_counter()
        with patcher:
            resp = self.scan(self.client, self.ws, data, "photo.jpg", "image/jpeg")
        self.assertEqual(resp.status_code, 200, resp.content[:300])
        self.assertEqual(len(decodes), 1, decodes)
        self.assertEqual(self.s2s.call_count, 1)


# ------------------------------------------------- (d) corrupt input -> 4xx ---
class CorruptInputTests(OCRViewMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ws, cls.parent, cls.kid = make_household("g1bad")

    def setUp(self):
        self.client = login(self.parent)

    def test_poc_GAP1_007_truncated_and_crc_broken_png_give_4xx_not_500(self):
        """GAP1-007 (ASVS V16.5.3, V5.2.2): a truncated PNG, a PNG with a flipped byte in IDAT
        (bad CRC), a PNG whose IDAT has a valid CRC but garbage deflate data, and a PNG with a
        bad IHDR CRC must each get a 4xx corrupted_image answer, never 500, and never reach
        Tesseract."""
        good = noisy_png()
        idat = next(c for c in png_chunks(good) if c[0] == b"IDAT")
        _, pos, length = idat
        cases = {}
        cases["truncated"] = good[: int(len(good) * 0.6)]
        flipped = bytearray(good)
        flipped[pos + 8 + length // 2] ^= 0xFF
        cases["crc_broken_idat"] = bytes(flipped)
        garbage = (
            good[:pos]
            + make_chunk(b"IDAT", random.Random(3).randbytes(length))
            + good[pos + 12 + length :]
        )
        cases["valid_crc_garbage_deflate"] = garbage
        ihdr_bad = bytearray(good)
        ihdr_bad[8 + 8 + 13] ^= 0xFF  # IHDR CRC byte
        cases["bad_ihdr_crc"] = bytes(ihdr_bad)
        cases["truncated_after_header"] = good[:60]
        bad = []
        for label, data in cases.items():
            resp = self.scan(self.client, self.ws, data)
            if not (400 <= resp.status_code < 500) or self.s2s.call_count:
                bad.append((label, resp.status_code, resp.content[:120]))
        self.assertEqual(bad, [])

    def test_poc_GAP1_008_truncated_jpeg_and_webp_give_4xx_not_500(self):
        """GAP1-008 (ASVS V16.5.3): truncated JPEG and WebP bodies give 4xx corrupted_image."""
        rnd = random.Random(5)
        img = Image.frombytes("RGB", (300, 200), rnd.randbytes(300 * 200 * 3))
        jb, wb = BytesIO(), BytesIO()
        img.save(jb, "JPEG")
        img.save(wb, "WEBP", lossless=True)
        cases = {
            "jpeg_cut_60": (
                jb.getvalue()[: int(len(jb.getvalue()) * 0.6)],
                "a.jpg",
                "image/jpeg",
            ),
            "webp_cut_60": (
                wb.getvalue()[: int(len(wb.getvalue()) * 0.6)],
                "a.webp",
                "image/webp",
            ),
        }
        bad = []
        for label, (data, name, ctype) in cases.items():
            resp = self.scan(self.client, self.ws, data, name, ctype)
            if not (400 <= resp.status_code < 500) or self.s2s.call_count:
                bad.append((label, resp.status_code, resp.content[:120]))
        self.assertEqual(bad, [])

    def test_poc_GAP1_009_mutated_images_never_raise_unexpected_exceptions(self):
        """GAP1-009 (ASVS V16.5.3, V16.5.1): 450 seeded byte-flip / truncation mutants of a PNG,
        JPEG and WebP (each carrying EXIF orientation) go through scan_event_image directly. The
        only exception type allowed out is EventOCRError (-> JSON 4xx). Any other exception
        (zlib.error, struct.error, KeyError, ...) falls to the generic `except Exception` in the
        view (views.py:972-984) and becomes a 500 with a logged traceback."""
        exif = Image.Exif()
        exif[0x0112] = 6
        base = Image.frombytes(
            "RGB", (160, 120), random.Random(9).randbytes(160 * 120 * 3)
        )
        seeds = {}
        for fmt, ext, ctype in (
            ("PNG", "png", "image/png"),
            ("JPEG", "jpg", "image/jpeg"),
            ("WEBP", "webp", "image/webp"),
        ):
            buf = BytesIO()
            kw = {"lossless": True} if fmt == "WEBP" else {}
            base.save(buf, fmt, exif=exif.tobytes(), **kw)
            seeds[fmt] = (buf.getvalue(), f"m.{ext}", ctype)
        rnd = random.Random(1337)
        escaped = {}
        total = 0
        with mock.patch(
            f"{OCR}.pytesseract.image_to_osd", side_effect=OSD_FAIL
        ), mock.patch(f"{OCR}.pytesseract.image_to_string", return_value=GOOD_TEXT):
            for fmt, (data, name, ctype) in seeds.items():
                for _ in range(150):
                    buf = bytearray(data)
                    mode = rnd.choice(("flip", "flip", "cut", "zero"))
                    if mode == "cut":
                        buf = buf[: rnd.randrange(30, len(buf))]
                    elif mode == "zero":
                        start = rnd.randrange(20, len(buf) - 8)
                        buf[start : start + rnd.randrange(1, 9)] = b"\x00" * 8
                        buf = buf[: len(data)]
                    else:
                        for _i in range(rnd.randrange(1, 6)):
                            buf[rnd.randrange(20, len(buf))] = rnd.randrange(256)
                    total += 1
                    upload = SimpleUploadedFile(name, bytes(buf), content_type=ctype)
                    try:
                        event_ocr.scan_event_image(upload, timezone_name="UTC")
                    except event_ocr.EventOCRError:
                        pass
                    except Exception as exc:  # noqa: BLE001 - this is what we hunt
                        key = f"{fmt}:{type(exc).__module__}.{type(exc).__name__}"
                        escaped.setdefault(key, []).append(str(exc)[:80])
        counts = {k: len(v) for k, v in escaped.items()}
        print(f"\n[GAP1-009] {total} mutants, escaped exception types: {counts}")
        self.assertEqual(escaped, {}, "unexpected exception types would be HTTP 500")

    def test_poc_GAP1_014_non_tiff_exif_in_png_or_webp_gives_4xx_not_500(self):
        """GAP1-014 (ASVS V16.5.3, V16.5.1, V5.2.2): a PNG with an eXIf chunk, or a WebP with an
        EXIF chunk, whose payload is not TIFF data (b"not a tiff at all!!") decodes fine in
        validate_and_load_image, but ImageOps.exif_transpose() in _normalize_canvas_and_size
        (event_ocr.py:386, outside the try/except at :357-415) raises SyntaxError('not a TIFF
        file'). It is not an EventOCRError, so the view's catch-all (views.py:972-984) logs a
        traceback and answers 500 ocr_failed. Any caregiver can make the app log errors at will
        (5/min). Secure behaviour: a 4xx corrupted_image answer. JPEG with the same payload is
        fine (control)."""
        bad_payloads = [
            b"not a tiff at all!!",
            b"MM\x03\x00*\x00\x00\x00\x08" + b"\xff" * 20,
        ]
        cases = []
        for fmt, name, ctype in (
            ("PNG", "e.png", "image/png"),
            ("WEBP", "e.webp", "image/webp"),
        ):
            for payload in bad_payloads:
                buf = BytesIO()
                Image.new("RGB", (200, 100), "white").save(buf, fmt, exif=payload)
                cases.append((fmt, payload[:6], buf.getvalue(), name, ctype))
        bad = []
        for fmt, payload, data, name, ctype in cases:
            with mock.patch("events.views.logger"):
                resp = self.scan(self.client, self.ws, data, name, ctype)
            if resp.status_code >= 500:
                bad.append(
                    (fmt, payload, resp.status_code, resp.json().get("error_code"))
                )
        self.assertEqual(
            bad, [], "attacker-controlled EXIF reaches the generic 500 path"
        )


# ------------------------------------------------- (e) semaphore DoS ---
class SemaphoreFairnessTests(OCRViewMixin, TransactionTestCase):
    def test_poc_GAP1_010_one_account_holds_both_ocr_slots_and_other_family_gets_429(
        self,
    ):
        """GAP1-010 (ASVS V15.2.2, V15.1.3; pitfalls 44, 92, 11): OCR_SLOTS is one process-wide
        BoundedSemaphore(2) (event_ocr.py:45) with a non-blocking acquire (:1770) that answers 429
        'busy' to anyone while two scans are running. Parent A fires two scans in parallel (the
        per-user limit allows 5/min and 30/h, so A can refill the slots as they free); mocked
        Tesseract takes 3 s per call. Parent B of a different household then scans once. Secure
        behaviour: B is not refused with 429 (per-user or per-household fairness, or a short wait
        queue). Today B gets 429 'busy'."""
        ws_a, parent_a, _ = make_household("g1a")
        ws_b, parent_b, _ = make_household("g1b")
        client_a1, client_a2, client_b = (
            login(parent_a),
            login(parent_a),
            login(parent_b),
        )
        entered = threading.Semaphore(0)

        def slow_ocr(*args, **kwargs):
            entered.release()
            time.sleep(3)
            return GOOD_TEXT

        results = {}

        def run(label, client, ws):
            try:
                url = reverse("events:scan_ocr", args=[str(ws.pk)])
                up = SimpleUploadedFile(
                    "scan.png", png_bytes(), content_type="image/png"
                )
                resp = client.post(url, {"document": up, "timezone": "UTC"})
                results[label] = (resp.status_code, resp.content[:120])
            finally:
                connections.close_all()

        with mock.patch(
            f"{OCR}.pytesseract.image_to_osd", side_effect=OSD_FAIL
        ), mock.patch(
            f"{OCR}.pytesseract.image_to_string", side_effect=slow_ocr
        ) as s2s:
            threads = [
                threading.Thread(target=run, args=("A1", client_a1, ws_a)),
                threading.Thread(target=run, args=("A2", client_a2, ws_a)),
            ]
            for t in threads:
                t.start()
            got = [entered.acquire(timeout=15) for _ in range(2)]
            self.assertEqual(got, [True, True], "both of A's scans must be inside OCR")
            started = time.monotonic()
            run("B", client_b, ws_b)  # runs in this thread, while A holds both slots
            b_elapsed = time.monotonic() - started
            for t in threads:
                t.join(timeout=30)
        print(
            f"\n[GAP1-010] results={results} B answered in {b_elapsed:.2f}s "
            f"(image_to_string calls={s2s.call_count})"
        )
        self.assertEqual(results["A1"][0], 200, results)
        self.assertEqual(results["A2"][0], 200, results)
        self.assertNotEqual(
            results["B"][0],
            429,
            f"family B (a different household and user) was refused: {results['B']}",
        )

    def test_control_GAP1_011_slot_is_released_after_every_outcome(self):
        """GAP1-011 (ASVS V16.5.3): the semaphore is released on success, validation error and
        OCR error (finally at event_ocr.py:1780), so a run of bad uploads cannot leak slots.
        """
        ws, parent, _ = make_household("g1rel")
        client = login(parent)
        for data, name in (
            (png_bytes(), "ok.png"),
            (b"not an image", "x.png"),
            (b"", "e.png"),
        ):
            self.scan(client, ws, data, name)
        self.assertTrue(event_ocr.OCR_SLOTS.acquire(blocking=False))
        self.assertTrue(event_ocr.OCR_SLOTS.acquire(blocking=False))
        event_ocr.OCR_SLOTS.release()
        event_ocr.OCR_SLOTS.release()


# ------------------------------------------------- (f) time budget ---
class VirtualClock:
    def __init__(self):
        self.now = 0.0

    def monotonic(self):
        return self.now


class TimeBudgetTests(OCRViewMixin, TestCase):
    @classmethod
    def setUpTestData(cls):
        cls.ws, cls.parent, cls.kid = make_household("g1time")

    def setUp(self):
        self.client = login(self.parent)

    def _run(self, osd_seconds, string_seconds):
        """Drive one scan on a virtual clock. Each mocked Tesseract call advances the clock by
        its scripted duration (a call at >= 15 s hits pytesseract's timeout and raises
        RuntimeError, as the real library does). Returns (virtual seconds, call log, status).
        """
        clock = VirtualClock()
        log = []
        script = list(string_seconds)

        def call(kind, seconds):
            seconds = min(seconds, event_ocr.OCR_TIMEOUT_SECONDS)
            clock.now += seconds
            log.append((kind, round(clock.now, 2)))
            if seconds >= event_ocr.OCR_TIMEOUT_SECONDS:
                raise RuntimeError("Tesseract process timeout")

        def fake_osd(*a, **kw):
            call("osd", osd_seconds)
            return {"rotate": 0, "orientation_conf": 0.0}

        def fake_string(*a, **kw):
            call("string", script.pop(0) if script else script_default)
            return ""  # no usable text, so every fallback pass is tried

        script_default = string_seconds[-1]
        fake_time = SimpleNamespace(monotonic=clock.monotonic)
        url = reverse("events:scan_ocr", args=[str(self.ws.pk)])
        up = SimpleUploadedFile(
            "scan.png", png_bytes((1200, 800)), content_type="image/png"
        )
        with mock.patch(f"{OCR}.time_module", fake_time), mock.patch(
            f"{OCR}.pytesseract.image_to_osd", side_effect=fake_osd
        ), mock.patch(f"{OCR}.pytesseract.image_to_string", side_effect=fake_string):
            resp = self.client.post(url, {"document": up, "timezone": "UTC"})
        return clock.now, log, resp.status_code

    def test_poc_GAP1_012_osd_pass_is_outside_the_25s_budget(self):
        """GAP1-012 (ASVS V15.2.2, V15.1.3; pitfall 11): OCR_TIME_BUDGET_SECONDS = 25 starts its
        deadline only after preprocess_image_for_ocr (event_ocr.py:561-562), which already ran one
        Tesseract OSD call (:441-445, up to 15 s), and it is checked only between passes. With
        every call taking 14 s the scan runs OSD + primary + one fallback = 42 s of OCR. Secure
        behaviour: a scan stays within the 25 s budget plus at most one in-flight call (40 s).
        """
        elapsed, log, status = self._run(14, [14])
        print(
            f"\n[GAP1-012] 14 s per call -> {elapsed:.1f} s total, calls={log}, http={status}"
        )
        limit = event_ocr.OCR_TIME_BUDGET_SECONDS + event_ocr.OCR_TIMEOUT_SECONDS
        self.assertLessEqual(
            elapsed, limit, f"OCR wall time {elapsed:.1f} s, calls {log}"
        )

    def test_poc_GAP1_013_worst_case_scan_runs_about_55s_against_nginx_60s(self):
        """GAP1-013 (ASVS V15.2.2, V15.1.3; pitfalls 11, 44): worst case on the virtual clock.
        OSD takes 14.99 s (not budgeted), the primary pass returns after 10 s with too little text,
        and the next two passes each take 14.99 s. The deadline is armed after OSD (at 14.99 s) so
        it falls at 39.99 s; the third call starts at 39.98 s, 0.01 s before it, so it is still
        allowed to run its full 15 s. Total ~55 s of Tesseract time, plus PIL work and the 3 s of
        parse_event_text, against nginx's default 60 s proxy_read_timeout (no override in
        .platform/hooks/postdeploy/03_https.sh), while the request holds one of two OCR slots.
        Secure behaviour: leave at least 10 s of margin (under 50 s)."""
        elapsed, log, status = self._run(14.99, [10, 14.99, 14.99, 14.99])
        print(
            f"\n[GAP1-013] worst case -> {elapsed:.2f} s of Tesseract time, calls={log}, http={status}"
        )
        self.assertLess(elapsed, 50, f"calls {log}")
