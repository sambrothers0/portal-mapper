/// <reference lib="webworker" />

import JSZip from 'jszip'

export {}

// Filter a Minecraft save zip down to a single dimension's region files *in the
// browser*, before anything is uploaded. A 4 GB save is mostly entities, POI,
// playerdata, and the other two dimensions — none of which a block search
// touches. Stripping to one dimension's `region/*.mca` (+ `.mcc` sidecars)
// commonly shrinks the upload by an order of magnitude, and upload time over a
// home connection dominates the end-to-end cost in production.

export type RegionFilterRequest = {
  type: 'filter'
  // The selected save as a Blob. Passed by reference through structured clone,
  // so a 4 GB save is never copied onto the main thread or into an ArrayBuffer;
  // JSZip reads the dimension's members straight out of it.
  file: Blob
  fileName: string
  dimension: string
}

export type RegionFilterResponse =
  | { type: 'filtered'; fileName: string; blob: Blob; entryCount: number }
  | { type: 'error'; message: string }

// Mirror of the backend's `_region_dimension`: a save keeps each dimension's
// block data in a directory literally named `region`, identified by its parent
// (`DIM-1` = Nether, `DIM1` = End, anything else = Overworld). We MUST preserve
// these paths in the repacked zip so the backend classifies them the same way —
// the backend stays the single source of truth for dimension derivation.
function regionDimension(name: string): string | null {
  if (!name.endsWith('.mca') && !name.endsWith('.mcc')) return null
  const parts = name.split('/').filter(Boolean)
  if (parts.length < 2 || parts[parts.length - 2] !== 'region') return null
  const parent = parts.length >= 3 ? parts[parts.length - 3] : ''
  if (parent === 'DIM-1') return 'nether'
  if (parent === 'DIM1') return 'end'
  return 'overworld'
}

self.addEventListener('message', async (event: MessageEvent<RegionFilterRequest>) => {
  const { file, fileName, dimension } = event.data

  try {
    const source = await JSZip.loadAsync(file)
    const out = new JSZip()

    let entryCount = 0
    for (const entry of Object.values(source.files)) {
      if (entry.dir || regionDimension(entry.name) !== dimension) continue
      // Repack with STORE: `.mca` chunks are already zlib-compressed internally,
      // so re-deflating them at the zip layer burns CPU for almost no shrink.
      const data = await entry.async('uint8array')
      out.file(entry.name, data, { compression: 'STORE' })
      entryCount += 1
    }

    if (entryCount === 0) {
      const result: RegionFilterResponse = {
        type: 'error',
        message:
          `No ${dimension} region files found in that save. ` +
          'Is this a valid Minecraft save, and has that dimension been visited?',
      }
      self.postMessage(result)
      return
    }

    const blob = await out.generateAsync({ type: 'blob', compression: 'STORE' })
    const result: RegionFilterResponse = { type: 'filtered', fileName, blob, entryCount }
    self.postMessage(result)
  } catch (error) {
    const result: RegionFilterResponse = {
      type: 'error',
      message: error instanceof Error ? error.message : 'Could not read that zip file',
    }
    self.postMessage(result)
  }
})
