import type { ReactNode } from 'react'
import businessArt from '../assets/fluent/business.png'
import conferenceArt from '../assets/fluent/conference.png'
import hackathonArt from '../assets/fluent/hackathon.png'
import reunionArt from '../assets/fluent/reunion.png'
import weddingArt from '../assets/fluent/wedding.png'

// Made-up event posters drawn in CSS, for the landing and category pages. Text is sized in
// container units (cqw), so a poster reads the same at any width.
export type PosterId =
  | 'hack24'
  | 'devconf'
  | 'founders'
  | 'wedding'
  | 'reunion'
  | 'launch'
  | 'pitch'
  | 'runclub'
  | 'workshop'
  | 'summer'
  | 'offsite'
  | 'boardgames'
  | 'yoga'

export default function Poster({ id, className = '' }: { id: PosterId; className?: string }) {
  return <div className={`@container relative aspect-square overflow-hidden select-none ${className}`}>{DESIGNS[id]}</div>
}

const DESIGNS: Record<PosterId, ReactNode> = {
  hack24: (
    <div className="flex size-full flex-col items-center justify-between bg-[#1d2cf3] p-[7cqw] text-white">
      <span className="text-[7cqw] font-medium">Austin AI Hackathon</span>
      <span className="font-mono text-[24cqw] leading-none font-bold">&lt;/&gt;</span>
      <span className="text-[5cqw] font-semibold">14 – 15 March</span>
    </div>
  ),
  devconf: (
    <div className="relative size-full bg-[linear-gradient(160deg,#2a1260,#6d28d9_55%,#c026d3)] p-[8cqw] text-white">
      <div className="absolute inset-0 bg-[linear-gradient(#ffffff14_1px,transparent_1px),linear-gradient(90deg,#ffffff14_1px,transparent_1px)] bg-[size:10cqw_10cqw]" />
      <div className="relative font-mono text-[5cqw] tracking-widest text-fuchsia-200">SF · 2 DAYS · 40 TALKS</div>
      <div className="relative mt-[4cqw] text-[19cqw] leading-[0.9] font-extrabold tracking-tight">DEV<br />CONF</div>
      <img src={conferenceArt} alt="" className="absolute right-[6cqw] bottom-[6cqw] w-[38cqw] drop-shadow-xl" />
    </div>
  ),
  founders: (
    <div className="relative flex size-full flex-col items-center justify-center bg-[radial-gradient(circle_at_50%_30%,#7c4a1e,#2b1608_75%)] p-[8cqw] text-center text-[#f6d38f]">
      <span className="text-[5.5cqw] tracking-[0.3em]">GOOD COMPANY</span>
      <span className="mt-[3cqw] font-serif text-[17cqw] leading-[0.9] font-bold">Founders<br />Dinner</span>
      <span className="mt-[4cqw] text-[5cqw] tracking-[0.3em] opacity-70">GOOD FOOD</span>
    </div>
  ),
  wedding: (
    <div className="relative flex size-full flex-col items-center justify-center bg-[#f7efe4] p-[8cqw] text-center text-[#b04a6f]">
      <img src={weddingArt} alt="" className="w-[30cqw]" />
      <span className="mt-[3cqw] text-[5cqw] tracking-[0.35em] text-[#8a7766]">SAVE THE DATE</span>
      <span className="mt-[2cqw] font-serif text-[13cqw] leading-none italic">Maya &amp; Leo</span>
      <span className="mt-[3cqw] text-[5cqw] text-[#8a7766]">06 · 14 · Napa Valley</span>
    </div>
  ),
  reunion: (
    <div className="relative flex size-full flex-col justify-end bg-[#ffd23f] p-[8cqw] text-[#1b1b1b]">
      <div className="absolute -top-[18cqw] -right-[18cqw] size-[70cqw] rounded-full bg-[#ff5c8a]" />
      <img src={reunionArt} alt="" className="absolute top-[10cqw] right-[10cqw] w-[34cqw]" />
      <span className="relative text-[6cqw] font-bold">CLASS OF ’16</span>
      <span className="relative text-[20cqw] leading-[0.85] font-black tracking-tight">RE<br />UNION</span>
    </div>
  ),
  launch: (
    <div className="relative flex size-full flex-col items-center justify-center bg-black p-[8cqw] text-center">
      <div className="absolute inset-[10cqw] rounded-full bg-[conic-gradient(#f0f,#0ff,#ff0,#f0f)] opacity-60 blur-[10cqw]" />
      <span className="relative text-[18cqw] leading-none font-black tracking-tight text-white">LAUNCH</span>
      <span className="relative text-[18cqw] leading-none font-black tracking-tight text-transparent [-webkit-text-stroke:0.5cqw_white]">NIGHT</span>
      <span className="relative mt-[4cqw] font-mono text-[5cqw] text-white/80">DJ SET · DEMOS · 8PM</span>
    </div>
  ),
  pitch: (
    <div className="relative size-full bg-[#fff4c7] p-[8cqw]">
      <span className="block text-[17cqw] leading-[0.95] font-black text-[#e23d6d] [text-shadow:1cqw_1cqw_0_#2b59c3]">PITCH<br />YOUR<br />IDEA</span>
      <img src={businessArt} alt="" className="absolute right-[6cqw] bottom-[6cqw] w-[36cqw] rotate-12" />
    </div>
  ),
  runclub: (
    <div className="relative flex size-full flex-col items-center justify-end bg-[linear-gradient(180deg,#d8f26b,#3b82c4)] p-[8cqw] text-center text-white">
      <div className="absolute inset-x-0 top-[38%] h-[30cqw] bg-[repeating-linear-gradient(100deg,#ffffff33_0_2cqw,transparent_2cqw_8cqw)]" />
      <span className="relative text-[22cqw] leading-[0.85] font-black tracking-tight italic [text-shadow:0_1cqw_3cqw_#0004]">LET’S<br />RUN</span>
    </div>
  ),
  workshop: (
    <div className="relative flex size-full flex-col justify-between bg-[#0f3d3e] p-[8cqw] text-[#bff3e1]">
      <span className="font-mono text-[5cqw]">WORKSHOP · 3–5 PM</span>
      <span className="text-[13cqw] leading-[0.95] font-extrabold">Ship an<br />agent in<br />2 hours</span>
      <span className="w-fit rounded-full bg-[#bff3e1] px-[4cqw] py-[1.5cqw] text-[5cqw] font-semibold text-[#0f3d3e]">Bring a laptop</span>
    </div>
  ),
  summer: (
    <div className="flex size-full flex-col bg-[linear-gradient(180deg,#2d4bff,#8fb8ff_45%,#f5a8c8_70%,#fde6ef)] p-[7cqw] text-center text-white">
      <span className="text-[4.5cqw] font-semibold">AUGUST 15TH</span>
      <span className="text-[12cqw] leading-none font-black tracking-tight">ROOFTOP</span>
      <span className="text-[6cqw] font-semibold">DJ SET &amp; LIGHT SHOW</span>
      <span className="mt-auto rounded-md bg-white/90 py-[1cqw] text-[11cqw] leading-none font-black text-black">SUMMER27</span>
    </div>
  ),
  offsite: (
    <div className="relative flex size-full flex-col justify-end bg-[linear-gradient(170deg,#0e7a5a,#073b2c)] p-[8cqw] text-white">
      <div className="absolute top-[12cqw] left-[10cqw] size-[30cqw] rounded-full bg-[#ffcf6b]" />
      <div className="absolute inset-x-0 bottom-[34cqw] h-[22cqw] bg-[#0a5a42] [clip-path:polygon(0_100%,20%_30%,38%_70%,60%_0,82%_60%,100%_20%,100%_100%)]" />
      <span className="relative text-[6cqw] tracking-[0.25em] opacity-80">TEAM OFFSITE</span>
      <span className="relative text-[16cqw] leading-none font-extrabold">Into the<br />Woods</span>
    </div>
  ),
  yoga: (
    <div className="relative flex size-full flex-col items-center bg-[linear-gradient(180deg,#fde2c7,#fbb6a4_40%,#f472b6_75%,#a78bfa)] p-[8cqw] text-center text-[#5b2142]">
      <div className="absolute top-[44%] left-1/2 size-[38cqw] -translate-x-1/2 rounded-full bg-[radial-gradient(circle,#fff7d6,#fcd34d)] shadow-[0_0_14cqw_#fde68a]" />
      <div className="absolute inset-x-0 bottom-0 h-[34%] bg-[repeating-linear-gradient(180deg,#ffffff55_0_1cqw,transparent_1cqw_4cqw)] [mask-image:linear-gradient(to_bottom,black,transparent)]" />
      <span className="relative text-[5cqw] font-semibold tracking-[0.25em]">6:30 AM · VENICE</span>
      <span className="relative mt-[2cqw] font-serif text-[15cqw] leading-none italic">Sunrise Yoga</span>
      <span className="relative mt-auto rounded-full bg-white/80 px-[4cqw] py-[1.5cqw] text-[5.5cqw] font-semibold">&amp; coffee after</span>
    </div>
  ),
  boardgames: (
    <div className="relative flex size-full items-center justify-center bg-[#e94e2b] p-[8cqw] text-center text-white">
      <div className="absolute inset-[5cqw] rounded-[6cqw] border-[1cqw] border-dashed border-white/50" />
      <img src={hackathonArt} alt="" className="absolute top-[8cqw] left-[8cqw] w-[26cqw] -rotate-12" />
      <span className="relative mt-[18cqw] text-[15cqw] leading-[0.9] font-black">GAME<br />NIGHT</span>
    </div>
  ),
}
