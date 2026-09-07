#!/usr/bin/env python3

from __future__ import annotations

import argparse
import math
import mimetypes
import os
import re
import shlex
import shutil
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from collections.abc import Callable
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import requests
from tqdm import tqdm


# ============================================================================
# Configuration
# ============================================================================

GOFILE_SERVERS_ENDPOINT = "https://api.gofile.io/servers"
FILEDITCH_ENDPOINT = "https://new.fileditch.com/upload.php"

MULTIUP_FASTEST_SERVER_ENDPOINT = (
    "https://multiup.io/api/get-fastest-server"
)
MULTIUP_LOGIN_ENDPOINT = "https://multiup.io/api/login"

UPLOADG_BASE_URL = "https://uploadg.com/api/v1"
UPLOADG_CHUNK_SIZE = 25 * 1024 * 1024
UPLOADG_MAX_PARTS = 10_000

USER_AGENT = "Mozilla/5.0"
DEFAULT_TIMEOUT = 60
MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 10
DOWNLOAD_CHUNK_SIZE = 10 * 1024 * 1024
MAX_FILE_SIZE = 150 * 1024 * 1024 * 1024

ESCAPE_TOKEN = "<echap>"

BLOCKED_EXTENSIONS = {
    ".php", ".php3", ".php4", ".php5", ".phtml",
    ".html", ".htm", ".js", ".mjs", ".cjs",
    ".exe", ".dll", ".com", ".scr", ".msi",
    ".apk", ".sh", ".bash", ".zsh", ".bat",
    ".cmd", ".ps1", ".py", ".pl", ".rb",
    ".cgi", ".jar", ".class", ".vbs", ".wsf",
}

ARCHIVE_SUFFIXES = (
    ".tar.gz",
    ".tar.bz2",
    ".tar.xz",
    ".zip",
    ".7z",
    ".rar",
    ".tar",
    ".tgz",
    ".tbz2",
    ".txz",
)

Uploader = Callable[[Path, str, int], tuple[str, int]]


# ============================================================================
# Utilitaires
# ============================================================================

def content_type_for(filename: str) -> str:
    content_type, _ = mimetypes.guess_type(filename)
    return content_type or "application/octet-stream"


def clean_filename(filename: str) -> str:
    filename = unquote(filename)
    filename = filename.replace("\\", "_").replace("/", "_")
    filename = re.sub(r"[\x00-\x1f\x7f]", "_", filename)
    filename = filename.strip(" .")
    return filename[:240] or "downloaded_file"


def filename_from_url(url: str) -> str:
    name = Path(unquote(urlparse(url).path)).name
    return clean_filename(name or "downloaded_file")


def filename_from_response(
    response: requests.Response,
    url: str,
) -> str:
    disposition = response.headers.get("Content-Disposition", "")

    match = re.search(
        r"filename\*=UTF-8''([^;]+)",
        disposition,
        flags=re.IGNORECASE,
    )

    if match:
        return clean_filename(match.group(1))

    match = re.search(
        r'filename="?([^";]+)"?',
        disposition,
        flags=re.IGNORECASE,
    )

    if match:
        return clean_filename(match.group(1))

    filename = filename_from_url(url)

    if Path(filename).suffix:
        return filename

    content_type = response.headers.get("Content-Type", "")
    extension = mimetypes.guess_extension(
        content_type.split(";", 1)[0].strip(),
    )

    return filename + (extension or ".bin")


def parse_urls(value: str) -> list[str]:
    urls: list[str] = []

    for url in re.findall(r"https?://[^\s]+", value, re.IGNORECASE):
        url = url.strip().rstrip(",;")

        if url:
            urls.append(url)

    return urls


def parse_custom_filenames(
    value: str | None,
    escape_enabled: bool,
) -> list[str | None]:
    if not value or not value.strip():
        return []

    try:
        names = shlex.split(value, posix=True)
    except ValueError as error:
        raise ValueError(
            "Les noms personnalisés contiennent des guillemets invalides."
        ) from error

    if not escape_enabled:
        return names

    return [
        None if name == ESCAPE_TOKEN else name
        for name in names
    ]


def unique_filename(
    filename: str,
    used: set[str],
) -> str:
    filename = clean_filename(filename)

    if filename not in used:
        used.add(filename)
        return filename

    path = Path(filename)
    counter = 2

    while True:
        candidate = f"{path.stem}_{counter}{path.suffix}"

        if candidate not in used:
            used.add(candidate)
            return candidate

        counter += 1


def unique_download_path(
    directory: Path,
    filename: str,
) -> Path:
    filename = clean_filename(filename)
    candidate = directory / filename

    if not candidate.exists():
        return candidate

    path = Path(filename)
    counter = 2

    while True:
        candidate = directory / f"{path.stem}_{counter}{path.suffix}"

        if not candidate.exists():
            return candidate

        counter += 1


def is_archive(path: Path) -> bool:
    return path.name.lower().endswith(ARCHIVE_SUFFIXES)


def upload_timeout(timeout: int) -> tuple[int, None]:
    """
    Timeout réseau pour les uploads.

    Le délai de connexion est limité, mais aucun délai de lecture/écriture
    n'est imposé pendant l'envoi du fichier.
    """
    return max(timeout, 60), None


# ============================================================================
# Réponses API
# ============================================================================

def response_json(
    response: requests.Response,
    service: str,
) -> dict[str, Any]:
    try:
        payload = response.json()
    except ValueError as error:
        raise RuntimeError(
            f"Réponse {service} invalide : {response.text}"
        ) from error

    if not isinstance(payload, dict):
        raise RuntimeError(
            f"Réponse {service} inattendue : {payload}"
        )

    return payload


def is_success_error(value: Any) -> bool:
    return value in {
        None,
        "",
        False,
        0,
        "0",
        "ok",
        "OK",
        "success",
    }


def find_upload_url(payload: dict[str, Any]) -> str | None:
    direct_keys = (
        "link",
        "url",
        "downloadPage",
        "downloadUrl",
        "directLink",
        "download",
    )

    for key in direct_keys:
        value = payload.get(key)

        if value:
            return str(value)

    nested_values = (
        payload.get("data"),
        payload.get("file"),
        payload.get("fileEntry"),
        payload.get("files"),
        payload.get("result"),
    )

    for value in nested_values:
        if isinstance(value, dict):
            result = find_upload_url(value)

            if result:
                return result

        elif isinstance(value, list):
            for item in value:
                if isinstance(item, dict):
                    result = find_upload_url(item)

                    if result:
                        return result

    return None


# ============================================================================
# GitHub Actions
# ============================================================================

def github_output(name: str, value: str) -> None:
    output_file = os.environ.get("GITHUB_OUTPUT")

    if not output_file:
        return

    with open(output_file, "a", encoding="utf-8") as file:
        file.write(f"{name}<<EOF\n")
        file.write(value)
        file.write("\nEOF\n")


def github_summary(
    api: str,
    mode: str,
    uploads: list[dict[str, Any]],
) -> None:
    summary_file = os.environ.get("GITHUB_STEP_SUMMARY")

    if not summary_file:
        return

    with open(summary_file, "a", encoding="utf-8") as file:
        file.write("## Détails des uploads\n\n")
        file.write(f"- Mode : `{mode}`\n")
        file.write(f"- API : `{api}`\n")
        file.write(f"- Nombre de fichiers : `{len(uploads)}`\n\n")

        for upload in uploads:
            file.write(f"### {upload['filename']}\n\n")
            file.write(f"- Taille : `{upload['size']}` octets\n")
            file.write(f"- URL : {upload['url']}\n\n")


# ============================================================================
# Téléchargement
# ============================================================================

def download_url(
    url: str,
    destination_dir: Path,
    timeout: int,
) -> Path:
    parsed_url = urlparse(url)

    if parsed_url.scheme not in {"http", "https"}:
        raise ValueError(f"URL non supportée : {url}")

    print(f"\n[TÉLÉCHARGEMENT] {url}")

    with requests.get(
        url,
        stream=True,
        allow_redirects=True,
        timeout=(timeout, 3600),
        headers={"User-Agent": USER_AGENT},
    ) as response:
        response.raise_for_status()

        filename = filename_from_response(
            response,
            response.url,
        )

        destination = unique_download_path(
            destination_dir,
            filename,
        )

        content_length = response.headers.get("Content-Length")

        try:
            total = int(content_length) if content_length else None
        except ValueError:
            total = None

        if total is not None and total > MAX_FILE_SIZE:
            raise ValueError("Le fichier dépasse 150 Go.")

        written = 0

        with destination.open("wb") as output:
            with tqdm(
                total=total,
                desc=destination.name,
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
            ) as progress:
                for chunk in response.iter_content(
                    chunk_size=DOWNLOAD_CHUNK_SIZE,
                ):
                    if not chunk:
                        continue

                    written += len(chunk)

                    if written > MAX_FILE_SIZE:
                        raise ValueError("Le fichier dépasse 150 Go.")

                    output.write(chunk)
                    progress.update(len(chunk))

    if destination.stat().st_size == 0:
        raise ValueError("Le téléchargement est vide.")

    print(
        f"[TÉLÉCHARGEMENT OK] {destination.name} "
        f"({destination.stat().st_size:,} octets)"
    )

    return destination


# ============================================================================
# Extraction des archives
# ============================================================================

def safe_path(root: Path, member_name: str) -> Path:
    normalized_name = member_name.replace("\\", "/")
    relative_path = Path(normalized_name)

    if relative_path.is_absolute():
        raise ValueError(
            f"Chemin absolu interdit dans l'archive : {member_name}"
        )

    root = root.resolve()
    target = (root / relative_path).resolve()

    try:
        target.relative_to(root)
    except ValueError as error:
        raise ValueError(
            f"Chemin dangereux dans l'archive : {member_name}"
        ) from error

    return target


def extract_zip(
    archive: Path,
    output: Path,
) -> None:
    with zipfile.ZipFile(archive) as zip_file:
        for info in zip_file.infolist():
            destination = safe_path(output, info.filename)

            if info.is_dir():
                destination.mkdir(parents=True, exist_ok=True)
                continue

            destination.parent.mkdir(parents=True, exist_ok=True)

            with zip_file.open(info) as source:
                with destination.open("wb") as target:
                    shutil.copyfileobj(source, target)


def extract_tar(
    archive: Path,
    output: Path,
) -> None:
    with tarfile.open(archive, "r:*") as tar_file:
        for member in tar_file.getmembers():
            destination = safe_path(output, member.name)

            if member.issym() or member.islnk():
                print(f"[IGNORÉ] Lien symbolique : {member.name}")
                continue

            if member.isdir():
                destination.mkdir(parents=True, exist_ok=True)
                continue

            if not member.isfile():
                print(f"[IGNORÉ] Élément non fichier : {member.name}")
                continue

            source = tar_file.extractfile(member)

            if source is None:
                continue

            destination.parent.mkdir(parents=True, exist_ok=True)

            with source:
                with destination.open("wb") as target:
                    shutil.copyfileobj(source, target)


def run_7z(command: list[str]) -> None:
    result = subprocess.run(
        command,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
    )

    if result.returncode == 0:
        return

    message = (
        result.stderr.strip()
        or result.stdout.strip()
        or "Erreur inconnue de 7z."
    )

    raise RuntimeError(message)


def extract_7z_or_rar(
    archive: Path,
    output: Path,
) -> None:
    output.mkdir(parents=True, exist_ok=True)

    run_7z(
        [
            "7z",
            "t",
            str(archive),
            "-bd",
        ]
    )

    run_7z(
        [
            "7z",
            "x",
            str(archive),
            f"-o{output}",
            "-y",
            "-aoa",
            "-bd",
        ]
    )

    root = output.resolve()

    for path in output.rglob("*"):
        try:
            path.resolve().relative_to(root)
        except ValueError as error:
            raise RuntimeError(
                f"Chemin extrait dangereux : {path}"
            ) from error


def extract_archive(
    archive: Path,
    output: Path,
) -> None:
    archive_name = archive.name.lower()
    output.mkdir(parents=True, exist_ok=True)

    if archive_name.endswith(".zip"):
        extract_zip(archive, output)
        return

    if archive_name.endswith(
        (
            ".tar",
            ".tar.gz",
            ".tgz",
            ".tar.bz2",
            ".tbz2",
            ".tar.xz",
            ".txz",
        )
    ):
        extract_tar(archive, output)
        return

    if archive_name.endswith((".7z", ".rar")):
        extract_7z_or_rar(archive, output)
        return

    raise RuntimeError(
        f"Format d'archive non supporté : {archive.name}"
    )


def collect_files(directory: Path) -> list[Path]:
    return [
        path
        for path in sorted(directory.rglob("*"))
        if path.is_file() and not path.is_symlink()
    ]


def extract_nested_archives(directory: Path) -> None:
    processed: set[Path] = set()

    while True:
        archives = [
            path
            for path in collect_files(directory)
            if is_archive(path) and path not in processed
        ]

        if not archives:
            return

        for archive in archives:
            processed.add(archive)
            output = archive.parent / f"{archive.stem}_extracted"

            try:
                extract_archive(archive, output)
            except Exception as error:
                print(
                    f"[ERREUR EXTRACTION] {archive.name} : {error}",
                    file=sys.stderr,
                )


def prepare_extracted_files(
    archive: Path,
    temporary_dir: Path,
) -> list[Path]:
    extract_dir = temporary_dir / "extracted"
    extract_dir.mkdir(parents=True, exist_ok=True)

    extract_archive(archive, extract_dir)
    extract_nested_archives(extract_dir)

    files = [
        path
        for path in collect_files(extract_dir)
        if path.suffix.lower() not in BLOCKED_EXTENSIONS
        and not is_archive(path)
    ]

    if not files:
        raise ValueError(
            "Aucun fichier autorisé n'a été trouvé dans l'archive."
        )

    return files


# ============================================================================
# API GoFile
# ============================================================================

def get_gofile_server(timeout: int) -> str:
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
                value = item.get(key)

                if value:
                    return str(value)

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
    server = get_gofile_server(timeout)

    token = os.environ.get("GOFILE_TOKEN", "").strip()
    folder_id = os.environ.get("GOFILE_FOLDER_ID", "").strip()

    headers = {
        "User-Agent": USER_AGENT,
    }

    if token:
        headers["Authorization"] = f"Bearer {token}"

    data: dict[str, str] = {}

    if folder_id:
        data["folderId"] = folder_id

    print("[GOFILE] Méthode : POST")

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
            timeout=upload_timeout(timeout),
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

    size = payload.get("size", file_path.stat().st_size)

    return url, int(size or file_path.stat().st_size)


# ============================================================================
# API FileDitch
# ============================================================================

def upload_fileditch(
    file_path: Path,
    filename: str,
    timeout: int,
) -> tuple[str, int]:
    print("[FILEDITCH] Méthode : POST")

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
            timeout=upload_timeout(timeout),
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

    return url, int(size or file_path.stat().st_size)


# ============================================================================
# API MultiUp
# ============================================================================

def get_multiup_upload_endpoint(timeout: int) -> str:
    response = requests.get(
        MULTIUP_FASTEST_SERVER_ENDPOINT,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT},
    )

    response.raise_for_status()

    payload = response_json(response, "MultiUp")

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
    username = os.environ.get("MULTIUP_USERNAME", "").strip()
    password = os.environ.get("MULTIUP_PASSWORD", "")

    if not username and not password:
        return None

    if not username or not password:
        raise ValueError(
            "MULTIUP_USERNAME et MULTIUP_PASSWORD doivent être définis "
            "ensemble."
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
    endpoint = get_multiup_upload_endpoint(timeout)
    user_id = get_multiup_user(timeout)

    data: dict[str, str] = {}

    if user_id:
        data["user"] = user_id

    print("[MULTIUP] Méthode : POST")
    print(f"[MULTIUP] Endpoint : {endpoint}")

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
            headers={
                "User-Agent": USER_AGENT,
                "Accept": "application/json",
                "Connection": "keep-alive",
            },
            timeout=upload_timeout(timeout),
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

    size = payload.get("size", file_path.stat().st_size)

    return url, int(size or file_path.stat().st_size)


# ============================================================================
# API UploadG
# ============================================================================

def uploadg_api(
    path: str,
    payload: dict[str, Any],
    token: str,
    timeout: int,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
        "Content-Type": "application/json",
    }

    if idempotency_key:
        headers["Idempotency-Key"] = idempotency_key

    # POST : appels API UploadG de contrôle
    response = requests.post(
        f"{UPLOADG_BASE_URL}{path}",
        headers=headers,
        json=payload,
        timeout=max(timeout, 60),
    )

    response.raise_for_status()

    return response_json(response, "UploadG")


def upload_uploadg(
    file_path: Path,
    filename: str,
    timeout: int,
) -> tuple[str, int]:
    token = os.environ.get("UPLOADG_TOKEN", "").strip()

    if not token:
        raise ValueError(
            "La variable d'environnement UPLOADG_TOKEN est absente."
        )

    extension = Path(filename).suffix.lstrip(".")
    size = file_path.stat().st_size

    if size <= 0:
        raise ValueError(f"Le fichier est vide : {filename}")

    mime = (
        mimetypes.guess_type(filename)[0]
        or "application/octet-stream"
    )

    total_parts = math.ceil(size / UPLOADG_CHUNK_SIZE)

    if not 1 <= total_parts <= UPLOADG_MAX_PARTS:
        raise ValueError(
            "L'upload multipart nécessite entre 1 et 10 000 parties."
        )

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

                    if (
                        isinstance(part_number, int)
                        and isinstance(url, str)
                    ):
                        urls[part_number] = url

                for part_number in part_numbers:
                    url = urls.get(part_number)

                    if not url:
                        raise RuntimeError(
                            f"URL signée absente pour la partie "
                            f"{part_number}."
                        )

                    chunk = source.read(UPLOADG_CHUNK_SIZE)

                    if not chunk:
                        raise IOError(
                            "Fin inattendue du fichier pendant l'upload."
                        )

                    # PUT : obligatoire pour l'URL signée de la partie S3
                    response = requests.put(
                        url,
                        data=chunk,
                        headers={
                            "Content-Length": str(len(chunk)),
                        },
                        timeout=upload_timeout(timeout),
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

        url = find_upload_url({"fileEntry": file_entry})

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


# ============================================================================
# Traitement des uploads
# ============================================================================

def upload_with_retry(
    uploader: Uploader,
    source_url: str,
    file_path: Path,
    filename: str,
    timeout: int,
) -> dict[str, Any]:
    last_error = ""

    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(
            f"\n[UPLOAD] {filename} "
            f"(tentative {attempt}/{MAX_ATTEMPTS})"
        )

        try:
            url, size = uploader(
                file_path,
                filename,
                timeout,
            )

            print(f"[UPLOAD OK] {url}")

            return {
                "filename": filename,
                "url": url,
                "size": size,
                "source_url": source_url,
            }

        except Exception as error:
            last_error = str(error)

            print(
                f"[UPLOAD ERREUR] {last_error}",
                file=sys.stderr,
            )

            if attempt < MAX_ATTEMPTS:
                delay = attempt * RETRY_DELAY_SECONDS

                print(
                    f"Nouvelle tentative dans {delay} secondes..."
                )

                time.sleep(delay)

    raise RuntimeError(
        f"Échec définitif de l'upload de {filename} : {last_error}"
    )


def run_archive_mode(
    uploader: Uploader,
    urls: list[str],
    custom_names: list[str | None],
    timeout: int,
    temporary_dir: Path,
) -> list[dict[str, Any]]:
    if not urls:
        raise ValueError("Aucune URL à archiver.")

    downloaded_files: list[tuple[str, Path]] = []

    for url in urls:
        local_file = download_url(
            url,
            temporary_dir,
            timeout,
        )

        downloaded_files.append((url, local_file))

    if custom_names and custom_names[0]:
        archive_filename = clean_filename(custom_names[0])

        if not archive_filename.lower().endswith(".zip"):
            archive_filename += ".zip"
    else:
        archive_filename = "archive.zip"

    zip_dir = temporary_dir / "generated_zips"
    zip_dir.mkdir(parents=True, exist_ok=True)

    zip_path = zip_dir / archive_filename
    used_inner_names: set[str] = set()

    with zipfile.ZipFile(
        zip_path,
        mode="w",
        compression=zipfile.ZIP_STORED,
        allowZip64=True,
    ) as archive:
        for _source_url, local_file in downloaded_files:
            inner_filename = unique_filename(
                local_file.name,
                used_inner_names,
            )

            archive.write(
                local_file,
                arcname=inner_filename,
            )

    print(
        f"\n[ZIP CRÉÉ] {archive_filename} "
        f"({len(downloaded_files)} fichier(s))"
    )

    source_urls = "\n".join(
        source_url
        for source_url, _local_file in downloaded_files
    )

    return [
        upload_with_retry(
            uploader=uploader,
            source_url=source_urls,
            file_path=zip_path,
            filename=archive_filename,
            timeout=timeout,
        )
    ]


def run_reupload_mode(
    uploader: Uploader,
    urls: list[str],
    custom_names: list[str | None],
    timeout: int,
    temporary_dir: Path,
) -> list[dict[str, Any]]:
    if not urls:
        raise ValueError("Aucune URL à ré-uploader.")

    if custom_names and len(custom_names) != len(urls):
        raise ValueError(
            "En mode re-upload, il faut fournir exactement "
            "un nom par URL."
        )

    uploads: list[dict[str, Any]] = []
    used_names: set[str] = set()

    for index, url in enumerate(urls):
        local_file = download_url(
            url,
            temporary_dir,
            timeout,
        )

        custom_name = custom_names[index] if custom_names else None

        filename = unique_filename(
            custom_name or local_file.name,
            used_names,
        )

        uploads.append(
            upload_with_retry(
                uploader=uploader,
                source_url=url,
                file_path=local_file,
                filename=filename,
                timeout=timeout,
            )
        )

    return uploads


def run_desarchive_mode(
    uploader: Uploader,
    url: str,
    timeout: int,
    temporary_dir: Path,
) -> list[dict[str, Any]]:
    archive = download_url(
        url,
        temporary_dir,
        timeout,
    )

    files = prepare_extracted_files(
        archive,
        temporary_dir,
    )

    uploads: list[dict[str, Any]] = []
    used_names: set[str] = set()

    for file_path in files:
        filename = unique_filename(
            file_path.name,
            used_names,
        )

        uploads.append(
            upload_with_retry(
                uploader=uploader,
                source_url=url,
                file_path=file_path,
                filename=filename,
                timeout=timeout,
            )
        )

    return uploads


# ============================================================================
# Arguments
# ============================================================================

def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Télécharge, archive, désarchive, ré-uploade "
            "et envoie des fichiers."
        )
    )

    parser.add_argument(
        "--api",
        required=True,
        choices=("gofile", "fileditch", "multiup", "uploadg"),
        help="API utilisée pour l'upload.",
    )

    parser.add_argument(
        "--mode",
        required=True,
        choices=("archive", "desarchive", "re-upload"),
        help=(
            "archive = crée un ZIP ; "
            "desarchive = extrait une archive ; "
            "re-upload = ré-envoie les fichiers."
        ),
    )

    parser.add_argument(
        "--source-urls",
        required=True,
        help="URLs HTTP/HTTPS séparées par des espaces.",
    )

    parser.add_argument(
        "--filenames",
        help=(
            "Nom du ZIP final en mode archive. "
            "En mode re-upload, fournir un nom par URL."
        ),
    )

    parser.add_argument(
        "--escape",
        action="store_true",
        help=(
            f"Interprète {ESCAPE_TOKEN} comme "
            "aucun nom personnalisé."
        ),
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
        help="Timeout réseau en secondes.",
    )

    return parser.parse_args()


def validate_arguments(
    args: argparse.Namespace,
    urls: list[str],
    custom_names: list[str | None],
) -> None:
    if args.timeout <= 0:
        raise ValueError(
            "Le timeout doit être supérieur à zéro."
        )

    if not urls:
        raise ValueError(
            "Aucune URL HTTP ou HTTPS valide."
        )

    if args.mode == "desarchive" and len(urls) != 1:
        raise ValueError(
            "Le mode desarchive nécessite exactement une URL."
        )

    if args.mode == "archive" and len(custom_names) > 1:
        raise ValueError(
            "Le mode archive accepte un seul nom pour le ZIP global."
        )

    if args.mode == "re-upload":
        if custom_names and len(custom_names) != len(urls):
            raise ValueError(
                "Le mode re-upload nécessite exactement "
                "un nom par URL."
            )

    if args.mode == "desarchive" and custom_names:
        print(
            "Avertissement : le nom personnalisé est ignoré "
            "en mode desarchive.",
            file=sys.stderr,
        )


# ============================================================================
# Programme principal
# ============================================================================

UPLOADERS: dict[str, Uploader] = {
    "gofile": upload_gofile,
    "fileditch": upload_fileditch,
    "multiup": upload_multiup,
    "uploadg": upload_uploadg,
}


def main() -> int:
    args = parse_arguments()

    urls = parse_urls(args.source_urls)
    uploader = UPLOADERS[args.api]

    try:
        custom_names = parse_custom_filenames(
            value=args.filenames,
            escape_enabled=args.escape,
        )

        validate_arguments(
            args=args,
            urls=urls,
            custom_names=custom_names,
        )

        with tempfile.TemporaryDirectory(
            prefix=f"upload_{args.api}_",
        ) as temporary:
            temporary_dir = Path(temporary)

            if args.mode == "archive":
                uploads = run_archive_mode(
                    uploader=uploader,
                    urls=urls,
                    custom_names=custom_names,
                    timeout=args.timeout,
                    temporary_dir=temporary_dir,
                )

            elif args.mode == "desarchive":
                uploads = run_desarchive_mode(
                    uploader=uploader,
                    url=urls[0],
                    timeout=args.timeout,
                    temporary_dir=temporary_dir,
                )

            else:
                uploads = run_reupload_mode(
                    uploader=uploader,
                    urls=urls,
                    custom_names=custom_names,
                    timeout=args.timeout,
                    temporary_dir=temporary_dir,
                )

    except Exception as error:
        print(
            f"\nErreur définitive : {error}",
            file=sys.stderr,
        )
        return 1

    if not uploads:
        print(
            "Aucun upload terminé.",
            file=sys.stderr,
        )
        return 1

    file_urls = "\n".join(
        upload["url"]
        for upload in uploads
    )

    github_output("file_urls", file_urls)
    github_output("file_url", uploads[-1]["url"])

    github_summary(
        api=args.api,
        mode=args.mode,
        uploads=uploads,
    )

    print("\n" + "=" * 70)
    print("Tous les uploads sont terminés.")
    print("=" * 70)

    for upload in uploads:
        print(
            f"{upload['filename']} -> {upload['url']}"
        )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
