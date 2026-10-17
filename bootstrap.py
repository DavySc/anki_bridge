"""One-time, by hand: pull your AnkiWeb collection onto this machine.

    ANKIWEB_USER=... ANKIWEB_PASS=... python bootstrap.py download

The server never does a full sync on its own. Use `upload` only if you are sure
this machine's collection should replace what is on AnkiWeb (it normally should
not).
"""

import os
import sys

from anki.collection import Collection

path = os.environ.get("ANKI_COLLECTION") or os.path.join(
    os.environ.get("DATA_DIR", "./data"), "owner", "collection.anki2"
)  # same default as server.py
direction = sys.argv[1] if len(sys.argv) > 1 else ""
if direction not in ("download", "upload"):
    sys.exit("usage: bootstrap.py download|upload")

os.umask(0o077)  # the collection is private, like everything the server writes
os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
col = Collection(path)
auth = col.sync_login(
    os.environ["ANKIWEB_USER"],
    os.environ["ANKIWEB_PASS"],
    os.environ.get("ANKIWEB_ENDPOINT") or None,
)
if direction == "upload" and input("This OVERWRITES AnkiWeb. Type 'overwrite': ") != "overwrite":
    sys.exit("aborted")
col.close_for_full_sync()
col.full_upload_or_download(auth=auth, server_usn=None, upload=direction == "upload")
print(f"{direction} done: {path}")
