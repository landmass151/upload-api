#!/usr/bin/env python3

from pathlib import Path

import requests

from upload_common import (
    FILEDITCH_ENDPOINT,
    USER_AGENT,
    content_type_for,
    main,
    response_json,
)


def upload_fileditch(
    file_path: Path,
    filename: str,
    timeout: int,
) -> tuple[str, int]:
    """Envoie un fichier vers la nouvelle API FileDitch."""

    if not file_path.is_file():
        raise FileNotFoundError(f"Fichier introuvable : {file_path}")

    file_size = file_path.stat().st_size

    if file_size == 0:
        raise ValueError("FileDitch refuse les fichiers vides.")

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
            timeout=(timeout, 3600),
        )

    # Les erreurs FileDitch sont généralement renvoyées en JSON.
    if not response.ok:
        try:
            payload = response.json()
        except ValueError:
            payload = response.text

        raise RuntimeError(
            f"FileDitch a refusé l'upload "
            f"(HTTP {response.status_code}) : {payload}"
        )

    payload = response_json(response, "FileDitch")

    if payload.get("success") is not True:
        raise RuntimeError(
            f"Réponse inattendue de FileDitch : {payload}"
        )

    url = payload.get("url")
    if not isinstance(url, str) or not url:
        raise RuntimeError(
            f"FileDitch n'a pas retourné d'URL : {payload}"
        )

    size = payload.get("size", file_size)

    try:
        size = int(size)
    except (TypeError, ValueError) as exc:
        raise RuntimeError(
            f"Taille invalide retournée par FileDitch : {payload}"
        ) from exc

    return url, size


if __name__ == "__main__":
    raise SystemExit(
        main(
            api="fileditch",
            uploader=upload_fileditch,
        )
    )
