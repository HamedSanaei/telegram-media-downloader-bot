# Third-party notices

The main application is distributed under its MIT license. Its immutable runtime installs
`gallery-dl` 1.32.17 as a separately executed Python program from
<https://codeberg.org/mikf/gallery-dl>, whose distribution is GPL-2.0-only.
The external compatibility extractor source at
`plugins/telegram_media_bot_gallerydl/instagram_profile.py`
is distributed under GPL-2.0-only and is loaded only inside that separate vendor subprocess.
Its complete source accompanies the wheel, source distribution and runtime image. No upstream
source is copied into this repository; the main application communicates through its controlled
subprocess boundary and does not import the compatibility extractor.

The corresponding GPL-2.0 license text is distributed at
`docs/licenses/gallery-dl-GPL-2.0.txt`. Upstream notices and package metadata remain present in the
installed Python distribution inside the Docker image.

Noto Sans Regular is bundled for administrator report rendering under the SIL Open Font License
1.1; its `OFL.txt` is shipped beside the font resource.

The `cryptography` package (runtime, Apache-2.0 or BSD-3-Clause dependencies on the Python Package
Index) is used for Ed25519 handoff signing/verification and AES-256-GCM credential-vault
encryption. Its transitive `cffi` dependency and the `pycparser`/`cffi` license
notices remain governed by their own upstream terms and are already present in the installed Python
distribution inside the Docker image. `cryptography` source code is not copied into this repository;
no plaintext keys, cookies, or secrets are stored through it.
