#!/opt/ytcut/venv/bin/python3
"""Standalone cleanup script for cron."""
import os, sys
sys.path.insert(0, '/opt/ytcut')
from datetime import datetime, timedelta
from pathlib import Path

VIDEOS = Path('/opt/ytcut/videos')
CUTS = Path('/opt/ytcut/cuts')
THUMBS = Path('/opt/ytcut/thumbs')
HOURS = int(os.environ.get('CLEANUP_HOURS', '24'))

now = datetime.utcnow()
removed = 0
for folder in (VIDEOS, CUTS, THUMBS):
    if not folder.exists():
        continue
    for p in folder.iterdir():
        try:
            if p.is_file() and (now - datetime.fromtimestamp(p.stat().st_mtime)) > timedelta(hours=HOURS):
                p.unlink()
                removed += 1
        except Exception:
            pass

print(f"[cleanup] {datetime.utcnow().isoformat()} removed {removed} files older than {HOURS}h")
