# Portal Mapper — Frontend

React 19 + TypeScript + Vite frontend for uploading Minecraft save files and
visualizing block locations on an interactive map.

## Requirements

- Node.js 20+ (required by Vite 8)

## Setup

```bash
npm install
```

## Running

```bash
npm run dev
```

The app will be available at `http://localhost:5173`.

The dev server expects the backend running at `http://localhost:8000`. See the [backend README](../backend/README.md) for setup.

### Pointing at a different backend

The API URL is read from the `VITE_API_URL` env var at build/dev time, falling
back to `http://localhost:8000/parse-blocks`. For a deployed backend, set it
before building:

```bash
VITE_API_URL=https://your-host/parse-blocks npm run build
```

## Other Commands

```bash
npm run build    # production build → dist/
npm run preview  # preview the production build locally
npm run lint     # run ESLint
```

## How it works

1. **Pick a save + dimension.** The selected `.zip` is validated against the
   4 GB limit on both select and submit.
2. **Filter in the browser** (`src/workers/regionFilter.worker.ts`). Before any
   upload, a web worker pares the save down to just the chosen dimension's
   `region/*.mca` (+ `.mcc`) files, repacked with the original paths preserved.
   This commonly shrinks a multi-GB upload by an order of magnitude. The blob is
   passed by reference so a 4 GB save is never read into a main-thread buffer.
3. **Upload + scan.** The filtered zip is POSTed to the backend with a real
   byte-level progress bar; once it lands, the backend scans and returns matches.
4. **Map** (`src/WorldMap.tsx`). Matches are plotted on a single canvas on
   Minecraft's X/Z plane — chunk-aligned grid, drag-to-pan, wheel/pinch zoom,
   auto-fit, hover-for-coordinates.

The UI runs through four phases — `idle → filtering → uploading → processing` —
reflected in both the on-page indicators and the browser tab title.

> **Important:** the worker's `regionDimension()` is an exact mirror of the
> backend's `_region_dimension()`. If one changes, change the other — the
> backend stays the source of truth for how a path maps to a dimension.

## Notable toolchain

Uses the React Compiler (`babel-plugin-react-compiler`) with strict
`eslint-plugin-react-hooks` v7. Practical consequence in `WorldMap.tsx`: **no
`setState` inside effect bodies** — keep state updates in event / `requestAnimationFrame`
/ `ResizeObserver` callbacks, and read live view/hover state from refs during
the canvas draw.