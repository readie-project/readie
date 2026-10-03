import ExecutionEnvironment from '@docusaurus/ExecutionEnvironment';

// Marks <html> while the page is scrolled, so the navbar can show a shadow only then
// (see `html[data-scrolled]` in custom.css). Docusaurus loads this on every page.
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
