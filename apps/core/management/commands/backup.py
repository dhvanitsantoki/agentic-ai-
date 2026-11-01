"""Backup: database dump + media archive into BACKUP_DIR, keeping the newest BACKUP_KEEP sets."""
import gzip
import os
import shutil
import sqlite3
import subprocess
import tarfile
from pathlib import Path

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import connection
from django.utils import timezone


class Command(BaseCommand):
    help = "Create a backup of the database and uploaded files (catalogues, images, quotation PDFs)."

    def handle(self, *args, **options):
        stamp = timezone.localtime().strftime("%Y%m%d-%H%M%S")
        target = Path(settings.BACKUP_DIR)
        target.mkdir(parents=True, exist_ok=True)
        db = settings.DATABASES["default"]
        if connection.vendor == "postgresql":
            out = target / f"db-{stamp}.sql.gz"
            env = {**os.environ, "PGPASSWORD": db.get("PASSWORD", "")}
            cmd = ["pg_dump", "--no-owner", "--format=plain", "-h", db["HOST"], "-p", str(db["PORT"]), "-U", db["USER"], db["NAME"]]
            try:
                proc = subprocess.run(cmd, env=env, capture_output=True, check=True, timeout=1800)
            except FileNotFoundError as exc:
                raise CommandError("pg_dump not found - install postgresql-client.") from exc
            except subprocess.CalledProcessError as exc:
                raise CommandError(f"pg_dump failed: {exc.stderr.decode()[:500]}") from exc
            with gzip.open(out, "wb") as fh:
                fh.write(proc.stdout)
        else:
            out = target / f"db-{stamp}.sqlite3"
            connection.ensure_connection()
            dst = sqlite3.connect(out)
            with dst:
                connection.connection.backup(dst)
            dst.close()
        media = Path(settings.MEDIA_ROOT)
        media_out = target / f"media-{stamp}.tar.gz"
        with tarfile.open(media_out, "w:gz") as tar:
            if media.exists():
                tar.add(media, arcname="media")
        self._rotate(target)
        self.stdout.write(self.style.SUCCESS(f"Backup written: {out.name}, {media_out.name}"))

    def _rotate(self, target: Path):
        keep = settings.BACKUP_KEEP
        for prefix in ("db-", "media-"):
            files = sorted((p for p in target.iterdir() if p.name.startswith(prefix)), key=lambda p: p.name, reverse=True)
            for old in files[keep:]:
                old.unlink(missing_ok=True)
