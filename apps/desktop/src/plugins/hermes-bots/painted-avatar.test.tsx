import { render } from '@testing-library/react'
import { describe, expect, it } from 'vitest'

import { PaintedAvatar, paintIllustratedFace } from './painted-avatar'

function mount(role = 'curious') {
  const { container } = render(<PaintedAvatar id={`test-${role}`} mood="idle" name="ceiastaff" role={role} size={36} />)

  return container.querySelector('svg')!
}

function motion(svg: SVGSVGElement) {
  return [
    ...svg.querySelectorAll('[data-hb-foot], [data-hb-painted-body], [data-hb-painted-eye], [data-hb-pupil]')
  ].map(node => node.outerHTML)
}

describe('painted mascot articulation', () => {
  it('keeps painted bodies, clipped pupils and feet in independent layers', () => {
    const svg = mount()
    expect(svg.querySelector('image')?.getAttribute('href')).toBeTruthy()
    expect(svg.querySelectorAll('[data-hb-foot]')).toHaveLength(2)
    expect(svg.querySelectorAll('[clip-path] [data-hb-pupil]')).toHaveLength(2)
    paintIllustratedFace(svg, 1, 2, -1, 1.2, false)
    const before = motion(svg)
    paintIllustratedFace(svg, 2, -2, 1, 0.02, false)
    expect(motion(svg)).not.toEqual(before)
    expect(svg.querySelector<SVGElement>('[data-hb-painted-eye]')?.style.transform).toContain('0.035')
  })

  it('greets the pointer with centered pupils, open eyelids and a foot gesture', () => {
    const svg = mount('chief')
    paintIllustratedFace(svg, 1, -2, 1, 0.5, false)
    expect(Number(svg.querySelector('[data-hb-pupil]')!.getAttribute('cx'))).toBe(27)
    svg.setAttribute('data-hb-hover', '1')

    for (let frame = 1; frame <= 12; frame++) {
      paintIllustratedFace(svg, 1 + frame / 10, -2, 1, 0.5, false)
    }

    expect(Number(svg.querySelector('[data-hb-pupil]')!.getAttribute('cx'))).toBeGreaterThan(34)
    expect(Number(svg.querySelector('[data-hb-lids]')!.getAttribute('y'))).toBeLessThan(35)
    svg.setAttribute('data-hb-hover', '0')

    for (let frame = 1; frame <= 12; frame++) {
      paintIllustratedFace(svg, 2.2 + frame / 10, -2, 1, 0.5, false)
    }

    expect(Number(svg.querySelector('[data-hb-pupil]')!.getAttribute('cx'))).toBeLessThan(28)
  })

  it('uses a visibly stronger body and foot rhythm while working', () => {
    const idle = mount()
    const busy = mount()
    busy.setAttribute('data-hb-mood', 'work')
    paintIllustratedFace(idle, 1, 0, 0, 1.2, false)
    paintIllustratedFace(busy, 1, 0, 0, 1.2, false)
    expect(busy.querySelector('[data-hb-foot]')!.getAttribute('transform')).not.toBe(
      idle.querySelector('[data-hb-foot]')!.getAttribute('transform')
    )
    expect(busy.querySelector('[data-hb-painted-body]')!.getAttribute('transform')).not.toBe(
      idle.querySelector('[data-hb-painted-body]')!.getAttribute('transform')
    )
  })

  it('uses a held tablet for work and emerging thought bubbles, clearing poses on idle', () => {
    const { container, rerender } = render(
      <PaintedAvatar id="state-props" mood="work" name="ceiastaff" role="curious" size={44} />
    )

    expect(container.querySelector('[data-hb-working-hands]')).not.toBeNull()
    expect(container.querySelector('[data-hb-thinking-bubble]')).toBeNull()
    const svg = container.querySelector('svg')!
    paintIllustratedFace(svg, 1, 0, 0, 1.2, true)
    const still = container.innerHTML
    paintIllustratedFace(svg, 20, 0, 0, 1.2, true)
    expect(container.innerHTML).toBe(still)
    rerender(<PaintedAvatar id="state-props" mood="think" name="ceiastaff" role="curious" size={44} />)
    expect(container.querySelector('[data-hb-working-hands]')).toBeNull()
    expect(container.querySelector('[data-hb-thinking-bubble]')).not.toBeNull()
    rerender(<PaintedAvatar id="state-props" mood="idle" name="ceiastaff" role="curious" size={44} />)
    expect(container.querySelector('[data-hb-working-hands], [data-hb-thinking-bubble]')).toBeNull()
  })

  it('crowns only the personal Chief and preserves the same painted body', () => {
    const { container, rerender } = render(
      <PaintedAvatar id="royal" mood="idle" name="default" role="chief" size={44} />
    )

    const body = container.querySelector('image')!.getAttribute('href')
    expect(container.querySelector('[data-hb-crown]')!.getAttribute('transform')).toContain('rotate(-16')
    rerender(<PaintedAvatar id="royal" mood="idle" name="applausestaff" role="chief" size={44} />)
    expect(container.querySelector('[data-hb-crown]')).toBeNull()
    expect(container.querySelector('image')!.getAttribute('href')).toBe(body)
  })

  it('parks all motion and resets gaze when reduced motion is requested', () => {
    const svg = mount('skeptical')
    svg.setAttribute('data-hb-hover', '1')
    svg.setAttribute('data-hb-mood', 'work')
    paintIllustratedFace(svg, 1, 2, -1, 0.02, true)
    const rest = motion(svg)
    paintIllustratedFace(svg, 10, -2, 1, 1.2, true)
    expect(motion(svg)).toEqual(rest)
  })
})
