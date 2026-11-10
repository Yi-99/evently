import type { SlideData } from '../../data/demoDeck'

// One 16:9 slide. Text is sized in container units so the same slide works as a thumbnail,
// on the presenter's screen and on a phone.
export default function Slide({ slide, className = '' }: { slide: SlideData; className?: string }) {
  if (slide.kind === 'image') return <ImageSlide image={slide.image} alt={slide.alt} className={className} />
  const dark = slide.kind !== 'list'
  return (
    // The wrapper is the size container: an element can't size itself in its own cqw units.
    <div className="@container w-full">
      <div
        className={`relative aspect-video w-full overflow-hidden rounded-[3cqw] ${
          dark ? 'bg-[linear-gradient(135deg,#312e81,#4f46e5_55%,#a855f7)] text-white' : 'bg-white text-gray-900'
        } ${className}`}
      >
        {dark && (
          <div
            aria-hidden
            className="absolute -top-1/3 -right-1/4 size-[80cqw] rounded-full bg-[radial-gradient(closest-side,#f472b680,transparent)]"
          />
        )}
        <div className="relative flex h-full flex-col justify-center px-[8cqw] py-[6cqw]">
          {slide.kind === 'list' ? (
            <>
              <h2 className="text-[5.5cqw] leading-tight font-bold tracking-tight">{slide.title}</h2>
              <ul className="mt-[3.5cqw] space-y-[1.6cqw] text-[3cqw] text-gray-600">
                {slide.points.map((p) => (
                  <li key={p} className="flex items-baseline gap-[1.6cqw] whitespace-pre-wrap">
                    <span className="size-[1cqw] shrink-0 -translate-y-[0.3cqw] rounded-full bg-accent" />
                    {p}
                  </li>
                ))}
              </ul>
            </>
          ) : (
            <>
              <h2 className={`${slide.kind === 'cover' ? 'text-[7.5cqw]' : 'text-[9cqw]'} leading-[1.05] font-extrabold tracking-tight`}>
                {slide.title}
              </h2>
              {slide.subtitle && <p className="mt-[2.5cqw] max-w-[70cqw] text-[3cqw] text-indigo-100">{slide.subtitle}</p>}
            </>
          )}
        </div>
      </div>
    </div>
  )
}

// An uploaded slide, letterboxed into 16:9 so 4:3 decks keep their shape.
// One URL per image for the life of the tab, so thumbnails and the main view share it.
const urls = new WeakMap<Blob, string>()
const urlFor = (b: Blob) => urls.get(b) ?? (urls.set(b, URL.createObjectURL(b)), urls.get(b)!)

function ImageSlide({ image, alt, className }: { image: Blob; alt: string; className: string }) {
  const src = urlFor(image)
  return (
    <div className="@container w-full">
      <div className={`relative aspect-video w-full overflow-hidden rounded-[3cqw] bg-gray-950 ${className}`}>
        <img src={src} alt={alt} className="size-full object-contain" draggable={false} />
      </div>
    </div>
  )
}
