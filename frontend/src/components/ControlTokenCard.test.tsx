import { beforeEach, describe, expect, it } from 'vitest'
import { render, screen } from '@testing-library/react'
import userEvent from '@testing-library/user-event'
import { AxiosHeaders, type InternalAxiosRequestConfig } from 'axios'
import ControlTokenCard from './ControlTokenCard'
import { api, buildWsUrl, getControlToken, isAuthError, isControlTokenError } from '../api/client'

beforeEach(() => localStorage.clear())

describe('control token', () => {
  it('saves the token in this browser and the API client sends it as X-Control-Token', async () => {
    render(<ControlTokenCard />)
    expect(screen.getByTestId('control-token-status')).toHaveTextContent('no token saved')
    const user = userEvent.setup()
    await user.type(screen.getByLabelText('Control token', { selector: 'input' }), 's3cr3t')
    await user.click(screen.getByRole('button', { name: 'Save' }))
    expect(screen.getByTestId('control-token-status')).toHaveTextContent('token saved')
    expect(getControlToken()).toBe('s3cr3t')
    const handler = (api.interceptors.request as unknown as {
      handlers: Array<{ fulfilled: (c: InternalAxiosRequestConfig) => InternalAxiosRequestConfig }>
    }).handlers[0]
    const cfg = handler.fulfilled({ headers: new AxiosHeaders() } as InternalAxiosRequestConfig)
    expect(cfg.headers.get('X-Control-Token')).toBe('s3cr3t')
    localStorage.clear()
    const none = handler.fulfilled({ headers: new AxiosHeaders() } as InternalAxiosRequestConfig)
    expect(none.headers.get('X-Control-Token')).toBeUndefined()      // no header when no token is saved
  })

  it('can clear the token', async () => {
    localStorage.setItem('control_token', 'abc')
    render(<ControlTokenCard />)
    expect(screen.getByTestId('control-token-status')).toHaveTextContent('token saved')
    await userEvent.setup().click(screen.getByRole('button', { name: 'Clear' }))
    expect(getControlToken()).toBe('')
    expect(screen.getByTestId('control-token-status')).toHaveTextContent('no token saved')
  })

  it('never renders the token value', () => {
    localStorage.setItem('control_token', 'topsecretvalue')
    render(<ControlTokenCard />)
    expect(document.body.textContent).not.toContain('topsecretvalue')
  })
})

describe('buildWsUrl', () => {
  it('derives the WebSocket URL from the SAME backend base as REST (one implementation)', () => {
    const url = buildWsUrl()
    expect(url).toMatch(/^wss?:\/\/.+\/api\/ws$/)
    expect(url).toContain('localhost')
  })
})

describe('401 classification', () => {
  const upstox = { response: { status: 401, data: { detail: 'Token invalid or expired' } } }
  const control = { response: { status: 401, data: { detail: 'Invalid control token.' } } }
  it('a rejected CONTROL token is not an expired Upstox token (and vice versa)', () => {
    expect(isControlTokenError(control)).toBe(true)
    expect(isAuthError(control)).toBe(false)         // must not raise the "Upstox token expired" banner
    expect(isControlTokenError(upstox)).toBe(false)
    expect(isAuthError(upstox)).toBe(true)
    expect(isAuthError({ response: { status: 401, data: {} } })).toBe(true)   // unchanged legacy behaviour
  })
})
