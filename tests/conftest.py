"""Point the app at a throwaway data dir so tests never touch ./data."""

import os
import tempfile

os.environ.setdefault("KITH_DATA_DIR", tempfile.mkdtemp(prefix="kith-test-"))
