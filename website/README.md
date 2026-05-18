# Noadcast website

Single-page static site summarising what Noadcast does. No build step —
just open `index.html` in a browser.

## Local preview

```
open website/index.html
```

Or, to serve it over HTTP (so relative paths and any future fetches work
the same way they would on a real host):

```
python3 -m http.server --directory website 8000
# then http://localhost:8000
```

## Replacing the screenshot placeholders

`screenshots/` ships four placeholder SVGs sized to roughly an iPhone
17 Pro aspect ratio. To swap in real screenshots, save them under the
same filenames (any image format the browser handles works — update the
`src` extension in `index.html` if you use PNG/WebP):

- `screenshots/queue.svg`
- `screenshots/now-playing.svg`
- `screenshots/transcript.svg`
- `screenshots/settings.svg`

The hero also references `screenshots/now-playing.svg`, so replacing
that one updates the hero image too.

## Hosting

The whole `website/` directory is static and deploys as-is to anything
that serves files (GitHub Pages, Cloudflare Pages, Netlify, S3, etc.).
