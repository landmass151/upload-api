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
from typing import Any
from urllib.parse import quote, unquote, urlparse

import requests
from tqdm import tqdm


# ============================================================================
# CONFIGURATION
# ============================================================================

GOFILE_SERVERS_ENDPOINT = "https://api.gofile.io/servers"
FILEDITCH_ENDPOINT = "https://new.fileditch.com/upload.php"
MULTIUP_ENDPOINT = "https://multiup.io/api/remote-upload"


# Liste complète des hébergeurs autorisés pour MultiUp.
#
# La sélection effective est effectuée avec la variable d'environnement
# MULTIUP_HOSTS.
#
# Exemples :
#
#   MULTIUP_HOSTS=all
#   MULTIUP_HOSTS="1fichier.com"
#   MULTIUP_HOSTS="1fichier.com, FireLoad.com"
#   MULTIUP_HOSTS="1fichier.com FireLoad.com"
AVAILABLE_MULTIUP_HOSTS = (
    "1fichier.com",
    "fireLoad.com",
    "hexload.com",
    "rapidgator.net",
    "vikingfile.com",
)


MAX_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 10
CHUNK_SIZE = 10 * 1024 * 1024
CONNECT_TIMEOUT = 60
ESCAPE_TOKEN = "<echap>"
ALLOWED_URL_SCHEMES = ("http://", "https://")


# ============================================================================
# OUTILS GÉNÉRAUX
# ============================================================================


def get_environment_value(name: str) -> str:
    """
    Retourne une variable d'environnement nettoyée.
    """

    return os.environ.get(name, "").strip()


def print_json_error(prefix: str, payload: object) -> str:
    """
    Formate proprement une erreur contenant une réponse JSON.
    """

    return (
        f"{prefix}\n"
        + json.dumps(
            payload,
            indent=2,
            ensure_ascii=False,
        )
    )


def decode_bytes(value: bytes) -> str:
    """
    Convertit des octets en texte UTF-8 sans provoquer d'erreur.
    """

    return value.decode(
        "utf-8",
        errors="replace",
    )


def is_valid_source_url(source_url: str) -> bool:
    """
    Vérifie que l'URL utilise HTTP ou HTTPS.
    """

    return source_url.startswith(ALLOWED_URL_SCHEMES)


# ============================================================================
# GESTION DES NOMS DE FICHIERS
# ============================================================================


def clean_filename(filename: str) -> str:
    """
    Nettoie un nom de fichier pour éviter les caractères problématiques.
    """

    replacements = {
        "/": "_",
        "\\": "_",
        "\x00": "_",
        "\r": "_",
        "\n": "_",
        ";": "_",
    }

    for old_value, new_value in replacements.items():
        filename = filename.replace(old_value, new_value)

    filename = filename.strip()

    return filename or "remote_file"


def get_filename(
    source_url: str,
    custom_filename: str | None,
) -> str:
    """
    Retourne le nom personnalisé ou détecte le nom depuis l'URL.
    """

    if custom_filename and custom_filename.strip():
        return clean_filename(custom_filename)

    parsed_url = urlparse(source_url)

    detected_filename = unquote(
        PurePosixPath(parsed_url.path).name
    )

    if not detected_filename:
        detected_filename = "remote_file"

    return clean_filename(detected_filename)


def parse_custom_filenames(
    raw_filenames: str | None,
    escape_enabled: bool,
) -> list[str | None]:
    """
    Analyse la liste des noms personnalisés.

    Exemple :

        "archive Linux.zip" <echap> rapport.pdf

    Donne :

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

    return [
        None if filename == ESCAPE_TOKEN else filename
        for filename in filenames
    ]


# ============================================================================
# SÉLECTION DES HÉBERGEURS MULTIUP
# ============================================================================


def get_selected_multiup_hosts() -> tuple[str, ...]:
    """
    Retourne les hébergeurs MultiUp sélectionnés.

    La variable MULTIUP_HOSTS accepte :

        all

    ou une liste séparée par des virgules :

        1fichier.com, FireLoad.com

    ou une liste séparée par des espaces :

        1fichier.com FireLoad.com

    Les noms sont comparés sans tenir compte de la casse.
    """

    raw_hosts = get_environment_value("MULTIUP_HOSTS")

    if not raw_hosts:
        return AVAILABLE_MULTIUP_HOSTS

    requested_hosts = [
        item.strip()
        for item in raw_hosts.replace(",", " ").split()
        if item.strip()
    ]

    if not requested_hosts:
        return AVAILABLE_MULTIUP_HOSTS

    if any(
        requested_host.lower() == "all"
        for requested_host in requested_hosts
    ):
        if len(requested_hosts) > 1:
            raise ValueError(
                'La valeur "all" ne peut pas être combinée '
                "avec un autre hébergeur."
            )

        return AVAILABLE_MULTIUP_HOSTS

    available_by_lowercase = {
        host.lower(): host
        for host in AVAILABLE_MULTIUP_HOSTS
    }

    selected_hosts: list[str] = []
    unknown_hosts: list[str] = []

    for requested_host in requested_hosts:
        canonical_host = available_by_lowercase.get(
            requested_host.lower()
        )

        if canonical_host is None:
            unknown_hosts.append(requested_host)
            continue

        if canonical_host not in selected_hosts:
            selected_hosts.append(canonical_host)

    if unknown_hosts:
        raise ValueError(
            "Hébergeur(s) MultiUp inconnu(s) : "
            + ", ".join(unknown_hosts)
            + "\nHébergeurs disponibles : "
            + ", ".join(AVAILABLE_MULTIUP_HOSTS)
        )

    if not selected_hosts:
        raise ValueError(
            "Aucun hébergeur MultiUp valide n'a été sélectionné."
        )

    return tuple(selected_hosts)


# ============================================================================
# SORTIES GITHUB ACTIONS
# ============================================================================


def write_github_output(
    name: str,
    value: str,
) -> None:
    """
    Écrit une valeur dans GITHUB_OUTPUT.

    Le format EOF autorise les valeurs multilignes.
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
    Ajoute les informations d'un upload au résumé GitHub Actions.
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


# ============================================================================
# GOFILE
# ============================================================================


def get_gofile_server() -> str:
    """
    Récupère automatiquement un serveur GoFile disponible.
    """

    response = requests.get(
        GOFILE_SERVERS_ENDPOINT,
        timeout=CONNECT_TIMEOUT,
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
                server_name = value.get(key)

                if server_name:
                    return str(server_name)

    direct_server = data.get("server")

    if direct_server:
        return str(direct_server)

    raise RuntimeError(
        print_json_error(
            "Aucun serveur GoFile trouvé dans la réponse :",
            payload,
        )
    )


# ============================================================================
# COMMANDES CURL
# ============================================================================


def escape_curl_form_filename(filename: str) -> str:
    """
    Échappe les caractères spéciaux utilisés dans curl --form.
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
    Construit la commande curl pour GoFile ou FileDitch.
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
            str(CONNECT_TIMEOUT),
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

        token = get_environment_value("GOFILE_TOKEN")
        folder_id = get_environment_value("GOFILE_FOLDER_ID")

        safe_filename = escape_curl_form_filename(filename)

        command = [
            "curl",
            "-4",
            "--http1.1",
            "--request",
            "POST",
            "--form",
            f"file=@-;filename={safe_filename}",
            "--connect-timeout",
            str(CONNECT_TIMEOUT),
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


def build_source_command(
    source_url: str,
) -> list[str]:
    """
    Construit la commande curl qui télécharge la source.
    """

    return [
        "curl",
        "-4",
        "-L",
        "--fail",
        "--silent",
        "--show-error",
        "--connect-timeout",
        str(CONNECT_TIMEOUT),
        "--max-time",
        "0",
        source_url,
    ]


# ============================================================================
# MULTIUP
# ============================================================================


def upload_multiup_remote(
    source_url: str,
    filename: str,
) -> tuple[int, str, str, int]:
    """
    Demande à MultiUp de récupérer directement le fichier depuis l'URL.

    Le fichier n'est pas téléchargé sur le runner GitHub.
    MultiUp effectue lui-même le téléchargement distant.
    """

    username = get_environment_value("MULTIUP_USERNAME")
    password = os.environ.get("MULTIUP_PASSWORD", "")

    selected_hosts = get_selected_multiup_hosts()

    payload: dict[str, str] = {
        "link": source_url,
        "fileName": filename,
    }

    if username:
        payload["username"] = username

    if password:
        payload["password"] = password

    for index, host in enumerate(
        selected_hosts,
        start=1,
    ):
        payload[f"host{index}"] = host

    print(
        "Hébergeurs MultiUp sélectionnés : "
        + ", ".join(selected_hosts)
    )

    try:
        response = requests.post(
            MULTIUP_ENDPOINT,
            data=payload,
            timeout=(CONNECT_TIMEOUT, None),
        )
    except requests.RequestException as error:
        return (
            1,
            "",
            f"Erreur de connexion à MultiUp : {error}",
            0,
        )

    if not response.ok:
        return (
            1,
            response.text,
            (
                "MultiUp a retourné le code HTTP "
                f"{response.status_code}."
            ),
            0,
        )

    return (
        0,
        response.text,
        "",
        0,
    )


# ============================================================================
# TRANSFERT VERS GOFILE OU FILEDITCH
# ============================================================================


def upload_stream(
    api: str,
    source_url: str,
    filename: str,
) -> tuple[int, str, str, int]:
    """
    Télécharge la source et transmet son contenu à l'API.

    Le fichier n'est pas stocké entièrement sur le disque.
    """

    source_process = subprocess.Popen(
        build_source_command(source_url),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )

    upload_process = subprocess.Popen(
        build_upload_command(api, filename),
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
            "Impossible d'ouvrir la sortie du téléchargement.",
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
                chunk = source_process.stdout.read(CHUNK_SIZE)

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

    if upload_broken and source_process.poll() is None:
        source_process.terminate()

    # Le flux stdin a déjà été fermé manuellement.
    # On empêche communicate() de tenter de le fermer une deuxième fois.
    upload_process.stdin = None

    upload_stdout, upload_stderr = upload_process.communicate()

    source_stderr = (
        source_process.stderr.read()
        if source_process.stderr
        else b""
    )

    source_return_code = source_process.wait()

    upload_output = decode_bytes(upload_stdout)
    upload_error = decode_bytes(upload_stderr)
    source_error = decode_bytes(source_stderr)

    # Le code 141 correspond généralement à SIGPIPE.
    source_failed = source_return_code not in (0, 141)

    upload_failed = (
        upload_process.returncode != 0
        or upload_broken
    )

    if source_failed or upload_failed:
        errors: list[str] = []

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


def upload_once(
    api: str,
    source_url: str,
    filename: str,
) -> tuple[int, str, str, int]:
    """
    Lance le type d'upload correspondant à l'API sélectionnée.
    """

    if api == "multiup":
        return upload_multiup_remote(
            source_url,
            filename,
        )

    print("Téléchargement de la source démarré.")
    print(f"API sélectionnée : {api}")

    return upload_stream(
        api,
        source_url,
        filename,
    )


# ============================================================================
# ANALYSE DES RÉPONSES API
# ============================================================================


def is_multiup_success(
    error_value: object,
) -> bool:
    """
    Détermine si le champ error de MultiUp indique une réussite.
    """

    return error_value in (
        None,
        "",
        False,
        0,
        "0",
        "ok",
        "OK",
        "success",
    )


def parse_response(
    api: str,
    response_text: str,
    filename: str,
    uploaded_size: int,
) -> tuple[str, str, object]:
    """
    Analyse la réponse JSON retournée par l'API.
    """

    try:
        response = json.loads(response_text)
    except json.JSONDecodeError as error:
        raise RuntimeError(
            "Réponse JSON invalide :\n"
            f"{response_text}"
        ) from error

    if not isinstance(response, dict):
        raise RuntimeError(
            "La réponse de l'API n'est pas un objet JSON :\n"
            f"{response_text}"
        )

    if api == "multiup":
        error_value = response.get("error")

        if not is_multiup_success(error_value):
            raise RuntimeError(
                print_json_error(
                    "MultiUp a refusé l'upload :",
                    response,
                )
            )

        file_url = response.get("link")

        if not file_url:
            raise RuntimeError(
                print_json_error(
                    "MultiUp n'a pas retourné de lien :",
                    response,
                )
            )

        final_filename = response.get(
            "fileName",
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

    if api == "fileditch":
        if not response.get("success"):
            raise RuntimeError(
                print_json_error(
                    "FileDitch a refusé l'upload :",
                    response,
                )
            )

        file_url = response.get("url")

        if not file_url:
            raise RuntimeError(
                print_json_error(
                    "FileDitch n'a pas retourné d'URL :",
                    response,
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
                print_json_error(
                    "GoFile a refusé l'upload :",
                    response,
                )
            )

        data = response.get(
            "data",
            response,
        )

        if not isinstance(data, dict):
            raise RuntimeError(
                print_json_error(
                    "Réponse GoFile invalide :",
                    response,
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
                print_json_error(
                    "GoFile n'a pas retourné de lien :",
                    response,
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


# ============================================================================
# ARGUMENTS
# ============================================================================


def parse_arguments() -> argparse.Namespace:
    """
    Définit et lit les arguments de ligne de commande.
    """

    parser = argparse.ArgumentParser(
        description=(
            "Upload de plusieurs URL vers GoFile, "
            "FileDitch ou MultiUp."
        )
    )

    parser.add_argument(
        "--api",
        required=True,
        choices=(
            "gofile",
            "fileditch",
            "multiup",
        ),
        help="API à utiliser.",
    )

    parser.add_argument(
        "--source-urls",
        required=True,
        help="URL directes séparées par des espaces.",
    )

    parser.add_argument(
        "--filenames",
        required=False,
        help=(
            "Noms personnalisés séparés par des espaces. "
            "Les noms contenant des espaces doivent être entre guillemets."
        ),
    )

    parser.add_argument(
        "--escape",
        action="store_true",
        help=(
            "Interprète <echap> comme une position sans nom personnalisé."
        ),
    )

    return parser.parse_args()


# ============================================================================
# TRAITEMENT D'UN FICHIER
# ============================================================================


def upload_file(
    api: str,
    source_url: str,
    filename: str,
    file_index: int,
    file_count: int,
) -> str:
    """
    Effectue l'upload d'un fichier avec les tentatives nécessaires.

    Retourne l'URL finale du fichier.
    """

    print("\n" + "=" * 70)
    print(f"Fichier {file_index}/{file_count}")
    print(f"URL source : {source_url}")
    print(f"Nom final  : {filename}")
    print(f"API        : {api}")
    print("=" * 70)

    last_error = ""

    for attempt in range(1, MAX_ATTEMPTS + 1):
        print(f"\nTentative {attempt}/{MAX_ATTEMPTS}")

        try:
            (
                return_code,
                response_text,
                error_text,
                uploaded_size,
            ) = upload_once(
                api,
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
                    api,
                    response_text,
                    filename,
                    uploaded_size,
                )

            except RuntimeError as error:
                print(
                    f"Erreur : {error}",
                    file=sys.stderr,
                )
                raise

            print("\nUpload terminé avec succès.")
            print(f"URL     : {file_url}")
            print(f"Fichier : {final_filename}")
            print(f"Taille  : {file_size} octets")

            write_github_summary(
                api,
                file_url,
                final_filename,
                file_size,
            )

            return file_url

        last_error = error_text or "Erreur inconnue."

        print(last_error)

        if attempt < MAX_ATTEMPTS:
            wait_seconds = attempt * RETRY_DELAY_SECONDS

            print(
                "Nouvelle tentative dans "
                f"{wait_seconds} secondes..."
            )

            time.sleep(wait_seconds)

    raise RuntimeError(
        f"Échec définitif pour : {source_url}\n"
        f"{last_error}"
    )


# ============================================================================
# PROGRAMME PRINCIPAL
# ============================================================================


def main() -> int:
    """
    Point d'entrée principal du script.
    """

    args = parse_arguments()

    if args.api == "multiup":
        try:
            selected_hosts = get_selected_multiup_hosts()
        except ValueError as error:
            print(
                f"Erreur : {error}",
                file=sys.stderr,
            )
            return 1

        print(
            "Hébergeurs MultiUp sélectionnés : "
            + ", ".join(selected_hosts)
        )

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
        if not is_valid_source_url(source_url):
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
            f"{custom_count} nom(s) personnalisé(s) détecté(s)."
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
            f"{remaining_count} fichier(s) restant(s) utiliseront "
            "le nom détecté depuis leur URL."
        )

    all_file_urls: list[str] = []

    for index, source_url in enumerate(
        source_urls,
        start=1,
    ):
        custom_filename: str | None = None
        custom_index = index - 1

        if custom_index < len(custom_filenames):
            custom_filename = custom_filenames[custom_index]

        filename = get_filename(
            source_url,
            custom_filename,
        )

        try:
            file_url = upload_file(
                api=args.api,
                source_url=source_url,
                filename=filename,
                file_index=index,
                file_count=len(source_urls),
            )

        except Exception as error:
            print(
                f"\n{error}",
                file=sys.stderr,
            )
            return 1

        all_file_urls.append(file_url)

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

    # Compatibilité avec l'ancien fonctionnement :
    # cette sortie contient uniquement la dernière URL.
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
