#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import shlex
import subprocess
import sys
import time
from pathlib import PurePosixPath
from urllib.parse import quote, unquote, urlparse

import requests
from tqdm import tqdm


GOFILE_SERVERS_ENDPOINT = "https://api.gofile.io/servers"
FILEDITCH_ENDPOINT = "https://new.fileditch.com/upload.php"

MAX_ATTEMPTS = 3
CONNECT_TIMEOUT = "60"
CHUNK_SIZE = 1024 * 1024
ESCAPE_TOKEN = "<echap>"


def get_filename(
    source_url: str,
    custom_filename: str | None,
) -> str:
    """
    Retourne le nom personnalisé ou détecte le nom depuis l'URL.
    """

    if custom_filename and custom_filename.strip():
        filename = custom_filename.strip()
    else:
        parsed_url = urlparse(source_url)

        filename = unquote(
            PurePosixPath(parsed_url.path).name
        )

    if not filename:
        filename = "remote_file"

    filename = filename.replace("/", "_")
    filename = filename.replace("\\", "_")
    filename = filename.replace("\x00", "_")
    filename = filename.replace("\r", "_")
    filename = filename.replace("\n", "_")
    filename = filename.replace(";", "_")
    filename = filename.strip()

    return filename or "remote_file"


def parse_custom_filenames(
    raw_filenames: str | None,
    escape_enabled: bool,
) -> list[str | None]:
    """
    Analyse les noms personnalisés.

    <echap> représente une position sans nom personnalisé.
    Dans ce cas, le nom est détecté automatiquement depuis l'URL.

    Exemple :

        "archive Linux.zip" <echap> rapport.pdf

    Résultat :

        [
            "archive Linux.zip",
            None,
            "rapport.pdf",
        ]
    """

    if not raw_filenames or not raw_filenames.strip():
        return []

    try:
        filenames = shlex.split(
            raw_filenames,
            posix=True,
        )
    except ValueError as error:
        raise ValueError(
            "Syntaxe invalide pour les noms personnalisés. "
            "Vérifiez les guillemets."
        ) from error

    if not escape_enabled:
        return filenames

    parsed_filenames: list[str | None] = []

    for filename in filenames:
        if filename == ESCAPE_TOKEN:
            parsed_filenames.append(None)
        else:
            parsed_filenames.append(filename)

    return parsed_filenames


def write_github_output(
    name: str,
    value: str,
) -> None:
    """
    Écrit une valeur dans GITHUB_OUTPUT.
    """

    output_path = os.environ.get("GITHUB_OUTPUT")

    if not output_path:
        return

    with open(
        output_path,
        "a",
        encoding="utf-8",
    ) as output_file:
        output_file.write(f"{name}<<EOF\n")
        output_file.write(value)
        output_file.write("\nEOF\n")


def write_github_summary(
    api: str,
    file_url: str,
    filename: str,
    size: object,
) -> None:
    """
    Ajoute un résultat au résumé GitHub Actions.
    """

    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")

    if not summary_path:
        return

    with open(
        summary_path,
        "a",
        encoding="utf-8",
    ) as summary:
        summary.write("## Upload terminé\n\n")
        summary.write(f"- API : `{api}`\n")
        summary.write(f"- Fichier : `{filename}`\n")
        summary.write(f"- Taille : `{size}` octets\n")
        summary.write(f"- URL : {file_url}\n\n")


def get_gofile_server() -> str:
    """
    Récupère automatiquement un serveur GoFile disponible.
    """

    response = requests.get(
        GOFILE_SERVERS_ENDPOINT,
        timeout=60,
    )
    response.raise_for_status()

    payload = response.json()
    data = payload.get("data", {})
    servers = data.get("servers")

    if isinstance(servers, dict):
        values = servers.values()
    elif isinstance(servers, list):
        values = servers
    else:
        values = []

    for value in values:
        if isinstance(value, str) and value:
            return value

        if isinstance(value, dict):
            for key in (
                "name",
                "server",
                "hostname",
            ):
                if value.get(key):
                    return str(value[key])

    direct_server = data.get("server")

    if direct_server:
        return str(direct_server)

    raise RuntimeError(
        "Aucun serveur GoFile trouvé dans la réponse :\n"
        + json.dumps(
            payload,
            indent=2,
            ensure_ascii=False,
        )
    )


def escape_curl_form_filename(filename: str) -> str:
    """
    Échappe les caractères spéciaux pour curl --form.
    """

    return (
        filename
        .replace("\\", "\\\\")
        .replace('"', '\\"')
    )


def build_upload_command(
    api: str,
    filename: str,
) -> list[str]:
    """
    Construit la commande curl correspondant à l'API.
    """

    if api == "fileditch":
        upload_url = (
            f"{FILEDITCH_ENDPOINT}"
            f"?filename={quote(filename)}"
        )

        return [
            "curl",
            "-4",
            "--http1.1",
            "--request",
            "POST",
            "--data-binary",
            "@-",
            "--header",
            "Content-Type: application/octet-stream",
            "--header",
            f"X-Filename: {filename}",
            "--connect-timeout",
            CONNECT_TIMEOUT,
            "--max-time",
            "0",
            "--silent",
            "--show-error",
            upload_url,
        ]

    if api == "gofile":
        server = get_gofile_server()

        upload_url = (
            f"https://{server}.gofile.io"
            "/contents/uploadfile"
        )

        token = os.environ.get(
            "GOFILE_TOKEN",
            "",
        ).strip()

        folder_id = os.environ.get(
            "GOFILE_FOLDER_ID",
            "",
        ).strip()

        safe_filename = escape_curl_form_filename(
            filename
        )

        command = [
            "curl",
            "-4",
            "--http1.1",
            "--request",
            "POST",
            "--form",
            f"file=@-;filename={safe_filename}",
            "--connect-timeout",
            CONNECT_TIMEOUT,
            "--max-time",
            "0",
            "--silent",
            "--show-error",
        ]

        if token:
            command.extend(
                [
                    "--header",
                    f"Authorization: Bearer {token}",
                ]
            )

        if folder_id:
            command.extend(
                [
                    "--form",
                    f"folderId={folder_id}",
                ]
            )

        command.append(upload_url)

        return command

    raise ValueError(
        f"API inconnue : {api}"
    )


def upload_once(
    api: str,
    source_url: str,
    filename: str,
) -> tuple[int, str, str, int]:
    """
    Télécharge la source et envoie son contenu directement
    vers GoFile ou FileDitch sans fichier temporaire.
    """

    source_command = [
        "curl",
        "-4",
        "-L",
        "--fail",
        "--silent",
        "--show-error",
        "--connect-timeout",
        CONNECT_TIMEOUT,
        "--max-time",
        "0",
        source_url,
    ]

    upload_command = build_upload_command(
        api,
        filename,
    )

    print("Téléchargement de la source démarré.")
    print(f"API sélectionnée : {api}")

    source_process = subprocess.Popen(
        source_command,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    upload_process = subprocess.Popen(
        upload_command,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    if source_process.stdout is None:
        source_process.kill()
        upload_process.kill()

        return (
            1,
            "",
            "Impossible d'ouvrir la sortie source.",
            0,
        )

    if upload_process.stdin is None:
        source_process.kill()
        upload_process.kill()

        return (
            1,
            "",
            "Impossible d'ouvrir l'entrée de l'upload.",
            0,
        )

    uploaded_size = 0
    upload_broken = False

    try:
        with tqdm(
            desc="Transfert",
            unit="B",
            unit_scale=True,
            unit_divisor=1024,
            dynamic_ncols=True,
            file=sys.stderr,
        ) as progress:
            while True:
                chunk = source_process.stdout.read(
                    CHUNK_SIZE
                )

                if not chunk:
                    break

                try:
                    upload_process.stdin.write(chunk)
                    upload_process.stdin.flush()

                    uploaded_size += len(chunk)
                    progress.update(len(chunk))

                except BrokenPipeError:
                    upload_broken = True
                    break

    finally:
        try:
            source_process.stdout.close()
        except OSError:
            pass

        try:
            upload_process.stdin.close()
        except (BrokenPipeError, OSError):
            pass

    if upload_broken:
        if source_process.poll() is None:
            source_process.terminate()

    upload_process.stdin = None

    upload_stdout, upload_stderr = (
        upload_process.communicate()
    )

    source_stderr = (
        source_process.stderr.read()
        if source_process.stderr
        else b""
    )

    source_return_code = source_process.wait()

    upload_output = upload_stdout.decode(
        "utf-8",
        errors="replace",
    )

    upload_error = upload_stderr.decode(
        "utf-8",
        errors="replace",
    )

    source_error = source_stderr.decode(
        "utf-8",
        errors="replace",
    )

    # 141 correspond généralement à SIGPIPE.
    source_failed = source_return_code not in (0, 141)

    upload_failed = (
        upload_process.returncode != 0
        or upload_broken
    )

    if source_failed or upload_failed:
        errors = []

        if source_failed:
            errors.append(
                "Erreur du téléchargement source "
                f"(code {source_return_code})."
            )

            if source_error.strip():
                errors.append(source_error.strip())

        if upload_failed:
            errors.append(
                "Erreur de l'upload "
                f"(code {upload_process.returncode})."
            )

            if upload_error.strip():
                errors.append(upload_error.strip())

        return (
            1,
            upload_output,
            "\n".join(errors),
            uploaded_size,
        )

    return (
        0,
        upload_output,
        "",
        uploaded_size,
    )


def parse_response(
    api: str,
    response_text: str,
    filename: str,
    uploaded_size: int,
) -> tuple[str, str, object]:
    """
    Analyse la réponse JSON de l'API.
    """

    try:
        response = json.loads(response_text)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            "Réponse JSON invalide :\n"
            f"{response_text}"
        ) from error

    if api == "fileditch":
        if not response.get("success"):
            raise RuntimeError(
                "FileDitch a refusé l'upload :\n"
                + json.dumps(
                    response,
                    indent=2,
                    ensure_ascii=False,
                )
            )

        file_url = response.get("url")

        if not file_url:
            raise RuntimeError(
                "FileDitch n'a pas retourné d'URL :\n"
                + json.dumps(
                    response,
                    indent=2,
                    ensure_ascii=False,
                )
            )

        final_filename = response.get(
            "filename",
            filename,
        )

        file_size = response.get(
            "size",
            uploaded_size,
        )

        return (
            str(file_url),
            str(final_filename),
            file_size,
        )

    if api == "gofile":
        status = response.get("status")

        if status not in (None, "ok"):
            raise RuntimeError(
                "GoFile a refusé l'upload :\n"
                + json.dumps(
                    response,
                    indent=2,
                    ensure_ascii=False,
                )
            )

        data = response.get(
            "data",
            response,
        )

        if not isinstance(data, dict):
            raise RuntimeError(
                "Réponse GoFile invalide :\n"
                + json.dumps(
                    response,
                    indent=2,
                    ensure_ascii=False,
                )
            )

        file_url = (
            data.get("downloadPage")
            or data.get("downloadUrl")
            or data.get("directLink")
            or data.get("link")
        )

        if not file_url:
            raise RuntimeError(
                "GoFile n'a pas retourné de lien :\n"
                + json.dumps(
                    response,
                    indent=2,
                    ensure_ascii=False,
                )
            )

        final_filename = data.get(
            "name",
            data.get("filename", filename),
        )

        file_size = data.get(
            "size",
            uploaded_size,
        )

        return (
            str(file_url),
            str(final_filename),
            file_size,
        )

    raise RuntimeError(
        f"API inconnue : {api}"
    )


def parse_arguments() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Upload de plusieurs URLs vers GoFile "
            "ou FileDitch."
        )
    )

    parser.add_argument(
        "--api",
        required=True,
        choices=("gofile", "fileditch"),
        help="API à utiliser.",
    )

    parser.add_argument(
        "--source-urls",
        required=True,
        help="URLs directes séparées par des espaces.",
    )

    parser.add_argument(
        "--filenames",
        required=False,
        help=(
            "Noms personnalisés séparés par des espaces. "
            "Les noms contenant des espaces doivent être "
            "entre guillemets."
        ),
    )

    parser.add_argument(
        "--escape",
        action="store_true",
        help=(
            "Interprète <echap> comme une position sans "
            "nom personnalisé."
        ),
    )

    return parser.parse_args()


def main() -> int:
    args = parse_arguments()

    source_urls = args.source_urls.split()

    if not source_urls:
        print(
            "Erreur : aucune URL fournie.",
            file=sys.stderr,
        )
        return 1

    try:
        custom_filenames = parse_custom_filenames(
            args.filenames,
            args.escape,
        )
    except ValueError as error:
        print(
            f"Erreur : {error}",
            file=sys.stderr,
        )
        return 1

    if len(custom_filenames) > len(source_urls):
        print(
            "Erreur : le nombre de noms personnalisés "
            "est supérieur au nombre d'URL.",
            file=sys.stderr,
        )
        print(
            f"URL détectées : {len(source_urls)}",
            file=sys.stderr,
        )
        print(
            f"Noms détectés : {len(custom_filenames)}",
            file=sys.stderr,
        )
        return 1

    for source_url in source_urls:
        if not source_url.startswith(
            ("http://", "https://")
        ):
            print(
                f"URL invalide : {source_url}",
                file=sys.stderr,
            )
            return 1

    custom_count = sum(
        filename is not None
        for filename in custom_filenames
    )

    skipped_count = sum(
        filename is None
        for filename in custom_filenames
    )

    if custom_count:
        print(
            f"{custom_count} nom(s) personnalisé(s) "
            "détecté(s)."
        )

    if skipped_count:
        print(
            f"{skipped_count} position(s) utiliseront "
            "le nom détecté depuis l'URL."
        )

    remaining_count = (
        len(source_urls) - len(custom_filenames)
    )

    if remaining_count > 0:
        print(
            f"{remaining_count} fichier(s) restant(s) "
            "utiliseront le nom détecté depuis leur URL."
        )

    all_file_urls: list[str] = []

    for index, source_url in enumerate(
        source_urls,
        start=1,
    ):
        custom_filename: str | None = None
        custom_index = index - 1

        if custom_index < len(custom_filenames):
            custom_filename = custom_filenames[
                custom_index
            ]

        filename = get_filename(
            source_url,
            custom_filename,
        )

        print("\n" + "=" * 70)
        print(
            f"Fichier {index}/{len(source_urls)}"
        )
        print(f"URL source : {source_url}")
        print(f"Nom final  : {filename}")
        print(f"API        : {args.api}")
        print("=" * 70)

        last_error = ""
        upload_successful = False

        for attempt in range(1, MAX_ATTEMPTS + 1):
            print(
                f"\nTentative {attempt}/{MAX_ATTEMPTS}"
            )

            try:
                (
                    return_code,
                    response_text,
                    error_text,
                    uploaded_size,
                ) = upload_once(
                    args.api,
                    source_url,
                    filename,
                )

            except Exception as error:
                return_code = 1
                response_text = ""
                error_text = str(error)
                uploaded_size = 0

            if return_code == 0:
                try:
                    (
                        file_url,
                        final_filename,
                        file_size,
                    ) = parse_response(
                        args.api,
                        response_text,
                        filename,
                        uploaded_size,
                    )

                except RuntimeError as error:
                    print(
                        f"Erreur : {error}",
                        file=sys.stderr,
                    )
                    return 1

                print(
                    "\nUpload terminé avec succès."
                )
                print(f"URL     : {file_url}")
                print(
                    f"Fichier : {final_filename}"
                )
                print(
                    f"Taille  : {file_size} octets"
                )

                all_file_urls.append(file_url)

                write_github_summary(
                    args.api,
                    file_url,
                    final_filename,
                    file_size,
                )

                upload_successful = True
                break

            last_error = (
                error_text
                or "Erreur inconnue."
            )

            print(last_error)

            if attempt < MAX_ATTEMPTS:
                wait_seconds = attempt * 10

                print(
                    "Nouvelle tentative dans "
                    f"{wait_seconds} secondes..."
                )

                time.sleep(wait_seconds)

        if not upload_successful:
            print(
                f"\nÉchec définitif : {source_url}",
                file=sys.stderr,
            )
            print(
                last_error,
                file=sys.stderr,
            )
            return 1

    if not all_file_urls:
        print(
            "Aucun upload terminé.",
            file=sys.stderr,
        )
        return 1

    all_urls_text = "\n".join(all_file_urls)

    write_github_output(
        "file_urls",
        all_urls_text,
    )

    # Compatibilité avec l'ancien workflow.
    write_github_output(
        "file_url",
        all_file_urls[-1],
    )

    print("\n" + "=" * 70)
    print("Tous les uploads sont terminés.")
    print("=" * 70)

    for file_url in all_file_urls:
        print(file_url)

    return 0


if __name__ == "__main__":
    sys.exit(main())
