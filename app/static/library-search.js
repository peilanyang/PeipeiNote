const timeFormat = new Intl.DateTimeFormat(document.documentElement.lang, { dateStyle: 'short', timeStyle: 'short' });

function localizeTimes(root) {
  for (const time of root.querySelectorAll('time.note-updated[datetime]')) {
    time.textContent = timeFormat.format(new Date(time.dateTime));
  }
}

localizeTimes(document);

const form = document.querySelector('#library-search');
if (form) {
  const topicNotes = document.querySelector('#topic-notes');
  let results = document.querySelector('#search-results');
  let timer;
  let pending;

  function formLocation() {
    const url = new URL(form.action);
    url.search = new URLSearchParams(new FormData(form));
    return url;
  }

  async function updateResults(location) {
    const controller = new AbortController();
    pending = controller;
    const query = new URL(location);
    query.searchParams.set('partial', 'true');
    try {
      const response = await fetch(query, { signal: controller.signal });
      if (!response.ok) throw new Error('Search request failed');
      const body = await response.text();
      if (controller.signal.aborted) return;
      const fragment = new DOMParser().parseFromString(body, 'text/html').querySelector('#search-results');
      if (!fragment) throw new Error('Missing search results');
      localizeTimes(fragment);
      results.replaceWith(fragment);
      results = fragment;
      if (topicNotes) topicNotes.hidden = true;
      else history.replaceState(null, '', location);
    } catch (error) {
      if (controller.signal.aborted) return;
      results.textContent = '暫時無法搜尋，請稍後再試。';
      results.hidden = false;
      if (topicNotes) topicNotes.hidden = true;
    }
  }

  function queueSearch(location = formLocation(), delay = 250) {
    clearTimeout(timer);
    pending?.abort();
    if (topicNotes && !location.searchParams.get('q')?.trim()) {
      results.hidden = true;
      topicNotes.hidden = false;
      return;
    }
    timer = setTimeout(() => updateResults(location), delay);
  }

  form.addEventListener('input', event => {
    if (!event.isComposing) queueSearch();
  });
  form.addEventListener('compositionend', () => queueSearch());

  if (topicNotes) {
    form.addEventListener('submit', event => {
      event.preventDefault();
      queueSearch(formLocation(), 0);
    });
    form.parentElement.addEventListener('click', event => {
      const link = event.target.closest('[data-search-page]');
      if (!link || event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return;
      event.preventDefault();
      queueSearch(new URL(link.href), 0);
    });
  }
}
