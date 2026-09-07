from __future__ import annotations

import argparse
import json
import mimetypes
import os
import re
import shlex
import shutil
import sys
import tempfile
import zipfile

from pathlib import Path
from urllib.parse import quote, unquote, urlsplit

import requests


DOWNLOAD_TIMEOUT = (30, 3600)
UPLOAD_TIMEOUT = (30, 3600)
CHUNK_SIZE = 1024 * 1024

http = requests.Session()
http.max_redirects = 10


class UploadError(Exception):
    pass


def read_urls(filename: str) -> list[str]:
    path = Path(filename)

    if not path.exists():
        raise UploadError(f"Fichier absent : {filename}")

    urls = []

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()

        if not line or line.startswith("#"):
            continue

        urls.append(line)

    if not urls:
        raise UploadError("Aucune URL dans urls.txt.")

    return urls


def read_names(filename: str, amount: int) -> list[str | None]:
    path = Path(filename)

    if not path.exists():
        return [None] * amount

    names = []

    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()

        if not line or line.lower() == "<echap>":
            names.append(None)
            continue

        try:
            parsed = shlex.split(line, posix=True)

            if len(parsed) == 1:
                line = parsed[0]

        except ValueError:
            line = line.strip('"').strip("'")

        line = line.strip().strip('"').strip("'").strip()
        names.append(line or None)

    names.extend([None] * max(0, amount - len(names)))

    return names[:amount]


def safe_filename(filename: str) -> str:
    filename = filename.replace("\x00", "")
    filename = filename.replace("/", "_").replace("\\", "_")
    filename = re.sub(r"[\r\n\t]", "_", filename)
    filename = Path(filename).name.strip()

    if not filename or filename in {".", ".."}:
        return "downloaded-file"

    return filename[:255]


def get_filename_from_response(
    response: requests.Response,
    source_url: str,
) -> str:
    content_disposition = response.headers.get(
        "Content-Disposition",
        "",
    )

    match = re.search(
        r"filename\*\s*=\s*UTF-8''([^;]+)",
        content_disposition,
        flags=re.IGNORECASE,
    )

    if match:
        return safe_filename(unquote(match.group(1)))

    match = re.search(
        r'filename\s*=\s*"([^"]+)"',
        content_disposition,
        flags=re.IGNORECASE,
    )

    if match:
        return safe_filename(match.group(1))

    match = re.search(
        r"filename\s*=\s*([^;]+)",
        content_disposition,
        flags=re.IGNORECASE,
    )

    if match:
        return safe_filename(match.group(1).strip())

    url_filename = Path(unquote(urlsplit(source_url).path)).name

    return safe_filename(url_filename or "downloaded-file")


def download_remote_file(
    url: str,
    directory: Path,
    custom_name: str | None,
) -> Path:
    print(f"Téléchargement : {url}")

    with http.get(
        url,
        stream=True,
        allow_redirects=True,
        timeout=DOWNLOAD_TIMEOUT,
        headers={
            "User-Agent": "GitHub-Remote-Uploader/1.0",
        },
    ) as response:
        response.raise_for_status()

        original_name = get_filename_from_response(response, url)
        filename = safe_filename(custom_name or original_name)
        output = directory / filename

        if output.exists():
            stem = output.stem
            suffix = output.suffix
            counter = 2

            while output.exists():
                output = directory / f"{stem}-{counter}{suffix}"
                counter += 1

        with output.open("wb") as file_handle:
            for chunk in response.iter_content(CHUNK_SIZE):
                if chunk:
                    file_handle.write(chunk)

    print(f"  Fichier local : {output.name}")
    return output


def parse_json_response(response: requests.Response) -> dict:
    try:
        data = response.json()

    except ValueError as error:
        raise UploadError(
            f"Réponse JSON invalide ({response.status_code}) : "
            f"{response.text[:300]}"
        ) from error

    if not response.ok:
        raise UploadError(
            f"Erreur HTTP {response.status_code} : "
            f"{json.dumps(data, ensure_ascii=False)}"
        )

    if not isinstance(data, dict):
        raise UploadError(
            "La réponse JSON n'est pas un objet : "
            f"{json.dumps(data, ensure_ascii=False)}"
        )

    return data


def collect_urls(value) -> list[str]:
    urls = []

    if isinstance(value, str):
        if value.startswith(("http://", "https://")):
            urls.append(value)

    elif isinstance(value, dict):
        for item in value.values():
            urls.extend(collect_urls(item))

    elif isinstance(value, list):
        for item in value:
            urls.extend(collect_urls(item))

    return urls


def is_success_response(data: dict) -> bool:
    """
    Plusieurs hébergeurs utilisent des formats différents.
    Multiup peut renvoyer {"error": "success"}.
    """

    error_value = str(data.get("error", "")).strip().lower()
    status_value = str(data.get("status", "")).strip().lower()

    if error_value in {"success", "ok"}:
        return True

    if status_value in {"success", "ok"}:
        return True

    return False


def upload_gofile(path: Path, remote_name: str | None) -> str:
    token = os.getenv("GOFILE_TOKEN")

    if not token:
        raise UploadError("Le secret GOFILE_TOKEN est absent.")

    filename = safe_filename(remote_name or path.name)

    with path.open("rb") as file_handle:
        response = http.post(
            "https://upload.gofile.io/uploadfile",
            headers={
                "Authorization": f"Bearer {token}",
                "Accept": "application/json",
            },
            files={
                "file": (
                    filename,
                    file_handle,
                    mimetypes.guess_type(filename)[0]
                    or "application/octet-stream",
                )
            },
            timeout=UPLOAD_TIMEOUT,
        )

    data = parse_json_response(response)

    if data.get("status") != "ok":
        raise UploadError(
            f"Gofile : {json.dumps(data, ensure_ascii=False)}"
        )

    result = data.get("data", {})

    return (
        result.get("downloadPage")
        or result.get("directLink")
        or result.get("code")
        or json.dumps(data, ensure_ascii=False)
    )


def upload_fileditch(path: Path, remote_name: str | None) -> str:
    filename = safe_filename(remote_name or path.name)

    upload_url = (
        "https://new.fileditch.com/upload.php"
        f"?filename={quote(filename, safe='')}"
    )

    with path.open("rb") as file_handle:
        response = http.put(
            upload_url,
            headers={
                "Content-Type": "application/octet-stream",
            },
            data=file_handle,
            timeout=UPLOAD_TIMEOUT,
        )

    data = parse_json_response(response)

    if data.get("success") is not True:
        raise UploadError(
            f"FileDitch : {json.dumps(data, ensure_ascii=False)}"
        )

    if not data.get("url"):
        raise UploadError("FileDitch n'a pas renvoyé d'URL.")

    return data["url"]


def upload_uploadg(path: Path, remote_name: str | None) -> str:
    token = os.getenv("UPLOADG_TOKEN")

    if not token:
        raise UploadError("Le secret UPLOADG_TOKEN est absent.")

    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/json",
    }

    size_response = http.get(
        "https://uploadg.com/api/v1/uploads/server-max-file-size",
        headers=headers,
        timeout=UPLOAD_TIMEOUT,
    )

    size_data = parse_json_response(size_response)
    max_size = size_data.get("maxSize")

    if isinstance(max_size, int) and path.stat().st_size > max_size:
        raise UploadError(
            "Le fichier dépasse la limite simple actuelle d'UploadG. "
            "Le multipart UploadG est nécessaire pour ce fichier."
        )

    filename = safe_filename(remote_name or path.name)

    with path.open("rb") as file_handle:
        response = http.post(
            "https://uploadg.com/api/v1/uploads",
            headers=headers,
            files={
                "file": (
                    filename,
                    file_handle,
                    mimetypes.guess_type(filename)[0]
                    or "application/octet-stream",
                )
            },
            timeout=UPLOAD_TIMEOUT,
        )

    data = parse_json_response(response)

    if data.get("status") != "success":
        raise UploadError(
            f"UploadG : {json.dumps(data, ensure_ascii=False)}"
        )

    urls = collect_urls(data)

    if urls:
        return urls[0]

    return json.dumps(data, ensure_ascii=False)


def multiup_login() -> str:
    username = os.getenv("MULTIUP_USERNAME")
    password = os.getenv("MULTIUP_PASSWORD")

    if not username or not password:
        raise UploadError(
            "Les secrets MULTIUP_USERNAME et MULTIUP_PASSWORD "
            "sont absents."
        )

    response = http.post(
        "https://multiup.io/api/login",
        data={
            "username": username,
            "password": password,
        },
        timeout=UPLOAD_TIMEOUT,
    )

    data = parse_json_response(response)

    # Multiup renvoie parfois :
    # {"error": "success", "user": "..."}
    error_value = str(data.get("error", "")).strip().lower()

    if error_value and error_value not in {"success", "ok"}:
        raise UploadError(f"Multiup login : {data['error']}")

    user_id = data.get("user")

    if not user_id:
        raise UploadError(
            "Identifiant Multiup absent : "
            f"{json.dumps(data, ensure_ascii=False)}"
        )

    return str(user_id)


def multiup_get_server() -> str:
    response = http.get(
        "https://multiup.io/api/get-fastest-server",
        timeout=UPLOAD_TIMEOUT,
    )

    data = parse_json_response(response)

    server = data.get("server")

    if not isinstance(server, str) or not server.strip():
        raise UploadError(
            "Serveur Multiup introuvable : "
            f"{json.dumps(data, ensure_ascii=False)}"
        )

    # Corrige les éventuels slashs échappés renvoyés par l'API.
    server = server.replace("\\/", "/").strip()

    if not server.startswith(("http://", "https://")):
        server = f"https://{server}"

    # L'API renvoie déjà :
    # https://cary.multiup.io/upload/index.php
    #
    # Il ne faut donc pas ajouter /upload/index.php ici.
    return server.rstrip("/")


def upload_multiup(path: Path, remote_name: str | None) -> str:
    user_id = multiup_login()
    upload_url = multiup_get_server()
    filename = safe_filename(remote_name or path.name)

    print(f"  Serveur Multiup : {upload_url}")

    with path.open("rb") as file_handle:
        response = http.post(
            upload_url,
            data={
                "user": user_id,
                "description": filename,
            },
            files={
                "files[]": (
                    filename,
                    file_handle,
                    mimetypes.guess_type(filename)[0]
                    or "application/octet-stream",
                )
            },
            timeout=UPLOAD_TIMEOUT,
        )

    data = parse_json_response(response)

    # Multiup peut utiliser "error": "success" comme indicateur
    # de réussite. Seules les autres valeurs sont considérées
    # comme des erreurs.
    error_value = str(data.get("error", "")).strip().lower()

    if error_value and error_value not in {"success", "ok"}:
        raise UploadError(
            f"Multiup : {json.dumps(data, ensure_ascii=False)}"
        )

    urls = collect_urls(data)

    if urls:
        return urls[0]

    # Si aucune URL n'est trouvée, on conserve la réponse complète
    # pour faciliter le diagnostic.
    return json.dumps(data, ensure_ascii=False)


UPLOADERS = {
    "gofile": upload_gofile,
    "fileditch": upload_fileditch,
    "uploadg": upload_uploadg,
    "multiup": upload_multiup,
}


def parse_providers(value: str) -> list[str]:
    providers = []

    for item in value.split(","):
        provider = item.strip().lower()

        if not provider:
            continue

        if provider not in UPLOADERS:
            raise UploadError(
                f"Hébergeur inconnu : {provider}. "
                f"Choix : {', '.join(UPLOADERS)}"
            )

        if provider not in providers:
            providers.append(provider)

    if not providers:
        raise UploadError("Aucun hébergeur sélectionné.")

    return providers


def upload_to_providers(
    path: Path,
    providers: list[str],
) -> list[dict]:
    results = []

    for provider in providers:
        print(f"Upload de {path.name} vers {provider}...")

        try:
            result = UPLOADERS[provider](path, path.name)

            print(f"  URL {provider} : {result}")

            results.append(
                {
                    "file": path.name,
                    "provider": provider,
                    "success": True,
                    "url": result,
                }
            )

        except Exception as error:
            print(
                f"  Échec {provider} : {error}",
                file=sys.stderr,
            )

            results.append(
                {
                    "file": path.name,
                    "provider": provider,
                    "success": False,
                    "error": str(error),
                }
            )

    return results


def create_uncompressed_zip(
    files: list[Path],
    destination: Path,
    archive_name: str,
) -> Path:
    filename = safe_filename(archive_name)

    if not filename.lower().endswith(".zip"):
        filename += ".zip"

    archive_path = destination / filename
    used_names = set()

    with zipfile.ZipFile(
        archive_path,
        mode="w",
        compression=zipfile.ZIP_STORED,
    ) as archive:
        for file_path in files:
            archive_entry_name = file_path.name
            stem = file_path.stem
            suffix = file_path.suffix
            counter = 2

            while archive_entry_name in used_names:
                archive_entry_name = f"{stem}-{counter}{suffix}"
                counter += 1

            used_names.add(archive_entry_name)
            archive.write(
                file_path,
                arcname=archive_entry_name,
            )

    return archive_path


def extract_zip_safely(
    archive_path: Path,
    destination: Path,
) -> list[Path]:
    extracted_files = []
    destination_root = destination.resolve()

    with zipfile.ZipFile(archive_path) as archive:
        for item in archive.infolist():
            target = (destination / item.filename).resolve()

            if not str(target).startswith(
                str(destination_root) + os.sep
            ):
                raise UploadError(
                    f"Chemin dangereux détecté dans le ZIP : "
                    f"{item.filename}"
                )

            if item.is_dir():
                continue

            target.parent.mkdir(
                parents=True,
                exist_ok=True,
            )

            with archive.open(item) as source:
                with target.open("wb") as output:
                    shutil.copyfileobj(source, output)

            extracted_files.append(target)

    return extracted_files


def run_reupload(
    urls: list[str],
    names: list[str | None],
    providers: list[str],
    workdir: Path,
) -> list[dict]:
    downloaded_dir = workdir / "downloaded"
    downloaded_dir.mkdir()

    results = []

    for index, url in enumerate(urls):
        file_path = download_remote_file(
            url,
            downloaded_dir,
            names[index],
        )

        results.extend(
            upload_to_providers(
                file_path,
                providers,
            )
        )

    return results


def run_archive(
    urls: list[str],
    names: list[str | None],
    providers: list[str],
    workdir: Path,
    archive_name: str,
) -> list[dict]:
    downloaded_dir = workdir / "downloaded"
    downloaded_dir.mkdir()

    files = []

    for index, url in enumerate(urls):
        file_path = download_remote_file(
            url,
            downloaded_dir,
            names[index],
        )

        files.append(file_path)

    archive_path = create_uncompressed_zip(
        files,
        workdir,
        archive_name,
    )

    print(
        f"ZIP créé sans compression : {archive_path.name}"
    )

    return upload_to_providers(
        archive_path,
        providers,
    )


def run_desarchive(
    urls: list[str],
    names: list[str | None],
    providers: list[str],
    workdir: Path,
) -> list[dict]:
    downloaded_dir = workdir / "downloaded"
    extracted_dir = workdir / "extracted"

    downloaded_dir.mkdir()
    extracted_dir.mkdir()

    results = []

    for index, url in enumerate(urls):
        archive_path = download_remote_file(
            url,
            downloaded_dir,
            names[index],
        )

        if not zipfile.is_zipfile(archive_path):
            print(
                f"{archive_path.name} n'est pas un ZIP. "
                "Upload du fichier original."
            )

            results.extend(
                upload_to_providers(
                    archive_path,
                    providers,
                )
            )

            continue

        target_dir = extracted_dir / f"archive-{index + 1}"
        target_dir.mkdir()

        extracted_files = extract_zip_safely(
            archive_path,
            target_dir,
        )

        for file_path in extracted_files:
            results.extend(
                upload_to_providers(
                    file_path,
                    providers,
                )
            )

    return results


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Remote uploader multi-hébergeurs."
    )

    parser.add_argument(
        "--mode",
        choices=(
            "re-upload",
            "archiver",
            "desarchiver",
        ),
        default="re-upload",
    )

    parser.add_argument(
        "--providers",
        required=True,
        help=(
            "Hébergeurs séparés par des virgules : "
            "gofile,fileditch,uploadg,multiup"
        ),
    )

    parser.add_argument(
        "--urls",
        default="urls.txt",
    )

    parser.add_argument(
        "--names",
        default="names.txt",
    )

    parser.add_argument(
        "--archive-name",
        default="remote-files.zip",
    )

    parser.add_argument(
        "--output",
        default="upload-results.json",
    )

    args = parser.parse_args()

    try:
        urls = read_urls(args.urls)
        names = read_names(
            args.names,
            len(urls),
        )
        providers = parse_providers(args.providers)

        with tempfile.TemporaryDirectory(
            prefix="remote-uploader-"
        ) as temporary:
            workdir = Path(temporary)

            if args.mode == "re-upload":
                results = run_reupload(
                    urls,
                    names,
                    providers,
                    workdir,
                )

            elif args.mode == "archiver":
                results = run_archive(
                    urls,
                    names,
                    providers,
                    workdir,
                    args.archive_name,
                )

            else:
                results = run_desarchive(
                    urls,
                    names,
                    providers,
                    workdir,
                )

        Path(args.output).write_text(
            json.dumps(
                results,
                indent=2,
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

        failed = sum(
            1
            for result in results
            if not result["success"]
        )

        print(
            f"Résultats enregistrés dans {args.output}"
        )

        if failed:
            print(
                f"{failed} upload(s) ont échoué.",
                file=sys.stderr,
            )

            return 1

        return 0

    except Exception as error:
        print(
            f"Erreur : {error}",
            file=sys.stderr,
        )

        return 1


if __name__ == "__main__":
    raise SystemExit(main())
