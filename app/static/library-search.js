const form = document.querySelector('#library-search');
if (form) {
  let timer;
  let pending;
  function queueSearch() {
    clearTimeout(timer);
    pending?.abort();
    timer = setTimeout(async () => {
      const controller = new AbortController();
      pending = controller;
      const location = new URL(form.action);
      location.search = new URLSearchParams(new FormData(form));
      const query = new URL(location);
      query.searchParams.set('partial', 'true');
      try {
        const response = await fetch(query, { signal: controller.signal });
        if (!response.ok || controller.signal.aborted) return;
        const body = await response.text();
        if (controller.signal.aborted) return;
        const fragment = new DOMParser().parseFromString(body, 'text/html').querySelector('#search-results');
        if (fragment) {
          document.querySelector('#search-results').replaceWith(fragment);
          history.replaceState(null, '', location);
        }
      } catch (error) {
        if (error.name !== 'AbortError') console.error('Search request failed', error);
      }
    }, 250);
  }
  form.addEventListener('input', event => {
    if (!event.isComposing) queueSearch();
  });
  form.addEventListener('compositionend', queueSearch);
}
