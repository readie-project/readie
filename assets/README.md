# assets

Brand assets for Readie: the wordmark, the icon, and banners built from them. Use
these files wherever Readie appears outside the documentation site, for example in
slides, posts, and package pages.

## Files

| File | Format | Contents |
| ---- | ------ | -------- |
| `wordmark-light.svg` | SVG, transparent background | The wordmark for light backgrounds: dark letters and blue trails |
| `wordmark-dark.svg` | SVG, transparent background | The wordmark for dark backgrounds: light letters and light-blue trails |
| `icon.svg` | SVG | The icon: the trails and the letter `r` on a dark tile with rounded corners |
| `icon.png` | PNG, 1024 × 1024 | The icon, with transparent corners |
| `banner-light.png` | PNG, 1920 × 1080 (16:9) | `wordmark-light.svg` centered on white |
| `banner-dark.png` | PNG, 1920 × 1080 (16:9) | `wordmark-dark.svg` centered on the dark background color |
| `banner-tagline-light.png` | PNG, 1920 × 1080 (16:9) | The wordmark and the tagline on white |
| `banner-tagline-dark.png` | PNG, 1920 × 1080 (16:9) | The wordmark and the tagline on the dark background color |
| `banner-strip-light.png` | PNG, 1584 × 396 (4:1) | A strip version of `banner-tagline-light.png`, sized for a LinkedIn cover image |
| `banner-strip-dark.png` | PNG, 1584 × 396 (4:1) | A strip version of `banner-tagline-dark.png`, sized for a LinkedIn cover image |
| `banner-wordmark-light.png` | PNG, 1584 × 396 (4:1) | `wordmark-light.svg` centered on white, without the tagline. The PyPI page (`pkg/README.pypi.md`) uses it |
| `banner-wordmark-dark.png` | PNG, 1584 × 396 (4:1) | `wordmark-dark.svg` centered on the dark background color, without the tagline |
| `banner-curl-light.png` | PNG, 1920 × 1080 (16:9) | The wordmark, the tagline, and the cURL example in a code card, on white |
| `banner-curl-dark.png` | PNG, 1920 × 1080 (16:9) | The wordmark, the tagline, and the cURL example in a code card, on the dark background color |
| `square-light.png` | PNG, 1080 × 1080 (1:1) | `wordmark-light.svg` centered on white |
| `square-dark.png` | PNG, 1080 × 1080 (1:1) | `wordmark-dark.svg` centered on the dark background color |
| `social-card-light.png` | PNG, 1200 × 630 (1.91:1) | The wordmark, the tagline, and `readie.org` on white, with a trail-blue bar along the bottom, sized for link previews (Open Graph and Twitter cards) |
| `social-card-dark.png` | PNG, 1200 × 630 (1.91:1) | The wordmark, the tagline, and `readie.org` on the dark background color, with a light-blue bar along the bottom, sized for link previews. The documentation site uses this card |

Choose the wordmark by the background it sits on: `wordmark-light.svg` on a light
background, `wordmark-dark.svg` on a dark one. Use the icon where the space is square
or small, such as an avatar or a favicon.

## Colors

| Element | On a light background | On a dark background |
| ------- | --------------------- | -------------------- |
| Letters | `#2a3138` | `#e3e7ea` |
| Trails | `#2a5db8` | `#8db0f8` |
| Tagline | `#56626c` | `#a3aeb7` |
| Background | `#ffffff` | `#16181d` |

The letter, tagline, and trail colors are the `--rd-ink`, `--rd-ink-2`, and
`--rd-accent` tokens of the light and dark themes in `docs/src/css/custom.css`. The
dark background is the fill of the icon tile.

## Type

The letters of the wordmark are outlines of Figtree Bold, a typeface under the SIL
Open Font License. The SVG files contain the outlines, so they render the same
without the font installed.

The tagline, strip, and cURL banners set the tagline in the style of the hero on the
documentation site: the system sans-serif font, weight 500, a line height of 1.35,
and the same antialiased text rendering. The tagline wraps at the same width as on
the site, scaled with the banner. The wordmark and the tagline below it are both
centered in the banner, and so are the lines of the tagline. In the strip banners
the content stays clear of the lower-left corner, where a profile photo overlaps a
cover image. The tagline reads:

```text
Secure sandboxes with optimized startup and import times for AI agents and running Python code remotely
```

## Code card

The cURL banners show this request below the tagline, in a card with a window bar:

```bash
curl -N https://run.readie.org -H 'Content-Type: application/json' \
  -d '{"code": "print(\"Hello from Readie\")"}'
```

The request is the one in the HTTP execution guide
(`docs/docs/guides/http-execution.md`), written on two lines with the short form `-d`
of `--data`.
The code is set in the system monospace font. Its colors come from the One Light and
One Dark themes that the documentation site uses for code blocks.

## Relation to the documentation site

The documentation site uses the same artwork:

| Site file | Copy in this directory |
| --------- | ---------------------- |
| `docs/static/img/wordmark.svg` | `wordmark-light.svg` |
| `docs/static/img/wordmark-dark.svg` | `wordmark-dark.svg` |
| `docs/static/img/logo.svg` | `icon.svg` |
| `docs/static/img/social-card.png` | `social-card-dark.png` |

The social cards are rendered from `docs/tools/social-card.html`, which lists the commands
that render both cards. The tagline is the same on the banners, the social cards, and the
documentation site, where it is the `tagline` in `docs/docusaurus.config.ts`.

When the artwork changes, update both copies of each SVG file and render the PNG
files again from the SVG files.
