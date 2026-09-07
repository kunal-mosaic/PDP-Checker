"""
Visual scraper — Zeus-first, Playwright fallback.

Strategy:
  1. Zeus mode  (preferred): if a Zeus cache file exists for this URL's page_id,
     download CDN images directly and skip Playwright entirely. This gives Claude
     Vision full-resolution, properly labelled images in display order.
  2. Playwright fallback: when no Zeus cache exists, Playwright loads the page,
     detects carousel/banner structure, and screenshots each section.
"""

import re
from pathlib import Path
from typing import List, Optional, Tuple
from playwright.sync_api import sync_playwright, Page
from scraper.models import PDPTextData, CarouselSlide, Banner, ZeusImage
from scraper.zeus_connector import get_zeus_images, download_zeus_images, get_zeus_reviews
from utils.config_loader import load_config
from utils.logger import get_logger

log = get_logger("visual_scraper")
config = load_config()
SCREENSHOTS_DIR = Path(config["report"]["screenshots_dir"])
W = config["scraper"]["screenshot_width"]
H = config["scraper"]["screenshot_height"]


# ── Carousel detection strategies ─────────────────────────────────────────────
# Each entry: (carousel_container_selector, slide_selector_within_container)
# Tried in order — first one that returns slides wins.

CAROUSEL_STRATEGIES: List[Tuple[str, str]] = [
    # Swiper.js (very common on modern sites)
    (".swiper-wrapper",         ".swiper-slide"),
    (".swiper",                 ".swiper-slide"),
    # Slick
    (".slick-list",             ".slick-slide:not(.slick-cloned)"),
    (".slick-track",            ".slick-slide:not(.slick-cloned)"),
    # Generic slider / carousel
    (".slider",                 "div, li"),
    ("[class*='carousel__track']", "[class*='carousel__slide']"),
    ("[class*='carousel-inner']",  "[class*='carousel-item']"),
    ("[class*='carousel']",    "[class*='slide']"),
    # Gallery / product images
    ("[class*='gallery-track']",  "[class*='gallery-item']"),
    ("[class*='product-gallery']","[class*='gallery-item'], [class*='slide']"),
    # React/custom
    ("[class*='Slider']",       "[class*='Slide']"),
    ("[class*='ImageGallery']", "[class*='image-gallery-slide']"),
    # Broad fallback
    ("[class*='slider']",       "div, li"),
]

# Banner selectors — tried in order
BANNER_SELECTORS = [
    "[class*='hero-banner']",
    "[class*='heroBanner']",
    "[class*='hero']:not(section)",
    "[class*='banner']:not(nav)",
    "[class*='Banner']:not(nav)",
    "[class*='promo-strip']",
    "[class*='announcement-bar']",
    "[class*='top-bar']",
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def _slug(url: str) -> str:
    return re.sub(r"[^\w]", "_", url)[:60]


def _save_screenshot(page: Page, element, filename: str) -> Optional[str]:
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    path = SCREENSHOTS_DIR / filename
    try:
        element.screenshot(path=str(path))
        return str(path)
    except Exception:
        try:
            page.screenshot(path=str(path), full_page=False)
            return str(path)
        except Exception as e:
            log.debug(f"Screenshot failed for {filename}: {e}")
            return None


def _get_slide_copy(slide) -> str:
    """
    Extract clean copy from a carousel slide.
    Targets headings and text elements — skips prices, buttons, numeric-only text.
    """
    copy_parts = []
    for sel in ["h1", "h2", "h3", "h4", "p",
                "[class*='title']", "[class*='heading']",
                "[class*='copy']", "[class*='desc']", "[class*='text']"]:
        try:
            for el in slide.query_selector_all(sel):
                t = el.inner_text().strip()
                if (t and len(t) > 4 and
                        not t.replace(".", "").replace("%", "").replace(
                            ",", "").replace("₹", "").replace(" ", "").isdigit()):
                    copy_parts.append(t)
        except Exception:
            pass

    if copy_parts:
        seen, deduped = set(), []
        for p in copy_parts:
            if p not in seen:
                seen.add(p)
                deduped.append(p)
        return " | ".join(deduped)[:400]

    # Fallback
    try:
        return slide.inner_text().strip()[:300]
    except Exception:
        return ""


def _is_visible_and_sized(el) -> bool:
    """Return True if element is visible and has meaningful dimensions."""
    try:
        box = el.bounding_box()
        return box is not None and box["width"] > 50 and box["height"] > 50
    except Exception:
        return False


# ── Carousel scraping ──────────────────────────────────────────────────────────

def _try_carousel_strategies(page: Page) -> Tuple[Optional[object], str, str]:
    """
    Try each carousel strategy in order.
    Returns (carousel_element, slide_selector, strategy_name) or (None, '', '').
    """
    for carousel_sel, slide_sel in CAROUSEL_STRATEGIES:
        try:
            carousels = page.query_selector_all(carousel_sel)
            for carousel in carousels:
                if not _is_visible_and_sized(carousel):
                    continue
                # Test if slides exist within this carousel
                test_slides = carousel.query_selector_all(slide_sel)
                if test_slides:
                    log.info(f"Carousel matched: {carousel_sel} → {len(test_slides)} slides using '{slide_sel}'")
                    return carousel, slide_sel, carousel_sel
        except Exception:
            continue
    return None, "", ""


def _scrape_carousel(page: Page, carousel_el, slide_sel: str,
                     carousel_idx: int, url_slug: str) -> List[CarouselSlide]:
    slides_data = []
    slide_els = carousel_el.query_selector_all(slide_sel)

    if not slide_els:
        # Fallback: screenshot the whole carousel as one slide
        copy = _get_slide_copy(carousel_el)
        path = _save_screenshot(page, carousel_el,
                                f"{url_slug}_carousel{carousel_idx}_full.png")
        return [CarouselSlide(index=1, copy=copy, screenshot_path=path)]

    log.info(f"Carousel {carousel_idx}: {len(slide_els)} slides found")

    for i, slide in enumerate(slide_els):
        try:
            if not _is_visible_and_sized(slide):
                continue

            # Advance carousel if needed
            if i > 0:
                next_btn = carousel_el.query_selector(
                    "button.next, button[aria-label*='next'], button[aria-label*='Next'], "
                    ".slick-next, .swiper-button-next, [data-slide='next'], "
                    "[class*='next-btn'], [class*='nextBtn']"
                )
                if next_btn and next_btn.is_visible():
                    try:
                        next_btn.click()
                        page.wait_for_timeout(500)
                    except Exception:
                        pass

            slide.scroll_into_view_if_needed()
            page.wait_for_timeout(250)

            copy = _get_slide_copy(slide)
            path = _save_screenshot(page, slide,
                                    f"{url_slug}_carousel{carousel_idx}_slide{i + 1}.png")

            if path:
                slides_data.append(CarouselSlide(
                    index=i + 1, copy=copy, screenshot_path=path
                ))
                log.debug(f"  Slide {i + 1}: '{copy[:60]}' → saved")

        except Exception as e:
            log.debug(f"  Slide {i + 1} skipped: {e}")
            continue

    return slides_data


def _fallback_scroll_capture(page: Page, url_slug: str) -> List[CarouselSlide]:
    """
    When no carousel structure is found, capture the page in 3 vertical
    sections (top third, middle third, bottom third) as pseudo-slides.
    Gives the visual scorer something to work with.
    """
    log.info("No carousel found — falling back to scroll-and-capture")
    SCREENSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    slides = []

    scroll_positions = [0, 0.33, 0.66]
    labels = ["top", "middle", "bottom"]

    for pos, label in zip(scroll_positions, labels):
        try:
            page.evaluate(f"window.scrollTo(0, document.body.scrollHeight * {pos})")
            page.wait_for_timeout(600)
            path = SCREENSHOTS_DIR / f"{url_slug}_section_{label}.png"
            page.screenshot(path=str(path), clip={"x": 0, "y": 0, "width": W, "height": H})
            # Get visible text at this scroll position
            visible_text = page.evaluate("""() => {
                const vp = { top: window.scrollY, bottom: window.scrollY + window.innerHeight };
                const els = document.querySelectorAll('h1,h2,h3,p,[class*="title"]');
                const texts = [];
                els.forEach(el => {
                    const r = el.getBoundingClientRect();
                    if (r.top >= 0 && r.bottom <= window.innerHeight) {
                        const t = el.innerText?.trim();
                        if (t && t.length > 5) texts.push(t);
                    }
                });
                return texts.slice(0, 5).join(' | ');
            }""") or ""
            slides.append(CarouselSlide(
                index=len(slides) + 1,
                copy=visible_text[:300],
                screenshot_path=str(path)
            ))
            log.debug(f"  Section {label}: captured")
        except Exception as e:
            log.debug(f"  Section {label} capture failed: {e}")

    return slides


# ── Banner scraping ────────────────────────────────────────────────────────────

def _scrape_banners(page: Page, url_slug: str) -> List[Banner]:
    banners_data = []

    for sel in BANNER_SELECTORS:
        try:
            banner_els = page.query_selector_all(sel)
            if not banner_els:
                continue
            log.info(f"Banner selector matched: {sel} → {len(banner_els)} banners")
            for i, banner in enumerate(banner_els[:5]):
                try:
                    if not _is_visible_and_sized(banner):
                        continue
                    box = banner.bounding_box()
                    if box:
                        if box["y"] < H * 0.3:
                            location = "hero"
                        elif box["y"] < H * 0.7:
                            location = "mid-page"
                        else:
                            location = "footer"
                    else:
                        location = f"banner-{i + 1}"
                    copy = _get_slide_copy(banner)
                    path = _save_screenshot(page, banner,
                                            f"{url_slug}_banner_{location}_{i + 1}.png")
                    if path:
                        banners_data.append(Banner(
                            location=location, copy=copy, screenshot_path=path
                        ))
                except Exception as e:
                    log.debug(f"  Banner {i + 1} skipped: {e}")
            if banners_data:
                break  # Stop after first selector that yields results
        except Exception:
            continue

    if not banners_data:
        log.debug("No banners found with any selector")
    return banners_data


# ── Playwright CDN image sweep ─────────────────────────────────────────────────

# Image CDN domains — only include media/image CDNs, never static-asset CDNs
_IMAGE_CDN_DOMAINS = (
    "i.mscwlns.co",       # ManMatters image CDN
    "cloudfront.net",
    "imgix.net",
    "cloudinary.com",
    "imagekit.io",
)
# Extensions that are definitely NOT images (JS, fonts, CSS, etc.)
_NON_IMAGE_EXTS = (".js", ".css", ".woff", ".woff2", ".ttf", ".eot",
                   ".map", ".json", ".txt", ".xml")

# Site chrome — header/footer/nav icons that aren't PDP content. Matched against
# the lowercased URL. These waste Vision slots and aren't part of the PDP story.
_CHROME_URL_PATTERNS = (
    "/header/", "/footer/",
    "manmatters%20logo", "manmatters logo", "_logo_", "logo_",
    "searchmm", "cartmm", "profille", "profile_",
    "gooplay", "appsto",                       # app store badges
    "/misc/fb_", "/misc/insta_", "/misc/twitter_", "/misc/yt_",
    "linkedin-icon", "x-logo", "facebook", "instagram", "/youtube",
    "playstore", "appstore", "google-play",
)

def _is_chrome_image(url: str) -> bool:
    """True if the URL is site chrome (header/footer/nav/social), not PDP content."""
    lower = url.lower()
    if any(p in lower for p in _CHROME_URL_PATTERNS):
        return True
    # Tiny transform widths (w-50, w-150) are almost always icons, not content
    m = re.search(r"[?&]tr=w-(\d+)", lower)
    if m and int(m.group(1)) <= 150:
        return True
    return False

def _playwright_cdn_sweep(url: str, url_slug: str) -> List[ZeusImage]:
    """
    Open the live PDP page with Playwright, scroll fully to trigger lazy loads,
    and collect every CDN image URL found in <img> src / srcset / CSS backgrounds.

    Returns ZeusImage objects (no local_path — caller downloads them).
    This always reflects the REAL live page, regardless of Zeus/staging cache quality.
    """
    found_urls: List[str] = []
    seen: set = set()

    def _is_cdn_image(s: str) -> bool:
        if not isinstance(s, str) or not s.startswith("http"):
            return False
        lower = s.lower()
        # Skip videos
        if any(v in lower for v in (".mp4", ".webm", ".mov", "video.")):
            return False
        # Skip non-image assets (JS, fonts, CSS)
        path = lower.split("?")[0]
        if any(path.endswith(ext) for ext in _NON_IMAGE_EXTS):
            return False
        # Must be an image CDN domain
        return any(cdn in lower for cdn in _IMAGE_CDN_DOMAINS)

    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True)
            ctx = browser.new_context(
                viewport={"width": W, "height": H},
                user_agent=(
                    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/124.0.0.0 Safari/537.36"
                ),
            )
            page = ctx.new_page()

            # Intercept network — catch every CDN image request (incl. lazy loads).
            # ctx_y holds the page-Y of whatever carousel we're currently clicking
            # through, so slides that load via network (and leave the DOM when the
            # carousel advances) still get a correct vertical position for ordering.
            ctx_y = {"y": None}
            net_y = {}   # base url -> page Y captured at load time

            def _on_request(req):
                u = req.url
                if _is_cdn_image(u) and u not in seen:
                    seen.add(u)
                    found_urls.append(u)
                    if ctx_y["y"] is not None:
                        net_y.setdefault(u.split("?")[0], ctx_y["y"])

            page.on("request", _on_request)

            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2000)

            # Pass 1: slow scroll top→bottom to trigger lazy-loaded images.
            # Re-reads scrollHeight each step because the page grows as content loads.
            page.evaluate("""async () => {
                const delay = ms => new Promise(r => setTimeout(r, ms));
                let y = 0;
                for (let i = 0; i < 40; i++) {
                    window.scrollTo(0, y);
                    await delay(350);
                    const total = document.body.scrollHeight;
                    y += Math.max(300, Math.floor(window.innerHeight * 0.8));
                    if (y >= total) { window.scrollTo(0, total); await delay(400); break; }
                }
            }""")
            page.wait_for_timeout(1000)

            # Pass 2: expand every accordion / collapsible so its images render.
            # Many PDP sections (FAQs, benefits, how-it-works) hide images until opened.
            try:
                page.evaluate("""async () => {
                    const delay = ms => new Promise(r => setTimeout(r, ms));
                    const triggers = document.querySelectorAll(
                        '[class*="accordion"] [class*="header"], [class*="accordion"] button, '
                        + '[class*="Accordion"] button, [aria-expanded="false"], '
                        + '[class*="collapse"] [role="button"], [class*="faq"] [class*="question"], '
                        + 'details > summary'
                    );
                    for (const t of triggers) {
                        try { t.click(); await delay(120); } catch (e) {}
                    }
                }""")
                page.wait_for_timeout(1200)
            except Exception as e:
                log.debug(f"Accordion expand pass failed: {e}")

            # Pass 3: advance every carousel/slider through its slides. Driven from
            # Python so each carousel's slides are tagged (via ctx_y) with the
            # carousel's page-Y — otherwise slides that load then leave the DOM
            # would have no position and sink to the bottom of the ordered list.
            _NEXT_SEL = (
                ".swiper-button-next, .slick-next, "
                ".carousel-mobile-nav.right, [class*='carousel-mobile-nav'][class*='right'], "
                "button[aria-label*='next' i], [class*='next-btn'], [class*='nextBtn'], "
                "[class*='arrow'][class*='right'], [class*='next'], [data-slide='next']"
            )
            try:
                page.evaluate("window.scrollTo(0, 0)")
                page.wait_for_timeout(400)
                next_btns = page.query_selector_all(_NEXT_SEL)
                for btn in next_btns:
                    try:
                        # Absolute page-Y of this carousel (scroll-independent)
                        y = btn.evaluate(
                            "el => Math.round(el.getBoundingClientRect().top + window.scrollY)"
                        )
                        ctx_y["y"] = y if isinstance(y, (int, float)) else None
                        btn.scroll_into_view_if_needed(timeout=2000)
                        page.wait_for_timeout(250)
                        for _ in range(15):  # cover long carousels (10-slide hero)
                            if not btn.is_visible():
                                break
                            try:
                                btn.click(timeout=1000)
                                page.wait_for_timeout(320)
                            except Exception:
                                break
                    except Exception:
                        continue
                    finally:
                        ctx_y["y"] = None
            except Exception as e:
                log.debug(f"Carousel advance pass failed: {e}")

            # Pagination bullets (kept in JS — usually reload already-seen slides)
            try:
                page.evaluate("""async () => {
                    const delay = ms => new Promise(r => setTimeout(r, ms));
                    const bullets = document.querySelectorAll(
                        '.swiper-pagination-bullet, [class*="bullet"], [class*="dot"], '
                        + '[class*="pagination"] > *, [class*="indicator"] > *'
                    );
                    for (const dot of bullets) {
                        try { if (dot.offsetParent) { dot.click(); await delay(250); } } catch (e) {}
                    }
                }""")
                page.wait_for_timeout(1000)
            except Exception as e:
                log.debug(f"Bullet pass failed: {e}")

            # Pass 4: final slow scroll to flush anything revealed by passes 2–3.
            page.evaluate("""async () => {
                const delay = ms => new Promise(r => setTimeout(r, ms));
                const total = document.body.scrollHeight;
                const step  = Math.floor(total / 12);
                for (let y = 0; y <= total; y += step) {
                    window.scrollTo(0, y);
                    await delay(250);
                }
                window.scrollTo(0, 0);
            }""")
            page.wait_for_timeout(1500)

            # Collect from DOM WITH each image's vertical position, so the final
            # list can be ordered top-to-bottom (true page display order).
            # Returns [{url, y, x, alt}] for every img/source/background image.
            dom_items = page.evaluate("""() => {
                const items = [];
                const push = (url, el, alt) => {
                    if (!url) return;
                    let y = 1e9, x = 0;
                    try {
                        const r = el.getBoundingClientRect();
                        y = Math.round(r.top + window.scrollY);
                        x = Math.round(r.left + window.scrollX);
                    } catch (e) {}
                    items.push({url, y, x, alt: alt || ''});
                };
                const pushSrcset = (ss, el, alt) => (ss || '').split(',').forEach(part => {
                    const u = part.trim().split(' ')[0];
                    if (u) push(u, el, alt);
                });
                // <img> with src + every lazy-load attribute variant
                document.querySelectorAll('img').forEach(el => {
                    const alt = el.getAttribute('alt') || '';
                    ['src','data-src','data-lazy-src','data-original'].forEach(attr => {
                        const v = el.getAttribute(attr);
                        if (v) push(v, el, alt);
                    });
                    pushSrcset(el.getAttribute('data-srcset'), el, alt);
                    pushSrcset(el.srcset, el, alt);
                });
                // <picture><source srcset> and bare <source>
                document.querySelectorAll('source').forEach(el => {
                    const parentImg = el.closest('picture')?.querySelector('img');
                    pushSrcset(el.getAttribute('srcset'), parentImg || el, '');
                    const s = el.getAttribute('src'); if (s) push(s, parentImg || el, '');
                });
                // CSS background-image on any element
                document.querySelectorAll('*').forEach(el => {
                    const bg = getComputedStyle(el).backgroundImage;
                    if (bg && bg !== 'none') {
                        const m = bg.match(/url\\(['"]?([^'"\\)]+)/);
                        if (m) push(m[1], el, '');
                    }
                });
                return items;
            }""") or []

            browser.close()

    except Exception as e:
        log.warning(f"Playwright CDN sweep failed for {url}: {e}")
        return []

    def _base(u: str) -> str:
        return u.split("?")[0]

    # Build a single list of content images, each with a page-Y position, then
    # sort top-to-bottom. Position sources, in priority order:
    #   1. DOM bounding-box Y (images present at final measurement)
    #   2. ctx_y carousel tag (slides that loaded then left the DOM when the
    #      carousel advanced — e.g. hero slides 2..10)
    #   3. unknown -> 1e9 (sorts to the very end)
    chrome_dropped = 0
    items = []          # (y, x, base, url, alt)
    seen_bases = set()

    # DOM images first — they carry true positions + alt labels
    for it in dom_items:
        u = it.get("url", "")
        if not _is_cdn_image(u):
            continue
        if _is_chrome_image(u):
            chrome_dropped += 1
            continue
        b = _base(u)
        if b in seen_bases:
            continue
        seen_bases.add(b)
        items.append((it.get("y", 1e9), it.get("x", 0), b, u, it.get("alt", "")))

    # Network images not in the final DOM — use the carousel Y tag if we have one
    positioned_by_ctx = 0
    network_unknown = 0
    for u in found_urls:
        if _is_chrome_image(u):
            continue
        b = _base(u)
        if b in seen_bases:
            continue
        seen_bases.add(b)
        y = net_y.get(b)
        if y is not None:
            positioned_by_ctx += 1
        else:
            y = 1e9
            network_unknown += 1
        items.append((y, 0, b, u, ""))

    # Sort everything top-to-bottom (page display order)
    items.sort(key=lambda t: (t[0], t[1]))

    log.info(
        f"Playwright CDN sweep: {len(items)} content images from {url} "
        f"({positioned_by_ctx} carousel-tagged, {network_unknown} unpositioned, "
        f"{chrome_dropped} chrome/nav icons dropped)"
    )

    # Hero region = top of page (above ~900px). The hero carousel's slides all
    # share the carousel's Y, so they group together at the top correctly.
    HERO_Y_THRESHOLD = 900
    images = []
    for i, (y, x, b, img_url, alt) in enumerate(items):
        position = "hero" if y < HERO_Y_THRESHOLD else "content"
        label = alt[:60] if alt else f"live_{i+1}"
        images.append(ZeusImage(
            url=img_url,
            position=position,
            widget_id=f"playwright_cdn_{i}",
            widget_type="CDN_SWEEP",
            index=i,
            label=label,
        ))

    return images


# ── Main ────────────────────────────────────────────────────────────────────────

def enrich_with_visuals(pdp_data: PDPTextData) -> PDPTextData:
    """
    Enriches PDPTextData with visual assets.

    Always runs both:
      1. Zeus CDN images (labelled, positioned, from cache)
      2. Playwright CDN sweep (all live page CDN images, catches staging cache mismatches)

    Zeus images take priority; Playwright sweep fills any gaps. This ensures we
    always score the REAL live page visuals even when Zeus has staging/wrong data.
    """
    url = pdp_data.url
    url_slug = _slug(url)

    # ── Step 1: Zeus images ────────────────────────────────────────────────────
    zeus_images = get_zeus_images(url)
    if zeus_images:
        zeus_images = download_zeus_images(zeus_images, url_slug)
        log.info(f"Zeus: {len(zeus_images)} images")

    # ── Step 2: Playwright CDN sweep — always runs ─────────────────────────────
    # Gets every CDN image from the LIVE page regardless of Zeus cache quality.
    # Critical when staging Zeus cache has wrong-product data.
    live_images = _playwright_cdn_sweep(url, url_slug)
    live_bases = {img.url.split("?")[0] for img in live_images}

    # Decide whether the Zeus cache is for the RIGHT product, using overlap with
    # the live page. Staging caches sometimes return a DIFFERENT product's images
    # (e.g. S3/2024121 returned weight-management images) — those have ~zero
    # overlap with the live page. A correct cache (e.g. S2) overlaps meaningfully,
    # even if the sweep didn't re-find every single image.
    #
    #   overlap ratio < 10%  → wrong-product cache → drop ALL Zeus, use live only
    #   overlap ratio ≥ 10%  → correct cache → KEEP ALL Zeus (preserves display
    #                          order + labels), then append live-only extras
    #
    # This avoids discarding legitimate Zeus images the sweep merely missed.
    if live_bases and zeus_images:
        zeus_bases = {z.url.split("?")[0] for z in zeus_images}
        overlap = len(zeus_bases & live_bases)
        ratio = overlap / len(zeus_bases) if zeus_bases else 0
        if ratio < 0.10:
            log.info(
                f"Zeus cache looks wrong-product (only {overlap}/{len(zeus_bases)} "
                f"images = {ratio:.0%} overlap with live page) — dropping all Zeus, "
                f"using live sweep ({len(live_images)} images)"
            )
            zeus_images = []
        else:
            log.info(
                f"Zeus cache matches live page ({overlap}/{len(zeus_bases)} = "
                f"{ratio:.0%} overlap) — keeping all {len(zeus_images)} Zeus images"
            )

    # Merge: Zeus images first (labelled/positioned), then live-only additions
    zeus_urls = {z.url.split("?")[0] for z in zeus_images}
    extra_live = [
        img for img in live_images
        if img.url.split("?")[0] not in zeus_urls
    ]

    if extra_live:
        log.info(f"Playwright CDN sweep added {len(extra_live)} images not in Zeus cache")
        extra_live = download_zeus_images(extra_live, url_slug)

    all_images = zeus_images + extra_live

    if all_images:
        pdp_data.zeus_images  = all_images
        pdp_data.zeus_sourced = True

        hero  = [z for z in all_images if z.position == "hero"]
        other = [z for z in all_images if z.position != "hero"]

        pdp_data.carousels = [
            CarouselSlide(index=z.index + 1, copy=z.label,
                          screenshot_path=z.local_path)
            for z in hero
        ]
        pdp_data.banners = [
            Banner(location=z.position, copy=z.label,
                   screenshot_path=z.local_path)
            for z in other if z.local_path
        ]

        # Reviews: prefer the source that actually has dates. Zeus (KAI) is
        # canonical when populated, but staging caches are sometimes empty or
        # dateless for newer product IDs — in that case the live-page reviews
        # (scraped from __NEXT_DATA__, which carry dateCreated) are better.
        zeus_reviews = get_zeus_reviews(url)
        pw_reviews   = pdp_data.reviews or []
        zeus_dated   = sum(1 for r in zeus_reviews if r.date)
        pw_dated     = sum(1 for r in pw_reviews if r.date)

        if zeus_dated:
            pdp_data.reviews = zeus_reviews
            log.info(f"Reviews: using {len(zeus_reviews)} Zeus ({zeus_dated} dated)")
        elif pw_dated:
            pdp_data.reviews = pw_reviews
            log.info(f"Reviews: Zeus empty/dateless — using {len(pw_reviews)} live-page reviews ({pw_dated} dated)")
        elif zeus_reviews:
            pdp_data.reviews = zeus_reviews
            log.info(f"Reviews: using {len(zeus_reviews)} Zeus (no dates available)")
        elif pw_reviews:
            log.warning(f"Reviews: using {len(pw_reviews)} live-page reviews (no dates available)")
        else:
            log.warning(f"No reviews from Zeus or live page for {url}")

        log.info(
            f"Visuals done → {len(all_images)} total "
            f"({len(zeus_images)} Zeus + {len(extra_live)} live-only) | "
            f"{len(pdp_data.carousels)} hero | {len(pdp_data.banners)} content"
        )
        return pdp_data

    # ── Playwright screenshot fallback (no CDN images at all) ──────────────────
    # Only reached when both Zeus cache and live page CDN sweep found nothing.
    log.info(f"No CDN images found — screenshot fallback → {url}")

    with sync_playwright() as pw:
        browser = pw.chromium.launch(headless=True)
        ctx = browser.new_context(
            viewport={"width": W, "height": H},
            user_agent=(
                "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        )
        page = ctx.new_page()

        try:
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(2500)

            # Scroll to trigger lazy loads, then return to top
            page.evaluate("window.scrollTo(0, document.body.scrollHeight / 2)")
            page.wait_for_timeout(800)
            page.evaluate("window.scrollTo(0, 0)")
            page.wait_for_timeout(500)

            # ── Carousel ──────────────────────────────────────────────────────
            carousel_el, slide_sel, strategy = _try_carousel_strategies(page)
            all_slides: List[CarouselSlide] = []

            if carousel_el:
                try:
                    slides = _scrape_carousel(page, carousel_el, slide_sel, 1, url_slug)
                    all_slides.extend(slides)
                except Exception as e:
                    log.warning(f"Carousel scrape failed: {e}")
            else:
                all_slides = _fallback_scroll_capture(page, url_slug)

            # ── Banners ───────────────────────────────────────────────────────
            banners = _scrape_banners(page, url_slug)

        except Exception as e:
            log.error(f"Visual scrape failed for {url}: {e}")
            return pdp_data
        finally:
            browser.close()

    pdp_data.carousels = all_slides
    pdp_data.banners   = banners

    log.info(
        f"Visuals done → {len(all_slides)} carousel slides | "
        f"{len(banners)} banners | "
        f"screenshots: {sum(1 for s in all_slides if s.screenshot_path)}"
    )
    return pdp_data
