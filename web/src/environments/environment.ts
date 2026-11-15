// SI-assisted: written with Claude Code (Anthropic LLM) under the author's direction and reviewed by the author.
// API base URL, chosen by where the site is opened:
// - Firebase Hosting (*.web.app / *.firebaseapp.com): the Cloud Run API, called directly. Its CORS_ORIGINS must
//   list the Firebase addresses (docs/RUNBOOK.md, step 10).
// - Anywhere else (ng serve on localhost, or the app served by FastAPI itself): same origin. The dev server
//   proxies /api to localhost:8000.
const CLOUD_RUN_API = 'https://hotspot-api-160194041919.us-central1.run.app';

const host = typeof location !== 'undefined' ? location.hostname : '';
const onFirebase = host.endsWith('.web.app') || host.endsWith('.firebaseapp.com');

export const environment = {
  apiBase: onFirebase ? CLOUD_RUN_API : '',
};
