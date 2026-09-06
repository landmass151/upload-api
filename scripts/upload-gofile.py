#!/usr/bin/env python3

from __future__ import annotations

import os
from pathlib import Path

import requests

from upload_common import (
    GOFILE_SERVERS_ENDPOINT,
    USER_AGENT,
    content_type_for,
    find_upload_url,
    main,
    response_json,
)


def get_gofile_server(timeout: int) -> str:
    """Retourne un serveur GoFile disponible."""
    response = requests.get(
        GOFILE_SERVERS_ENDPOINT,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()

    payload = response_json(response, "GoFile")
    data = payload.get("data", {})
    servers = data.get("servers", [])

    values = servers.values() if isinstance(servers, dict) else servers

    for item in values:
        if isinstance(item, str) and item:
            return item

        if isinstance(item, dict):
            for key in ("name", "server", "hostname"):
                if item.get(key):
                    return str(item[key])

    if data.get("server"):
        return str(data["server"])

    raise RuntimeError(
        "Aucun serveur GoFile n'a été retourné."
    )


def upload_gofile(
    file_path: Path,
    filename: str,
    timeout: int,
) -> tuple[str, int]:
    """Envoie un fichier vers GoFile."""
    server = get_gofile_server(timeout)

    token = os.environ.get("GOFILE_TOKEN", "").strip()
    folder_id = os.environ.get("GOFILE_FOLDER_ID", "").strip()

    headers: dict[str, str] = {}

    if token:
        headers["Authorization"] = f"Bearer {token}"

    data: dict[str, str] = {}

    if folder_id:
        data["folderId"] = folder_id

    with file_path.open("rb") as file:
        response = requests.post(
            f"https://{server}.gofile.io/contents/uploadfile",
            files={
                "file": (
                    filename,
                    file,
                    content_type_for(filename),
                )
            },
            data=data,
            headers=headers,
            timeout=(timeout, 3600),
        )

    response.raise_for_status()

    payload = response_json(response, "GoFile")

    if payload.get("status") not in (None, "ok"):
        raise RuntimeError(
            f"GoFile a refusé l'upload : {payload}"
        )

    url = find_upload_url(payload)

    if not url:
        raise RuntimeError(
            f"GoFile n'a pas retourné de lien : {payload}"
        )

    size = payload.get(
        "size",
        file_path.stat().st_size,
    )

    return url, int(size)


if __name__ == "__main__":
    raise SystemExit(
        main(
            api="gofile",
            uploader=upload_gofile,
        )
    )
