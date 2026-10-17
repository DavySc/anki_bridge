"""One-time, by hand: pull your AnkiWeb collection onto this machine.

    ANKIWEB_USER=... ANKIWEB_PASS=... python bootstrap.py download

The server never does a full sync on its own. Use `upload` only if you are sure
this machine's collection should replace what is on AnkiWeb (it normally should
not).

Like Anki's own client, this makes a normal sync call first: AnkiWeb answers it with the
server that holds the account (`new_endpoint`), and the full transfer must go there.
`download` writes to a temporary file and moves it into place only when it worked, so a
failure never leaves an empty collection for the server to mistake for yours.
"""

import os
import sys

from anki.collection import Collection
from anki.sync_pb2 import SyncCollectionResponse

R = SyncCollectionResponse.ChangesRequired
FULL_KINDS = (R.FULL_SYNC, R.FULL_DOWNLOAD, R.FULL_UPLOAD)


def main(direction: str, path: str, env=os.environ) -> str:
    if direction not in ("download", "upload"):
        sys.exit("usage: bootstrap.py download|upload")
    if direction == "download" and os.path.exists(path):
        sys.exit(f"{path} already exists; a download would replace it. Move it away first if that is intended.")

    os.umask(0o077)  # the collection is private, like everything the server writes
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    work = path + ".part" if direction == "download" else path
    col = Collection(work)
    try:
        auth = col.sync_login(env["ANKIWEB_USER"], env["ANKIWEB_PASS"], env.get("ANKIWEB_ENDPOINT") or None)
        out = col.sync_collection(auth, sync_media=False)  # reports what is needed; moves no data when a full sync is
        if out.new_endpoint:
            auth.endpoint = out.new_endpoint
        kind = R.Name(out.required)
        print(f"AnkiWeb says: {kind}" + (" (using the server it named)" if out.new_endpoint else ""))
        if direction == "download" and out.required not in (R.FULL_SYNC, R.FULL_DOWNLOAD):
            sys.exit(f"Expected AnkiWeb to need a full download here, but it said {kind}. Nothing was changed.")
        if direction == "upload":
            if out.required not in FULL_KINDS:
                sys.exit(f"AnkiWeb says {kind}: a normal sync is enough, no upload needed. Nothing was changed.")
            if input("This OVERWRITES AnkiWeb. Type 'overwrite': ") != "overwrite":
                sys.exit("aborted")
        col.close_for_full_sync()
        col.full_upload_or_download(auth=auth, server_usn=None, upload=direction == "upload")
        col.reopen(after_full_sync=True)
    except BaseException:
        col.close()
        if direction == "download":
            for suffix in ("", "-wal", "-shm"):
                if os.path.exists(work + suffix):
                    os.remove(work + suffix)
        raise
    col.close()
    if direction == "download":
        os.replace(work, path)
    return f"{direction} done: {path}"


if __name__ == "__main__":
    default = os.environ.get("ANKI_COLLECTION") or os.path.join(
        os.environ.get("DATA_DIR", "./data"), "owner", "collection.anki2"
    )  # same default as server.py
    print(main(sys.argv[1] if len(sys.argv) > 1 else "", default))
