# site (WS7)

asimoov.com: Astro 5 + Starlight, static, deployed to GitHub Pages by
`.github/workflows/site.yml` on push to `main`.

```bash
cd site
npm ci
npm run dev      # http://localhost:4321
npm run build    # -> site/dist
```

- `src/pages/index.astro` is the English landing page; `src/pages/fr/` is the
  French skeleton. Both override Starlight's routing.
- `src/content/docs/docs/` is the Starlight documentation.
  `contracts.md` is generated from the repo's `docs/contracts.md` by
  `scripts/sync-docs.mjs` (run automatically by `npm run build`) and is
  gitignored — edit the source, not the copy.
- `src/components/Eyes.astro` is the hero: a self-contained canvas that
  follows the cursor, blinks and cycles emotions. It exposes
  `canvas.applyFaceState(state)` taking a `face_state.v1` object, so
  `src/asimoov/faces/web/face.js` can drive it once that module exports a
  renderer that works without a hub connection.
- `public/diagrams/architecture.svg` is the architecture diagram, inlined on
  the page and reused in the repository `README.md`.
- `public/CNAME` pins the custom domain. Fonts are self-hosted: the site
  makes no third-party request.

Palette and type live in `src/styles/theme.css`. See `plan.md` section 6 for
the art direction.
