"""Generate static HTML pages for blog posts and a posts index JSON.

After a post is published on platforms, this module produces:
- blog/post-{id}.html  — full SEO-ready page for each post
- blog/posts.json      — index used by blog.html to list posts
- blog/thumb-{id}.jpg  — small thumbnail saved before media cleanup
"""
from __future__ import annotations

import json
import logging
from datetime import datetime
from html import escape
from pathlib import Path
from typing import Optional

from config.settings import settings

logger = logging.getLogger(__name__)

SITE_URL = "https://www.im-in.net"
BLOG_DIR_NAME = "blog"
THUMB_SIZE = (260, 150)
THUMB_QUALITY = 80


def _blog_dir() -> Path:
    d = Path(settings.data_dir) / BLOG_DIR_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def save_thumbnail(post_id: int, image_path: str) -> Optional[str]:
    """Create a small JPEG thumbnail from the original image before it's deleted.

    Returns the relative URL suitable for use in blog HTML/JSON
    (e.g. ``blog/thumb-42.jpg`` — relative to blog.html on VPS),
    or ``None`` if the source image can't be read.
    """
    src = Path(image_path)
    if not src.is_file():
        logger.warning("Thumbnail source missing: %s", src)
        return None

    try:
        from PIL import Image
        img = Image.open(src)
        img = img.convert("RGB")

        w, h = img.size
        side = min(w, h)
        left = (w - side) // 2
        top = (h - side) // 2
        img = img.crop((left, top, left + side, top + side))
        img = img.resize(THUMB_SIZE, Image.LANCZOS)

        thumb_name = f"thumb-{post_id}.jpg"
        thumb_path = _blog_dir() / thumb_name
        img.save(thumb_path, "JPEG", quality=THUMB_QUALITY, optimize=True)
        logger.info("Saved thumbnail %s (%dx%d)", thumb_name, *THUMB_SIZE)
        return f"blog/{thumb_name}"
    except Exception:
        logger.warning("Failed to create thumbnail for post %d", post_id, exc_info=True)
        return None


def _thumb_url_if_exists(post_id: int) -> Optional[str]:
    """Return relative thumbnail URL if the file already exists on disk."""
    thumb = _blog_dir() / f"thumb-{post_id}.jpg"
    if thumb.is_file():
        return f"blog/thumb-{post_id}.jpg"
    return None


def _parse_translations(raw: Optional[str]) -> dict:
    """Parse JSON translations field, returning empty dict on failure."""
    if not raw:
        return {}
    try:
        return json.loads(raw)
    except Exception:
        return {}


def _map_url(lat: float, lon: float, name: str = "") -> str:
    from urllib.parse import quote
    q = quote(name) if name else f"{lat},{lon}"
    return f"https://www.google.com/maps/search/?api=1&query={q}"


def _fmt_date(dt_val: Optional[datetime]) -> str:
    if not dt_val:
        return ""
    if isinstance(dt_val, str):
        return dt_val[:10]
    return dt_val.strftime("%Y-%m-%d")


def _fmt_date_human(dt_val: Optional[datetime]) -> str:
    if not dt_val:
        return ""
    months = ["січня", "лютого", "березня", "квітня", "травня", "червня",
              "липня", "серпня", "вересня", "жовтня", "листопада", "грудня"]
    if isinstance(dt_val, str):
        try:
            dt_val = datetime.fromisoformat(dt_val)
        except Exception:
            return dt_val[:10]
    return f"{dt_val.day} {months[dt_val.month - 1]} {dt_val.year}"


import re as _re

_STOP_WORDS = {"і", "в", "на", "з", "та", "до", "що", "як", "це", "для", "не", "або",
               "the", "a", "an", "in", "on", "of", "to", "and", "is", "for", "with", "by",
               "від", "при", "за", "по", "із", "але", "ще", "про", "у", "їх", "він", "вона"}

def _extract_keywords(title: str, content: str, place_name: Optional[str] = None) -> str:
    """Extract SEO keywords from title, content and place."""
    text = f"{title} {(content or '')[:500]}"
    words = _re.findall(r"[a-zA-Zа-яА-ЯіІїЇєЄґҐ'ʼ]{4,}", text.lower())
    seen = set()
    kw = []
    for w in words:
        if w not in _STOP_WORDS and w not in seen:
            seen.add(w)
            kw.append(w)
        if len(kw) >= 15:
            break
    base = ["подорожі", "туризм", "travel", "I'M IN"]
    if place_name:
        base.insert(0, place_name)
    return ", ".join(base + kw)


def generate_post_html(
    post_id: int,
    title: str,
    content: str,
    published_at: Optional[datetime] = None,
    image_url: Optional[str] = None,
    source_url: Optional[str] = None,
    latitude: Optional[float] = None,
    longitude: Optional[float] = None,
    place_name: Optional[str] = None,
    translations: Optional[dict] = None,
) -> Path:
    """Generate a static HTML page for a single post. Returns the file path."""

    safe_title = escape(title or "Новина")
    safe_content = escape(content or "")
    content_paragraphs = "\n".join(
        f"<p>{escape(line)}</p>" for line in (content or "").split("\n") if line.strip()
    )
    date_iso = _fmt_date(published_at)
    date_human = _fmt_date_human(published_at)
    canonical = f"{SITE_URL}/blog/post-{post_id}.html"
    if image_url and not image_url.startswith("http"):
        og_image = f"{SITE_URL}/{image_url}"
    else:
        og_image = image_url or f"{SITE_URL}/logo-imin.png"
    description = escape((content or "")[:160].replace("\n", " "))

    geo_html = ""
    if latitude and longitude:
        map_link = _map_url(latitude, longitude, place_name or "")
        geo_html = f"""
        <a class="post-geo" href="{escape(map_link)}" target="_blank" rel="noopener">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="18" height="18">
                <path d="M21 10c0 7-9 13-9 13s-9-6-9-13a9 9 0 1118 0z"/><circle cx="12" cy="10" r="3"/>
            </svg>
            {escape(place_name or f'{latitude:.4f}, {longitude:.4f}')}
        </a>"""

    source_html = ""
    if source_url:
        try:
            from urllib.parse import urlparse
            domain = urlparse(source_url).hostname or ""
            domain = domain.replace("www.", "")
        except Exception:
            domain = "джерело"
        source_html = f"""
        <a class="post-source" href="{escape(source_url)}" target="_blank" rel="noopener">
            <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" width="16" height="16">
                <path d="M10 13a5 5 0 007.54.54l3-3a5 5 0 00-7.07-7.07l-1.72 1.71"/>
                <path d="M14 11a5 5 0 00-7.54-.54l-3 3a5 5 0 007.07 7.07l1.71-1.71"/>
            </svg>
            Джерело: {escape(domain)}
        </a>"""

    image_html = ""
    if image_url:
        img_src = image_url
        if img_src.startswith("blog/"):
            img_src = img_src[len("blog/"):]
        image_html = f'<img class="post-hero" src="{escape(img_src)}" alt="{safe_title}" loading="lazy" onerror="this.style.display=\'none\'">'

    translations_json = json.dumps(translations or {}, ensure_ascii=False)
    title_json = json.dumps(title or "", ensure_ascii=False)
    content_json = json.dumps(content or "", ensure_ascii=False)

    title_for_json = json.dumps(title or "Новина", ensure_ascii=False)
    desc_for_json = json.dumps((content or "")[:160].replace("\n", " "), ensure_ascii=False)

    hreflang_tags = f'    <link rel="alternate" hreflang="uk" href="{canonical}">\n'
    tr = translations or {}
    for lang_code in ["en", "fr", "es", "de", "it", "el"]:
        if lang_code in tr:
            hreflang_tags += f'    <link rel="alternate" hreflang="{lang_code}" href="{canonical}">\n'
    hreflang_tags += f'    <link rel="alternate" hreflang="x-default" href="{canonical}">'

    og_locale_alts = ""
    locale_map = {"en": "en_US", "fr": "fr_FR", "es": "es_ES", "de": "de_DE", "it": "it_IT", "el": "el_GR"}
    for lc, loc in locale_map.items():
        if lc in tr:
            og_locale_alts += f'\n    <meta property="og:locale:alternate" content="{loc}">'

    keywords = _extract_keywords(title, content, place_name)
    keywords_json = json.dumps(keywords, ensure_ascii=False)

    geo_json = ""
    if latitude and longitude:
        geo_json = f""",
        "contentLocation": {{
            "@type": "Place",
            "name": {json.dumps(place_name or "", ensure_ascii=False)},
            "geo": {{"@type": "GeoCoordinates", "latitude": {latitude}, "longitude": {longitude}}}
        }}"""

    html = f"""<!DOCTYPE html>
<html lang="uk">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>{safe_title} — I'M IN Blog</title>
    <meta name="description" content="{description}">
    <meta name="keywords" content="{escape(keywords)}">
    <meta name="robots" content="index, follow, max-image-preview:large, max-snippet:-1">
    <meta name="theme-color" content="#7C3AED">
    <link rel="icon" type="image/svg+xml" href="../favicon.svg">
    <link rel="icon" type="image/png" sizes="128x128" href="../favicon.png">
    <link rel="canonical" href="{canonical}">
    <meta property="og:type" content="article">
    <meta property="og:url" content="{canonical}">
    <meta property="og:title" content="{safe_title}">
    <meta property="og:description" content="{description}">
    <meta property="og:image" content="{escape(og_image)}">
    <meta property="og:image:alt" content="{safe_title}">
    <meta property="og:site_name" content="I'M IN">
    <meta property="og:locale" content="uk_UA">{og_locale_alts}
    <meta property="article:published_time" content="{date_iso}">
    <meta property="article:section" content="Travel">
    <meta property="article:publisher" content="https://www.im-in.net">
    <meta name="twitter:card" content="summary_large_image">
    <meta name="twitter:title" content="{safe_title}">
    <meta name="twitter:description" content="{description}">
    <meta name="twitter:image" content="{escape(og_image)}">
    <meta name="twitter:image:alt" content="{safe_title}">
{hreflang_tags}
    <link rel="preconnect" href="https://fonts.googleapis.com">
    <link rel="preconnect" href="https://fonts.gstatic.com" crossorigin>
    <link href="https://fonts.googleapis.com/css2?family=Inter:wght@400;500;600;700;800&display=swap" rel="stylesheet">
    <script type="application/ld+json">
    {{
        "@context": "https://schema.org",
        "@type": "BlogPosting",
        "@id": "{canonical}",
        "mainEntityOfPage": "{canonical}",
        "headline": {title_for_json},
        "description": {desc_for_json},
        "datePublished": "{date_iso}",
        "dateModified": "{date_iso}",
        "image": "{escape(og_image)}",
        "url": "{canonical}",
        "inLanguage": "uk",
        "author": {{
            "@type": "Organization",
            "@id": "{SITE_URL}/#organization",
            "name": "I'M IN"
        }},
        "publisher": {{
            "@type": "Organization",
            "@id": "{SITE_URL}/#organization",
            "name": "I'M IN",
            "url": "{SITE_URL}",
            "logo": {{
                "@type": "ImageObject",
                "url": "{SITE_URL}/logo-imin.png"
            }}
        }},
        "keywords": {keywords_json}{geo_json}
    }}
    </script>
    <script type="application/ld+json">
    {{
        "@context": "https://schema.org",
        "@type": "BreadcrumbList",
        "itemListElement": [
            {{"@type": "ListItem", "position": 1, "name": "Головна", "item": "{SITE_URL}/"}},
            {{"@type": "ListItem", "position": 2, "name": "Блог", "item": "{SITE_URL}/blog.html"}},
            {{"@type": "ListItem", "position": 3, "name": {title_for_json}, "item": "{canonical}"}}
        ]
    }}
    </script>
    <style>
        *, *::before, *::after {{ margin: 0; padding: 0; box-sizing: border-box; }}
        :root {{
            --primary: #7C3AED;
            --primary-dark: #6D28D9;
            --primary-light: #EDE9FE;
            --text-dark: #1C1B1F;
            --text-muted: #5F5E63;
            --white: #ffffff;
        }}
        body {{
            font-family: 'Inter', -apple-system, BlinkMacSystemFont, sans-serif;
            color: var(--text-dark);
            line-height: 1.7;
            background: var(--white);
        }}
        .post-header {{
            background: var(--primary);
            padding: 1.5rem 0;
        }}
        .container {{ max-width: 760px; margin: 0 auto; padding: 0 24px; }}
        .post-header .container {{
            display: flex; align-items: center; justify-content: space-between;
        }}
        .post-header a {{
            color: rgba(255,255,255,0.7); text-decoration: none; font-weight: 500;
            font-size: 0.9rem; display: flex; align-items: center; gap: 0.4rem;
            transition: color 0.3s;
        }}
        .post-header a:hover {{ color: #fff; }}
        .post-header img {{ height: 32px; border-radius: 6px; }}
        .post-hero {{
            width: 100px; height: 50px; object-fit: cover;
            border-radius: 8px; margin: 2rem 0 1.5rem;
            float: left; margin-right: 1.25rem; margin-bottom: 0.5rem;
        }}
        .post-date {{
            color: var(--text-muted); font-size: 0.85rem; font-weight: 500;
            margin-bottom: 0.5rem;
        }}
        .post-title {{
            font-size: 1.75rem; font-weight: 800; line-height: 1.3;
            margin-bottom: 1.25rem;
        }}
        .post-body p {{
            color: var(--text-dark); font-size: 1rem; margin-bottom: 1rem;
        }}
        .post-meta {{
            display: flex; flex-wrap: wrap; gap: 0.75rem;
            margin-top: 1.5rem; padding-top: 1.25rem;
            border-top: 1px solid #e5e7eb;
        }}
        .post-geo, .post-source {{
            display: inline-flex; align-items: center; gap: 0.35rem;
            padding: 0.35rem 0.75rem; border-radius: 50px;
            font-size: 0.8rem; font-weight: 600;
            text-decoration: none; transition: opacity 0.2s;
        }}
        .post-geo:hover, .post-source:hover {{ opacity: 0.75; }}
        .post-geo {{ background: #DBEAFE; color: #2563EB; }}
        .post-source {{ background: #FEF3C7; color: #D97706; }}
        .post-footer {{
            background: var(--text-dark); padding: 2rem 0; text-align: center;
            color: rgba(255,255,255,0.4); font-size: 0.85rem; margin-top: 3rem;
        }}
        .post-footer a {{ color: rgba(255,255,255,0.6); text-decoration: none; }}
        .post-footer a:hover {{ color: #fff; }}
        .post-lang-bar {{
            background: var(--primary-light); padding: 0.5rem 0; text-align: center;
        }}
        .post-lang-bar button {{
            background: none; border: 1px solid transparent; padding: 0.25rem 0.6rem;
            border-radius: 50px; cursor: pointer; font-size: 0.75rem; font-weight: 600;
            color: var(--text-muted); transition: all 0.2s; font-family: inherit;
        }}
        .post-lang-bar button.active {{ background: var(--primary); color: #fff; }}
        .post-lang-bar button:hover:not(.active) {{ border-color: var(--primary); color: var(--primary); }}
        @media (max-width: 768px) {{
            .post-title {{ font-size: 1.35rem; }}
            .post-hero {{ border-radius: 12px; max-height: 280px; }}
        }}
    </style>
</head>
<body>
    <header class="post-header">
        <div class="container">
            <a href="../index.html"><img src="../logo-imin.png" alt="I'M IN" width="120" height="44"></a>
            <nav aria-label="Навігація">
                <a href="../blog.html" id="back-link">
                    <svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" width="18" height="18"><path d="M19 12H5M12 19l-7-7 7-7"/></svg>
                    <span id="back-text">Всі новини</span>
                </a>
            </nav>
        </div>
    </header>
    <div class="post-lang-bar" role="navigation" aria-label="Language">
        <button data-lang-btn="uk" class="active">UK</button>
        <button data-lang-btn="en">EN</button>
        <button data-lang-btn="fr">FR</button>
        <button data-lang-btn="es">ES</button>
        <button data-lang-btn="de">DE</button>
        <button data-lang-btn="it">IT</button>
        <button data-lang-btn="el">EL</button>
    </div>
    <main>
        <article class="container" itemscope itemtype="https://schema.org/BlogPosting">
            {image_html}
            <time class="post-date" datetime="{date_iso}" itemprop="datePublished">{date_human}</time>
            <h1 class="post-title" id="post-title" itemprop="headline">{safe_title}</h1>
            <div class="post-body" id="post-body" itemprop="articleBody">
                {content_paragraphs}
            </div>
            <div class="post-meta">
                {geo_html}
                {source_html}
            </div>
        </article>
    </main>
    <footer class="post-footer">
        <div class="container">
            <nav aria-label="Footer" style="margin-bottom:0.75rem;">
                <a href="../index.html">I'M IN</a> &middot;
                <a href="../blog.html">Блог</a> &middot;
                <a href="../terms.html">Угода</a> &middot;
                <a href="../privacy.html">Конфіденційність</a>
            </nav>
            <p>&copy; 2026 I'M IN. <a href="../index.html">im-in.net</a></p>
        </div>
    </footer>
    <script>
    (function() {{
        var T = {translations_json};
        T["uk"] = {{"title": {title_json}, "content": {content_json}}};
        var backLabels = {{"uk":"Всі новини","en":"All news","fr":"Toutes les nouvelles","es":"Todas las noticias","de":"Alle Nachrichten","it":"Tutte le notizie","el":"Όλα τα νέα"}};
        var lang = localStorage.getItem('language') || 'uk';
        function setLang(l) {{
            lang = l; localStorage.setItem('language', l);
            document.documentElement.lang = l;
            document.querySelectorAll('[data-lang-btn]').forEach(function(b) {{ b.classList.toggle('active', b.getAttribute('data-lang-btn') === l); }});
            var t = T[l] || T['uk'];
            document.getElementById('post-title').textContent = t.title;
            var body = document.getElementById('post-body');
            body.innerHTML = t.content.split('\\n').filter(function(p){{ return p.trim(); }}).map(function(p){{ var s=document.createElement('span'); s.textContent=p; return '<p>'+s.innerHTML+'</p>'; }}).join('');
            document.getElementById('back-text').textContent = backLabels[l] || backLabels['uk'];
        }}
        document.querySelectorAll('[data-lang-btn]').forEach(function(btn) {{ btn.addEventListener('click', function() {{ setLang(btn.getAttribute('data-lang-btn')); }}); }});
        setLang(lang);
    }})();
    </script>
</body>
</html>"""

    out_path = _blog_dir() / f"post-{post_id}.html"
    out_path.write_text(html, encoding="utf-8")
    logger.info("Generated blog page: %s", out_path.name)
    return out_path


def generate_posts_index(posts: list[dict]) -> Path:
    """Generate posts.json index for blog.html to consume locally."""
    out_path = _blog_dir() / "posts.json"
    out_path.write_text(json.dumps(posts, ensure_ascii=False, default=str), encoding="utf-8")
    logger.info("Generated posts.json with %d entries", len(posts))
    return out_path


async def generate_all_published() -> list[Path]:
    """Generate HTML pages for all published posts + index JSON."""
    from sqlalchemy import select, func, desc
    from db.database import async_session
    from db.models import Post, Publication, PostStatus

    async with async_session() as session:
        pub_date_sub = (
            select(
                Publication.post_id,
                func.max(Publication.published_at).label("published_at"),
            )
            .where(Publication.status == PostStatus.PUBLISHED)
            .group_by(Publication.post_id)
            .subquery()
        )

        result = await session.execute(
            select(Post, pub_date_sub.c.published_at)
            .join(pub_date_sub, Post.id == pub_date_sub.c.post_id)
            .order_by(desc(pub_date_sub.c.published_at))
        )
        rows = result.all()

    if not rows:
        logger.info("No published posts — nothing to generate")
        return []

    generated: list[Path] = []
    index_entries: list[dict] = []

    for post, published_at in rows:
        image_url = _thumb_url_if_exists(post.id)
        translations = _parse_translations(post.translations)

        page = generate_post_html(
            post_id=post.id,
            title=post.title or "",
            content=post.content_raw or "",
            published_at=published_at,
            image_url=image_url,
            source_url=post.source_url,
            latitude=post.latitude,
            longitude=post.longitude,
            place_name=post.place_name,
            translations=translations,
        )
        generated.append(page)

        index_entries.append({
            "id": post.id,
            "title": post.title,
            "content_raw": post.content_raw,
            "source": post.source,
            "source_url": post.source_url,
            "latitude": post.latitude,
            "longitude": post.longitude,
            "place_name": post.place_name,
            "image_url": image_url,
            "published_at": published_at,
            "created_at": post.created_at,
            "translations": translations,
        })

    idx_path = generate_posts_index(index_entries)
    generated.append(idx_path)

    sitemap_path = _generate_sitemap(index_entries)
    if sitemap_path:
        generated.append(sitemap_path)

    logger.info("Blog generation complete: %d pages + index + sitemap", len(rows))
    return generated


def _generate_sitemap(entries: list[dict]) -> Optional[Path]:
    """Generate sitemap.xml with all pages including blog posts."""
    try:
        today = datetime.now().strftime("%Y-%m-%d")
        lines = [
            '<?xml version="1.0" encoding="UTF-8"?>',
            '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">',
            f'  <url><loc>{SITE_URL}/</loc><lastmod>{today}</lastmod><changefreq>weekly</changefreq><priority>1.0</priority></url>',
            f'  <url><loc>{SITE_URL}/blog.html</loc><lastmod>{today}</lastmod><changefreq>daily</changefreq><priority>0.9</priority></url>',
            f'  <url><loc>{SITE_URL}/terms.html</loc><lastmod>2026-03-08</lastmod><changefreq>monthly</changefreq><priority>0.3</priority></url>',
            f'  <url><loc>{SITE_URL}/privacy.html</loc><lastmod>2026-03-08</lastmod><changefreq>monthly</changefreq><priority>0.3</priority></url>',
        ]
        for e in entries:
            pub = e.get("published_at")
            if pub:
                if isinstance(pub, str):
                    d = pub[:10]
                else:
                    d = pub.strftime("%Y-%m-%d")
            else:
                d = today
            lines.append(f'  <url><loc>{SITE_URL}/blog/post-{e["id"]}.html</loc><lastmod>{d}</lastmod><changefreq>monthly</changefreq><priority>0.7</priority></url>')
        lines.append('</urlset>')

        sitemap_path = _blog_dir() / "sitemap.xml"
        sitemap_path.write_text("\n".join(lines), encoding="utf-8")
        logger.info("Generated sitemap.xml with %d URLs", len(entries) + 4)
        return sitemap_path
    except Exception:
        logger.warning("Failed to generate sitemap", exc_info=True)
        return None
