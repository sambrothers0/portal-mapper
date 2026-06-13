import { useEffect, useState } from 'react'
import { PortalLoader } from './PortalLoader'
import { WorldMap } from './WorldMap'
import type { BlockMatch } from './WorldMap'
import type { RegionFilterRequest, RegionFilterResponse } from './workers/regionFilter.worker'

type ParseResult = {
  block_type: string
  dimension: string
  count: number
  matches: BlockMatch[]
}

const API_URL = import.meta.env.VITE_API_URL ?? 'http://localhost:8000/parse-blocks'
const DEFAULT_BLOCK = 'minecraft:nether_portal'

// Which dimension's region files to scan. The `id` is the value the backend
// expects in the `dimension` form field; the `label` is what players call it.
const DIMENSIONS = [
  { id: 'overworld', label: 'Overworld' },
  { id: 'nether', label: 'Nether' },
  { id: 'end', label: 'End' },
] as const
type Dimension = (typeof DIMENSIONS)[number]['id']
const DEFAULT_DIMENSION: Dimension = 'overworld'
const dimensionLabel = (id: string) => DIMENSIONS.find((d) => d.id === id)?.label ?? id

const MAX_FILE_BYTES = 4 * 1024 * 1024 * 1024 // 4 GB
const MAX_FILE_LABEL = '4 GB'

// Shown once the upload finishes and the server is grinding through region
// files. There's no real progress signal from the parse step, so instead of a
// fake bar we cycle these while it works. Keep them grounded — a touch of the
// tarot/divination theme, not full fortune-teller.
const PROCESSING_MESSAGES = [
  'Reading what the world remembers',
  'Tracing chunks across the overworld',
  'Following portals through the dark',
  'Sifting the region files, block by block',
  'Charting the spaces in between',
]

const formatSize = (bytes: number) => `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`

type Phase = 'idle' | 'filtering' | 'uploading' | 'processing'

// Run the selected save through the region-filter worker, returning a zip that
// holds only the chosen dimension's region files. This is the big production
// win: a multi-GB save usually carries a few hundred MB of region files for one
// dimension, so we upload that instead of the whole world. Paths are preserved
// so the backend still derives the dimension itself.
function filterDimension(
  file: File,
  dimension: Dimension,
  onProgress: (percent: number) => void,
): Promise<File> {
  return new Promise((resolve, reject) => {
    const worker = new Worker(new URL('./workers/regionFilter.worker.ts', import.meta.url), {
      type: 'module',
    })

    worker.onmessage = (event: MessageEvent<RegionFilterResponse>) => {
      const data = event.data
      // Progress ticks keep streaming while the worker repacks — report and wait
      // for the terminal 'filtered'/'error' message before tearing the worker down.
      if (data.type === 'progress') {
        onProgress(data.percent)
        return
      }
      worker.terminate()
      if (data.type === 'filtered') {
        // Keep a .zip name so the backend's extension check passes.
        resolve(new File([data.blob], `filtered-${file.name}`, { type: 'application/zip' }))
      } else {
        reject(new Error(data.message))
      }
    }
    worker.onerror = (event) => {
      worker.terminate()
      reject(new Error(event.message || 'Failed to read the save in the browser'))
    }

    const request: RegionFilterRequest = {
      type: 'filter',
      file,
      fileName: file.name,
      dimension,
    }
    worker.postMessage(request)
  })
}

// Upload via XHR (not fetch) so we get a real byte-level progress event for the
// upload, then flip to the "processing" phase the moment the bytes are all sent.
function postWithUploadProgress(
  url: string,
  form: FormData,
  onProgress: (percent: number) => void,
  onUploaded: () => void,
): Promise<ParseResult> {
  return new Promise((resolve, reject) => {
    const xhr = new XMLHttpRequest()
    xhr.open('POST', url)

    xhr.upload.onprogress = (event) => {
      if (event.lengthComputable) {
        onProgress(Math.round((event.loaded / event.total) * 100))
      }
    }
    // Fires when the request body is fully sent — upload done, server now working.
    xhr.upload.onload = () => {
      onProgress(100)
      onUploaded()
    }

    xhr.onload = () => {
      let payload: unknown = null
      try {
        payload = JSON.parse(xhr.responseText)
      } catch {
        // fall through to status-based error below
      }
      if (xhr.status >= 200 && xhr.status < 300) {
        resolve(payload as ParseResult)
        return
      }
      const detail = (payload as { detail?: string } | null)?.detail
      reject(new Error(detail ?? `Request failed with status ${xhr.status}`))
    }
    xhr.onerror = () => reject(new Error('Network error — could not reach the server'))

    xhr.send(form)
  })
}

function App() {
  const [selectedFile, setSelectedFile] = useState<File | null>(null)
  const [blockType, setBlockType] = useState(DEFAULT_BLOCK)
  const [dimension, setDimension] = useState<Dimension>(DEFAULT_DIMENSION)
  const [phase, setPhase] = useState<Phase>('idle')
  const [uploadProgress, setUploadProgress] = useState(0)
  const [messageIndex, setMessageIndex] = useState(0)
  const [messageVisible, setMessageVisible] = useState(true)
  const [result, setResult] = useState<ParseResult | null>(null)
  const [error, setError] = useState<string | null>(null)
  // Set when the error is specifically about file size, so we can offer the
  // trim-your-world hint instead of a bare "too big" message.
  const [oversize, setOversize] = useState(false)
  const [copied, setCopied] = useState(false)

  const busy = phase !== 'idle'

  const copyCoordinates = async () => {
    if (!result) return
    try {
      await navigator.clipboard.writeText(JSON.stringify(result.matches))
      setCopied(true)
      setTimeout(() => setCopied(false), 2000)
    } catch {
      setError('Could not copy to the clipboard.')
    }
  }

  // Reflect the current phase in the browser tab title so progress is legible
  // even when the tab is backgrounded during a long scan.
  useEffect(() => {
    const suffix =
      phase === 'filtering'
        ? 'Compressing'
        : phase === 'uploading'
          ? 'Uploading'
          : phase === 'processing'
            ? 'Processing'
            : 'Home'
    document.title = `Tarot - ${suffix}`
  }, [phase])

  // While the server is processing, rotate the flavor text with a fade: drop to
  // opacity 0, swap the line once it's faded out, then fade the new line back in.
  // The swap timeout is tracked alongside the interval so both are torn down on
  // cleanup — otherwise a pending swap could fire after the phase has moved on.
  useEffect(() => {
    if (phase !== 'processing') return
    setMessageIndex(0)
    setMessageVisible(true)
    let swap: ReturnType<typeof setTimeout> | undefined
    const interval = setInterval(() => {
      setMessageVisible(false)
      swap = setTimeout(() => {
        setMessageIndex((index) => (index + 1) % PROCESSING_MESSAGES.length)
        setMessageVisible(true)
      }, 400)
    }, 2800)
    return () => {
      clearInterval(interval)
      if (swap) clearTimeout(swap)
    }
  }, [phase])

  const handleSubmit = async () => {
    if (!selectedFile) {
      setError('Choose a zip file first.')
      return
    }

    if (selectedFile.size > MAX_FILE_BYTES) {
      setError(`That file is ${formatSize(selectedFile.size)}. The maximum is ${MAX_FILE_LABEL}.`)
      setOversize(true)
      return
    }

    setPhase('filtering')
    setUploadProgress(0)
    setMessageIndex(0)
    setMessageVisible(true)
    setError(null)
    setOversize(false)
    setCopied(false)
    setResult(null)

    try {
      // Strip the save to just this dimension's region files before uploading.
      // The compress/trim phase drives the bar 0→100, then we reset it so the
      // upload phase tracks bytes sent from zero.
      const filtered = await filterDimension(selectedFile, dimension, setUploadProgress)

      setPhase('uploading')
      setUploadProgress(0)
      const form = new FormData()
      form.append('file', filtered)
      form.append('block_type', blockType)
      form.append('dimension', dimension)

      const parsed = await postWithUploadProgress(
        API_URL,
        form,
        setUploadProgress,
        () => setPhase('processing'),
      )
      setResult(parsed)
    } catch (submissionError) {
      setError(submissionError instanceof Error ? submissionError.message : 'Something went wrong')
      setOversize(false)
    } finally {
      setPhase('idle')
    }
  }

  return (
    <main className="relative min-h-screen overflow-x-hidden bg-[#06040d] text-white">
      <div className="bg-blobs" aria-hidden="true">
        <div className="blob blob-1" />
        <div className="blob blob-2" />
        <div className="blob blob-3" />
        <div className="blob blob-4" />
      </div>
      <div className="relative z-10 mx-auto flex w-full max-w-2xl flex-col items-center px-6 pt-14 text-center sm:pt-20">
        {/* Header */}
        <div className="w-full">
          {/* Tighter size/tracking on phones so the wide letter-spacing doesn't
              overflow the viewport; full drama from sm up. */}
          <p className="mb-10 inline-block text-4xl uppercase tracking-[0.4em] text-violet-200/70 sm:mb-14 sm:text-6xl sm:tracking-[1em]">
            TAROT
          </p>
          <h1 className="text-2xl font-semibold tracking-tight text-white sm:text-5xl lg:text-4xl">
            Instantly plot any block type in your Minecraft world
          </h1>
          <p className="mx-auto mt-4 max-w-xl text-sm leading-7 text-slate-300 sm:text-base">
            Locate forgotten nether portals, builds, or previously explored areas
          </p>
        </div>

        {/* Input */}
        <section className="mt-10 w-full rounded-[2rem] border border-violet-300/20 bg-slate-950/60 p-6 shadow-2xl shadow-black/30 backdrop-blur-xl sm:p-8">
          <div className="grid gap-4">
            <label className="grid gap-2 text-left">
              <span className="text-xs font-medium uppercase tracking-[0.35em] text-violet-200/70">
                Zip file
              </span>
              <input
                type="file"
                accept=".zip,application/zip"
                onChange={(event) => {
                  const file = event.target.files?.[0] ?? null
                  if (file && file.size > MAX_FILE_BYTES) {
                    setError(`That file is ${formatSize(file.size)}. The maximum is ${MAX_FILE_LABEL}.`)
                    setOversize(true)
                    setSelectedFile(null)
                    event.target.value = ''
                    return
                  }
                  setError(null)
                  setOversize(false)
                  setSelectedFile(file)
                }}
                className="rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-sm text-slate-200 file:mr-4 file:rounded-full file:border-0 file:bg-violet-400/20 file:px-4 file:py-2 file:text-sm file:font-medium file:text-violet-100 hover:bg-white/7"
              />
              <span className="text-xs text-slate-500">Zipped save folder · up to {MAX_FILE_LABEL}</span>
            </label>

            <label className="grid gap-2 text-left">
              <span className="text-xs font-medium uppercase tracking-[0.35em] text-violet-200/70">
                Block type
              </span>
              <input
                type="text"
                value={blockType}
                onChange={(event) => setBlockType(event.target.value)}
                placeholder="minecraft:nether_portal"
                className="rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-sm text-slate-100 outline-none placeholder:text-slate-500 focus:border-violet-300/40"
              />
            </label>

            <div className="grid gap-2 text-left">
              <span className="text-xs font-medium uppercase tracking-[0.35em] text-violet-200/70">
                Dimension
              </span>
              <div role="group" aria-label="Dimension" className="grid grid-cols-3 gap-2">
                {DIMENSIONS.map((option) => {
                  const selected = dimension === option.id
                  return (
                    <button
                      key={option.id}
                      type="button"
                      aria-pressed={selected}
                      disabled={busy}
                      onClick={() => setDimension(option.id)}
                      className={`rounded-2xl border px-4 py-3 text-sm font-medium transition disabled:cursor-not-allowed disabled:opacity-60 ${
                        selected
                          ? 'border-violet-300/40 bg-violet-500/20 text-violet-50 shadow-sm shadow-violet-500/20'
                          : 'border-white/10 bg-white/5 text-slate-300 hover:bg-white/10'
                      }`}
                    >
                      {option.label}
                    </button>
                  )
                })}
              </div>
            </div>

            <button
              type="button"
              onClick={handleSubmit}
              disabled={busy}
              className="rounded-full border border-violet-300/20 bg-violet-500/10 px-5 py-3 text-sm font-medium text-violet-100 shadow-sm transition hover:bg-violet-400/20 disabled:cursor-not-allowed disabled:opacity-60"
            >
              {phase === 'filtering'
                ? 'Preparing...'
                : phase === 'uploading'
                  ? 'Uploading...'
                  : phase === 'processing'
                    ? 'Scanning...'
                    : 'Scan world'}
            </button>
          </div>

          {/* Filtering phase — paring the save down to one dimension in-browser,
              with a real progress bar for the compress/trim work. */}
          {phase === 'filtering' ? (
            <div className="mt-6 text-left">
              <div className="mb-2 flex items-center justify-between text-xs font-medium uppercase tracking-[0.35em] text-violet-200/70">
                <span>Compressing world</span>
                <span className="tabular-nums text-violet-100">{uploadProgress}%</span>
              </div>
              <div className="h-2 w-full overflow-hidden rounded-full border border-white/10 bg-white/5">
                <div
                  className="h-full rounded-full bg-gradient-to-r from-violet-400 to-indigo-400 shadow-[0_0_12px_rgba(168,85,247,0.6)] transition-all duration-300"
                  style={{ width: `${uploadProgress}%` }}
                />
              </div>
              <p className="mt-2 text-xs text-slate-500">
                Paring your world down to the {dimensionLabel(dimension)} — only those region files travel.
              </p>
            </div>
          ) : null}

          {/* Upload phase — real, byte-level progress for the upload only. */}
          {phase === 'uploading' ? (
            <div className="mt-6 text-left">
              <div className="mb-2 flex items-center justify-between text-xs font-medium uppercase tracking-[0.35em] text-violet-200/70">
                <span>Uploading world</span>
                <span className="tabular-nums text-violet-100">{uploadProgress}%</span>
              </div>
              <div className="h-2 w-full overflow-hidden rounded-full border border-white/10 bg-white/5">
                <div
                  className="h-full rounded-full bg-gradient-to-r from-violet-400 to-indigo-400 shadow-[0_0_12px_rgba(168,85,247,0.6)] transition-all duration-300"
                  style={{ width: `${uploadProgress}%` }}
                />
              </div>
              <p className="mt-2 text-xs text-slate-500">Sending your save to the server — the scan begins once it lands.</p>
            </div>
          ) : null}

          {/* Processing phase — the backend gives no percent, so the determinate
              bar is replaced by an indeterminate sweep with cycling flavor text. */}
          {phase === 'processing' ? (
            <div className="mt-6 text-left">
              <div className="mb-2 flex items-center justify-between text-xs font-medium uppercase tracking-[0.35em] text-violet-200/70">
                <span>Scanning world</span>
              </div>
              <div className="relative h-2 w-full overflow-hidden rounded-full border border-white/10 bg-white/5">
                <span className="indeterminate-bar" />
              </div>
              <p
                className={`mt-2 text-xs text-violet-100/90 transition-opacity duration-300 ${messageVisible ? 'opacity-100' : 'opacity-0'}`}
              >
                {PROCESSING_MESSAGES[messageIndex]}
              </p>
            </div>
          ) : null}

          {error ? (
            <div className="mt-5 rounded-2xl border border-red-400/20 bg-red-500/10 px-4 py-3 text-sm text-red-100">
              <p>{error}</p>
              {oversize ? (
                <p className="mt-2 text-red-100/80">
                  Too large to upload? Trim away unexplored chunks with{' '}
                  <a
                    href="https://github.com/Querz/mcaselector"
                    target="_blank"
                    rel="noreferrer"
                    className="underline underline-offset-2 hover:text-white"
                  >
                    MCA Selector
                  </a>
                  , then re-zip your save and try again.
                </p>
              ) : null}
            </div>
          ) : null}

          {result ? (
            <div className="mt-6">
              <div className="rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-sm text-slate-200">
                <p>
                  Found {result.count} matching blocks for{' '}
                  <span className="text-violet-200">{result.block_type}</span> in the{' '}
                  <span className="text-violet-200">{dimensionLabel(result.dimension)}</span>.
                </p>
                {result.count > 0 ? (
                  <button
                    type="button"
                    onClick={copyCoordinates}
                    className="mt-3 inline-flex items-center gap-2 rounded-full border border-violet-300/20 bg-violet-500/10 px-3 py-1.5 text-xs font-medium text-violet-100 transition hover:bg-violet-400/20"
                  >
                    {copied ? 'Copied' : 'Copy coordinates as JSON'}
                  </button>
                ) : null}
              </div>
            </div>
          ) : (null)}
        </section>
      </div>

      {/* Divider between the upload section and the map */}
      <div className="relative z-10 mx-auto mt-16 h-px w-full max-w-2xl bg-gradient-to-r from-transparent via-violet-300/25 to-transparent" />

      {/* Map — square, sized to 90% of the smaller viewport dimension. max-w-full
          + aspect-square keeps it square while never spilling past the page
          padding on narrow portrait phones (where 90vmin == 90vw). */}
      <div className="relative z-10 mx-auto mt-12 flex w-full flex-col items-center px-6">
        <div className="relative aspect-square w-[90vmin] max-w-full overflow-hidden rounded-3xl border border-violet-300/15 bg-slate-950/40 shadow-2xl shadow-black/40 backdrop-blur-sm">
          {result ? (
            <WorldMap matches={result.matches} />
          ) : (
            <div className="absolute inset-0 flex items-center justify-center">
              {busy ? (
                <PortalLoader />
              ) : (
                <p className="text-xs text-slate-600">Upload a world zip and hit Scan to generate the map</p>
              )}
            </div>
          )}
          {/* While scanning, keep the loader visible even if a previous map is shown */}
          {busy && result ? (
            <div className="absolute inset-0 flex items-center justify-center bg-slate-950/60 backdrop-blur-sm">
              <PortalLoader />
            </div>
          ) : null}
        </div>
        <p className="mt-3 text-center text-xs text-slate-500">
          Drag to pan · scroll to zoom · hover a pin for its coordinates
        </p>
      </div>

      {/* Divider between the map and the footer */}
      <div className="relative z-10 mx-auto mt-16 h-px w-full max-w-2xl bg-gradient-to-r from-transparent via-violet-300/25 to-transparent" />

      {/* Footer */}
      <footer className="relative z-10 pb-16">
        <p className="mx-auto mt-8 max-w-2xl text-center text-sm text-slate-400 sm:text-base">
          Created by{' '}
          <a
            href="https://sambrothers0.github.io"
            className="text-violet-200 underline underline-offset-4 transition hover:text-violet-100"
            target="_blank"
            rel="noreferrer"
          >
            Sam Brothers
          </a>
        </p>

        <div className="mx-auto mt-6 flex items-center justify-center gap-3">
          <a
            href="https://github.com/sambrothers0/portal-mapper"
            className="rounded-full p-1.5 text-violet-200/70 transition hover:bg-white/10 hover:text-violet-100"
            target="_blank"
            rel="noreferrer"
            aria-label="GitHub"
          >
            <svg viewBox="0 0 24 24" fill="currentColor" className="h-4 w-4" aria-hidden="true">
              <path d="M12 .5C5.37.5 0 5.78 0 12.29c0 5.21 3.44 9.63 8.21 11.19.6.11.82-.25.82-.56 0-.28-.01-1.02-.02-2-3.34.71-4.04-1.59-4.04-1.59-.55-1.38-1.34-1.75-1.34-1.75-1.09-.74.08-.73.08-.73 1.21.08 1.84 1.23 1.84 1.23 1.07 1.8 2.81 1.28 3.5.98.11-.76.42-1.28.76-1.57-2.67-.3-5.47-1.31-5.47-5.83 0-1.29.47-2.34 1.23-3.17-.12-.3-.53-1.52.12-3.17 0 0 1-.32 3.3 1.21.96-.26 1.98-.39 3-.4 1.02.01 2.04.14 3 .4 2.3-1.53 3.3-1.21 3.3-1.21.65 1.65.24 2.87.12 3.17.77.83 1.23 1.88 1.23 3.17 0 4.53-2.81 5.53-5.49 5.82.43.36.81 1.08.81 2.18 0 1.57-.01 2.84-.01 3.23 0 .31.21.68.83.56C20.57 21.91 24 17.49 24 12.29 24 5.78 18.63.5 12 .5z" />
            </svg>
          </a>
          <div className="h-4 w-px bg-violet-300/20" />
          <a
            href="https://buymeacoffee.com/sambrothers"
            className="rounded-full p-1.5 text-violet-200/70 transition hover:bg-white/10 hover:text-violet-100"
            target="_blank"
            rel="noreferrer"
            aria-label="Buy me a coffee"
          >
            <svg
              viewBox="0 0 24 24"
              fill="none"
              stroke="currentColor"
              strokeWidth={2}
              strokeLinecap="round"
              strokeLinejoin="round"
              className="h-4 w-4"
              aria-hidden="true"
            >
              <path d="M17 8h1a4 4 0 1 1 0 8h-1" />
              <path d="M3 8h14v9a4 4 0 0 1-4 4H7a4 4 0 0 1-4-4Z" />
              <line x1="6" x2="6" y1="2" y2="4" />
              <line x1="10" x2="10" y1="2" y2="4" />
              <line x1="14" x2="14" y1="2" y2="4" />
            </svg>
          </a>
        </div>
      </footer>
    </main>
  )
}

export default App
