/// <reference lib="webworker" />

import JSZip from 'jszip'

export {}

type ZipPrinterMessage = {
  type: 'print-zip'
  fileName: string
  buffer: ArrayBuffer
}

type ZipPrinterResult = {
  type: 'printed-zip'
  fileName: string
  entryCount: number
}

self.addEventListener('message', async (event: MessageEvent<ZipPrinterMessage>) => {
  const { fileName, buffer } = event.data

  console.log('Zip worker received archive:', fileName)

  const zip = await JSZip.loadAsync(buffer)
  const entries = Object.values(zip.files)

  for (const [index, entry] of entries.entries()) {
    if (entry.dir) {
      console.log(`[${index + 1}/${entries.length}] ${entry.name}`, {
        dir: true,
      })
      continue
    }

    const data = await entry.async('uint8array')

    console.log(`[${index + 1}/${entries.length}] ${entry.name}`, {
      dir: false,
      size: data.byteLength,
    })
  }

  const result: ZipPrinterResult = {
    type: 'printed-zip',
    fileName,
    entryCount: entries.length,
  }

  self.postMessage(result)
})