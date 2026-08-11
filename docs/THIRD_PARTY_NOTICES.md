# Third-party notices

This application remains distributed under its own MIT license. Its immutable runtime installs
`gallery-dl` 1.32.8 as a separately executed Python program from
<https://github.com/mikf/gallery-dl>. The upstream project identifies that distribution as
GPL-2.0. No gallery-dl source code is copied into this repository and application code communicates
with it only through a controlled subprocess boundary.

The corresponding GPL-2.0 license text is distributed at
`docs/licenses/gallery-dl-GPL-2.0.txt`. Upstream notices and package metadata remain present in the
installed Python distribution inside the Docker image.

Noto Sans Regular is bundled for administrator report rendering under the SIL Open Font License
1.1; its `OFL.txt` is shipped beside the font resource.
