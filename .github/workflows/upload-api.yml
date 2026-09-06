name: Upload API

# Le fichier est situé à l’emplacement suivant :
# .github/workflows/upload-api.yml

on:
  workflow_dispatch:
    inputs:
      mode:
        description: "Mode d'exécution"
        required: true
        type: choice
        default: archive
        options:
          - archive
          - desarchive
          - re-upload

      apis:
        description: >-
          APIs à utiliser, séparées par des virgules :
          gofile,fileditch,multiup
        required: true
        type: string
        default: gofile

      source_urls:
        description: >-
          En mode archive : une ou plusieurs URLs séparées par des espaces.
          En mode desarchive : une seule URL d'archive.
          En mode re-upload : une ou plusieurs URLs séparées par des espaces.
        required: true
        type: string

      filename:
        description: >-
          En mode archive : nom personnalisé du ZIP final.
          En mode re-upload : noms personnalisés séparés par des espaces,
          dans le même ordre que les URLs.
        required: false
        type: string

      escape:
        description: >-
          Utilise <echap> pour conserver le nom détecté automatiquement.
        required: false
        type: boolean
        default: false

permissions:
  contents: read

jobs:
  upload:
    name: Upload vers ${{ inputs.apis }}
    runs-on: ubuntu-latest

    steps:
      - name: Récupérer le dépôt
        uses: actions/checkout@v5

      - name: Installer les outils système
        if: ${{ inputs.mode == 'desarchive' }}
        run: |
          set -euo pipefail

          sudo apt-get update

          sudo apt-get install -y \
            p7zip-full \
            unrar-free \
            unzip \
            tar

      - name: Installer Python
        uses: actions/setup-python@v6
        with:
          python-version: "3.12"

      - name: Installer les dépendances Python
        run: |
          set -euo pipefail

          python -m pip install \
            --disable-pip-version-check \
            --upgrade \
            pip

          python -m pip install \
            --disable-pip-version-check \
            requests \
            tqdm

      - name: Vérifier les outils
        run: |
          set -euo pipefail

          python --version
          curl --version

          if [ "${{ inputs.mode }}" = "desarchive" ]; then
            7z
            unzip -v
            tar --version
          fi

      - name: Télécharger et envoyer les fichiers
        id: upload
        shell: bash
        env:
          MODE: ${{ inputs.mode }}
          APIS: ${{ inputs.apis }}
          SOURCE_URLS: ${{ inputs.source_urls }}
          CUSTOM_FILENAMES: ${{ inputs.filename }}
          ENABLE_ESCAPE: ${{ inputs.escape }}

          GOFILE_TOKEN: ${{ secrets.GOFILE_TOKEN }}
          GOFILE_FOLDER_ID: ${{ secrets.GOFILE_FOLDER_ID }}

          MULTIUP_USERNAME: ${{ secrets.MULTIUP_USERNAME }}
          MULTIUP_PASSWORD: ${{ secrets.MULTIUP_PASSWORD }}

        run: |
          set -euo pipefail

          declare -A API_SCRIPTS=(
            [gofile]="scripts/upload-gofile.py"
            [fileditch]="scripts/upload-fileditch.py"
            [multiup]="scripts/upload-multiup.py"
          )

          output_dir="$RUNNER_TEMP/upload_outputs"
          mkdir -p "$output_dir"

          combined_urls="$output_dir/all_urls.txt"
          : > "$combined_urls"

          IFS=',' read -ra SELECTED_APIS <<< "$APIS"

          if [ "${#SELECTED_APIS[@]}" -eq 0 ]; then
            echo "Aucune API sélectionnée." >&2
            exit 1
          fi

          for selected_api in "${SELECTED_APIS[@]}"; do
            api="$(echo "$selected_api" | xargs | tr '[:upper:]' '[:lower:]')"

            if [ -z "$api" ]; then
              continue
            fi

            if [[ -z "${API_SCRIPTS[$api]+x}" ]]; then
              echo "API inconnue : $api" >&2
              echo "APIs disponibles : gofile, fileditch, multiup" >&2
              exit 1
            fi

            script="${API_SCRIPTS[$api]}"
            api_output="$output_dir/${api}.out"

            : > "$api_output"

            command=(
              python "$script"
              --mode "$MODE"
              --source-urls "$SOURCE_URLS"
            )

            if [ -n "$CUSTOM_FILENAMES" ]; then
              command+=(
                --filenames
                "$CUSTOM_FILENAMES"
              )
            fi

            if [ "$ENABLE_ESCAPE" = "true" ]; then
              command+=(--escape)
            fi

            echo
            echo "=========================================="
            echo "Upload avec : $api"
            echo "Script      : $script"
            echo "=========================================="

            GITHUB_OUTPUT="$api_output" "${command[@]}"

            api_urls="$(
              awk '
                /^file_urls<<EOF$/ {
                  capture = 1
                  next
                }

                capture && /^EOF$/ {
                  exit
                }

                capture {
                  print
                }
              ' "$api_output"
            )"

            if [ -z "$api_urls" ]; then
              echo "Aucun lien retourné par $api." >&2
              exit 1
            fi

            {
              echo "[$api]"
              printf '%s\n' "$api_urls"
              echo
            } >> "$combined_urls"
          done

          if [ ! -s "$combined_urls" ]; then
            echo "Aucun lien généré." >&2
            exit 1
          fi

          {
            echo "file_urls<<EOF"
            cat "$combined_urls"
            echo "EOF"
          } >> "$GITHUB_OUTPUT"

          echo
          echo "Tous les uploads sélectionnés sont terminés."

      - name: Afficher les liens générés
        if: success()
        shell: bash
        env:
          FILE_URLS: ${{ steps.upload.outputs.file_urls }}

        run: |
          set -euo pipefail

          echo "Liens générés :"
          printf '%s\n' "$FILE_URLS"

          {
            echo "## Uploads terminés"
            echo
            echo "- Mode : \`${{ inputs.mode }}\`"
            echo "- APIs : \`${{ inputs.apis }}\`"
            echo
            echo '```text'
            printf '%s\n' "\$FILE\_URLS"
            echo '```'
          } >> "$GITHUB_STEP_SUMMARY"
