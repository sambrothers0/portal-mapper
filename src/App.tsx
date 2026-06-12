import { useState } from 'react'
import { PortalLoader } from './PortalLoader'

type BlockMatch = {
  x: number
  y: number
  z: number
}

type ParseResult = {
  block_type: string
  count: number
  matches: BlockMatch[]
}

const API_URL = import.meta.env.VITE_API_URL ?? 'http://localhost:8000/parse-blocks'
const DEFAULT_BLOCK = 'minecraft:nether_portal'

const MAX_FILE_BYTES = 4 * 1024 * 1024 * 1024 // 4 GB
const MAX_FILE_LABEL = '4 GB'

const formatSize = (bytes: number) => `${(bytes / 1024 / 1024 / 1024).toFixed(2)} GB`

function App() {
  const [selectedFile, setSelectedFile] = useState<File | null>(null)
  const [blockType, setBlockType] = useState(DEFAULT_BLOCK)
  const [loading, setLoading] = useState(false)
  const [result, setResult] = useState<ParseResult | null>(null)
  const [error, setError] = useState<string | null>(null)

  const handleSubmit = async () => {
    if (!selectedFile) {
      setError('Choose a zip file first.')
      return
    }

    if (selectedFile.size > MAX_FILE_BYTES) {
      setError(`That file is ${formatSize(selectedFile.size)}. The maximum is ${MAX_FILE_LABEL}.`)
      return
    }

    setLoading(true)
    setError(null)
    setResult(null)

    try {
      const form = new FormData()
      form.append('file', selectedFile)
      form.append('block_type', blockType)

      const response = await fetch(API_URL, {
        method: 'POST',
        body: form,
      })

      if (!response.ok) {
        const payload = (await response.json().catch(() => null)) as { detail?: string } | null
        throw new Error(payload?.detail ?? `Request failed with status ${response.status}`)
      }

      const parsed = (await response.json()) as ParseResult
      console.log(parsed)
      setResult(parsed)
    } catch (submissionError) {
      setError(submissionError instanceof Error ? submissionError.message : 'Something went wrong')
    } finally {
      setLoading(false)
    }
  }

  return (
    <main className="relative min-h-screen overflow-hidden bg-[#06040d] text-white">
      <div className="pointer-events-none absolute inset-0 bg-[radial-gradient(circle_at_top_left,rgba(168,85,247,0.22),transparent_32%),radial-gradient(circle_at_80%_20%,rgba(99,102,241,0.18),transparent_28%),linear-gradient(180deg,rgba(255,255,255,0.02),transparent_45%)]" />
      <div className="pointer-events-none absolute inset-x-0 top-0 h-px bg-gradient-to-r from-transparent via-violet-300/60 to-transparent" />

      <div className="relative flex min-h-screen">
        {/* Left panel */}
        <div className="flex w-[55%] flex-col justify-center px-10 lg:px-16">
          <div className="w-full">
            <p className="mb-6 inline-block text-6xl uppercase tracking-[1em] text-violet-200/70">
              TAROT
            </p>
            <h1 className="text-2xl font-semibold tracking-tight text-white sm:text-5xl lg:text-4xl">
              Instantly visualize any block type in your Minecraft world
            </h1>
            <p className="mt-4 max-w-2xl text-sm leading-7 text-slate-300 sm:text-base">
              Locate forgotten nether portals, builds, or previously explored areas
            </p>
          </div>

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
                      setSelectedFile(null)
                      event.target.value = ''
                      return
                    }
                    setError(null)
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

              <button
                type="button"
                onClick={handleSubmit}
                disabled={loading}
                className="rounded-full border border-violet-300/20 bg-violet-500/10 px-5 py-3 text-sm font-medium text-violet-100 shadow-sm transition hover:bg-violet-400/20 disabled:cursor-not-allowed disabled:opacity-60"
              >
                {loading ? 'Scanning...' : 'Scan world'}
              </button>
            </div>

            {error ? (
              <p className="mt-5 rounded-2xl border border-red-400/20 bg-red-500/10 px-4 py-3 text-sm text-red-100">
                {error}
              </p>
            ) : null}

            {result ? (
              <div className="mt-6">
                <div className="rounded-2xl border border-white/10 bg-white/5 px-4 py-3 text-sm text-slate-200">
                  Found {result.count} matching blocks for <span className="text-violet-200">{result.block_type}</span>.
                </div>
              </div>
            ) : (
              <p className="mt-6 text-sm leading-7 text-slate-300">
                Upload a zipped save folder, enter a block ID, and the results will appear here.
              </p>
            )}
          </section>
        </div>

        {/* Right panel — map placeholder */}
        <div className="flex w-[45%] items-center justify-center p-8">
          <div className="relative aspect-square w-full rounded-2xl border border-violet-300/10 bg-white/[0.015]">
            {loading ? (
              <div className="absolute inset-0 flex items-center justify-center">
                <PortalLoader />
              </div>
            ) : !result ? (
              <div className="absolute inset-0 flex items-center justify-center">
                <p className="text-xs text-slate-600">Upload a world zip and hit Scan to generate the map</p>
              </div>
            ) : null}
          </div>
        </div>
      </div>
    </main>
  )
}

export default App
