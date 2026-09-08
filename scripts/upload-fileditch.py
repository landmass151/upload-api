#!/usr/bin/env python3

from pathlib import Path

import requests

from upload_common import (
    USER_AGENT,
    main,
)


FILEDITCH_ENDPOINT = "https://new.fileditch.com/upload.php"


def upload_fileditch(
    file_path: Path,
    filename: str,
    timeout: int,
) -> tuple[str, int]:
    """Upload brut vers FileDitch avec l'API actuelle."""

    if not file_path.is_file():
        raise FileNotFoundError(
            f"Fichier introuvable : {file_path}"
        )

    file_size = file_path.stat().st_size

    if file_size == 0:
        raise ValueError("FileDitch refuse les fichiers vides.")

    with file_path.open("rb") as file:
        response = requests.post(
            FILEDITCH_ENDPOINT,
            params={
                "filename": filename,
            },
            data=file,
            headers={
                # Upload brut, comme indiqué dans la documentation.
                "Content-Type": "application/octet-stream",
                "X-Filename": filename,
                "User-Agent": USER_AGENT,
            },
            timeout=(
                timeout,
                3600,
            ),
        )

    try:
        payload = response.json()
    except ValueError:
        payload = {
            "error": response.text[:1000],
        }

    if response.status_code == 403:
        raise RuntimeError(
            "FileDitch a bloqué ce fichier (HTTP 403). "
            f"Réponse du serveur : {payload}"
        )

    if response.status_code == 429:
        retry_after = response.headers.get("Retry-After", "inconnu")

        raise RuntimeError(
            "FileDitch limite les requêtes (HTTP 429). "
            f"Retry-After : {retry_after}"
        )

    if response.status_code >= 400:
        raise RuntimeError(
            f"FileDitch a refusé l'upload "
            f"(HTTP {response.status_code}) : {payload}"
        )

    if payload.get("success") is not True:
        raise RuntimeError(
            f"Réponse FileDitch invalide : {payload}"
        )

    url = payload.get("url")

    if not isinstance(url, str) or not url:
        raise RuntimeError(
            f"URL absente de la réponse FileDitch : {payload}"
        )

    size = int(payload.get("size", file_size))

    return url, size


if __name__ == "__main__":
    raise SystemExit(
        main(
            api="fileditch",
            uploader=upload_fileditch,
        )
    )
