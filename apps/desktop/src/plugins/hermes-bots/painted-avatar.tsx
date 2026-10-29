import atlas from './painted-bodies.png'

const ROLES = ['chief', 'curious', 'alert', 'dreamer', 'skeptical', 'detective']
const COLORS = ['#69778d', '#39dcb5', '#ffa12a', '#f25c98', '#168dff', '#99d62c']

export function paintedPersonaColor(role: string): string | undefined {
  return COLORS[ROLES.indexOf(role)]
}

/** Painted body textures with independent, clipped eyes and articulated feet. */
export function PaintedAvatar({
  role,
  hovered = false,
  name,
  mood,
  size,
  id
}: {
  role: string
  hovered?: boolean
  name: string
  mood: string
  size: number
  id: string
}) {
  const index = ROLES.indexOf(role)
  const color = COLORS[index]
  const sleepy = role === 'chief' || role === 'skeptical'
  const royal = ['hermes', 'default'].includes(name.toLowerCase())

  return (
    <svg
      aria-hidden
      data-bot-face={name}
      data-hb-hover={hovered ? '1' : '0'}
      data-hb-mood={mood}
      data-hb-painted={role}
      height={size}
      style={{ display: 'block', overflow: 'visible' }}
      viewBox="0 0 100 108"
      width={size}
    >
      <defs>
        <linearGradient id={`${id}-gold`} x1="0" x2="1" y1="0" y2="1">
          <stop stopColor="#fff0ad" />
          <stop offset="0.5" stopColor="#edbd49" />
          <stop offset="1" stopColor="#a56c2b" />
        </linearGradient>
        <clipPath id={`${id}-lids`}>
          <rect data-hb-lids="1" height="100" width="100" x="0" y={sleepy ? 48 : 0} />
        </clipPath>
        <linearGradient id={`${id}-foot`} x1="0" x2="1" y1="0" y2="1">
          <stop stopColor={color} />
          <stop offset="1" stopColor="#092047" />
        </linearGradient>
        {[0, 1].map(i => (
          <clipPath id={`${id}-eye-${i}`} key={i}>
            <ellipse cx={35 + i * 30} cy={48} rx={13} ry={17} />
          </clipPath>
        ))}
      </defs>
      {[0, 1].map(i => (
        <g data-hb-foot={i} key={i}>
          <path
            d={
              i === 0
                ? 'M31 84 Q30 89 30 93 Q22 93 22 98 Q24 101 38 99 L39 87 Z'
                : 'M61 87 L62 99 Q76 101 78 98 Q78 93 70 93 Q70 89 69 84 Z'
            }
            fill={`url(#${id}-foot)`}
          />
        </g>
      ))}
      <g data-hb-painted-body="1">
        <svg
          height="96"
          preserveAspectRatio="none"
          viewBox={`${(index % 3) * 512} ${Math.floor(index / 3) * 512} 512 512`}
          width="100"
          x="0"
          y="0"
        >
          <image height="1024" href={atlas} width="1536" />
        </svg>
        {royal && (
          <g data-hb-crown="1" transform="rotate(-16 45 18)">
            <path
              d="M30 19 L28 3 L37 11 L45 0 L53 11 L62 3 L60 19 Z"
              fill={`url(#${id}-gold)`}
              stroke="#a97432"
              strokeLinejoin="round"
              strokeWidth="1.2"
            />
            <path d="M30 18 H60 V24 Q45 27 30 24 Z" fill={`url(#${id}-gold)`} stroke="#a97432" strokeWidth="1.2" />
            <path d="M32 19 H58" stroke="#fff2bc" strokeWidth="1.5" />
            <path d="M45 18 L48 21 L45 24 L42 21 Z" fill="#bd5573" />
          </g>
        )}
        <g data-hb-painted-face="1">
          {[0, 1].map(i => (
            <g
              clipPath={`url(#${id}-lids)`}
              data-hb-painted-eye={i}
              key={i}
              style={{ transformOrigin: `${35 + i * 30}px 48px`, transform: `scaleY(1)` }}
            >
              <ellipse cx={35 + i * 30} cy="48" fill="#fff8da" rx="13" ry="17" />
              <g clipPath={`url(#${id}-eye-${i})`}>
                <ellipse cx={31 + i * 30} cy="47" data-hb-pupil={i} fill="#061c47" rx="7" ry="11" />
              </g>
            </g>
          ))}
          <path
            d="M24 25 Q32 20 39 24 M60 24 Q68 22 75 28"
            data-hb-painted-brows="1"
            fill="none"
            stroke="#061c47"
            strokeLinecap="round"
            strokeWidth="4.5"
          />
          <path
            d={sleepy ? 'M42 72 Q50 81 59 71' : 'M41 70 Q50 75 60 68 Q59 87 49 84 Q42 82 41 70 Z'}
            data-hb-painted-mouth="1"
            fill={sleepy || role === 'detective' ? 'none' : '#061c47'}
            stroke="#061c47"
            strokeLinecap="round"
            strokeWidth="4"
          />
          {!sleepy && role !== 'detective' && <ellipse cx="51" cy="81" fill="#f27291" rx="5" ry="2.5" />}
        </g>
        {mood === 'work' && (
          <g data-hb-working-hands="1">
            <path d="M78 76 Q83 84 74 88" fill="none" stroke={color} strokeLinecap="round" strokeWidth="5" />
            <g data-hb-tablet="1" transform="rotate(-14 65 80)">
              <rect fill="#182a48" height="27" rx="4" stroke="#9babc0" strokeWidth="1.5" width="34" x="46" y="66" />
              <rect fill="#c5e4de" height="20" rx="2" width="28" x="49" y="69" />
              <path d="M53 74 H70 M53 79 H66 M53 84 H72" stroke="#6b9a94" strokeLinecap="round" strokeWidth="1.6" />
              <circle cx="63" cy="91" fill="#9babc0" r="1" />
              <ellipse cx="78" cy="87" fill={`url(#${id}-foot)`} rx="5" ry="4" />
            </g>
            <g data-hb-tapping-hand="1">
              <path d="M23 73 Q30 84 53 79" fill="none" stroke={color} strokeLinecap="round" strokeWidth="5" />
              <ellipse cx="54" cy="79" fill={`url(#${id}-foot)`} rx="5.5" ry="4" />
              <path d="M54 78 L60 75" stroke={color} strokeLinecap="round" strokeWidth="3" />
            </g>
          </g>
        )}
      </g>
      {mood === 'think' && (
        <g data-hb-thinking-bubble="1">
          <circle cx="73" cy="20" data-hb-thought-puff="0" fill="#fff5db" r="2" />
          <circle cx="79" cy="13" data-hb-thought-puff="1" fill="#fff5db" r="3" />
          <g data-hb-thought-cloud="1">
            <path
              d="M80 4 C76 -2 82 -7 87 -6 C90 -13 100 -12 103 -6 C111 -7 115 1 110 6 C109 13 97 15 93 10 C87 14 78 11 80 4 Z"
              fill="#fff5db"
              stroke="#a5b3c3"
              strokeWidth="1.2"
            />
            {[0, 1, 2].map(i => (
              <circle cx={87 + i * 7} cy="2" data-hb-thought-mark={i} fill="#60738d" key={i} r="1.6" />
            ))}
          </g>
        </g>
      )}
    </svg>
  )
}

const activityEntrances = new WeakMap<SVGSVGElement, { mood: string; since: number }>()

const greetings = new WeakMap<SVGSVGElement, { amount: number; time: number }>()

function greetingAmount(svg: SVGSVGElement, t: number, reduced: boolean) {
  const greeting = greetings.get(svg) || { amount: 0, time: t - 1 / 15 }
  const target = !reduced && svg.getAttribute('data-hb-hover') === '1' ? 1 : 0
  const blend = 1 - Math.exp(-Math.max(0, Math.min(0.1, t - greeting.time)) / 0.12)
  const hello = reduced ? 0 : greeting.amount + (target - greeting.amount) * blend
  greetings.set(svg, { amount: hello, time: t })
  return hello
}

export function paintIllustratedFace(
  svg: SVGSVGElement,
  t: number,
  gazeX: number,
  gazeY: number,
  eyeOpen: number,
  reduced: boolean
) {
  const role = svg.getAttribute('data-hb-painted')
  const sleepy = role === 'chief' || role === 'skeptical'
  const hello = greetingAmount(svg, t, reduced)
  const mood = svg.getAttribute('data-hb-mood') || 'idle'
  const working = mood === 'work'
  const thinking = mood === 'think'
  let entrance = activityEntrances.get(svg)

  if (!entrance || entrance.mood !== mood) {
    entrance = { mood, since: t }
    activityEntrances.set(svg, entrance)
  }

  const reveal = reduced ? 1 : 1 - (1 - Math.min(1, Math.max(0, (t - entrance.since) / 0.35))) ** 3

  if (working) {
    gazeX = 3.4 + Math.sin(t * 1.2) * 0.3
    gazeY = 2.2
  }

  const energy = working ? 2.5 : 1
  gazeX = reduced ? 0 : gazeX * (1 - hello)
  gazeY = reduced ? 0 : gazeY * (1 - hello)
  const breath = reduced ? 0 : Math.sin(t * (working ? 2.8 : 1.7))
  const body = svg.querySelector('[data-hb-painted-body]')
  body?.setAttribute(
    'transform',
    `translate(${reduced ? 0 : gazeX * 0.65} ${breath * -1.2 * energy - hello * 2}) translate(50 88) rotate(${reduced ? 0 : gazeX * 0.65 + (thinking ? -3 + Math.sin(t * 1.3) : working ? Math.sin(t * 2.8) * 1.4 : 0)}) scale(${1 + breath * 0.008 * energy + hello * 0.035} ${1 - breath * 0.008 * energy + hello * 0.035}) translate(-50 -88)`
  )
  svg.querySelector('[data-hb-lids]')?.setAttribute('y', String(sleepy ? 48 - hello * 14 : 0))
  svg.querySelectorAll('[data-hb-painted-eye]').forEach((eye, i) => {
    const baseline = role === 'detective' && i === 1 ? 0.78 : 1

    const open = reduced ? baseline : Math.min(1, eyeOpen / (sleepy ? 0.5 : 1.15)) * baseline

    ;(eye as SVGElement).style.transform = `scaleY(${Math.max(0.035, open)})`
  })
  svg.querySelectorAll('[data-hb-pupil]').forEach((pupil, i) => {
    pupil.setAttribute('cx', String(31 + i * 30 + gazeX * 2 + hello * 4))
    pupil.setAttribute('cy', String(47 + gazeY * 1.8))
  })
  paintExpression(svg, role, sleepy, hello)

  const tap = reduced ? 0 : Math.max(0, Math.sin(t * 6))
  svg.querySelector('[data-hb-tapping-hand]')?.setAttribute('transform', `translate(${tap * 1.5} ${-tap * 1.5})`)
  svg
    .querySelector('[data-hb-thinking-bubble]')
    ?.setAttribute(
      'transform',
      `translate(0 ${reduced ? 0 : (1 - reveal) * 9 - Math.sin(t * 1.4) * 1.2}) translate(85 15) scale(${0.3 + reveal * 0.7}) translate(-85 -15)`
    )
  svg.querySelectorAll('[data-hb-thought-puff], [data-hb-thought-mark]').forEach((puff, i) => {
    puff.setAttribute('opacity', String(reduced ? 1 : 0.45 + 0.55 * Math.max(0, Math.sin(t * 2.2 - i * 0.6))))
  })
  svg.querySelectorAll('[data-hb-foot]').forEach((foot, i) => {
    const step = reduced ? 0 : Math.sin(t * (working ? 5 : role === 'alert' ? 3.2 : 1.7) + i * Math.PI)
    foot.setAttribute(
      'transform',
      `translate(0 ${Math.min(0, step) * (working ? 3 : 1.3) - hello * (i ? 0 : 1)}) rotate(${step * (working ? 7 : 3) + hello * (i ? -7 : 7)} ${i ? 65 : 35} 91)`
    )
  })
}

function paintExpression(svg: SVGSVGElement, role: string | null, sleepy: boolean, hello: number) {
  const brows = svg.querySelector('[data-hb-painted-brows]')
  brows?.setAttribute('transform', `translate(0 ${-hello * 2})`)

  if (role === 'skeptical') {
    brows?.setAttribute('d', 'M23 23 Q31 30 40 27 M60 22 Q69 16 76 25')
  }

  if (role === 'detective') {
    brows?.setAttribute('d', 'M24 23 Q32 17 40 22 M60 29 Q69 32 77 27')
  }

  const mouth = svg.querySelector('[data-hb-painted-mouth]')

  if (role === 'detective') {
    mouth?.setAttribute('d', 'M46 75 Q50 69 54 75')
  } else if (sleepy) {
    mouth?.setAttribute('d', 'M43 73 Q51 79 59 72')
  }

}
