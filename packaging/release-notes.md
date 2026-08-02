# RelayDeck Local Release

This release provides native Windows x64, macOS Intel, and macOS Apple Silicon packages.

## Verification

Download the matching package and `SHA256SUMS.txt` from this release. Verify the checksum before opening the installer or disk image.

## Runtime notes

The package contains only application code, pinned Python dependencies, and configuration templates. It does not include `.env`, credentials, provider state, browser sessions, `data/`, `logs/`, or `run/`.

On macOS, allow the first-run Gatekeeper prompt after verifying the checksum. Browser SSO needs a separate Chromium runtime installation through `RelayDeck.app/Contents/MacOS/RelayDeck --install-browser-runtime`.
