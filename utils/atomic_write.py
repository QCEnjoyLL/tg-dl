"""Atomic text writes.

``config.yaml`` / ``data.yaml`` are written on every Web UI save and on exit.
A plain ``open(path, "w")`` truncates the file first, so a crash, ``SIGKILL``
or a full disk in the middle of the write left a 0-byte or half-written YAML
behind and the next start failed to load it.

The write goes through a temp file in the same directory and ``os.replace``
(atomic on POSIX and Windows). Docker setups bind-mount ``config.yaml`` as a
single file, where ``os.replace`` fails with ``EBUSY``; only those errors fall
back to copying the already complete temp file over the target.
"""

import errno
import os
import shutil
import tempfile

#: Errors that mean "rename() cannot replace this path" — docker-compose
#: bind-mounts a single file. A full disk (ENOSPC) or a read-only mount
#: (EROFS) must propagate so the caller can report that nothing was saved.
_REPLACE_FALLBACK_ERRNOS = (
    errno.EBUSY,
    errno.EXDEV,
    errno.EPERM,
    errno.EACCES,
    errno.EINVAL,
)


def write_text_atomic(path: str, text: str) -> None:
    """Write ``text`` to ``path`` without ever truncating a previous version."""
    directory = os.path.dirname(path) or "."
    handle, temp_path = tempfile.mkstemp(
        dir=directory, prefix=".config-", suffix=".yaml"
    )
    try:
        with os.fdopen(handle, "w", encoding="utf-8") as temp_file:
            temp_file.write(text)
            temp_file.flush()
            os.fsync(temp_file.fileno())
        os.replace(temp_path, path)
    except OSError as replace_err:
        if replace_err.errno not in _REPLACE_FALLBACK_ERRNOS:
            if os.path.exists(temp_path):
                os.remove(temp_path)
            raise
        try:
            # The temp file is already complete, so copying it can never leave
            # a half-written config behind (opening the target with "w" first
            # is what lost the config when the write itself failed).
            shutil.copyfile(temp_path, path)
        finally:
            if os.path.exists(temp_path):
                os.remove(temp_path)
    except Exception:
        if os.path.exists(temp_path):
            os.remove(temp_path)
        raise
