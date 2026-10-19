"""Find, review and download Pixabay photos for menu dishes.

Stage 1 - DRY RUN (default, read-only):
    python manage.py fetch_menu_images [--only "Paneer Tikka"] [--report PATH] [--no-cache]

  * reads dishes from the database (read-only, never writes),
  * searches Pixabay and scores results against dish keywords,
  * rejects meat photos for veg dishes and unsuitable photos for every dish
    (AI-generated, illustrations, alcohol, wallpapers) and prints why,
  * never pre-selects anything: approval happens only in a separate manifest,
  * writes nothing to the project. A review JSON is written only when --report
    is given. Pixabay requires 24h caching, so search results are cached in the
    system temp folder unless --no-cache is used.

Stage 2 - DOWNLOAD (only with explicit human approval):
    python manage.py fetch_menu_images --download --manifest PATH [--force]

  * never touches the database,
  * downloads only entries with "approved": true in the manifest,
  * re-checks every approved ID against a fresh Pixabay API lookup (ID, page URL,
    veg and unsuitable-tag rules) and takes the image URL from that response,
  * accepts only https://cdn.pixabay.com / https://pixabay.com image URLs,
    refuses redirects, wrong content types, oversized or undecodable files,
  * resizes to max 640px wide, saves a metadata-free JPEG to
    frontend/images/menu/<dish-slug>.jpg and records attribution in credits.json,
  * never overwrites an existing image unless --force is given,
  * never substitutes another candidate when one fails.

Pixabay rules this follows (https://pixabay.com/api/docs/):
  * API responses are cached for 24 hours,
  * returned image URLs are temporary - images are downloaded to our own
    hosting instead of hotlinking,
  * the API key comes from the PIXABAY_API_KEY environment variable and is
    never printed, logged or written to any file.
"""
import io
import json
import os
import re
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import requests
from django.conf import settings
from django.core.management.base import BaseCommand, CommandError

from food.models import FoodItem

PIXABAY_URL = "https://pixabay.com/api/"
LICENSE_NAME = "Pixabay Content License"
LICENSE_URL = "https://pixabay.com/service/license-summary/"
CACHE_TTL_SECONDS = 24 * 60 * 60   # Pixabay asks for 24h caching
REQUEST_GAP_SECONDS = 0.7          # stay far below 100 requests / 60 seconds
MANIFEST_VERSION = 1

ALLOWED_IMAGE_HOSTS = {"cdn.pixabay.com", "pixabay.com"}
ALLOWED_CONTENT_TYPES = {"image/jpeg", "image/png", "image/webp"}
MAX_DOWNLOAD_BYTES = 5 * 1024 * 1024   # webformat images are far smaller
MIN_SIDE_PX = 200
MAX_SIDE_PX = 8000
TARGET_MAX_WIDTH = 640
JPEG_QUALITY = 82

# Every dish the command knows about.
#   strong  = terms that point at this exact dish            -> MATCH
#   weak    = same family of food, needs a human look         -> WEAK
#   reject  = extra terms that make a photo wrong for THIS dish (e.g. a kebab
#             photo for a curry); applied to veg and non-veg dishes alike
#   allow   = veg-rule exceptions for this dish (paneer tikka is often tagged
#             "kebab"); never overrides the unsuitable-content rules
SEARCH_PLAN = {
    "Paneer Tikka": dict(veg=True, query="paneer tikka", strong=["paneer", "tikka"],
                         weak=["tandoori", "grilled", "skewer"], allow=["kebab"]),
    "Chilli Garlic Mushroom": dict(veg=True, query="chilli garlic mushroom", strong=["mushroom", "mushrooms"],
                                   weak=["stir fry", "garlic"]),
    "Chicken Seekh Kebab": dict(veg=False, query="seekh kebab", strong=["seekh", "seekh kebab", "kebab", "kabab"],
                                weak=["skewer", "grilled meat"], reject=["shashlik", "pork", "bacon", "ham"]),
    "Honey Chilli Potato": dict(veg=True, query="chilli potato", strong=["chilli potato", "potato", "potatoes"],
                                weak=["fries", "french fries"], reject=["soup"]),
    "Dal Makhani": dict(veg=True, query="dal makhani", strong=["dal makhani", "dal", "daal", "dhal", "lentils"],
                        weak=["curry"], reject=["leaf", "plant", "flora"]),
    "Paneer Butter Masala": dict(veg=True, query="paneer butter masala", strong=["paneer"],
                                 weak=["masala", "curry", "gravy"], reject=["spices", "herbs"]),
    "Chicken Biryani": dict(veg=False, query="chicken biryani", strong=["biryani", "biriyani"],
                            weak=["rice", "pilaf"], reject=["mutton", "beef", "pork", "prawn", "shrimp", "fish"]),
    "Mutton Rogan Josh": dict(veg=False, query="rogan josh", strong=["rogan josh", "mutton curry", "mutton", "lamb curry"],
                              weak=["curry", "lamb"], reject=["kebab", "shashlik", "skewered meat", "bbq", "barbecue"]),
    "Veg Thali": dict(veg=True, query="indian thali", strong=["thali", "indian thali"], weak=["indian food", "platter"]),
    "Chole Bhature": dict(veg=True, query="chole bhature", strong=["chole", "bhature", "chana", "chickpeas", "chickpea curry"],
                          weak=["curry", "puri", "poori"]),
    "Butter Naan": dict(veg=True, query="naan bread", strong=["naan", "nan bread"], weak=["flatbread", "bread"]),
    "Laccha Paratha": dict(veg=True, query="paratha", strong=["paratha", "parotta"], weak=["flatbread", "roti", "chapati"]),
    "Jeera Rice": dict(veg=True, query="jeera rice", strong=["jeera", "jeera rice", "cumin rice"],
                       weak=["rice", "basmati"], reject=["harvest", "paddy field", "ricefield", "farm", "farmer", "plantation"]),
    "Gulab Jamun": dict(veg=True, query="gulab jamun", strong=["gulab jamun", "jamun"], weak=["indian sweet", "dessert", "syrup"]),
    "Gajar Ka Halwa": dict(veg=True, query="carrot halwa", strong=["halwa", "gajar", "carrot halwa"],
                           weak=["carrot", "dessert"], reject=["carrot cake", "cake"]),
    "Masala Chai": dict(veg=True, query="masala chai", strong=["chai", "masala tea"], weak=["tea", "milk tea"],
                        reject=["coffee", "cappuccino"]),
    "Sweet Lassi": dict(veg=True, query="lassi", strong=["lassi", "sweet lassi"], weak=["yogurt", "yoghurt", "buttermilk"],
                        reject=["mango lassi", "chocolate", "green apple", "apple", "mango"]),
    "Fresh Lime Soda": dict(veg=True, query="lime soda", strong=["lime soda", "lemonade", "lime juice"],
                            weak=["lime", "lemon", "soda"]),
    "Veg Pulao": dict(veg=True, query="veg pulao", strong=["pulao", "pulav", "pilaf", "pilau"], weak=["rice", "fried rice"]),
    "Assorted Snack Box": dict(veg=True, query="indian snacks", strong=["samosa", "samosas", "pakora", "pakoda", "bhaji"],
                               weak=["snack", "snacks"], reject=["vendor", "people", "market"]),
}

# A veg dish must never be shown with a meat, fish or egg photo
NON_VEG_TERMS = [
    "chicken", "chicken curry", "meat", "flesh", "mutton", "lamb", "beef", "pork", "goulash",
    "kebab", "kabab", "shashlik", "shish kebab", "korma", "keema", "fish", "prawn", "prawns",
    "shrimp", "seafood", "crab", "bacon", "ham", "sausage", "salami", "steak", "turkey", "duck",
    "egg", "eggs",
]

# Never suitable for a restaurant menu photo, for any dish
UNSUITABLE_TERMS = [
    "ai generated", "ai art", "generative ai", "digital art", "illustration", "drawing", "cartoon",
    "render", "3d", "alcohol", "alcoholic", "gin", "vodka", "rum", "whisky", "whiskey", "beer",
    "wine", "cocktail", "liquor", "wallpaper",
]

RESCUE_SUFFIX = "(rescue deal)"


# --------------------------------------------------------------- matching
def base_dish_name(name):
    """'Veg Pulao (rescue deal)' -> 'Veg Pulao'."""
    return re.sub(re.escape(RESCUE_SUFFIX), "", name or "", flags=re.IGNORECASE).strip()


def normalize_text(text):
    """Lowercase, turn punctuation/hyphens into spaces and collapse whitespace."""
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (text or "").lower()).split())


def parse_tags(tags_text):
    tags = [normalize_text(t) for t in (tags_text or "").split(",")]
    return sorted({t for t in tags if t})


def term_hits(terms, tags):
    """Whole-word / whole-phrase matching: 'ham' matches 'smoked ham' but not
    'hamus'; 'egg' does not match 'eggplant'; 'dal' does not match 'sandal'."""
    hits = []
    for term in terms:
        norm = normalize_text(term)
        if not norm:
            continue
        pattern = re.compile(r"(?<![a-z0-9])" + re.escape(norm) + r"(?![a-z0-9])")
        if any(pattern.search(tag) for tag in tags):
            hits.append(term)
    return hits


def evaluate_hit(hit, plan):
    """Return a dict: status MATCH / WEAK / NONE / REJECTED, score, matched terms
    and - for rejections - the reason. Tag checks are safeguards only; they do
    not prove that a photo really shows the dish."""
    tags = parse_tags(hit.get("tags"))
    if hit.get("type") not in (None, "photo"):
        return {"status": "REJECTED", "reason": f"not a photo (type={hit.get('type')})", "terms": [], "score": 0}

    unsuitable = term_hits(UNSUITABLE_TERMS, tags)
    if unsuitable:
        return {"status": "REJECTED", "reason": "unsuitable content", "terms": unsuitable, "score": 0}

    if plan.get("veg"):
        allowed = {normalize_text(a) for a in plan.get("allow", [])}
        non_veg = [t for t in term_hits(NON_VEG_TERMS, tags) if normalize_text(t) not in allowed]
        if non_veg:
            return {"status": "REJECTED", "reason": "non-veg content for a veg dish", "terms": non_veg, "score": 0}

    wrong = term_hits(plan.get("reject", []), tags)
    if wrong:
        return {"status": "REJECTED", "reason": "does not fit this dish", "terms": wrong, "score": 0}

    strong = term_hits(plan["strong"], tags)
    weak = term_hits(plan.get("weak", []), tags)
    if strong:
        return {"status": "MATCH", "score": 3 * len(strong) + len(weak), "terms": strong + weak, "reason": None}
    if weak:
        return {"status": "WEAK", "score": len(weak), "terms": weak, "reason": None}
    return {"status": "NONE", "score": 0, "terms": [], "reason": "no dish keyword in tags"}


def rank_hits(hits, plan, limit=3):
    """Split results into ranked usable candidates and rejected ones (with reasons)."""
    order = {"MATCH": 0, "WEAK": 1}
    usable, rejected = [], []
    for hit in hits:
        result = evaluate_hit(hit, plan)
        if result["status"] in order:
            usable.append((order[result["status"]], -result["score"], -(hit.get("likes") or 0), result, hit))
        elif result["status"] == "REJECTED":
            rejected.append({"pixabay_id": hit.get("id"), "reason": result["reason"], "terms": result["terms"]})
    usable.sort(key=lambda r: r[:3])
    return [{"result": r, "hit": h} for _, _, _, r, h in usable[:limit]], rejected


def dish_slug(name):
    slug = re.sub(r"[^a-z0-9]+", "-", base_dish_name(name).lower()).strip("-")
    if not re.fullmatch(r"[a-z0-9]+(-[a-z0-9]+)*", slug) or len(slug) > 60:
        raise ValueError(f"cannot build a safe filename from {name!r}")
    return slug


# ------------------------------------------------------- download checks
def validate_image_url(url):
    """Only plain https URLs on Pixabay's own hosts are accepted."""
    if not isinstance(url, str) or not url:
        return "missing image URL"
    parsed = urlparse(url)
    if parsed.scheme != "https":
        return "image URL is not https"
    if parsed.username or parsed.password:
        return "image URL contains credentials"
    if parsed.port not in (None, 443):
        return "image URL uses a non-standard port"
    if (parsed.hostname or "").lower() not in ALLOWED_IMAGE_HOSTS:
        return f"image host not allowed ({parsed.hostname})"
    return None


def validate_page_url(url, pixabay_id):
    if not isinstance(url, str):
        return "page_url missing"
    parsed = urlparse(url)
    if parsed.scheme != "https" or (parsed.hostname or "").lower() != "pixabay.com":
        return "page_url must be an https://pixabay.com/... link"
    if not parsed.path.rstrip("/").endswith(f"-{pixabay_id}"):
        return "page_url does not belong to this pixabay_id"
    return None


def sniff_image_type(data):
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if len(data) >= 12 and data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def process_image(data):
    """Decode, sanity-check, resize and re-encode as a metadata-free JPEG.
    Returns (jpeg_bytes, (src_w, src_h), (out_w, out_h))."""
    from PIL import Image, UnidentifiedImageError

    try:
        with Image.open(io.BytesIO(data)) as probe:
            probe.verify()                       # structural check
        img = Image.open(io.BytesIO(data))
        img.load()                               # full decode
    except (UnidentifiedImageError, OSError, SyntaxError, ValueError, Image.DecompressionBombError) as exc:
        raise ValueError(f"image could not be decoded ({type(exc).__name__})") from None

    if img.format not in ("JPEG", "PNG", "WEBP"):
        raise ValueError(f"unexpected image format {img.format}")
    width, height = img.size
    if min(width, height) < MIN_SIDE_PX or max(width, height) > MAX_SIDE_PX:
        raise ValueError(f"unexpected image size {width}x{height}")

    if img.mode in ("RGBA", "LA", "P"):
        img = img.convert("RGBA")
        background = Image.new("RGB", img.size, (255, 255, 255))
        background.paste(img, mask=img.split()[-1])
        img = background
    elif img.mode != "RGB":
        img = img.convert("RGB")

    if width > TARGET_MAX_WIDTH:
        new_height = max(1, round(height * TARGET_MAX_WIDTH / width))
        img = img.resize((TARGET_MAX_WIDTH, new_height), Image.LANCZOS)

    clean = Image.new("RGB", img.size)           # fresh image = no EXIF/XMP/ICC/comments
    clean.paste(img)
    out = io.BytesIO()
    clean.save(out, format="JPEG", quality=JPEG_QUALITY, optimize=True, progressive=True)
    return out.getvalue(), (width, height), clean.size


class Command(BaseCommand):
    help = ("Find Pixabay photos for menu dishes (dry run, default) or download "
            "explicitly approved ones (--download --manifest PATH).")

    def add_arguments(self, parser):
        parser.add_argument("--only", action="append", default=[], help="Dry run: only this dish (repeatable).")
        parser.add_argument("--report", help="Dry run: also write the review JSON to this path.")
        parser.add_argument("--no-cache", action="store_true",
                            help="Do not read or write the 24h Pixabay cache.")
        parser.add_argument("--download", action="store_true",
                            help="Download the images approved in --manifest (no database access).")
        parser.add_argument("--manifest", help="Download: path to the human approval manifest JSON.")
        parser.add_argument("--output-dir", help="Download: target folder (default: <repo>/frontend/images/menu).")
        parser.add_argument("--force", action="store_true",
                            help="Dry run: include dishes that already have an image. "
                                 "Download: allow replacing existing image files.")

    # ------------------------------------------------------------- setup
    def handle(self, *args, **options):
        api_key = os.getenv("PIXABAY_API_KEY", "").strip()
        if not api_key:
            raise CommandError("PIXABAY_API_KEY is not set (add it to backend/.env). Nothing was done.")
        self.api_key = api_key
        self.requests_used = 0
        self.use_cache = not options["no_cache"]
        self.cache_path = os.path.join(tempfile.gettempdir(), "freshplate_pixabay_cache.json")
        self.cache = self._load_cache() if self.use_cache else {}

        if options["download"]:
            return self._run_download(options)
        if options["manifest"] or options["output_dir"]:
            raise CommandError("--manifest/--output-dir only work together with --download. Nothing was done.")
        return self._run_dry_run(options)

    # ============================================================ DRY RUN
    def _run_dry_run(self, options):
        dishes = list(FoodItem.objects.select_related("category").order_by("category__name", "name"))
        if options["only"]:
            wanted = {normalize_text(n) for n in options["only"]}
            dishes = [d for d in dishes if normalize_text(d.name) in wanted or normalize_text(base_dish_name(d.name)) in wanted]
        if not dishes:
            raise CommandError("No matching dishes found in the database. Nothing was done.")

        skipped = [d for d in dishes if (d.image_url or d.image) and not options["force"]]
        todo = [d for d in dishes if d not in skipped]

        self.stdout.write(f"Pixabay DRY RUN | {len(dishes)} dishes | {len(skipped)} skipped (already have an image)"
                          f" | cache: {'24h' if self.use_cache else 'off'}")
        self.stdout.write("-" * 96)

        results, summary = [], {"MATCH": 0, "WEAK": 0, "NONE": 0, "NO_PLAN": 0, "ERROR": 0}
        for dish in todo:
            entry = self._review_dish(dish)
            results.append(entry)
            summary[entry["status"]] += 1
            self._print_entry(entry)
        for dish in skipped:
            self.stdout.write(f"[SKIP ] {dish.name} - already has an image (use --force to include)")

        if self.use_cache:
            self._save_cache()
        self.stdout.write("-" * 96)
        self.stdout.write(f"Summary: {summary['MATCH']} MATCH | {summary['WEAK']} WEAK | {summary['NONE']} NO MATCH"
                          f" | {summary['NO_PLAN']} no search plan | {summary['ERROR']} errors"
                          f" | Pixabay requests used: {self.requests_used}")

        if options["report"]:
            report = {
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "source": "Pixabay API (https://pixabay.com/api/docs/)",
                "note": ("DRY RUN. Nothing here is approved. Image URLs are temporary (~24h). To download, list "
                         "the images you approve with \"approved\": true in a separate manifest and run --download."),
                "summary": summary,
                "dishes": results,
            }
            with open(options["report"], "w", encoding="utf-8") as fh:
                json.dump(report, fh, indent=2, ensure_ascii=False)
            self.stdout.write(f"Review file: {os.path.abspath(options['report'])}")

        self.stdout.write("MATCH/WEAK labels come from tags only - open each page link and check the photo yourself.")
        self.stdout.write(self.style.SUCCESS("Nothing was downloaded. Nothing was written to the database."))

    def _review_dish(self, dish):
        name = base_dish_name(dish.name)
        entry = {"dish_id": dish.id, "name": dish.name, "category": dish.category.name, "is_veg": dish.is_veg,
                 "queries": [], "status": "NONE", "candidates": [], "rejected": []}
        plan = SEARCH_PLAN.get(name)
        if not plan:
            entry["status"] = "NO_PLAN"
            return entry
        if plan["veg"] != dish.is_veg:
            entry["warning"] = "veg flag in the database differs from the search plan - the stricter (veg) rules apply"
            plan = dict(plan, veg=True)

        queries = [plan["query"]]
        if normalize_text(name) != normalize_text(plan["query"]):
            queries.append(name.lower())          # broader fallback, only if the first finds nothing usable
        for q in queries:
            entry["queries"].append(q)
            hits, error = self._search({"q": q, "image_type": "photo", "category": "food", "safesearch": "true",
                                        "per_page": 10, "order": "popular", "lang": "en"})
            if error:
                entry["status"], entry["error"] = "ERROR", error
                return entry
            usable, rejected = rank_hits(hits, plan)
            entry["rejected"].extend(rejected)
            if usable:
                entry["candidates"] = [self._candidate_view(i + 1, u) for i, u in enumerate(usable)]
                entry["status"] = usable[0]["result"]["status"]
                break
        return entry

    @staticmethod
    def _candidate_view(rank, usable):
        hit, result = usable["hit"], usable["result"]
        return {
            "rank": rank, "status": result["status"], "score": result["score"], "matched_terms": result["terms"],
            "pixabay_id": hit.get("id"), "page_url": hit.get("pageURL"), "tags": hit.get("tags"),
            "author": hit.get("user"), "author_id": hit.get("user_id"),
            "size": f"{hit.get('imageWidth')}x{hit.get('imageHeight')}",
            "preview_url_temporary": hit.get("previewURL"),
        }

    def _print_entry(self, entry):
        label = {"MATCH": "MATCH", "WEAK": "WEAK ", "NONE": "NONE ", "NO_PLAN": "NOPLN", "ERROR": "ERROR"}[entry["status"]]
        queries = " -> ".join(f'"{q}"' for q in entry["queries"]) or "-"
        self.stdout.write(f"[{label}] {entry['name']}  ({entry['category']}, {'veg' if entry['is_veg'] else 'non-veg'})"
                          f"  q={queries}")
        if entry.get("warning"):
            self.stdout.write(f"        warning: {entry['warning']}")
        if entry["status"] == "ERROR":
            self.stdout.write(f"        error: {entry.get('error')}")
        elif entry["status"] == "NO_PLAN":
            self.stdout.write("        no search plan for this dish")
        elif not entry["candidates"]:
            self.stdout.write("        no usable photo found -> keep the emoji")
        for c in entry["candidates"]:
            self.stdout.write(f"   {c['rank']}) {c['status']:<5} id={c['pixabay_id']}  by {c['author']}  {c['size']}"
                              f"  matched: {', '.join(c['matched_terms'])}")
            self.stdout.write(f"      page: {c['page_url']}")
        for r in entry["rejected"]:
            self.stdout.write(f"      rejected id={r['pixabay_id']}: {r['reason']} ({', '.join(r['terms'])})")

    # =========================================================== DOWNLOAD
    def _run_download(self, options):
        """Never touches the database."""
        approvals = self._load_manifest(options["manifest"])
        out_dir = self._output_dir(options["output_dir"])

        downloaded, skipped, failed = [], [], []
        for item in approvals:
            if item.get("error"):
                failed.append((item["dish"], item.get("pixabay_id"), item["error"]))
                continue
            status, detail = self._download_one(item, out_dir, options["force"])
            {"downloaded": downloaded, "skipped": skipped, "failed": failed}[status].append(
                (item["dish"], item["pixabay_id"], detail))

        if downloaded:
            self._write_credits(out_dir, [d[2] for d in downloaded])
        if self.use_cache:
            self._save_cache()

        self.stdout.write("-" * 96)
        for dish, pid, info in downloaded:
            self.stdout.write(self.style.SUCCESS(f"[SAVED] {dish} <- {pid}: {info['file']} "
                                                 f"({info['width']}x{info['height']}, {info['bytes'] // 1024} KB)"))
        for dish, pid, reason in skipped:
            self.stdout.write(f"[SKIP ] {dish} <- {pid}: {reason}")
        for dish, pid, reason in failed:
            self.stdout.write(self.style.ERROR(f"[FAIL ] {dish} <- {pid}: {reason}"))
        self.stdout.write(f"Downloaded {len(downloaded)} | skipped {len(skipped)} | failed {len(failed)}"
                          f" | Pixabay API requests: {self.requests_used} | folder: {out_dir}")
        self.stdout.write("The database was not touched. image_url values are unchanged.")
        if failed:
            raise CommandError(f"{len(failed)} approved image(s) failed - no other candidate was used instead.")

    def _load_manifest(self, path):
        if not path:
            raise CommandError("--download needs --manifest PATH (the human approval file). Nothing was downloaded.")
        try:
            with open(path, encoding="utf-8") as fh:
                data = json.load(fh)
        except FileNotFoundError:
            raise CommandError(f"Manifest not found: {path}. Nothing was downloaded.") from None
        except (OSError, ValueError) as exc:
            raise CommandError(f"Manifest is not valid JSON ({type(exc).__name__}). Nothing was downloaded.") from None
        if not isinstance(data, dict) or data.get("version") != MANIFEST_VERSION:
            raise CommandError(f"Manifest must be an object with \"version\": {MANIFEST_VERSION}. Nothing was downloaded.")
        entries = data.get("approvals")
        if not isinstance(entries, list):
            raise CommandError("Manifest needs an \"approvals\" list. Nothing was downloaded.")

        items, not_approved = [], 0
        for raw in entries:
            if not isinstance(raw, dict):
                items.append({"dish": "?", "error": "entry is not an object"})
                continue
            dish = raw.get("dish")
            pid = raw.get("pixabay_id")
            item = {"dish": dish if isinstance(dish, str) else "?", "pixabay_id": pid, "page_url": raw.get("page_url")}
            if raw.get("approved") is not True:
                not_approved += 1                     # only an explicit true counts as approval
                continue
            base = base_dish_name(dish) if isinstance(dish, str) else ""
            if base not in SEARCH_PLAN:
                item["error"] = "unknown dish name (must match the menu exactly)"
            elif not isinstance(pid, int) or isinstance(pid, bool) or pid <= 0:
                item["error"] = "pixabay_id must be a positive integer"
            else:
                problem = validate_page_url(raw.get("page_url"), pid)
                if problem:
                    item["error"] = problem
            item["base"] = base
            items.append(item)

        # Conflicts: one dish approved twice, or one photo approved for two dishes
        by_dish, by_id = {}, {}
        for item in items:
            if not item.get("error"):
                by_dish.setdefault(item["base"], []).append(item)
                by_id.setdefault(item["pixabay_id"], []).append(item)
        for group in list(by_dish.values()) + list(by_id.values()):
            if len(group) > 1:
                for item in group:
                    item["error"] = "conflicting approvals (same dish or same photo approved more than once)"

        self.stdout.write(f"Manifest: {len(entries)} entries | {len(items)} approved | {not_approved} not approved (ignored)")
        if not items:
            raise CommandError("The manifest has no entries with \"approved\": true. Nothing was downloaded.")
        return items

    def _output_dir(self, override):
        if override:
            out_dir = Path(override).resolve()
        else:
            frontend = (Path(settings.BASE_DIR).parent / "frontend").resolve()
            if not frontend.is_dir():
                raise CommandError(f"Frontend folder not found at {frontend}. Nothing was downloaded.")
            out_dir = frontend / "images" / "menu"
        return out_dir                                  # created only when a file is actually saved

    def _download_one(self, item, out_dir, force):
        """Returns ('downloaded'|'skipped'|'failed', info-or-reason)."""
        pid, plan = item["pixabay_id"], SEARCH_PLAN[item["base"]]
        try:
            target = (out_dir / f"{dish_slug(item['base'])}.jpg").resolve()
        except ValueError as exc:
            return "failed", str(exc)
        if target.parent != out_dir:
            return "failed", "unsafe target path"
        if target.exists() and not force:
            return "skipped", f"{target.name} already exists (use --force to replace)"

        # 1) Fresh API lookup: the approved ID must still exist and match the manifest
        hits, error = self._search({"id": pid})
        if error:
            return "failed", error
        hit = next((h for h in hits if h.get("id") == pid), None)
        if hit is None:
            return "failed", "Pixabay has no image with this ID"
        if (hit.get("pageURL") or "").rstrip("/") != (item["page_url"] or "").rstrip("/"):
            return "failed", "page_url in the manifest does not match Pixabay's record for this ID"

        # 2) The same safety rules as the dry run (approval cannot override them)
        verdict = evaluate_hit(hit, plan)
        if verdict["status"] == "REJECTED":
            return "failed", f"{verdict['reason']} ({', '.join(verdict['terms'])})"

        # 3) Download from the URL Pixabay just returned (never from the manifest)
        image_url = hit.get("webformatURL")
        problem = validate_image_url(image_url)
        if problem:
            return "failed", problem
        data, problem = self._fetch_image_bytes(image_url)
        if problem:
            return "failed", problem

        try:
            jpeg, src_size, out_size = process_image(data)
        except ValueError as exc:
            return "failed", str(exc)

        out_dir.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".part")
        with open(tmp, "wb") as fh:
            fh.write(jpeg)
        os.replace(tmp, target)
        return "downloaded", {
            "dish": item["dish"], "file": target.name, "pixabay_id": pid, "page_url": hit.get("pageURL"),
            "author": hit.get("user") or None, "author_id": hit.get("user_id") or None,
            "license": LICENSE_NAME, "license_url": LICENSE_URL,
            "downloaded_at": datetime.now(timezone.utc).isoformat(),
            "source_size": f"{src_size[0]}x{src_size[1]}", "width": out_size[0], "height": out_size[1],
            "bytes": len(jpeg),
        }

    def _fetch_image_bytes(self, url):
        try:
            response = requests.get(url, timeout=(5, 20), stream=True, allow_redirects=False,
                                    headers={"User-Agent": "FreshPlate-menu-images/1.0"})
        except requests.RequestException as exc:
            return None, f"image download failed ({type(exc).__name__})"
        try:
            if 300 <= response.status_code < 400:
                return None, f"redirect refused (HTTP {response.status_code})"
            if response.status_code != 200:
                return None, f"image download returned HTTP {response.status_code}"
            ctype = (response.headers.get("Content-Type") or "").split(";")[0].strip().lower()
            if ctype not in ALLOWED_CONTENT_TYPES:
                return None, f"unexpected content type ({ctype or 'none'})"
            declared = response.headers.get("Content-Length")
            if declared and declared.isdigit() and int(declared) > MAX_DOWNLOAD_BYTES:
                return None, "image is larger than the 5 MB limit"
            buf = bytearray()
            for chunk in response.iter_content(64 * 1024):
                buf.extend(chunk)
                if len(buf) > MAX_DOWNLOAD_BYTES:
                    return None, "image is larger than the 5 MB limit"
        finally:
            response.close()
        data = bytes(buf)
        sniffed = sniff_image_type(data)
        if sniffed is None:
            return None, "file signature is not JPEG/PNG/WebP"
        if sniffed != ctype:
            return None, f"file signature ({sniffed}) does not match content type ({ctype})"
        return data, None

    def _write_credits(self, out_dir, new_entries):
        path = out_dir / "credits.json"
        existing = []
        if path.exists():
            try:
                with open(path, encoding="utf-8") as fh:
                    existing = json.load(fh).get("images", [])
            except (OSError, ValueError, AttributeError):
                raise CommandError("credits.json exists but is not valid - fix or remove it, then rerun.") from None
        by_dish = {e.get("dish"): e for e in existing if isinstance(e, dict)}
        for entry in new_entries:
            clean = {k: v for k, v in entry.items() if k != "bytes"}
            by_dish[entry["dish"]] = clean
        payload = {
            "source": "Pixabay (https://pixabay.com)",
            "license": LICENSE_NAME,
            "license_url": LICENSE_URL,
            "note": "Photos downloaded via the Pixabay API, resized for the menu. Author = Pixabay username.",
            "images": sorted(by_dish.values(), key=lambda e: e.get("dish") or ""),
        }
        tmp = path.with_name("credits.json.part")
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2, ensure_ascii=False)
        os.replace(tmp, path)

    # ============================================================ Pixabay
    def _search(self, extra_params):
        """GET the Pixabay API. Returns (hits, error). The key is never printed or cached."""
        cache_key = json.dumps(extra_params, sort_keys=True)
        if self.use_cache:
            cached = self.cache.get(cache_key)
            if cached and time.time() - cached.get("fetched_at", 0) < CACHE_TTL_SECONDS:
                return cached["hits"], None

        params = dict(extra_params, key=self.api_key)
        try:
            time.sleep(REQUEST_GAP_SECONDS)
            response = requests.get(PIXABAY_URL, params=params, timeout=15)
            self.requests_used += 1
        except requests.RequestException as exc:
            # The exception text can contain the full URL (with the key), so only the type is shown
            return [], f"network error ({type(exc).__name__})"

        if response.status_code == 429:
            return [], "rate limit reached (HTTP 429) - wait a minute and run again"
        if response.status_code != 200:
            return [], f"HTTP {response.status_code} from Pixabay"
        remaining = response.headers.get("X-RateLimit-Remaining")
        if remaining is not None and str(remaining).isdigit() and int(remaining) < 5:
            reset = str(response.headers.get("X-RateLimit-Reset", "60"))
            time.sleep(min(int(reset) if reset.isdigit() else 60, 60))
        try:
            hits = response.json().get("hits", [])
        except (ValueError, AttributeError):
            return [], "Pixabay returned invalid JSON"
        if self.use_cache:
            self.cache[cache_key] = {"fetched_at": time.time(), "hits": hits}
        return hits, None

    def _load_cache(self):
        try:
            with open(self.cache_path, encoding="utf-8") as fh:
                data = json.load(fh)
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save_cache(self):
        try:
            with open(self.cache_path, "w", encoding="utf-8") as fh:
                json.dump(self.cache, fh)
        except OSError:
            self.stderr.write("Could not write the Pixabay cache file (results are still shown).")
