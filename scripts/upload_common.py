#!/usr/bin/env python3

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
from collections.abc import Callable
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse

import requests
from tqdm import tqdm


GOFILE_SERVERS_ENDPOINT = "https://api.gofile.io/servers"
FILEDITCH_ENDPOINT = "https://new.fileditch.com/upload.php"

MULTIUP_FASTEST_SERVER_ENDPOINT = (
    "https://multiup.io/api/get-fastest-server"
)

MULTIUP_LOGIN_ENDPOINT = "https://multiup.io/api/login"

USER_AGENT = "Mozilla"

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

Uploader = Callable[
    [Path, str, int],
    tuple[str, int],
]


# ============================================================================
# GitHub Actions
# ============================================================================

def github_output(name: str, value: str) -> None:
    """Écrit une valeur dans GITHUB_OUTPUT."""
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
    """Ajoute les uploads au résumé GitHub Actions."""
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
# Fichiers et URLs
# ============================================================================

def clean_filename(filename: str) -> str:
    """Nettoie un nom de fichier."""
    filename = unquote(filename)
    filename = filename.replace("\\", "_").replace("/", "_")
    filename = re.sub(r"[\x00-\x1f\x7f]", "_", filename)
    filename = filename.strip(" .")

    return filename[:240] or "downloaded_file"


def content_type_for(filename: str) -> str:
    """Retourne le type MIME d'un fichier."""
    content_type, _ = mimetypes.guess_type(filename)

    return content_type or "application/octet-stream"


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
    """Détermine le nom du fichier depuis la réponse HTTP."""
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
    """Extrait les URLs HTTP et HTTPS."""
    found_urls = re.findall(
        r"https?://[^\s]+",
        value,
        flags=re.IGNORECASE,
    )

    urls: list[str] = []

    for url in found_urls:
        url = url.strip().rstrip(",;")

        if url:
            urls.append(url)

    return urls


def parse_custom_filenames(
    value: str | None,
    escape_enabled: bool,
) -> list[str | None]:
    """Analyse les noms personnalisés."""
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
    """Évite les collisions entre noms de fichiers."""
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
    """Retourne un chemin disponible."""
    filename = clean_filename(filename)
    candidate = directory / filename

    if not candidate.exists():
        return candidate

    path = Path(filename)
    counter = 2

    while True:
        candidate = directory / (
            f"{path.stem}_{counter}{path.suffix}"
        )

        if not candidate.exists():
            return candidate

        counter += 1


def is_archive(path: Path) -> bool:
    """Indique si un fichier est une archive supportée."""
    return path.name.lower().endswith(ARCHIVE_SUFFIXES)


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
# Extraction des archives
# ============================================================================

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
    """Extrait une archive ZIP de manière sécurisée."""
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
    """Extrait une archive TAR sans suivre les liens."""
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
    """Sélectionne le moteur d'extraction."""
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
    """Retourne les fichiers ordinaires triés."""
    return [
        path
        for path in sorted(directory.rglob("*"))
        if path.is_file() and not path.is_symlink()
    ]


def extract_nested_archives(directory: Path) -> None:
    """Extrait récursivement les archives imbriquées."""
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
    """Reconnaît les différentes valeurs indiquant un succès."""
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


def find_upload_url(
    payload: dict[str, Any],
) -> str | None:
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
# Traitement des uploads
# ============================================================================

def upload_with_retry(
    uploader: Uploader,
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
    api: str,
    urls: list[str],
    custom_names: list[str | None],
    timeout: int,
    temporary_dir: Path,
) -> list[dict[str, Any]]:
    """Télécharge toutes les URLs, crée un ZIP et l'envoie."""
    if not urls:
        raise ValueError("Aucune URL à archiver.")

    downloaded_files: list[tuple[str, Path]] = []

    for url in urls:
        local_file = download_url(
            url=url,
            destination_dir=temporary_dir,
            timeout=timeout,
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
                filename=local_file.name,
                used=used_inner_names,
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

    upload = upload_with_retry(
        uploader=uploader,
        source_url=source_urls,
        file_path=zip_path,
        filename=archive_filename,
        timeout=timeout,
    )

    return [upload]


def run_reupload_mode(
    uploader: Uploader,
    urls: list[str],
    custom_names: list[str | None],
    timeout: int,
    temporary_dir: Path,
) -> list[dict[str, Any]]:
    """Télécharge puis ré-envoie les fichiers sans les modifier."""
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
            url=url,
            destination_dir=temporary_dir,
            timeout=timeout,
        )

        custom_name = (
            custom_names[index]
            if custom_names
            else None
        )

        filename = unique_filename(
            filename=custom_name or local_file.name,
            used=used_names,
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
    """Analyse les arguments de ligne de commande."""
    parser = argparse.ArgumentParser(
        description=(
            "Télécharge, archive, désarchive, ré-uploade "
            "et envoie des fichiers."
        )
    )

    parser.add_argument(
        "--mode",
        required=True,
        choices=("archive", "desarchive", "re-upload"),
        help=(
            "archive = crée un ZIP ; "
            "desarchive = extrait une archive ; "
            "re-upload = ré-envoie les fichiers sans modification."
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
            "Nom personnalisé du ZIP final en mode archive. "
            "En mode re-upload, fournir un nom par URL, "
            "dans le même ordre."
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
    """Valide les arguments."""
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
            "Le mode archive accepte un seul nom "
            "pour le ZIP global."
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
# Entrée principale commune
# ============================================================================

def main(
    api: str,
    uploader: Uploader,
) -> int:
    """Point d'entrée commun aux trois APIs."""
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
            prefix=f"upload_{api}_",
        ) as temporary:
            temporary_dir = Path(temporary)

            if args.mode == "archive":
                uploads = run_archive_mode(
                    uploader=uploader,
                    api=api,
                    urls=urls,
                    custom_names=custom_names,
                    timeout=args.timeout,
                    temporary_dir=temporary_dir,
                )

            elif args.mode == "desarchive":
                uploads = run_desarchive_mode(
                    uploader=uploader,
                    api=api,
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
        api=api,
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
