#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
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
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse

import requests
from tqdm import tqdm


GOFILE_SERVERS_ENDPOINT = "https://api.gofile.io/servers"
FILEDITCH_ENDPOINT = "https://new.fileditch.com/upload.php"
MULTIUP_ENDPOINT = "https://multiup.io/api/remote-upload"

USER_AGENT = "github-actions-upload-api/1.0"

MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 10
CHUNK_SIZE = 1024 * 1024
MAX_FILE_SIZE = 150 * 1024 * 1024 * 1024

DEFAULT_TIMEOUT = 60
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

        for item in uploads:
            file.write(f"### {item['filename']}\n\n")
            file.write(f"- Taille : `{item['size']}` octets\n")
            file.write(f"- URL : {item['url']}\n\n")


# ============================================================================
# Noms de fichiers et URLs
# ============================================================================


def clean_filename(filename: str) -> str:
    filename = unquote(filename)
    filename = filename.replace("\\", "_")
    filename = filename.replace("/", "_")
    filename = re.sub(r"[\x00-\x1f\x7f]", "_", filename)
    filename = filename.strip(" .")

    if not filename:
        return "downloaded_file"

    return filename[:240]


def filename_from_url(url: str) -> str:
    name = Path(unquote(urlparse(url).path)).name

    if not name:
        return "downloaded_file"

    return clean_filename(name)


def parse_urls(value: str) -> list[str]:
    urls = re.findall(
        r"https?://[^\s]+",
        value,
        flags=re.IGNORECASE,
    )

    result: list[str] = []

    for url in urls:
        url = url.strip().rstrip(",;")

        if url and url not in result:
            result.append(url)

    return result


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


def get_filename(
    source_url: str,
    custom_filename: str | None,
) -> str:
    if custom_filename and custom_filename.strip():
        return clean_filename(custom_filename)

    return filename_from_url(source_url)


# ============================================================================
# Téléchargement
# ============================================================================


def download_url(
    url: str,
    destination_dir: Path,
    timeout: int,
) -> Path:
    parsed = urlparse(url)

    if parsed.scheme not in {"http", "https"}:
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


def filename_from_response(
    response: requests.Response,
    url: str,
) -> str:
    header = response.headers.get("Content-Disposition", "")

    match = re.search(
        r"filename\*=UTF-8''([^;]+)",
        header,
        flags=re.IGNORECASE,
    )

    if match:
        return clean_filename(match.group(1))

    match = re.search(
        r'filename="?([^";]+)"?',
        header,
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


# ============================================================================
# Extraction sécurisée
# ============================================================================


def is_archive(path: Path) -> bool:
    return path.name.lower().endswith(
        (
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
    )


def safe_path(root: Path, member_name: str) -> Path:
    member_name = member_name.replace("\\", "/")
    relative = Path(member_name)

    if relative.is_absolute():
        raise ValueError(f"Chemin absolu interdit : {member_name}")

    root = root.resolve()
    target = (root / relative).resolve()

    try:
        target.relative_to(root)
    except ValueError as error:
        raise ValueError(
            f"Chemin dangereux dans l'archive : {member_name}"
        ) from error

    return target


def extract_zip(archive: Path, output: Path) -> None:
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


def extract_tar(archive: Path, output: Path) -> None:
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

    if result.returncode != 0:
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


def extract_archive(archive: Path, output: Path) -> None:
    name = archive.name.lower()

    if name.endswith(".zip"):
        extract_zip(archive, output)
    elif name.endswith(".tar") or name.endswith(
        (
            ".tar.gz",
            ".tgz",
            ".tar.bz2",
            ".tbz2",
            ".tar.xz",
            ".txz",
        )
    ):
        extract_tar(archive, output)
    elif name.endswith((".7z", ".rar")):
        extract_7z_or_rar(archive, output)
    else:
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
                    f"[ERREUR EXTRACTION] "
                    f"{archive.name} : {error}"
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
# APIs d'upload
# ============================================================================


def get_gofile_server(timeout: int) -> str:
    response = requests.get(
        GOFILE_SERVERS_ENDPOINT,
        timeout=timeout,
        headers={"User-Agent": USER_AGENT},
    )
    response.raise_for_status()

    payload = response.json()
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
    server = get_gofile_server(timeout)

    token = os.environ.get("GOFILE_TOKEN", "").strip()
    folder_id = os.environ.get("GOFILE_FOLDER_ID", "").strip()

    headers = {}

    if token:
        headers["Authorization"] = f"Bearer {token}"

    data = {}

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
    payload = response.json()

    if payload.get("status") not in (None, "ok"):
        raise RuntimeError(
            f"GoFile a refusé l'upload : {payload}"
        )

    data = payload.get("data", payload)

    if not isinstance(data, dict):
        raise RuntimeError("Réponse GoFile invalide.")

    url = (
        data.get("downloadPage")
        or data.get("downloadUrl")
        or data.get("directLink")
        or data.get("link")
    )

    if not url:
        raise RuntimeError(
            f"GoFile n'a pas retourné de lien : {payload}"
        )

    size = data.get("size", file_path.stat().st_size)

    return str(url), int(size)


def upload_fileditch(
    file_path: Path,
    filename: str,
    timeout: int,
) -> tuple[str, int]:
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

    try:
        payload = response.json()
    except ValueError as error:
        raise RuntimeError(
            f"Réponse FileDitch invalide : {response.text}"
        ) from error

    if not payload.get("success"):
        raise RuntimeError(
            f"FileDitch a refusé l'upload : {payload}"
        )

    url = payload.get("url")

    if not url:
        raise RuntimeError(
            f"FileDitch n'a pas retourné d'URL : {payload}"
        )

    size = payload.get("size", file_path.stat().st_size)

    return str(url), int(size)


def upload_multiup(
    source_url: str,
    filename: str,
    timeout: int,
) -> tuple[str, int]:
    payload = {
        "link": source_url,
        "fileName": filename,
    }

    username = os.environ.get("MULTIUP_USERNAME", "").strip()
    password = os.environ.get("MULTIUP_PASSWORD", "")

    if username:
        payload["username"] = username

    if password:
        payload["password"] = password

    response = requests.post(
        MULTIUP_ENDPOINT,
        data=payload,
        timeout=(timeout, 3600),
        headers={"User-Agent": USER_AGENT},
    )

    response.raise_for_status()
    result = response.json()

    error = result.get("error")

    if error not in (
        None,
        "",
        False,
        0,
        "0",
        "ok",
        "OK",
        "success",
    ):
        raise RuntimeError(
            f"MultiUp a refusé l'upload : {result}"
        )

    url = result.get("link")

    if not url:
        raise RuntimeError(
            f"MultiUp n'a pas retourné de lien : {result}"
        )

    size = result.get("size", 0)

    return str(url), int(size or 0)


def upload_local_file(
    api: str,
    file_path: Path,
    filename: str,
    timeout: int,
) -> tuple[str, int]:
    if api == "gofile":
        return upload_gofile(file_path, filename, timeout)

    if api == "fileditch":
        return upload_fileditch(file_path, filename, timeout)

    raise ValueError(
        "MultiUp ne peut pas envoyer un fichier local."
    )


# ============================================================================
# Traitement
# ============================================================================


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


def upload_with_retry(
    api: str,
    mode: str,
    source_url: str,
    file_path: Path | None,
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
            if api == "multiup":
                if mode != "archive":
                    raise ValueError(
                        "MultiUp est disponible uniquement "
                        "en mode archive."
                    )

                url, size = upload_multiup(
                    source_url,
                    filename,
                    timeout,
                )
            else:
                if file_path is None:
                    raise ValueError(
                        "Fichier local manquant pour l'upload."
                    )

                url, size = upload_local_file(
                    api,
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
        f"Échec définitif de l'upload de {filename} : "
        f"{last_error}"
    )


def run_archive_mode(
    api: str,
    urls: list[str],
    custom_names: list[str | None],
    timeout: int,
    temporary_dir: Path,
) -> list[dict[str, Any]]:
    uploads = []

    for index, url in enumerate(urls):
        custom_name = (
            custom_names[index]
            if index < len(custom_names)
            else None
        )

        filename = get_filename(url, custom_name)

        if api == "multiup":
            uploads.append(
                upload_with_retry(
                    api=api,
                    mode="archive",
                    source_url=url,
                    file_path=None,
                    filename=filename,
                    timeout=timeout,
                )
            )
            continue

        local_file = download_url(
            url,
            temporary_dir,
            timeout,
        )

        uploads.append(
            upload_with_retry(
                api=api,
                mode="archive",
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
    if api == "multiup":
        raise ValueError(
            "L'API MultiUp n'est pas compatible avec "
            "le mode desarchive."
        )

    archive = download_url(
        url,
        temporary_dir,
        timeout,
    )

    files = prepare_extracted_files(
        archive,
        temporary_dir,
    )

    uploads = []
    used_names: set[str] = set()

    for file_path in files:
        filename = unique_filename(
            file_path.name,
            used_names,
        )

        uploads.append(
            upload_with_retry(
                api=api,
                mode="desarchive",
                source_url=url,
                file_path=file_path,
                filename=filename,
                timeout=timeout,
            )
        )

    return uploads


# ============================================================================
# Programme principal
# ============================================================================


def parse_arguments() -> argparse.Namespace:
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
    )

    parser.add_argument(
        "--api",
        required=True,
        choices=("gofile", "fileditch", "multiup"),
    )

    parser.add_argument(
        "--source-urls",
        required=True,
        help="URLs HTTP/HTTPS séparées par des espaces.",
    )

    parser.add_argument(
        "--filenames",
        help="Noms personnalisés séparés par des espaces.",
    )

    parser.add_argument(
        "--escape",
        action="store_true",
        help="Interprète <echap> comme aucun nom personnalisé.",
    )

    parser.add_argument(
        "--timeout",
        type=int,
        default=DEFAULT_TIMEOUT,
    )

    return parser.parse_args()


def main() -> int:
    args = parse_arguments()

    if args.timeout <= 0:
        print(
            "Le timeout doit être supérieur à zéro.",
            file=sys.stderr,
        )
        return 1

    urls = parse_urls(args.source_urls)

    if not urls:
        print(
            "Aucune URL HTTP ou HTTPS valide.",
            file=sys.stderr,
        )
        return 1

    if args.mode == "desarchive" and len(urls) != 1:
        print(
            "Le mode desarchive nécessite exactement une URL.",
            file=sys.stderr,
        )
        return 1

    try:
        custom_names = parse_custom_filenames(
            args.filenames,
            args.escape,
        )
    except ValueError as error:
        print(f"Erreur : {error}", file=sys.stderr)
        return 1

    if len(custom_names) > len(urls):
        print(
            "Le nombre de noms personnalisés ne peut pas "
            "dépasser le nombre d'URLs.",
            file=sys.stderr,
        )
        return 1

    if args.mode == "desarchive" and custom_names:
        print(
            "Les noms personnalisés sont ignorés en mode desarchive.",
            file=sys.stderr,
        )

    try:
        with tempfile.TemporaryDirectory(
            prefix="upload_api_"
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
        print(f"\nErreur définitive : {error}", file=sys.stderr)
        return 1

    if not uploads:
        print(
            "Aucun upload terminé.",
            file=sys.stderr,
        )
        return 1

    urls_text = "\n".join(
        item["url"]
        for item in uploads
    )

    github_output("file_urls", urls_text)
    github_output("file_url", uploads[-1]["url"])

    github_summary(
        api=args.api,
        mode=args.mode,
        uploads=uploads,
    )

    print("\n" + "=" * 70)
    print("Tous les uploads sont terminés.")
    print("=" * 70)

    for item in uploads:
        print(
            f"{item['filename']} -> {item['url']}"
        )

    return 0


if __name__ == "__main__":
    sys.exit(main())
