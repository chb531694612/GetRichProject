export type ApiEnvelope<T> = { level: string; detail?: string; data: T }

let csrfToken = ''

export function setCsrfToken(value: string) {
  csrfToken = value
}

export async function apiGet<T>(path: string): Promise<T> {
  const response = await fetch(path, { credentials: 'same-origin' })
  const body = (await response.json()) as ApiEnvelope<T>
  if (!response.ok) throw new Error(body.detail || `请求失败（${response.status}）`)
  return body.data
}

export async function apiPost<T = Record<string, unknown>>(
  path: string,
  payload: Record<string, unknown>,
): Promise<ApiEnvelope<T>> {
  const response = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'Content-Type': 'application/json', 'X-CSRF-Token': csrfToken },
    body: JSON.stringify(payload),
  })
  const body = (await response.json()) as ApiEnvelope<T>
  if (!response.ok) throw new Error(body.detail || `操作失败（${response.status}）`)
  return body
}

export async function apiUpload<T = Record<string, unknown>>(
  path: string,
  file: File,
  planId: string,
): Promise<ApiEnvelope<T>> {
  const form = new FormData()
  form.append('plan_id', planId)
  form.append('ticket_image', file)
  const response = await fetch(path, {
    method: 'POST',
    credentials: 'same-origin',
    headers: { 'X-CSRF-Token': csrfToken },
    body: form,
  })
  const body = (await response.json()) as ApiEnvelope<T>
  if (!response.ok) throw new Error(body.detail || `上传失败（${response.status}）`)
  return body
}
