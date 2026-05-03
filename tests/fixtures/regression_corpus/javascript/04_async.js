export async function load(url) {
  const r = await fetch(url);
  return r.json();
}
