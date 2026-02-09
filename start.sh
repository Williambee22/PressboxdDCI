#!/usr/bin/env bash
set -e

# Where Railway will mount your Volume
: "${DB_PATH:=/data/site.db}"

# If there's no DB file on the volume yet, seed it from repo
if [ ! -f "$DB_PATH" ]; then
  echo "No DB found at $DB_PATH; seeding..."
  mkdir -p "$(dirname "$DB_PATH")"
  if [ -f "seed/site.db" ]; then
    cp "seed/site.db" "$DB_PATH"
  else
    # If you don't have a seed db, you can create an empty file;
    # your app should then create tables on first run.
    touch "$DB_PATH"
  fi
fi

# Start gunicorn (1 worker is safest for SQLite)
exec gunicorn -w 1 -b 0.0.0.0:${PORT:-8080} app:app