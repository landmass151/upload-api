#!/usr/bin/env python3

from __future__ import annotations

import math
import mimetypes
import os
from pathlib import Path

import requests

from upload_common import (
    response_json,
    find_upload_url,
    main,
)


UPLOADG_BASE_URL = "https://uploadg.com/api/v1"
UPLOADG_CHUNK_SIZE = 50 * 1024 * 1024
UPLOADG_MAX_PARTS = 10_000


def uploadg_api(
    path: str,
    payload: dict,
    token: str,
    timeout: int,
    idempotency_key: str | None = None,
) -> dict:
    """Appelle l'API UploadG."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key

    response = requests.post(
        f"{UPLOADG_BASE_URL}{path}",
        headers=headers,
        json=payload,
        timeout=timeout,
    )
    response.raise_for_status()

    return response_json(response, "UploadG")


def upload_uploadg(
    file_path: Path,
    filename: str,
    timeout: int,
) -> tuple[str, int]:
    """Envoie un fichier vers UploadG par multipart upload."""
    token = os.environ.get("UPLOADG_TOKEN", "").strip()

    if not token:
        raise ValueError(
            "La variable d'environnement UPLOADG_TOKEN est absente."
        )

    extension = Path(filename).suffix.lstrip(".")
    size = file_path.stat().st_size

    if size <= 0:
        raise ValueError(
            f"Le fichier est vide : {filename}"
        )

    mime = (
       mimetypes.guess_type(filename)[0]
       or "application/octet-stream"
    )

    total_parts = math.ceil(size / UPLOADG_CHUNK_SIZE)

    if not 1 <= total_parts <= UPLOADG_MAX_PARTS:
        raise ValueError(
            "L'upload multipart nécessite entre 1 et 10 000 parties."
        )

    # Ces clés doivent être propres à chaque upload.
    create_key = os.urandom(16).hex()
    complete_key = os.urandom(16).hex()
    entry_key = os.urandom(16).hex()

    session = uploadg_api(
        path="/s3/multipart/create",
        payload={
            "filename": filename,
            "mime": mime,
            "size": size,
            "extension": extension,
        },
        token=token,
        timeout=timeout,
        idempotency_key=create_key,
    )

    try:
        key = session["key"]
        upload_id = session["uploadId"]
        upload_token = session["uploadToken"]
    except KeyError as error:
        raise RuntimeError(
            f"Réponse de création UploadG invalide : {session}"
        ) from error

    assembled = False
    parts: list[dict[str, str | int]] = []

    try:
        with file_path.open("rb") as source:
            for batch_start in range(
                1,
                total_parts + 1,
                100,
            ):
                part_numbers = list(
                    range(
                        batch_start,
                        min(batch_start + 100, total_parts + 1),
                    )
                )

                signed = uploadg_api(
                    path="/s3/multipart/batch-sign-part-urls",
                    payload={
                        "key": key,
                        "uploadId": upload_id,
                        "uploadToken": upload_token,
                        "partNumbers": part_numbers,
                    },
                    token=token,
                    timeout=timeout,
                )

                signed_urls = signed.get("urls")

                if not isinstance(signed_urls, list):
                    raise RuntimeError(
                        "UploadG n'a pas retourné de liste d'URLs signées."
                    )

                urls: dict[int, str] = {}

                for item in signed_urls:
                    if not isinstance(item, dict):
                        continue

                    part_number = item.get("partNumber")
                    url = item.get("url")

                    if isinstance(part_number, int) and isinstance(
                        url,
                        str,
                    ):
                        urls[part_number] = url

                for part_number in part_numbers:
                    url = urls.get(part_number)

                    if not url:
                        raise RuntimeError(
                            "URL signée absente pour la partie "
                            f"{part_number}."
                        )

                    chunk = source.read(UPLOADG_CHUNK_SIZE)

                    if not chunk:
                        raise IOError(
                            "Fin inattendue du fichier pendant l'upload."
                        )

                    response = requests.put(
                        url,
                        data=chunk,
                        timeout=max(timeout, 300),
                    )
                    response.raise_for_status()

                    etag = response.headers.get("ETag")

                    if not etag:
                        raise IOError(
                            f"Aucun ETag retourné pour la partie "
                            f"{part_number}."
                        )

                    parts.append(
                        {
                            "ETag": etag,
                            "PartNumber": part_number,
                        }
                    )

        uploadg_api(
            path="/s3/multipart/complete",
            payload={
                "key": key,
                "uploadId": upload_id,
                "uploadToken": upload_token,
                "parts": parts,
            },
            token=token,
            timeout=timeout,
            idempotency_key=complete_key,
        )

        assembled = True

        result = uploadg_api(
            path="/s3/entries",
            payload={
                "clientName": filename,
                "clientExtension": extension,
                "clientMime": mime,
                "filename": key.rsplit("/", 1)[-1],
                "key": key,
                "uploadId": upload_id,
                "uploadToken": upload_token,
                "size": size,
            },
            token=token,
            timeout=timeout,
            idempotency_key=entry_key,
        )

        file_entry = result.get("fileEntry")

        if not isinstance(file_entry, dict):
            raise RuntimeError(
                f"UploadG n'a pas retourné de fileEntry : {result}"
            )

        url = find_upload_url(
            {
                "fileEntry": file_entry,
            }
        )

        if not url:
            raise RuntimeError(
                "UploadG a terminé l'upload mais n'a pas retourné "
                f"d'URL publique : {result}"
            )

        return url, size

    except Exception:
        if not assembled:
            try:
                uploadg_api(
                    path="/s3/multipart/abort",
                    payload={
                        "key": key,
                        "uploadId": upload_id,
                        "uploadToken": upload_token,
                    },
                    token=token,
                    timeout=timeout,
                )
            except Exception:
                pass

        raise


if __name__ == "__main__":
    raise SystemExit(
        main(
            api="uploadg",
            uploader=upload_uploadg,
        )
    )
