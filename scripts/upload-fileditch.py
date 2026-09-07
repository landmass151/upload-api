from pathlib import Path

import requests

from upload_common import (
    FILEDITCH_ENDPOINT,
    USER_AGENT,
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
    headers = {
        "User-Agent": USER_AGENT,
        "Accept": "application/json, text/plain, */*",
        "Referer": "https://fileditch.com/",
        "Origin": "https://fileditch.com",
    }

    with requests.Session() as session:
        session.headers.update(headers)

        with file_path.open("rb") as file:
            response = session.post(
                FILEDITCH_ENDPOINT,
                params={"filename": filename},
                files={
                    "file": (
                        filename,
                        file,
                        "application/octet-stream",
                    ),
                },
                timeout=(timeout, 3600),
            )

        if response.status_code == 403:
            body = response.text[:500].replace("\n", " ")
            raise RuntimeError(
                f"FileDitch refuse la requête avec HTTP 403 : {body}"
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

        size = payload.get("size", file_path.stat().st_size)

        return str(url), int(size)
