import type { SlideData } from '../data/demoDeck'

// Turns an uploaded slide file into slide images, entirely in the browser.
// A PDF becomes one image per page; a set of images becomes one slide each, in file-name order.

export const ACCEPT = '.pdf,application/pdf,image/png,image/jpeg,image/webp'
const MAX_PAGES = 150
// Rendered width of each PDF page; sharp on a projector without huge files.
const RENDER_WIDTH = 1920

const ext = (f: File) => f.name.split('.').pop()?.toLowerCase() ?? ''
const isPdf = (f: File) => f.type === 'application/pdf' || ext(f) === 'pdf'
const isImage = (f: File) => /^image\/(png|jpeg|webp)$/.test(f.type)

export class SlideFileError extends Error {}

export function deckTitle(files: File[]): string {
  const name = files[0]?.name.replace(/\.[^.]+$/, '').trim()
  return files.length === 1 && name ? name : 'Uploaded slides'
}

export async function readSlides(files: File[], onProgress: (done: number, total: number) => void): Promise<SlideData[]> {
  if (!files.length) throw new SlideFileError('Choose a file to upload.')
  if (files.some((f) => ['ppt', 'pptx', 'key', 'odp'].includes(ext(f))))
    throw new SlideFileError(
      'PowerPoint and Keynote files can’t be shown in the browser. Export your deck as a PDF (in PowerPoint, Keynote or Google Slides: File, then Export or Download as PDF) and upload that.',
    )
  if (files.length === 1 && isPdf(files[0])) return readPdf(files[0], onProgress)
  if (files.every(isImage)) {
    if (files.length > MAX_PAGES) throw new SlideFileError(`That’s ${files.length} images. Upload up to ${MAX_PAGES} slides.`)
    const sorted = [...files].sort((a, b) => a.name.localeCompare(b.name, undefined, { numeric: true }))
    onProgress(sorted.length, sorted.length)
    return sorted.map((f, i) => ({ kind: 'image', image: f, alt: `Slide ${i + 1}` }))
  }
  throw new SlideFileError('Upload one PDF, or PNG, JPG or WebP images (one per slide).')
}

async function readPdf(file: File, onProgress: (done: number, total: number) => void): Promise<SlideData[]> {
  // Loaded on demand: pdf.js is large and only this flow needs it.
  const pdfjs = await import('pdfjs-dist')
  const { default: workerSrc } = await import('pdfjs-dist/build/pdf.worker.min.mjs?url')
  pdfjs.GlobalWorkerOptions.workerSrc = workerSrc

  const task = pdfjs.getDocument({ data: await file.arrayBuffer() })
  let doc
  try {
    doc = await task.promise
  } catch {
    throw new SlideFileError('That PDF couldn’t be opened. It may be damaged or password-protected.')
  }
  if (doc.numPages > MAX_PAGES) throw new SlideFileError(`That PDF has ${doc.numPages} pages. Upload up to ${MAX_PAGES} slides.`)

  const slides: SlideData[] = []
  for (let n = 1; n <= doc.numPages; n++) {
    const page = await doc.getPage(n)
    const viewport = page.getViewport({ scale: RENDER_WIDTH / page.getViewport({ scale: 1 }).width })
    const canvas = document.createElement('canvas')
    canvas.width = Math.round(viewport.width)
    canvas.height = Math.round(viewport.height)
    await page.render({ canvas, viewport }).promise
    const image = await new Promise<Blob>((resolve, reject) =>
      canvas.toBlob((b) => (b ? resolve(b) : reject(new SlideFileError(`Page ${n} couldn’t be rendered.`))), 'image/webp', 0.9),
    )
    slides.push({ kind: 'image', image, alt: `Slide ${n}` })
    page.cleanup()
    onProgress(n, doc.numPages)
  }
  await task.destroy()
  return slides
}
