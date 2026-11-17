# Bundled card fonts

The tweet cards (`../cards.py`) use Space Grotesk and Geist Mono. They are bundled here so the cards never depend on
fonts installed on the machine that renders them (headless Chrome silently falls back to a serif otherwise).
`../fonts.py` checks each file against the sha256 below, embeds it in the card HTML as a base64 data-URI `@font-face`,
and `../build_day.sh` refuses to build (`font_unresolved`) unless both families render in Chrome.

| File | Upstream (github.com/google/fonts) | sha256 | Version | License | Copyright |
|---|---|---|---|---|---|
| `SpaceGrotesk-wght.ttf` | `ofl/spacegrotesk/SpaceGrotesk[wght].ttf` @ `00a38a53f92aef923b9353f40128e8f4552ddae4` | `acad6de1fc93436f5c0f1f4137751ef04f1aea3063e7036535970ffcfbd79f72` | 2.000 (variable, wght 300–700) | SIL OFL 1.1 | Copyright 2020 The Space Grotesk Project Authors (https://github.com/floriankarsten/space-grotesk) |
| `SpaceGrotesk-OFL.txt` | `ofl/spacegrotesk/OFL.txt` @ `00a38a53f92aef923b9353f40128e8f4552ddae4` | `564ce565c371c5e5bbf286006565a7c9aa55a9f56e7ca58d56e05d649dd61a72` | | | license text for the file above |
| `GeistMono-wght.ttf` | `ofl/geistmono/GeistMono[wght].ttf` @ `121c8e30e20960139d24d1e93f350745205dd680` | `d00e590b8eb3a59acc329b2d044fd143ae935090b7da33199ebee27cc7de8196` | 1.701 (variable, wght 100–900) | SIL OFL 1.1 | Copyright 2024 The Geist Project Authors (https://github.com/vercel/geist-font.git) |
| `GeistMono-OFL.txt` | `ofl/geistmono/OFL.txt` @ `121c8e30e20960139d24d1e93f350745205dd680` | `1781d2806a07d91c4edf4740b88449fab7d0eadad53f7c351b94cd4d4eb8c00f` | | | license text for the file above |

The files are the exact upstream bytes, renamed only (`[wght]` dropped from the file name).

## License
Both fonts are under the SIL Open Font License, Version 1.1 (full text in the `*-OFL.txt` files next to each font).
The OFL permits bundling and redistributing the fonts with other software, provided the copyright notice and license
travel with them, which is what the `*-OFL.txt` files are for. The Reserved Font Name restriction only applies to
modified versions; these files are unmodified, so the original family names are used as is.

## Updating a font
Replace the `.ttf` (and its `OFL.txt` if upstream changed it), then update its sha256 in `../fonts.py` (`FONTS`) and in
the table above, plus the weight range in `FONTS` if it changed. `tests/test_tweet_v2_fonts.py` checks the hashes, the
family names in the TTF `name` table and the license texts, and renders a card in Chromium with system fonts hidden.
