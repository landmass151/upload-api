#!/usr/bin/env python3
"""
Télécharge, extrait et envoie des fichiers vers GoFile, FileDitch ou MultiUp.

Modes disponibles :
- archive    : télécharge chaque URL puis envoie le fichier complet ;
- desarchive : télécharge une archive, l'extrait récursivement et envoie
               chaque fichier autorisé séparément.

Variables d'environnement facultatives :

GoFile :
- GOFILE_TOKEN
- GOFILE_FOLDER_ID

MultiUp :
- MULTIUP_USERNAME
- MULTIUP_PASSWORD

GitHub Actions :
- GITHUB_OUTPUT
- GITHUB_STEP_SUMMARY
"""

from __future__ import annotations

import argparse
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

USER_AGENT = "Mozilla/5.0 (Android 15; Mobile; rv:136.0) Gecko/136.0 Firefox/136.0"

DEFAULT_TIMEOUT = 60
MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 10
CHUNK_SIZE = 10 * 1024 * 1024
MAX_FILE_SIZE = 150 * 1024 * 1024 * 1024

ESCAPE_TOKEN = "<echap>"

BLOCKED_EXTENSIONS = {
    ".php",
    ".php3",
    ".php4",
    ".php5",
    ".phtml",
    ".html",
    ".htm",
    ".js",
    ".mjs",
    ".cjs",
    ".exe",
    ".dll",
    ".com",
    ".scr",
    ".msi",
    ".apk",
    ".sh",
    ".bash",
    ".zsh",
    ".bat",
    ".cmd",
    ".ps1",
    ".py",
    ".pl",
    ".rb",
    ".cgi",
    ".jar",
    ".class",
    ".vbs",
    ".wsf",
}

ARCHIVE_SUFFIXES = (
    ".zip",
    ".7z",
    ".rar",
    ".tar",
    ".tar.gz",
    ".tgz",
    ".tar.bz2",
    ".tbz2",
    ".tar.xz",
    ".txz",
)


# ============================================================================
# GitHub Actions
# ============================================================================


def github_output(name: str, value: str) -> None:
    """Écrit une valeur dans le fichier GITHUB_OUTPUT."""
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
    """Ajoute les détails des uploads au résumé GitHub Actions."""
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
# Noms de fichiers et URLs
# ============================================================================


def clean_filename(filename: str) -> str:
    """Nettoie un nom de fichier fourni par une URL ou HTTP."""
    filename = unquote(filename)
    filename = filename.replace("\\", "_").replace("/", "_")
    filename = re.sub(r"[\x00-\x1f\x7f]", "_", filename)
    filename = filename.strip(" .")

    return filename[:240] or "downloaded_file"


def filename_from_url(url: str) -> str:
    """Déduit un nom de fichier depuis une URL."""
    filename = Path(unquote(urlparse(url).path)).name

    if not filename:
        return "downloaded_file"

    return clean_filename(filename)


def filename_from_response(
    response: requests.Response,
    url: str,
) -> str:
    """Détermine le nom depuis Content-Disposition, l'URL ou le MIME."""
    content_disposition = response.headers.get(
        "Content-Disposition",
        "",
    )

    match = re.search(
        r"filename\*=UTF-8''([^;]+)",
        content_disposition,
        flags=re.IGNORECASE,
    )

    if match:
        return clean_filename(match.group(1))

    match = re.search(
        r'filename="?([^";]+)"?',
        content_disposition,
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
    """Extrait les URLs HTTP et HTTPS uniques."""
    found_urls = re.findall(
        r"https?://[^\s]+",
        value,
        flags=re.IGNORECASE,
    )

    urls: list[str] = []

    for url in found_urls:
        url = url.strip().rstrip(",;")

        if url and url not in urls:
            urls.append(url)

    return urls


def parse_custom_filenames(
    value: str | None,
    escape_enabled: bool,
) -> list[str | None]:
    """Analyse les noms personnalisés séparés par des espaces."""
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


def get_filename(
    source_url: str,
    custom_filename: str | None,
) -> str:
    """Retourne le nom personnalisé ou celui détecté depuis l'URL."""
    if custom_filename and custom_filename.strip():
        return clean_filename(custom_filename)

    return filename_from_url(source_url)


def unique_filename(
    filename: str,
    used: set[str],
) -> str:
    """Évite les collisions de noms."""
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


# ============================================================================
# Téléchargement
# ============================================================================


def download_url(
    url: str,
    destination_dir: Path,
    timeout: int,
) -> Path:
    """Télécharge une URL dans un dossier temporaire."""
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
        destination = destination_dir / filename

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
                desc=filename,
                unit="B",
                unit_scale=True,
                unit_divisor=1024,
            ) as progress:
                for chunk in response.iter_content(
                    chunk_size=CHUNK_SIZE,
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
# Extraction sécurisée
# ============================================================================


def is_archive(path: Path) -> bool:
    """Indique si le fichier possède une extension d'archive supportée."""
    return path.name.lower().endswith(ARCHIVE_SUFFIXES)


def safe_path(root: Path, member_name: str) -> Path:
    """Empêche les chemins absolus et les traversées de répertoire."""
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
    """Extrait une archive ZIP en contrôlant chaque chemin."""
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
    """Extrait une archive TAR sans suivre les liens symboliques."""
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
    """Exécute une commande 7z."""
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
    """Teste puis extrait une archive 7z ou RAR."""
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
    """Sélectionne le moteur d'extraction adapté."""
    archive_name = archive.name.lower()

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
    """Retourne les fichiers ordinaires triés."""
    return [
        path
        for path in sorted(directory.rglob("*"))
        if path.is_file() and not path.is_symlink()
    ]


def extract_nested_archives(directory: Path) -> None:
    """Extrait récursivement les archives trouvées."""
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
    """Extrait l'archive principale et ses archives imbriquées."""
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
# Réponses API
# ============================================================================


def response_json(
    response: requests.Response,
    service: str,
) -> dict[str, Any]:
    """Convertit une réponse JSON en dictionnaire."""
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
    """Reconnaît les différentes valeurs de succès des APIs."""
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
    """Recherche récursivement une URL dans une réponse API."""
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
# GoFile
# ============================================================================


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
                    "application/octet-stream",
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

    size = payload.get("size", file_path.stat().st_size)

    return url, int(size)


# ============================================================================
# FileDitch
# ============================================================================


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
                "Content-Type": "application/octet-stream",
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

    size = payload.get("size", file_path.stat().st_size)

    return url, int(size)


# ============================================================================
# MultiUp
# ============================================================================


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
            f'Endpoint MultiUp invalide : {endpoint}'
        )

    return endpoint


@lru_cache(maxsize=1)
def get_multiup_user(timeout: int) -> str | None:
    """Connecte l'utilisateur à MultiUp si les identifiants sont présents."""
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
    """Envoie un fichier vers le serveur MultiUp le plus rapide."""
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
                    "application/octet-stream",
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

    size = payload.get("size", file_path.stat().st_size)

    return url, int(size or file_path.stat().st_size)


def upload_local_file(
    api: str,
    file_path: Path,
    filename: str,
    timeout: int,
) -> tuple[str, int]:
    """Sélectionne l'API d'upload."""
    uploaders = {
        "gofile": upload_gofile,
        "fileditch": upload_fileditch,
        "multiup": upload_multiup,
    }

    try:
        uploader = uploaders[api]
    except KeyError as error:
        raise ValueError(f"API inconnue : {api}") from error

    return uploader(file_path, filename, timeout)


# ============================================================================
# Traitement des uploads
# ============================================================================


def upload_with_retry(
    api: str,
    source_url: str,
    file_path: Path,
    filename: str,
    timeout: int,
) -> dict[str, Any]:
    """Envoie un fichier avec plusieurs tentatives."""
    last_error = ""

    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(
            f"\n[UPLOAD] {filename} "
            f"(tentative {attempt}/{MAX_ATTEMPTS})"
        )

        try:
            url, size = upload_local_file(
                api=api,
                file_path=file_path,
                filename=filename,
                timeout=timeout,
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
    api: str,
    urls: list[str],
    custom_names: list[str | None],
    timeout: int,
    temporary_dir: Path,
) -> list[dict[str, Any]]:
    """Télécharge et envoie chaque URL comme fichier complet."""
    uploads: list[dict[str, Any]] = []

    for index, url in enumerate(urls):
        custom_name = (
            custom_names[index]
            if index < len(custom_names)
            else None
        )

        filename = get_filename(url, custom_name)
        local_file = download_url(
            url=url,
            destination_dir=temporary_dir,
            timeout=timeout,
        )

        uploads.append(
            upload_with_retry(
                api=api,
                source_url=url,
                file_path=local_file,
                filename=filename,
                timeout=timeout,
            )
        )

    return uploads


def run_desarchive_mode(
    api: str,
    url: str,
    timeout: int,
    temporary_dir: Path,
) -> list[dict[str, Any]]:
    """Télécharge une archive et envoie ses fichiers autorisés."""
    archive = download_url(
        url=url,
        destination_dir=temporary_dir,
        timeout=timeout,
    )

    files = prepare_extracted_files(
        archive=archive,
        temporary_dir=temporary_dir,
    )

    uploads: list[dict[str, Any]] = []
    used_names: set[str] = set()

    for file_path in files:
        filename = unique_filename(
            filename=file_path.name,
            used=used_names,
        )

        uploads.append(
            upload_with_retry(
                api=api,
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
    """Analyse les arguments de ligne de commande."""
    parser = argparse.ArgumentParser(
        description=(
            "Télécharge, désarchive et envoie des fichiers "
            "vers GoFile, FileDitch ou MultiUp."
        )
    )

    parser.add_argument(
        "--mode",
        required=True,
        choices=("archive", "desarchive"),
        help="Mode d'exécution.",
    )

    parser.add_argument(
        "--api",
        required=True,
        choices=("gofile", "fileditch", "multiup"),
        help="API d'upload.",
    )

    parser.add_argument(
        "--source-urls",
        required=True,
        help="URLs HTTP/HTTPS séparées par des espaces.",
    )

    parser.add_argument(
        "--filenames",
        help=(
            "Noms personnalisés séparés par des espaces. "
            "Utilisez des guillemets pour les espaces."
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
    """Valide les arguments et leurs combinaisons."""
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

    if len(custom_names) > len(urls):
        raise ValueError(
            "Le nombre de noms personnalisés ne peut pas "
            "dépasser le nombre d'URLs."
        )

    if args.mode == "desarchive" and custom_names:
        print(
            "Avertissement : les noms personnalisés sont ignorés "
            "en mode desarchive.",
            file=sys.stderr,
        )


# ============================================================================
# Programme principal
# ============================================================================


def main() -> int:
    """Point d'entrée principal."""
    args = parse_arguments()
    urls = parse_urls(args.source_urls)

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
            prefix="upload_api_",
        ) as temporary:
            temporary_dir = Path(temporary)

            if args.mode == "archive":
                uploads = run_archive_mode(
                    api=args.api,
                    urls=urls,
                    custom_names=custom_names,
                    timeout=args.timeout,
                    temporary_dir=temporary_dir,
                )
            else:
                uploads = run_desarchive_mode(
                    api=args.api,
                    url=urls[0],
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
    sys.exit(main())
