// Minimal JSON <-> WebAuthn conversions (avoids depending on parse*FromJSON support).
const b64urlToBuf = (s: string) => {
  const pad = '='.repeat((4 - (s.length % 4)) % 4)
  const b = atob((s + pad).replace(/-/g, '+').replace(/_/g, '/'))
  const u = new Uint8Array(b.length)
  for (let i = 0; i < b.length; i++) u[i] = b.charCodeAt(i)
  return u.buffer
}
const bufToB64url = (buf: ArrayBuffer) => {
  const u = new Uint8Array(buf)
  let s = ''
  for (let i = 0; i < u.length; i++) s += String.fromCharCode(u[i])
  return btoa(s).replace(/\+/g, '-').replace(/\//g, '_').replace(/=+$/, '')
}

export function passkeysSupported(): boolean {
  return typeof window !== 'undefined' && !!window.PublicKeyCredential && !!navigator.credentials && window.isSecureContext
}

export async function createPasskey(options: any): Promise<any> {
  const publicKey: PublicKeyCredentialCreationOptions = {
    ...options,
    challenge: b64urlToBuf(options.challenge),
    user: { ...options.user, id: b64urlToBuf(options.user.id) },
    excludeCredentials: (options.excludeCredentials || []).map((c: any) => ({ ...c, id: b64urlToBuf(c.id) })),
  }
  const cred = (await navigator.credentials.create({ publicKey })) as PublicKeyCredential | null
  if (!cred) throw new Error('No credential returned')
  const r = cred.response as AuthenticatorAttestationResponse
  return {
    id: cred.id, rawId: bufToB64url(cred.rawId), type: cred.type,
    response: {
      clientDataJSON: bufToB64url(r.clientDataJSON), attestationObject: bufToB64url(r.attestationObject),
      transports: typeof r.getTransports === 'function' ? r.getTransports() : [],
    },
    clientExtensionResults: cred.getClientExtensionResults?.() || {},
    authenticatorAttachment: (cred as any).authenticatorAttachment ?? null,
  }
}

export async function getPasskey(options: any): Promise<any> {
  const publicKey: PublicKeyCredentialRequestOptions = {
    ...options,
    challenge: b64urlToBuf(options.challenge),
    allowCredentials: (options.allowCredentials || []).map((c: any) => ({ ...c, id: b64urlToBuf(c.id) })),
  }
  const cred = (await navigator.credentials.get({ publicKey })) as PublicKeyCredential | null
  if (!cred) throw new Error('No credential returned')
  const r = cred.response as AuthenticatorAssertionResponse
  return {
    id: cred.id, rawId: bufToB64url(cred.rawId), type: cred.type,
    response: {
      clientDataJSON: bufToB64url(r.clientDataJSON), authenticatorData: bufToB64url(r.authenticatorData),
      signature: bufToB64url(r.signature), userHandle: r.userHandle ? bufToB64url(r.userHandle) : null,
    },
    clientExtensionResults: cred.getClientExtensionResults?.() || {},
    authenticatorAttachment: (cred as any).authenticatorAttachment ?? null,
  }
}

export function describeWebAuthnError(e: any): string {
  const name = e?.name
  if (name === 'NotAllowedError') return 'The passkey request was cancelled or timed out. Nothing was changed.'
  if (name === 'InvalidStateError') return 'This authenticator already has a passkey for this account.'
  if (name === 'SecurityError') return 'The browser blocked the request because this address does not match the configured passkey hostname, or the page is not served over HTTPS.'
  if (name === 'NotSupportedError') return 'This browser or authenticator does not support the required passkey type.'
  return e?.message || 'Passkey operation failed.'
}
