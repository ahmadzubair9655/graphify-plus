export async function fetchJson<T>(url: string): Promise<T> {
  const r = await fetch(url);
  return r.json();
}
