import os
import shutil
from pathlib import Path
from tempfile import gettempdir
from uuid import uuid4

import pytest


# Automated tests must never consume a developer's saved model credentials or quota.
os.environ["CHIP_AGENT_MODEL_ENABLED"] = "false"
os.environ["CHIP_AGENT_RUNTIME_MODE"] = "test"


@pytest.fixture
def tmp_path():
    """Provide a Windows-sandbox-safe temporary directory.

    Python 3.14 applies a user-only ACL for pytest's default 0o700 temporary
    directories on Windows. Sandboxed child processes can then lose access to
    a directory they just created, so tests use an isolated 0o777 directory
    under the system temporary root and remove it after each case when no
    Windows file handle remains open.
    """
    root = Path(gettempdir()) / "chip-design-agent-pytest"
    root.mkdir(mode=0o777, exist_ok=True)
    path = root / uuid4().hex
    path.mkdir(mode=0o777)
    try:
        yield path
    finally:
        shutil.rmtree(path, ignore_errors=True)
        try:
            root.rmdir()
        except OSError:
            pass

