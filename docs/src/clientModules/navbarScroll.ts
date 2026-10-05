import ExecutionEnvironment from '@docusaurus/ExecutionEnvironment';

// Marks <html> while the page is scrolled, so the navbar can show a shadow only then
// (see `html[data-scrolled]` in custom.css), and keeps smooth scrolling for jumps within a page
// only. Docusaurus loads this on every page.
function update(): void {
  document.documentElement.toggleAttribute('data-scrolled', window.scrollY > 0);
}

if (ExecutionEnvironment.canUseDOM) {
  window.addEventListener('scroll', update, {passive: true});
  update();
}

// A client-side navigation resets the scroll position, so recheck after each route change.
export function onRouteDidUpdate(): void {
  update();
}

// custom.css makes scrolling smooth. A jump within a page, such as a link to a section, should
// animate, but opening another page should start at the top at once instead of scrolling up
// from wherever the last page was. So scrolling is instant for the moment a page change resets it.
export function onRouteUpdate({
  location,
  previousLocation,
}: {
  location: {pathname: string};
  previousLocation: {pathname: string} | null;
}): void {
  if (!previousLocation || previousLocation.pathname === location.pathname) return;
  const root = document.documentElement;
  root.style.scrollBehavior = 'auto';
  window.setTimeout(() => root.style.removeProperty('scroll-behavior'), 200);
}
