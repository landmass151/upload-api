#!/usr/bin/env python3

from pathlib import Path

import requests

from upload_common import (
    USER_AGENT,
    content_type_for,
    main,
    response_json,
)


FILEDITCH_ENDPOINT = "https://new.fileditch.com/upload.php"


def upload_fileditch(
    file_path: Path,
    filename: str,
    timeout: int,
) -> tuple[str, int]:
    """Envoie un fichier à FileDitch avec l'API raw upload."""

    file_size = file_path.stat().st_size

    with file_path.open("rb") as file:
        response = requests.put(
            FILEDITCH_ENDPOINT,
            params={"filename": filename},
            data=file,
            headers={
                "Content-Type": content_type_for(filename),
                "X-Filename": filename,
                "User-Agent": USER_AGENT,
            },
            # Timeout de connexion uniquement.
            # L'API raw de FileDitch n'impose pas de durée maximale.
            timeout=(timeout, None),
        )

    response.raise_for_status()

    payload = response_json(response, "FileDitch")

    if payload.get("success") is not True:
        raise RuntimeError(
            f"FileDitch a refusé l’upload : "
            f"{payload.get('error', payload)}"
        )

    url = payload.get("url")

    if not isinstance(url, str) or not url:
        raise RuntimeError(
            f"FileDitch n’a pas retourné d’URL : {payload}"
        )

    size = payload.get("size", file_size)

    try:
        size = int(size)
    except (TypeError, ValueError) as error:
        raise RuntimeError(
            f"Taille de fichier invalide retournée par FileDitch : "
            f"{payload}"
        ) from error

    return url, size


if __name__ == "__main__":
    raise SystemExit(
        main(
            api="fileditch",
            uploader=upload_fileditch,
        )
    )
