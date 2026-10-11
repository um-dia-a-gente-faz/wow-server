"""Backup directory stats for the exporter (#361). Stdlib only, so it is testable without pymysql."""
from pathlib import Path

SUFFIX = ".sql.gz"  # scripts/backup-db.sh; its in-progress `.sql.gz.part` files do not match


def backup_stats(directory) -> tuple[int, float, float]:
    """(count, newest mtime in epoch seconds, newest size in bytes) of finished dumps.

    No backups, or a missing or unreadable directory, gives (0, 0.0, 0.0): a timestamp of 0
    makes `time() - ts` huge, so the stale-backup alert fires.
    """
    count, newest_mtime, newest_size = 0, 0.0, 0.0
    try:
        entries = list(Path(directory).iterdir())
    except (OSError, ValueError):
        return 0, 0.0, 0.0
    for entry in entries:
        if not entry.name.endswith(SUFFIX):
            continue
        try:
            if not entry.is_file():
                continue
            st = entry.stat()
        except OSError:
            continue
        count += 1
        if st.st_mtime >= newest_mtime:
            newest_mtime, newest_size = st.st_mtime, float(st.st_size)
    return count, newest_mtime, newest_size
