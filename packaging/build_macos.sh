#!/bin/sh
set -eu

root=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
output_dir="$root/dist"
version="0.0.0-dev"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --output-dir) output_dir=$2; shift 2 ;;
    --version) version=$2; shift 2 ;;
    *) echo "Unknown argument: $1" >&2; exit 2 ;;
  esac
done

case "$(uname -m)" in
  x86_64) asset_name="RelayDeck-x64.dmg" ;;
  arm64) asset_name="RelayDeck-arm64.dmg" ;;
  *) echo "Unsupported macOS architecture: $(uname -m)" >&2; exit 1 ;;
esac

build_root="$root/packaging/build/macos"
payload_root="$build_root/payload"
runtime_root="$payload_root/runtime"
build_env_root="$build_root/build-env"
app_root="$build_root/RelayDeck.app"
rm -rf "$build_root"
mkdir -p "$payload_root" "$output_dir"

python3 "$root/packaging/build_payload.py" --from-git-head "$root" "$payload_root"
python3 -m venv "$runtime_root"
python3 -m venv "$build_env_root"
runtime_python="$runtime_root/bin/python"
build_python="$build_env_root/bin/python"
"$runtime_python" -m pip install --disable-pip-version-check --no-cache-dir -r "$payload_root/requirements.txt"
"$build_python" -m pip install --disable-pip-version-check --no-cache-dir -r "$root/packaging/requirements-packaging.txt"
"$build_python" -m PyInstaller --noconfirm --windowed --name RelayDeck --distpath "$build_root" --workpath "$build_root/pyinstaller-work" --specpath "$build_root" "$root/packaging/launcher.py"

mkdir -p "$app_root/Contents/Resources"
mv "$payload_root" "$app_root/Contents/Resources/app"
touch "$app_root/Contents/Resources/app/RELAYDECK_VERSION"
printf '%s\n' "$version" > "$app_root/Contents/Resources/app/RELAYDECK_VERSION"
hdiutil create -volname RelayDeck -srcfolder "$app_root" -ov -format UDZO "$output_dir/$asset_name"
