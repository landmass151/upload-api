#!/usr/bin/env python3

from pathlib import Path
from typing import Tuple

import requests

from upload_common import (
    USER_AGENT,
    content_type_for,
    main,
)


FILEDITCH_ENDPOINT = "https://new.fileditch.com/upload.php"


def upload_fileditch(
    file_path: Path,
    filename: str,
    timeout: int,
) -> Tuple[str, int]:
    """Upload brut vers la nouvelle API FileDitch."""

    if not file_path.exists():
        raise FileNotFoundError(
            f"Fichier introuvable : {file_path}"
        )

    if not file_path.is_file():
        raise ValueError(
            f"Le chemin n'est pas un fichier : {file_path}"
        )

    file_size = file_path.stat().st_size

    if file_size == 0:
        raise ValueError("FileDitch refuse les fichiers vides.")

    try:
        with file_path.open("rb") as file:
            response = requests.put(
                FILEDITCH_ENDPOINT,
                params={
                    "filename": filename,
                },
                data=file,
                headers={
                    "Content-Type": content_type_for(filename),
                    "X-Filename": filename,
                    "User-Agent": USER_AGENT,
                },
                timeout=(
                    timeout,
                    3600,
                ),
            )
    except requests.RequestException as error:
        raise RuntimeError(
            f"Erreur réseau pendant l'upload FileDitch : {error}"
        ) from error

    try:
        payload = response.json()
    except ValueError as error:
        raise RuntimeError(
            "FileDitch a retourné une réponse qui n'est pas du JSON : "
            f"{response.text[:500]}"
        ) from error

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
            f"URL absente dans la réponse FileDitch : {payload}"
        )

    size = payload.get("size", file_size)

    try:
        size = int(size)
    except (TypeError, ValueError) as error:
        raise RuntimeError(
            f"Taille invalide dans la réponse FileDitch : {payload}"
        ) from error

    return url, size


if __name__ == "__main__":
    raise SystemExit(
        main(
            api="fileditch",
            uploader=upload_fileditch,
        )
    )
