"""Synchronise generated blog pages to the VPS via SFTP.

If VPS_SSH_HOST is not configured, pages are only generated locally
on Railway (served at /static/blog/) and accessible via API fallback.
"""
from __future__ import annotations

import io
import logging
from pathlib import Path

from config.settings import settings

logger = logging.getLogger(__name__)

BLOG_DIR = "blog"


async def sync_blog_to_vps() -> int:
    """Generate all blog pages and push to VPS. Returns count of synced files."""
    from content.blog_generator import generate_all_published, _blog_dir

    generated = await generate_all_published()
    if not generated:
        return 0

    blog_dir = _blog_dir()
    thumbs = list(blog_dir.glob("thumb-*.jpg"))
    all_files = list(set(generated + thumbs))

    sitemap_file = blog_dir / "sitemap.xml"

    has_creds = settings.vps_ssh_host and (settings.vps_ssh_password or settings.vps_ssh_key)
    if not has_creds:
        logger.info("VPS SSH not configured — blog pages saved locally only (%s)", blog_dir)
        return len(all_files)

    blog_files = [f for f in all_files if f.name != "sitemap.xml"]
    pushed = _sftp_push(blog_files)

    root_files: list[Path] = []
    if sitemap_file.is_file():
        root_files.append(sitemap_file)
    website_files = _fetch_website_files()
    _inject_blog_links(website_files, blog_dir)
    if not sitemap_file.is_file():
        sitemap_from_gh = [f for f in website_files if f.name == "sitemap.xml"]
        root_files.extend(sitemap_from_gh)
    root_files.extend([f for f in website_files if f.name != "sitemap.xml"])
    if root_files:
        pushed += _sftp_push_to_root(root_files)

    return pushed


def _inject_blog_links(website_files: list[Path], blog_dir: Path) -> None:
    """Replace static blog link placeholders in blog.html with real post titles."""
    import json
    from html import escape

    blog_html = next((f for f in website_files if f.name == "blog.html"), None)
    if not blog_html:
        return

    posts_json = blog_dir / "posts.json"
    if not posts_json.is_file():
        return

    try:
        posts = json.loads(posts_json.read_text(encoding="utf-8"))
    except Exception:
        logger.debug("Could not parse posts.json for link injection")
        return

    if not posts:
        return

    links = []
    for p in posts:
        pid = p.get("id", "")
        title = escape(p.get("title") or f"Пост #{pid}")
        links.append(f'                        <li><a href="blog/post-{pid}.html">{title}</a></li>')

    links_html = (
        '                <div id="blog-static-links" class="blog-static-links">\n'
        '                    <h2>Усі статті блогу</h2>\n'
        '                    <ul>\n'
        + "\n".join(links) + "\n"
        '                    </ul>\n'
        '                </div>'
    )

    content = blog_html.read_text(encoding="utf-8")
    start_marker = "<!-- BLOG_STATIC_LINKS_START -->"
    end_marker = "<!-- BLOG_STATIC_LINKS_END -->"

    if start_marker in content and end_marker in content:
        before = content[:content.index(start_marker) + len(start_marker)]
        after = content[content.index(end_marker):]
        content = before + "\n" + links_html + "\n                " + after
        blog_html.write_text(content, encoding="utf-8")
        logger.info("Injected %d static blog links into blog.html", len(posts))


def _fetch_website_files() -> list[Path]:
    """Download latest website files from GitHub and return local paths."""
    import tempfile, httpx
    base = "https://raw.githubusercontent.com/IgorKravchenko1976/im-in-website/main"
    files_to_sync = [
        "blog.html", "index.html", "robots.txt", "sitemap.xml",
        "terms.html", "privacy.html", "404.html",
        "translations.js", "styles.css", "manifest.json",
        "script.js", "favicon.svg",
    ]
    result = []
    tmp_dir = Path(tempfile.mkdtemp(prefix="vps_sync_"))
    for fname in files_to_sync:
        try:
            resp = httpx.get(f"{base}/{fname}", timeout=15, follow_redirects=True)
            if resp.status_code == 200:
                local = tmp_dir / fname
                local.write_bytes(resp.content)
                result.append(local)
        except Exception:
            logger.debug("Could not fetch %s from GitHub", fname)
    logger.info("Fetched %d website files from GitHub for VPS sync", len(result))
    return result


def _sftp_push(files: list[Path]) -> int:
    """Push files to VPS via SFTP using paramiko (password or key auth)."""
    try:
        import paramiko
    except ImportError:
        logger.error("paramiko not installed — cannot push to VPS (pip install paramiko)")
        return 0

    host = settings.vps_ssh_host
    port = settings.vps_ssh_port
    user = settings.vps_ssh_user
    password = settings.vps_ssh_password
    key_data = settings.vps_ssh_key
    remote_dir = settings.vps_blog_path

    logger.info("SFTP push to %s@%s:%d%s (%d files)", user, host, port, remote_dir, len(files))

    pkey = None
    if key_data:
        try:
            pkey = paramiko.RSAKey.from_private_key(io.StringIO(key_data))
        except Exception:
            try:
                pkey = paramiko.Ed25519Key.from_private_key(io.StringIO(key_data))
            except Exception:
                logger.warning("Failed to parse SSH key — falling back to password")

    if not pkey and not password:
        logger.error("No valid SSH key or password — cannot push to VPS")
        return 0

    pushed = 0
    try:
        transport = paramiko.Transport((host, port))
        if pkey:
            transport.connect(username=user, pkey=pkey)
        else:
            transport.connect(username=user, password=password)

        sftp = paramiko.SFTPClient.from_transport(transport)

        try:
            sftp.stat(remote_dir)
        except FileNotFoundError:
            _mkdir_p(sftp, remote_dir)

        for local_path in files:
            remote_path = f"{remote_dir}/{local_path.name}"
            sftp.put(str(local_path), remote_path)
            pushed += 1

        sftp.close()
        transport.close()
        logger.info("SFTP push complete: %d/%d files", pushed, len(files))
    except Exception:
        logger.exception("SFTP push failed after %d files", pushed)

    return pushed


def _sftp_push_to_root(files: list[Path]) -> int:
    """Push files to VPS website root (parent of blog dir) via SFTP."""
    try:
        import paramiko
    except ImportError:
        return 0

    host = settings.vps_ssh_host
    port = settings.vps_ssh_port
    user = settings.vps_ssh_user
    password = settings.vps_ssh_password
    key_data = settings.vps_ssh_key
    root_dir = str(Path(settings.vps_blog_path).parent)

    pkey = None
    if key_data:
        try:
            pkey = paramiko.RSAKey.from_private_key(io.StringIO(key_data))
        except Exception:
            try:
                pkey = paramiko.Ed25519Key.from_private_key(io.StringIO(key_data))
            except Exception:
                pass

    if not pkey and not password:
        return 0

    pushed = 0
    try:
        transport = paramiko.Transport((host, port))
        if pkey:
            transport.connect(username=user, pkey=pkey)
        else:
            transport.connect(username=user, password=password)
        sftp = paramiko.SFTPClient.from_transport(transport)
        for local_path in files:
            remote_path = f"{root_dir}/{local_path.name}"
            sftp.put(str(local_path), remote_path)
            pushed += 1
            logger.info("SFTP root push: %s -> %s", local_path.name, remote_path)
        sftp.close()
        transport.close()
    except Exception:
        logger.warning("SFTP root push failed", exc_info=True)
    return pushed


def _mkdir_p(sftp, remote_dir: str) -> None:
    """Recursively create remote directories (like mkdir -p)."""
    parts = remote_dir.split("/")
    current = ""
    for part in parts:
        if not part:
            current = "/"
            continue
        current = current.rstrip("/") + "/" + part
        try:
            sftp.stat(current)
        except FileNotFoundError:
            sftp.mkdir(current)
