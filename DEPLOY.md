# DocQA — GitHub Pages Deployment

This turns DocQA into an installable PWA on a real `https://` origin, which fixes:
- The split-context bug (model loads but chat can't see it) under `content://`
- Aggressive GPU teardown when the app is backgrounded on mobile
- Lets users "Add to Home Screen" and run it like a native app

---

## Files in this package

| File | Purpose |
|---|---|
| `index.html` | The app (renamed from docqa.html) |
| `manifest.json` | PWA metadata — name, icons, display mode |
| `sw.js` | Service worker — caches app shell for offline use |
| `icon-192.png` | App icon (small) |
| `icon-512.png` | App icon (large) |
| `icon-512-maskable.png` | Android adaptive icon |

---

## One-time setup

1. Create a new GitHub repository (e.g. `docqa`). It can be **public** or **private** — Pages works on private repos with any paid plan, otherwise use public.

2. Upload all six files to the repository root (not in a subfolder).

3. In the repo: **Settings → Pages**
   - Source: **Deploy from a branch**
   - Branch: **main** (or **master**), folder: **/ (root)**
   - Save.

4. Wait ~1 minute. GitHub shows your URL:
   `https://YOUR-USERNAME.github.io/docqa/`

5. Open that URL on your phone. The first load downloads the app shell (tiny). Then **Chrome menu → Add to Home Screen**. It now launches fullscreen like an app.

---

## CRITICAL: every time you update the app

The service worker caches the app shell. If you don't bump the version, installed users keep the **old** cached version forever.

**On every deploy:**
1. Replace `index.html` (and any other changed files) in the repo.
2. Open `sw.js` and change the version line:
   ```js
   const CACHE_VERSION = 'docqa-v1';   // → 'docqa-v2', 'docqa-v3', ...
   ```
3. Commit both. Installed users get the update on their next launch.

**This is the single most common PWA mistake — bump `CACHE_VERSION` every single deploy.**

---

## What is and isn't cached

- **Cached (offline-ready):** the app shell — HTML, manifest, icons.
- **Not cached by the service worker:** WebLLM and transformers.js CDN bundles, and the model weights. These are large and cross-origin; the browser's own cache handles the model weights (same as before). The first run still needs internet to pull the libraries and models; after that the models are cached by the browser.

---

## Distributing to other crew

Just send them the URL. They open it, Add to Home Screen, load a model once, import your `kb.json`. No app store, no sideloading.

When you ship a new KB, they re-import the new `kb.json` via Models → Import KB. When you update the app itself, they get it automatically on next launch (because you bumped `CACHE_VERSION`).

---

## Note on PairBoard integration

None of this blocks dropping DocQA into PairBoard later. The service worker registration is guarded — it only runs when DocQA is standalone (`!window.PB`). Inside PairBoard, the module code runs unchanged and PairBoard's own shell/manifest takes over. The PWA files here are simply ignored in that context.
