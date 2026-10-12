export async function loadCatalog(url = '/frontend/preview/catalog.json', fetchImpl = fetch) {
  const response = await fetchImpl(url);
  if (!response.ok) throw new Error('页面目录读取失败');
  const pages = await response.json();
  const seen = new Set();
  if (!Array.isArray(pages) || !pages.length) throw new Error('页面目录为空');
  for (const page of pages) {
    if (!/^[a-z][a-z-]*$/.test(page.id) || seen.has(page.id) || !page.label || !page.group || !page.content_version ||
        !['demo','development','new'].includes(page.origin) || typeof page.hidden !== 'boolean' ||
        !Array.isArray(page.annual_tasks) || !page.annual_tasks.length ||
        !page.annual_tasks.every(x => /^Y1-0[1-4]$|^Y2-0[1-2]$|^Y3-0[1-6]$/.test(x)) ||
        !['normal','empty','error','denied','stale','unconnected'].every(x => page.scenarios?.includes(x))) {
      throw new Error('页面目录无效');
    }
    seen.add(page.id);
  }
  return pages;
}
