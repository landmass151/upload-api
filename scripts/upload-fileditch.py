#!/usr/bin/env python3

from pathlib import Path

import requests

from upload_common import (
    FILEDITCH_ENDPOINT,
    USER_AGENT,
    content_type_for,
    find_upload_url,
    main,
    response_json,
)


def upload_fileditch(
    file_path: Path,
    filename: str,
    timeout: int,
) -> tuple[str, int]:
    """Envoie un fichier vers FileDitch."""
    with file_path.open("rb") as file:
        response = requests.post(
            FILEDITCH_ENDPOINT,
            params={"filename": filename},
            data=file,
            headers={
                "Content-Type": content_type_for(filename),
                "X-Filename": filename,
                "User-Agent": USER_AGENT,
            },
            timeout=(timeout, 3600),
        )

    response.raise_for_status()

    payload = response_json(response, "FileDitch")

    if not payload.get("success"):
        raise RuntimeError(
            f"FileDitch a refusé l'upload : {payload}"
        )

    url = find_upload_url(payload)

    if not url:
        raise RuntimeError(
            f"FileDitch n'a pas retourné d'URL : {payload}"
        )

    size = payload.get(
        "size",
        file_path.stat().st_size,
    )

    return url, int(size)


if __name__ == "__main__":
    raise SystemExit(
        main(
            api="fileditch",
            uploader=upload_fileditch,
        )
    )

