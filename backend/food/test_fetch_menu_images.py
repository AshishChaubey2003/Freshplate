"""Local tests for the fetch_menu_images command.

* All HTTP is mocked - no real Pixabay request is ever made.
* Tests use SimpleTestCase, which makes Django raise on ANY database access,
  so every passing test also proves the command did not touch the database.
* The dish list for the dry run is faked, so these tests do not depend on the
  FoodItem model's other fields.
"""
import io
import json
import os
import tempfile
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

from django.core.management import call_command
from django.core.management.base import CommandError
from django.test import SimpleTestCase
from PIL import Image

from food.management.commands import fetch_menu_images as fmi

API = fmi.PIXABAY_URL


# ------------------------------------------------------------------ helpers
def make_hit(pid, tags, user="chef_anna", **extra):
    hit = {
        "id": pid, "type": "photo", "tags": tags, "likes": 5, "user": user, "user_id": 4242,
        "pageURL": f"https://pixabay.com/photos/dish-{pid}/",
        "webformatURL": f"https://cdn.pixabay.com/photo/2020/01/01/00/00/dish-{pid}_640.jpg",
        "previewURL": f"https://cdn.pixabay.com/photo/2020/01/01/00/00/dish-{pid}_150.jpg",
        "imageWidth": 4000, "imageHeight": 3000,
    }
    hit.update(extra)
    if hit["user"] is None:
        del hit["user"]
    return hit


def jpeg_bytes(size=(800, 600), exif=True):
    img = Image.new("RGB", size, (200, 120, 40))
    out = io.BytesIO()
    kwargs = {}
    if exif:
        exif_data = Image.Exif()
        exif_data[0x010F] = "SecretCameraMaker"
        kwargs["exif"] = exif_data
    img.save(out, format="JPEG", **kwargs)
    return out.getvalue()


def png_bytes(size=(800, 600), alpha=True):
    img = Image.new("RGBA" if alpha else "RGB", size, (10, 200, 10, 128) if alpha else (10, 200, 10))
    out = io.BytesIO()
    img.save(out, format="PNG")
    return out.getvalue()


class FakeResponse:
    def __init__(self, status=200, headers=None, body=b"", json_data=None):
        self.status_code = status
        self.headers = headers or {}
        self._body = body
        self._json = json_data

    def json(self):
        if self._json is None:
            raise ValueError("no json")
        return self._json

    def iter_content(self, chunk_size):
        for i in range(0, len(self._body), chunk_size):
            yield self._body[i:i + chunk_size]

    def close(self):
        pass


class FakeWeb:
    """Routes mocked requests.get calls and records them."""

    def __init__(self):
        self.by_id = {}          # pixabay id -> hit (returned for ?id=)
        self.by_query = {}       # query -> list of hits
        self.images = {}         # url -> FakeResponse
        self.calls = []          # (url, params)

    def add(self, hit, image=None):
        self.by_id[hit["id"]] = hit
        if image is not None:
            self.images[hit["webformatURL"]] = image
        return hit

    def __call__(self, url, params=None, **kwargs):
        self.calls.append((url, dict(params or {}), kwargs))
        if url == API:
            if "id" in params:
                hit = self.by_id.get(params["id"])
                hits = [hit] if hit else []
            else:
                hits = self.by_query.get(params.get("q"), [])
            return FakeResponse(200, json_data={"hits": hits})
        if url in self.images:
            return self.images[url]
        return FakeResponse(404)

    @property
    def image_calls(self):
        return [c for c in self.calls if c[0] != API]

    @property
    def api_calls(self):
        return [c for c in self.calls if c[0] == API]


def image_ok(body=None, ctype="image/jpeg"):
    body = body if body is not None else jpeg_bytes()
    return FakeResponse(200, {"Content-Type": ctype, "Content-Length": str(len(body))}, body)


def entry(dish, pid, approved=True, page_url=None):
    return {"dish": dish, "pixabay_id": pid, "page_url": page_url or f"https://pixabay.com/photos/dish-{pid}/",
            "approved": approved}


class FakeFood:
    """Stands in for FoodItem.objects in dry-run tests (read-only)."""

    def __init__(self, dishes):
        self.dishes = dishes
        self.writes = 0

    def select_related(self, *a):
        return self

    def order_by(self, *a):
        return list(self.dishes)

    def __getattr__(self, name):          # create/update/delete/... must never be called
        raise AssertionError(f"database method {name!r} was used")


def dish(name, veg=True, image_url="", did=1):
    return SimpleNamespace(id=did, name=name, is_veg=veg, image_url=image_url, image=None,
                           category=SimpleNamespace(name="Cat"))


# ------------------------------------------------------------ base test case
class CommandTestCase(SimpleTestCase):
    databases = set()

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.out = self.root / "out" / "menu"
        self.manifest_path = self.root / "manifest.json"
        self.web = FakeWeb()
        env = mock.patch.dict(os.environ, {"PIXABAY_API_KEY": "TEST-KEY-NOT-REAL"})
        env.start()
        self.addCleanup(env.stop)
        for target, kw in [
            (mock.patch.object(fmi.requests, "get", self.web), {}),
            (mock.patch.object(fmi.time, "sleep", lambda s: None), {}),
            (mock.patch.object(fmi.tempfile, "gettempdir", lambda: str(self.root / "systemp")), {}),
        ]:
            target.start()
            self.addCleanup(target.stop)
        (self.root / "systemp").mkdir()

    def write_manifest(self, approvals, version=1):
        self.manifest_path.write_text(json.dumps({"version": version, "approvals": approvals}), encoding="utf-8")

    def download(self, *extra, expect_error=False):
        out = io.StringIO()
        args = ["fetch_menu_images", "--download", "--manifest", str(self.manifest_path),
                "--output-dir", str(self.out), "--no-cache", *extra]
        if expect_error:
            with self.assertRaises(CommandError) as ctx:
                call_command(*args, stdout=out, stderr=io.StringIO())
            return out.getvalue() + "\n" + str(ctx.exception)
        call_command(*args, stdout=out, stderr=io.StringIO())
        return out.getvalue()

    def tree(self, base=None):
        base = base or self.root
        return sorted(p.relative_to(base).as_posix() for p in base.rglob("*"))

    def assertNothingDownloaded(self):
        self.assertEqual(self.web.image_calls, [], "an image URL was requested")
        self.assertFalse(self.out.exists() and any(self.out.iterdir()), "files were written")


# ============================================================== scorer tests
class ScorerTests(SimpleTestCase):
    def plan(self, name):
        return fmi.SEARCH_PLAN[name]

    def test_veg_dishes_reject_meat_terms(self):
        for term in ["chicken", "flesh", "goulash", "meat", "kebab", "shashlik", "korma", "chicken curry",
                     "mutton", "fish", "egg"]:
            with self.subTest(term=term):
                result = fmi.evaluate_hit({"tags": f"dal, curry, {term}"}, self.plan("Dal Makhani"))
                self.assertEqual(result["status"], "REJECTED")
                self.assertIn("non-veg", result["reason"])
                self.assertTrue(result["terms"])

    def test_veg_rejection_applies_to_every_veg_dish(self):
        for name, plan in fmi.SEARCH_PLAN.items():
            if plan["veg"]:
                with self.subTest(dish=name):
                    result = fmi.evaluate_hit({"tags": f"{plan['strong'][0]}, chicken curry"}, plan)
                    self.assertEqual(result["status"], "REJECTED")

    def test_paneer_tikka_may_use_kebab_tag_but_not_chicken(self):
        plan = self.plan("Paneer Tikka")
        self.assertEqual(fmi.evaluate_hit({"tags": "paneer, tikka, kebab"}, plan)["status"], "MATCH")
        self.assertEqual(fmi.evaluate_hit({"tags": "paneer, tikka, kebab, chicken"}, plan)["status"], "REJECTED")

    def test_non_veg_dishes_accept_appropriate_meat(self):
        cases = {
            "Chicken Biryani": "biryani, chicken, rice, meat",
            "Mutton Rogan Josh": "mutton, curry, meat, flesh",
            "Chicken Seekh Kebab": "seekh kebab, chicken, meat, skewer",
        }
        for name, tags in cases.items():
            with self.subTest(dish=name):
                self.assertEqual(fmi.evaluate_hit({"tags": tags}, self.plan(name))["status"], "MATCH")

    def test_non_veg_per_dish_rules_still_apply(self):
        # a kebab photo is wrong for a curry; a pork photo is wrong for chicken biryani
        self.assertEqual(fmi.evaluate_hit({"tags": "mutton, kebab, meat"}, self.plan("Mutton Rogan Josh"))["status"],
                         "REJECTED")
        self.assertEqual(fmi.evaluate_hit({"tags": "biryani, pork"}, self.plan("Chicken Biryani"))["status"], "REJECTED")

    def test_unsuitable_tags_rejected_for_veg_and_non_veg(self):
        for term in ["ai generated", "digital art", "illustration", "alcohol", "gin", "cocktail", "wallpaper"]:
            for name in ("Paneer Tikka", "Chicken Biryani"):
                with self.subTest(term=term, dish=name):
                    plan = self.plan(name)
                    result = fmi.evaluate_hit({"tags": f"{plan['strong'][0]}, {term}"}, plan)
                    self.assertEqual(result["status"], "REJECTED")
                    self.assertEqual(result["reason"], "unsuitable content")

    def test_tag_normalisation(self):
        plan = self.plan("Paneer Tikka")
        for tags in ["  AI   Generated , paneer", "Digital-Art,paneer", "paneer,\tILLUSTRATION", "paneer, Ai_Generated"]:
            with self.subTest(tags=tags):
                self.assertEqual(fmi.evaluate_hit({"tags": tags}, plan)["status"], "REJECTED")

    def test_no_broad_substring_false_positives(self):
        veg = self.plan("Dal Makhani")
        for tag in ["ginger", "eggplant", "hamburger", "sandal", "pancake", "chickpeas", "rendering", "fisherman"]:
            with self.subTest(tag=tag):
                result = fmi.evaluate_hit({"tags": f"dal, {tag}"}, veg)
                self.assertNotEqual(result["status"], "REJECTED", result)
        self.assertEqual(fmi.term_hits(["dal"], ["sandal"]), [])
        self.assertEqual(fmi.term_hits(["egg"], ["eggplant"]), [])
        self.assertEqual(fmi.term_hits(["gin"], ["gin tonic"]), ["gin"])

    def test_non_photo_rejected(self):
        result = fmi.evaluate_hit({"tags": "paneer, tikka", "type": "illustration"}, self.plan("Paneer Tikka"))
        self.assertEqual(result["status"], "REJECTED")

    def test_ambiguous_results_are_never_selected(self):
        plan = self.plan("Paneer Tikka")
        self.assertEqual(fmi.evaluate_hit({"tags": "food, plate, delicious"}, plan)["status"], "NONE")
        hits = [make_hit(1, "food, plate"), make_hit(2, "grilled, skewer"), make_hit(3, "chicken, paneer"),
                make_hit(4, "paneer, tikka")]
        usable, rejected = fmi.rank_hits(hits, plan)
        self.assertEqual([u["hit"]["id"] for u in usable], [4, 2])          # NONE (1) is dropped, WEAK is kept after MATCH
        self.assertEqual([r["pixabay_id"] for r in rejected], [3])
        self.assertTrue(all(r["reason"] and r["terms"] for r in rejected))   # reasons are recorded

    def test_slug_is_safe(self):
        self.assertEqual(fmi.dish_slug("Veg Pulao (rescue deal)"), "veg-pulao")
        self.assertEqual(fmi.dish_slug("Chole Bhature"), "chole-bhature")
        for bad in ["", "///", "a" * 100]:
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    fmi.dish_slug(bad)
        for hostile in ["../../etc/passwd", "..\\..\\win.ini", "a/b"]:       # sanitised, never a path
            slug = fmi.dish_slug(hostile)
            self.assertRegex(slug, r"^[a-z0-9]+(-[a-z0-9]+)*$")

    def test_url_validation(self):
        ok = "https://cdn.pixabay.com/photo/2020/a_640.jpg"
        self.assertIsNone(fmi.validate_image_url(ok))
        for bad in [None, "", "http://cdn.pixabay.com/a.jpg", "https://evil.com/a.jpg",
                    "https://cdn.pixabay.com.evil.com/a.jpg", "https://user:pw@cdn.pixabay.com/a.jpg",
                    "https://cdn.pixabay.com:8443/a.jpg", "ftp://cdn.pixabay.com/a.jpg", "//cdn.pixabay.com/a.jpg",
                    "file:///etc/passwd"]:
            with self.subTest(url=bad):
                self.assertIsNotNone(fmi.validate_image_url(bad))
        self.assertIsNone(fmi.validate_page_url("https://pixabay.com/photos/x-123/", 123))
        self.assertIsNotNone(fmi.validate_page_url("https://pixabay.com/photos/x-123/", 124))
        self.assertIsNotNone(fmi.validate_page_url("https://evil.com/photos/x-123/", 123))
        self.assertIsNotNone(fmi.validate_page_url("http://pixabay.com/photos/x-123/", 123))

    def test_process_image(self):
        out, src, size = fmi.process_image(jpeg_bytes((2000, 1000)))
        self.assertEqual((src, size), ((2000, 1000), (640, 320)))
        img = Image.open(io.BytesIO(out))
        self.assertEqual(img.format, "JPEG")
        self.assertEqual(len(img.getexif()), 0)
        self.assertNotIn("icc_profile", img.info)
        out, _, size = fmi.process_image(jpeg_bytes((500, 400)))        # never upscaled
        self.assertEqual(size, (500, 400))
        out, _, _ = fmi.process_image(png_bytes((900, 700)))            # alpha flattened
        self.assertEqual(Image.open(io.BytesIO(out)).mode, "RGB")
        for bad in [b"not an image", jpeg_bytes()[:200], jpeg_bytes((50, 50)), b""]:
            with self.subTest(size=len(bad)):
                with self.assertRaises(ValueError):
                    fmi.process_image(bad)


# ============================================================= dry-run tests
class DryRunTests(CommandTestCase):
    def run_dry(self, dishes, *extra):
        out = io.StringIO()
        fake = FakeFood(dishes)
        with mock.patch.object(fmi.FoodItem, "objects", fake):
            call_command("fetch_menu_images", *extra, stdout=out, stderr=io.StringIO())
        return out.getvalue()

    def test_dry_run_downloads_nothing_and_writes_nothing(self):
        self.web.by_query["paneer tikka"] = [make_hit(11, "paneer, tikka"), make_hit(12, "chicken, tikka")]
        before = self.tree()
        output = self.run_dry([dish("Paneer Tikka")], "--no-cache")
        self.assertEqual(self.web.image_calls, [])
        self.assertEqual(self.tree(), before)                          # nothing created anywhere under the temp root
        self.assertIn("Nothing was downloaded", output)
        self.assertIn("rejected id=12: non-veg content for a veg dish", output)   # rejection reason is logged
        self.assertNotIn("TEST-KEY-NOT-REAL", output)

    def test_dry_run_never_authorises_a_download(self):
        self.web.by_query["paneer tikka"] = [make_hit(11, "paneer, tikka")]
        report = self.root / "review.json"
        output = self.run_dry([dish("Paneer Tikka")], "--no-cache", "--report", str(report))
        data = json.loads(report.read_text())
        entry_ = data["dishes"][0]
        self.assertEqual(entry_["status"], "MATCH")
        self.assertNotIn("chosen", json.dumps(data))
        self.assertNotIn("approved", json.dumps(data["dishes"]))       # candidates carry no approval flag
        self.assertEqual(self.web.image_calls, [])
        self.assertIn("tags only", output)

    def test_dry_run_cache_only_goes_to_system_temp(self):
        self.web.by_query["paneer tikka"] = [make_hit(11, "paneer, tikka")]
        self.run_dry([dish("Paneer Tikka")])
        self.assertEqual([p for p in self.tree() if not p.startswith("systemp")], [])
        self.assertEqual([p for p in self.tree() if p.startswith("systemp/")], ["systemp/freshplate_pixabay_cache.json"])
        cached = (self.root / "systemp" / "freshplate_pixabay_cache.json").read_text()
        self.assertNotIn("TEST-KEY-NOT-REAL", cached)                  # the key is never written to disk

    def test_dry_run_rejects_download_flags(self):
        with self.assertRaises(CommandError):
            call_command("fetch_menu_images", "--manifest", str(self.manifest_path), stdout=io.StringIO())

    def test_missing_api_key(self):
        with mock.patch.dict(os.environ, {"PIXABAY_API_KEY": ""}):
            with self.assertRaises(CommandError):
                call_command("fetch_menu_images", stdout=io.StringIO())
        self.assertEqual(self.web.calls, [])

    def test_dry_run_database_is_read_only(self):
        # FakeFood raises on anything except select_related/order_by; SimpleTestCase blocks real queries
        self.web.by_query["paneer tikka"] = []
        self.web.by_query["paneer tikka".lower()] = []
        self.run_dry([dish("Paneer Tikka")], "--no-cache")


# ============================================================ manifest tests
class ManifestTests(CommandTestCase):
    def assertFailsSafely(self, text_part=None):
        output = self.download(expect_error=True)
        self.assertEqual(self.web.calls, [], "no network request may happen for a bad manifest")
        self.assertFalse(self.out.exists())
        if text_part:
            self.assertIn(text_part, output)
        return output

    def test_missing_manifest_argument(self):
        with self.assertRaises(CommandError):
            call_command("fetch_menu_images", "--download", "--no-cache", stdout=io.StringIO())
        self.assertEqual(self.web.calls, [])

    def test_manifest_file_not_found(self):
        self.assertFailsSafely("not found")

    def test_manifest_invalid_json(self):
        self.manifest_path.write_text("{ not json", encoding="utf-8")
        self.assertFailsSafely("not valid JSON")

    def test_manifest_wrong_shape(self):
        for payload in ["[]", '"x"', json.dumps({"version": 2, "approvals": []}),
                        json.dumps({"version": 1}), json.dumps({"version": 1, "approvals": {}})]:
            with self.subTest(payload=payload):
                self.manifest_path.write_text(payload, encoding="utf-8")
                self.assertFailsSafely()

    def test_no_approved_entries(self):
        self.write_manifest([entry("Paneer Tikka", 11, approved=False)])
        self.assertFailsSafely("no entries")

    def test_only_literal_true_counts_as_approval(self):
        for value in ["true", "yes", 1, "approved", None]:
            with self.subTest(value=value):
                e = entry("Paneer Tikka", 11)
                e["approved"] = value
                self.write_manifest([e])
                self.assertFailsSafely()

    def test_match_alone_cannot_authorise(self):
        # the dish is a clear MATCH, but nobody approved it -> nothing happens
        hit = self.web.add(make_hit(11, "paneer, tikka"), image_ok())
        self.assertEqual(fmi.evaluate_hit(hit, fmi.SEARCH_PLAN["Paneer Tikka"])["status"], "MATCH")
        self.write_manifest([entry("Paneer Tikka", 11, approved=False)])
        self.assertFailsSafely()
        self.write_manifest([])
        self.assertFailsSafely()

    def test_bad_entries_fail_individually(self):
        self.web.add(make_hit(21, "naan, bread"), image_ok())
        self.write_manifest([
            entry("Butter Naan", 21),                                         # good
            entry("Mystery Dish", 22),                                        # unknown dish
            entry("../../evil", 23),                                          # traversal attempt as dish name
            entry("Gulab Jamun", "24"),                                       # id is a string
            entry("Masala Chai", 25, page_url="https://evil.com/photos/x-25/"),   # wrong host
            entry("Fresh Lime Soda", 26, page_url="https://pixabay.com/photos/x-999/"),   # url of another id
            "just a string",
        ])
        output = self.download(expect_error=True)
        for fragment in ["unknown dish", "positive integer", "must be an https://pixabay.com", "does not belong"]:
            self.assertIn(fragment, output)
        self.assertEqual([c[1].get("id") for c in self.web.api_calls], [21])      # only the valid entry was looked up
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["butter-naan.jpg", "credits.json"])

    def test_duplicate_and_conflicting_entries(self):
        self.web.add(make_hit(31, "naan"), image_ok())
        self.web.add(make_hit(32, "naan"), image_ok())
        # same dish approved twice with different photos, and one photo approved for two dishes
        self.write_manifest([entry("Butter Naan", 31), entry("Butter Naan", 32),
                             entry("Laccha Paratha", 33), entry("Masala Chai", 33)])
        output = self.download(expect_error=True)
        self.assertEqual(output.count("conflicting approvals"), 4)
        self.assertEqual(self.web.api_calls, [])
        self.assertNothingDownloaded()


# ============================================================ download tests
class DownloadTests(CommandTestCase):
    def approve(self, name, pid, tags, image=None, **hit_kwargs):
        hit = self.web.add(make_hit(pid, tags, **hit_kwargs), image if image is not None else image_ok())
        self.write_manifest(getattr(self, "_approvals", []) + [entry(name, pid)])
        self._approvals = getattr(self, "_approvals", []) + [entry(name, pid)]
        return hit

    def test_success_resizes_strips_metadata_and_writes_credits(self):
        self.approve("Paneer Tikka", 11, "paneer, tikka", image_ok(jpeg_bytes((1600, 1200))))
        self.assertIn("SAVED", self.download())
        target = self.out / "paneer-tikka.jpg"
        img = Image.open(target)
        self.assertEqual((img.format, img.size), ("JPEG", (640, 480)))
        self.assertEqual(len(img.getexif()), 0)
        self.assertFalse(list(self.out.glob("*.part")))
        credits = json.loads((self.out / "credits.json").read_text())
        item = credits["images"][0]
        self.assertEqual((item["dish"], item["pixabay_id"], item["file"]), ("Paneer Tikka", 11, "paneer-tikka.jpg"))
        self.assertEqual(item["page_url"], "https://pixabay.com/photos/dish-11/")
        self.assertEqual(item["author"], "chef_anna")
        self.assertEqual(item["license"], fmi.LICENSE_NAME)
        self.assertRegex(item["downloaded_at"], r"^\d{4}-\d{2}-\d{2}T")
        self.assertNotIn("TEST-KEY-NOT-REAL", json.dumps(credits))
        # image URL came from the fresh API response, API was asked by id
        self.assertEqual(self.web.api_calls[0][1]["id"], 11)
        self.assertTrue(self.web.image_calls[0][2]["allow_redirects"] is False)
        self.assertIn("timeout", self.web.image_calls[0][2])

    def test_missing_attribution_is_not_invented(self):
        self.approve("Butter Naan", 12, "naan", user=None)
        self.download()
        item = json.loads((self.out / "credits.json").read_text())["images"][0]
        self.assertIsNone(item["author"])

    def test_png_source_is_converted_to_jpeg(self):
        self.approve("Butter Naan", 13, "naan", image_ok(png_bytes(), "image/png"))
        self.download()
        self.assertEqual(Image.open(self.out / "butter-naan.jpg").format, "JPEG")

    def test_only_approved_ids_are_downloaded(self):
        self.web.add(make_hit(41, "paneer, tikka"), image_ok())
        self.web.add(make_hit(42, "naan"), image_ok())
        self.write_manifest([entry("Paneer Tikka", 41), entry("Butter Naan", 42, approved=False)])
        self.download()
        self.assertEqual([p.name for p in self.out.glob("*.jpg")], ["paneer-tikka.jpg"])
        self.assertEqual([c[0] for c in self.web.image_calls], [self.web.by_id[41]["webformatURL"]])

    def test_unknown_id_and_mismatched_page_url(self):
        self.write_manifest([entry("Paneer Tikka", 51)])                        # API knows no id 51
        self.assertIn("no image with this ID", self.download(expect_error=True))
        self.assertNothingDownloaded()
        self.web.add(make_hit(52, "paneer, tikka", pageURL="https://pixabay.com/photos/other-998877/"), image_ok())
        self.write_manifest([entry("Paneer Tikka", 52)])
        self.assertIn("does not match", self.download(expect_error=True))
        self.assertNothingDownloaded()

    def test_manifest_cannot_override_safety_rules(self):
        self.approve("Dal Makhani", 61, "dal, chicken curry")
        self.assertIn("non-veg content", self.download(expect_error=True))
        self.assertNothingDownloaded()
        self.web.add(make_hit(62, "paneer, ai generated"), image_ok())
        self.write_manifest([entry("Paneer Tikka", 62)])
        self.assertIn("unsuitable content", self.download(expect_error=True))
        self.assertNothingDownloaded()

    def test_manifest_image_url_is_ignored(self):
        self.web.add(make_hit(71, "paneer, tikka"), image_ok())
        e = entry("Paneer Tikka", 71)
        e["image_url"] = "https://evil.example.com/steal.jpg"
        self.write_manifest([e])
        self.download()
        self.assertEqual({c[0] for c in self.web.image_calls}, {self.web.by_id[71]["webformatURL"]})

    def test_invalid_image_urls_from_api_are_refused(self):
        for n, bad in enumerate(["http://cdn.pixabay.com/a.jpg", "https://evil.example.com/a.jpg",
                                 "https://cdn.pixabay.com.evil.com/a.jpg", "https://u:p@cdn.pixabay.com/a.jpg",
                                 "https://cdn.pixabay.com:8443/a.jpg", "file:///etc/passwd", "", None]):
            with self.subTest(url=bad):
                hit = make_hit(80 + n, "paneer, tikka", webformatURL=bad)
                self.web.by_id[hit["id"]] = hit
                self.write_manifest([entry("Paneer Tikka", hit["id"])])
                self.download(expect_error=True)
                self.assertNothingDownloaded()

    def test_bad_downloads_are_rejected(self):
        big = b"\xff\xd8\xff" + b"0" * (fmi.MAX_DOWNLOAD_BYTES + 10)
        cases = {
            "redirect refused": FakeResponse(302, {"Location": "https://evil.example.com/x.jpg"}),
            "HTTP 404": FakeResponse(404),
            "HTTP 500": FakeResponse(500),
            "unexpected content type (text/html)": image_ok(b"<html></html>", "text/html"),
            "unexpected content type (image/gif)": image_ok(b"GIF89a....", "image/gif"),
            "unexpected content type (none)": FakeResponse(200, {}, jpeg_bytes()),
            "larger than the 5 MB": FakeResponse(200, {"Content-Type": "image/jpeg", "Content-Length": str(len(big))}, big),
            "larger than the 5 MB limit ": FakeResponse(200, {"Content-Type": "image/jpeg"}, big),
            "file signature is not": image_ok(b"this is not an image at all", "image/jpeg"),
            "does not match content type": image_ok(png_bytes(), "image/jpeg"),
            "could not be decoded": image_ok(jpeg_bytes()[:300], "image/jpeg"),
            "unexpected image size": image_ok(jpeg_bytes((60, 60)), "image/jpeg"),
        }
        for n, (expected, response) in enumerate(cases.items()):
            with self.subTest(expected=expected):
                self.web.calls.clear()
                self.web.add(make_hit(100 + n, "paneer, tikka"), response)
                self.write_manifest([entry("Paneer Tikka", 100 + n)])
                output = self.download(expect_error=True)
                self.assertIn(expected.strip(), output)
                self.assertFalse((self.out / "paneer-tikka.jpg").exists())
                self.assertFalse(list(self.out.glob("*.part")) if self.out.exists() else False)

    def test_network_errors_are_reported_without_leaking_url(self):
        def boom(url, params=None, **kw):
            raise fmi.requests.ConnectionError(f"failed for {url}?key=TEST-KEY-NOT-REAL")
        with mock.patch.object(fmi.requests, "get", boom):
            self.write_manifest([entry("Paneer Tikka", 11)])
            output = self.download(expect_error=True)
        self.assertNotIn("TEST-KEY-NOT-REAL", output)
        self.assertIn("network error", output)

    def test_existing_file_is_not_overwritten_without_force(self):
        self.out.mkdir(parents=True)
        target = self.out / "paneer-tikka.jpg"
        target.write_bytes(b"ORIGINAL")
        credits_before = None
        self.approve("Paneer Tikka", 11, "paneer, tikka")
        output = self.download()
        self.assertIn("already exists", output)
        self.assertEqual(target.read_bytes(), b"ORIGINAL")
        self.assertEqual(self.web.calls, [])                       # skipped before any network request
        self.assertFalse((self.out / "credits.json").exists())
        self.download("--force")
        self.assertNotEqual(target.read_bytes(), b"ORIGINAL")
        self.assertEqual(Image.open(target).format, "JPEG")
        self.assertIsNone(credits_before)

    def test_failure_is_reported_and_never_substituted(self):
        self.web.add(make_hit(111, "paneer, tikka"), image_ok())
        self.web.add(make_hit(112, "naan"), FakeResponse(404))
        self.web.by_query["naan bread"] = [make_hit(113, "naan, bread")]      # a tempting alternative
        self.web.images[self.web.by_query["naan bread"][0]["webformatURL"]] = image_ok()
        self.write_manifest([entry("Paneer Tikka", 111), entry("Butter Naan", 112)])
        output = self.download(expect_error=True)
        self.assertIn("FAIL", output)
        self.assertIn("no other candidate was used", output)
        self.assertTrue((self.out / "paneer-tikka.jpg").exists())             # the good one is kept
        self.assertFalse((self.out / "butter-naan.jpg").exists())
        self.assertFalse(any(c[1].get("q") for c in self.web.api_calls))      # no search for an alternative
        credits = json.loads((self.out / "credits.json").read_text())
        self.assertEqual([i["dish"] for i in credits["images"]], ["Paneer Tikka"])

    def test_credits_are_merged_between_runs(self):
        self.web.add(make_hit(121, "paneer, tikka"), image_ok())
        self.web.add(make_hit(122, "naan"), image_ok())
        self.write_manifest([entry("Paneer Tikka", 121)])
        self.download()
        self.write_manifest([entry("Butter Naan", 122)])
        self.download()
        dishes = [i["dish"] for i in json.loads((self.out / "credits.json").read_text())["images"]]
        self.assertEqual(dishes, ["Butter Naan", "Paneer Tikka"])

    def test_corrupt_credits_file_is_not_overwritten(self):
        self.out.mkdir(parents=True)
        (self.out / "credits.json").write_text("{ broken", encoding="utf-8")
        self.web.add(make_hit(131, "paneer, tikka"), image_ok())
        self.write_manifest([entry("Paneer Tikka", 131)])
        self.download(expect_error=True)
        self.assertEqual((self.out / "credits.json").read_text(), "{ broken")

    def test_download_never_touches_the_database(self):
        self.web.add(make_hit(141, "paneer, tikka"), image_ok())
        self.write_manifest([entry("Paneer Tikka", 141)])
        sentinel = mock.MagicMock()
        sentinel.objects = mock.PropertyMock(side_effect=AssertionError("database was accessed"))
        with mock.patch.object(fmi, "FoodItem", sentinel):
            self.download()
        self.assertEqual(sentinel.mock_calls, [])
        # SimpleTestCase (databases = set()) would also have raised on any real query
        with self.assertRaises(Exception):
            from food.models import FoodItem
            list(FoodItem.objects.all())

    def test_output_stays_inside_target_folder(self):
        self.web.add(make_hit(151, "paneer, tikka"), image_ok())
        self.write_manifest([entry("Paneer Tikka", 151)])
        self.download()
        created = [p for p in self.tree() if p.startswith("out")]
        self.assertTrue(all(p.startswith("out/menu") or p == "out" for p in created), created)
        self.assertEqual(sorted(p.name for p in self.out.iterdir()), ["credits.json", "paneer-tikka.jpg"])

    def test_default_output_folder_is_frontend_images_menu(self):
        project = self.root / "project"
        (project / "backend").mkdir(parents=True)
        (project / "frontend").mkdir()
        self.web.add(make_hit(161, "paneer, tikka"), image_ok())
        self.write_manifest([entry("Paneer Tikka", 161)])
        with self.settings(BASE_DIR=project / "backend"):
            call_command("fetch_menu_images", "--download", "--manifest", str(self.manifest_path), "--no-cache",
                         stdout=io.StringIO())
        self.assertTrue((project / "frontend" / "images" / "menu" / "paneer-tikka.jpg").exists())

    def test_default_output_folder_requires_frontend(self):
        project = self.root / "project"
        (project / "backend").mkdir(parents=True)
        self.web.add(make_hit(171, "paneer, tikka"), image_ok())
        self.write_manifest([entry("Paneer Tikka", 171)])
        with self.settings(BASE_DIR=project / "backend"):
            with self.assertRaises(CommandError):
                call_command("fetch_menu_images", "--download", "--manifest", str(self.manifest_path), "--no-cache",
                             stdout=io.StringIO())
        self.assertEqual(self.web.image_calls, [])
