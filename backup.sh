#!/bin/bash
# Backup SQLite DB for Yestubers
BACKUP_DIR="/opt/ytcut/backups"
DB="/opt/ytcut/yestubers.db"
mkdir -p "$BACKUP_DIR"
TS=$(date +%Y%m%d_%H%M%S)
cp "$DB" "$BACKUP_DIR/app_${TS}.db"
# Keep last 14 backups
ls -t "$BACKUP_DIR"/app_*.db 2>/dev/null | tail -n +15 | xargs -r rm -f
echo "[backup] ${TS} backed up ${DB}"
