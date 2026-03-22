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

    has_creds = settings.vps_ssh_host and (settings.vps_ssh_password or settings.vps_ssh_key)
    if not has_creds:
        logger.info("VPS SSH not configured — blog pages saved locally only (%s)",
                     _blog_dir())
        return len(generated)

    pushed = _sftp_push(generated)
    return pushed


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
