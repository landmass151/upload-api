#!/usr/bin/env python3

from __future__ import annotations

import os
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

import requests

from upload_common import (
    MULTIUP_FASTEST_SERVER_ENDPOINT,
    MULTIUP_LOGIN_ENDPOINT,
    USER_AGENT,
    content_type_for,
    find_upload_url,
    is_success_error,
    main,
    response_json,
)


def get_multiup_upload_endpoint(timeout: int) -> str:
    """Retourne l'endpoint MultiUp le plus rapide."""
    response = requests.get(
        MULTIUP_FASTEST_SERVER_ENDPOINT,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()

    payload = response_json(
        response,
        "MultiUp fastest server",
    )

    if not is_success_error(payload.get("error")):
        raise RuntimeError(
            f"MultiUp n'a pas retourné de serveur valide : {payload}"
        )

    endpoint = payload.get("server")

    if not isinstance(endpoint, str) or not endpoint.strip():
        raise RuntimeError(
            f"Le champ server est absent de la réponse MultiUp : {payload}"
        )

    endpoint = endpoint.strip()
    parsed_endpoint = urlparse(endpoint)

    if parsed_endpoint.scheme not in {"http", "https"}:
        raise RuntimeError(
            f"Schéma invalide pour l'endpoint MultiUp : {endpoint}"
        )

    if not parsed_endpoint.netloc:
        raise RuntimeError(
            f"Endpoint MultiUp invalide : {endpoint}"
        )

    return endpoint


@lru_cache(maxsize=1)
def get_multiup_user(timeout: int) -> str | None:
    """Connecte l'utilisateur à MultiUp si nécessaire."""
    username = os.environ.get("MULTIUP_USERNAME", "").strip()
    password = os.environ.get("MULTIUP_PASSWORD", "")

    if not username and not password:
        return None

    if not username or not password:
        raise ValueError(
            "MULTIUP_USERNAME et MULTIUP_PASSWORD doivent "
            "être définis ensemble."
        )

    response = requests.post(
        MULTIUP_LOGIN_ENDPOINT,
        data={
            "username": username,
            "password": password,
        },
        timeout=timeout,
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()

    payload = response_json(response, "MultiUp")

    if not is_success_error(payload.get("error")):
        raise RuntimeError(
            f"Connexion MultiUp refusée : {payload}"
        )

    user_id = payload.get("user")

    if user_id is None:
        raise RuntimeError(
            f"MultiUp n'a pas retourné d'identifiant utilisateur : {payload}"
        )

    return str(user_id)


def upload_multiup(
    file_path: Path,
    filename: str,
    timeout: int,
) -> tuple[str, int]:
    """Envoie un fichier vers MultiUp."""
    endpoint = get_multiup_upload_endpoint(timeout)
    user_id = get_multiup_user(timeout)

    data: dict[str, str] = {}

    if user_id:
        data["user"] = user_id

    with file_path.open("rb") as file:
        response = requests.post(
            endpoint,
            files={
                "files[]": (
                    filename,
                    file,
                    content_type_for(filename),
                )
            },
            data=data,
            timeout=(timeout, 3600),
            headers={"User-Agent": USER_AGENT},
        )

    response.raise_for_status()

    payload = response_json(response, "MultiUp")

    if not is_success_error(payload.get("error")):
        raise RuntimeError(
            f"MultiUp a refusé l'upload : {payload}"
        )

    url = find_upload_url(payload)

    if not url:
        raise RuntimeError(
            f"MultiUp n'a pas retourné de lien : {payload}"
        )

    size = payload.get(
        "size",
        file_path.stat().st_size,
    )

    return url, int(size or file_path.stat().st_size)


if __name__ == "__main__":
    raise SystemExit(
        main(
            api="multiup",
            uploader=upload_multiup,
        )
    )
