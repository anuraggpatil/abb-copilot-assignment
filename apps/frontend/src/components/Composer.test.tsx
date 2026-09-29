/**
 * The message box, on its own.
 *
 * The behaviours here are small but each one has a way of quietly regressing: a textarea that
 * swallows Enter turns a chat back into a form; one that sends on Shift+Enter makes long questions
 * impossible to type; and a box that does not clear invites the same question twice.
 */

import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { describe, expect, it, vi } from 'vitest'

import Composer from './Composer'

function setup(overrides: Partial<Parameters<typeof Composer>[0]> = {}) {
  const props = {
    busy: false,
    inConversation: false,
    onAsk: vi.fn(),
    onCancel: vi.fn(),
    onReset: vi.fn(),
    ...overrides,
  }
  render(<Composer {...props} />)
  return { ...props, user: userEvent.setup(), box: screen.getByLabelText('Message the copilot') }
}

describe('the composer', () => {
  it('sends on Enter and keeps the question it sent out of the box', async () => {
    const { user, box, onAsk } = setup()

    await user.clear(box)
    await user.type(box, 'why is pump 101 alarming?{Enter}')

    expect(onAsk).toHaveBeenCalledWith('why is pump 101 alarming?')
    expect(box).toHaveValue('')
  })

  it('makes a newline on Shift+Enter instead of sending', async () => {
    const { user, box, onAsk } = setup()

    await user.clear(box)
    await user.type(box, 'first line{Shift>}{Enter}{/Shift}second line')

    expect(onAsk).not.toHaveBeenCalled()
    expect(box).toHaveValue('first line\nsecond line')
  })

  it('refuses a question too short to investigate', async () => {
    const { user, box, onAsk } = setup()

    await user.clear(box)
    await user.type(box, 'hm{Enter}')

    expect(onAsk).not.toHaveBeenCalled()
  })

  // Before a conversation there is nothing here but the box: the opening screen carries the example
  // questions, and duplicating them as chips put the same three questions on the screen twice.
  it('offers nothing but the box before a conversation exists', () => {
    setup()

    expect(screen.queryByText('Follow-ups')).not.toBeInTheDocument()
    expect(screen.queryByRole('button', { name: 'New conversation' })).not.toBeInTheDocument()
  })

  it('offers follow-up chips and a reset once the thread has a turn', async () => {
    const { user, box, onReset } = setup({ inConversation: true })

    expect(screen.getByText('Follow-ups')).toBeInTheDocument()
    // A chip fills the box rather than sending: a follow-up is often the chip plus a word.
    await user.click(screen.getByRole('button', { name: 'Isolation' }))
    expect((box as HTMLTextAreaElement).value).toContain('isolating it')

    await user.click(screen.getByRole('button', { name: 'New conversation' }))
    expect(onReset).toHaveBeenCalled()
  })

  it('swaps Send for Stop while a turn is running', async () => {
    const { user, onCancel } = setup({ busy: true })

    expect(screen.getByRole('button', { name: 'Investigating…' })).toBeDisabled()
    await user.click(screen.getByRole('button', { name: 'Stop' }))

    expect(onCancel).toHaveBeenCalled()
  })
})
