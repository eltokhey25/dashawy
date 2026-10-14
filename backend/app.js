/**
 * @file   Express application wiring
 * @module app
 *
 * Builds the single Express instance: security headers, CORS, rate limiting, body parsing,
 * every route module, static file serving, and the terminal error handler. Required by
 * server.js and by nothing else.
 *
 * @author Eltokhey
 * @since  2026-09
 */
// Express application wiring.
const path = require('path');
const express = require('express');
const config = require('./config');
const { securityHeaders, corsPolicy } = require('./middleware/security-headers');
const rateLimit = require('./middleware/rate-limit');
const { errorHandler } = require('./utils/errors');

const authRoutes = require('./routes/auth.routes');
const consentRoutes = require('./routes/consent.routes');
const dataSubjectRoutes = require('./routes/data-subject.routes');
const customerRoutes = require('./routes/customer.routes');
const technicianRoutes = require('./routes/technician.routes');
const adminRoutes = require('./routes/admin.routes');
const publicRoutes = require('./routes/public.routes');
const notificationsRoutes = require('./routes/notifications.routes');
const messagesRoutes = require('./routes/messages.routes');
const { authenticate } = require('./middleware/auth');
const refs = require('./services/refs');
const notifications = require('./services/notifications');

const app = express();

// Trust exactly one proxy hop in front of the app, so req.ip (used by the rate
// limiter) is the real client and not a spoofable X-Forwarded-For value — and
// so req.secure reads X-Forwarded-Proto as https behind Railway, which is what
// the CORS middleware uses to recognise a same-origin https request.
// This MUST stay above the CORS middleware below: trust proxy has to be set
// before req.protocol is read for the first time. A value of 0 or an unset
// TRUST_PROXY_HOPS still means "one hop", which is Railway's topology.
app.set(
  'trust proxy',
  Number(process.env.TRUST_PROXY_HOPS) > 0 ? Number(process.env.TRUST_PROXY_HOPS) : 1
);
app.disable('x-powered-by');

// FIX 4: cap request bodies. 100kb is plenty for the JSON payloads here; a
// technician document upload is multipart and is bounded by multer instead.
app.use(express.json({ limit: '100kb' }));
app.use(express.urlencoded({ extended: true, limit: '100kb' }));

// FIX 2: security headers (CSP, nosniff, frame options, referrer policy...).
// HSTS is only emitted when the app is actually served over TLS, otherwise the
// browser would be told to pin http:// in local/dev use.
app.use(
  securityHeaders({
    hsts: config.IS_PRODUCTION,
  })
);

// FIX 3: CORS allow-list replaces the previous `Access-Control-Allow-Origin: *`.
// Same-origin is always allowed — the frontend is served by this process, so
// the request's own origin is the site's own domain and must never depend on
// an env var being remembered. `allowedOrigins` only carries the EXTRA origins
// from CORS_ORIGINS (a second domain, staging, a split dev frontend).
app.use(
  corsPolicy({
    allowedOrigins: config.ALLOWED_ORIGINS,
    allowSameOrigin: true,
    allowlistSource: config.ALLOWED_ORIGINS_SOURCE,
    credentials: true,
  })
);

// FIX 1: rate limiting. A general budget for the whole API plus a much tighter
// one on the auth endpoints, where the only thing an attacker wants is to guess
// a password or spam-register accounts.
const apiLimiter = rateLimit({
  windowMs: 15 * 60 * 1000,
  max: 300,
  name: 'api',
  standardHeaders: true,
  legacyHeaders: false,
  message: { error: 'عدد كبير من الطلبات. حاول مرة أخرى بعد قليل.' },
});

app.use('/api/', apiLimiter);

// Health endpoint.
//
// uptime and version are here so a load balancer or an uptime monitor can tell
// WHICH build answered — a canary that only returns {"ok":true} cannot detect a
// bad deploy, and a process that has wedged its request handler but still holds
// the TCP socket is indistinguishable from a healthy one without it.
//
// `push` reports whether Web Push is usable, which is a degradation the process
// survives rather than fails on: a missing or malformed VAPID key pair leaves
// the server fully up with notifications off, and nothing else in the response
// would hint at why they stopped arriving.
const startedAt = Date.now();

app.get('/api/health', (req, res) => {
  res.json({
    ok: true,
    uptime: Math.floor((Date.now() - startedAt) / 1000),
    version: require('../package.json').version,
    env: config.NODE_ENV,
    push: notifications.isReady(),
  });
});

// Public endpoint exposing the supported specialties (so the UI can render options).
app.get('/api/specialties', (req, res) => res.json({ specialties: refs.getSpecialties(false) }));

// Public endpoint exposing the Alexandria neighborhoods (registration/request forms).
app.get('/api/alexandria-areas', (req, res) => res.json({ areas: config.ALEXANDRIA_AREAS }));

// Public subset of settings (safe values used by the public site).
app.get('/api/settings', (req, res) => {
  const all = refs.getSettings();
  res.json({
    settings: {
      site_name: all.site_name || 'صلحها',
      support_phone: all.support_phone || '',
      support_email: all.support_email || '',
      auto_assign_enabled: all.auto_assign_enabled !== '0',
    },
  });
});

// Auth routes are public.
app.use('/api/auth', authRoutes);

// Legal compliance routes: mounted before the global JWT guard so the router
// can apply per-route authentication; any truly public legal endpoints can
// live here without a token.
app.use('/api/consent', consentRoutes);
app.use('/api/data-subject', dataSubjectRoutes);

// Public technician directory and profiles. Mounted before the global JWT guard
// below so these endpoints stay reachable without a token.
app.use('/api/public', publicRoutes);

// Web Push. Also mounted before the global JWT guard: GET /vapid-public-key is
// needed by a page that may not have a session yet. The subscribe/unsubscribe
// routes carry their own `authenticate`.
app.use('/api/notifications', notificationsRoutes);

// Per-request chat. Mounted before the global guard because the router runs
// `authenticate` itself over its whole surface, including /unread-count.
app.use('/api/messages', messagesRoutes);

// Public static serving for the two public image folders only (profile image and
// portfolio gallery). This must stay before any admin-guarded /uploads mount so
// these paths can never fall through to a protected handler. Note the narrower
// prefixes: the identity-document uploads under uploads/technicians/user-*/
// are intentionally NOT exposed here and stay behind the admin document route.
//
// Extra safety for anything served out of /uploads: `nosniff` stops a browser
// from ignoring the Content-Type and guessing the type, so a file can never be
// reinterpreted as script even if its extension were ever wrong. It must be
// registered BEFORE the static mounts, because once a static handler responds
// the headers can no longer be changed.
app.use('/uploads', (req, res, next) => {
  res.setHeader('X-Content-Type-Options', 'nosniff');
  next();
});
app.use(
  '/uploads/technicians/profiles',
  express.static(path.join(__dirname, '..', 'uploads', 'technicians', 'profiles'))
);
app.use(
  '/uploads/technicians/portfolio',
  express.static(path.join(__dirname, '..', 'uploads', 'technicians', 'portfolio'))
);

// Everything below /api requires a valid JWT.
app.use('/api', authenticate);

// Role-scoped APIs.
app.use('/api/customer', customerRoutes);
app.use('/api/technician', technicianRoutes);
app.use('/api/admin', adminRoutes);

// Uploaded technician documents are no longer served statically. They are
// streamed through GET /api/admin/technicians/:id/document/:type so that the
// admin token is required even for <img>/<a> requests.

// PWA: serve the service worker with the correct scope header and the
// manifest with its proper MIME type. Must be declared before the static
// middleware so these responses win over express.static defaults.
app.get('/sw.js', (req, res) => {
  res.set('Content-Type', 'application/javascript');
  res.set('Service-Worker-Allowed', '/');
  res.sendFile(path.join(__dirname, '..', 'frontend', 'sw.js'));
});

app.get('/manifest.webmanifest', (req, res) => {
  res.set('Content-Type', 'application/manifest+json');
  res.sendFile(path.join(__dirname, '..', 'frontend', 'manifest.webmanifest'));
});

// Clean admin entry points (no .html). /admin serves the standalone admin login;
// the rest serve the existing admin pages. Auth for these pages is enforced
// client-side (JWT in localStorage) via js/admin.js, which sends non-admins to /admin.
const frontendDir = path.join(__dirname, '..', 'frontend');

// Clean entry points for the split auth pages (legacy /auth.html redirects to /login.html).
const authPages = {
  '/login': 'login.html',
  '/register': 'register.html',
  '/register-tech': 'register-tech.html',
  '/change-password': 'change-password.html',
  // Where a Google sign-in lands when the customer has no phone yet. It is a
  // first-class route rather than a redirect target only, so a customer who
  // bookmarks it or reloads mid-onboarding keeps working.
  '/complete-profile': 'complete-profile.html',
  // The Uber-style request wizard. Served from the root rather than under
  // /customer/ because it is the flow the homepage CTA points at; the old
  // /customer/create-request.html form stays as the deep-link fallback for the
  // service- and technician-preset links.
  '/request': 'request.html',
};
Object.entries(authPages).forEach(([route, file]) => {
  app.get([route, `${route}/`], (req, res) => {
    res.sendFile(path.join(frontendDir, file));
  });
});

const adminPages = {
  '/admin': 'admin/login.html',
  '/admin/dashboard': 'admin/dashboard.html',
  '/admin/technicians': 'admin/technicians.html',
  '/admin/finance': 'admin/finance.html',
  '/admin/wallets': 'admin/wallets.html',
  '/admin/requests': 'admin/requests.html',
  '/admin/reviews': 'admin/reviews.html',
  '/admin/customers': 'admin/customers.html',
  '/admin/specialties': 'admin/specialties.html',
  '/admin/settings': 'admin/settings.html',
  '/admin/data-requests': 'admin/data-requests.html',
  '/admin/admins': 'admin/admins.html',
};
Object.entries(adminPages).forEach(([route, file]) => {
  app.get([route, `${route}/`], (req, res) => {
    res.sendFile(path.join(frontendDir, file));
  });
});

// The customer landing page. Registered explicitly (rather than left to the
// static handler) so the .html extension stays optional, like /request and the
// admin pages above: /customer/home and /customer/home.html must both resolve.
const customerPages = {
  '/customer/home': 'customer/home.html',
};
Object.entries(customerPages).forEach(([route, file]) => {
  app.get([route, `${route}/`], (req, res) => {
    res.sendFile(path.join(frontendDir, file));
  });
});

// Static sitemap.
//
// Hand-written rather than generated by a library: the public surface here is a
// fixed set of marketing and auth pages, none of which carry per-user state, so
// a dynamic crawler over the routes would only rediscover these same handful —
// and would waste requests on the authenticated pages behind localStorage auth,
// which it cannot see and which should never be indexed.
//
// Registered before express.static so an explicit route always wins over a
// same-named file in frontend/.
//
// SITE_URL is read from the environment because the domain differs between
// environments, and a sitemap must name the canonical host: pointing Google at
// a Railway preview domain would have it index that host instead of the real
// one. Unset falls back to the production domain used elsewhere in the config.
app.get('/sitemap.xml', (req, res) => {
  const baseUrl = (process.env.SITE_URL || 'https://dashawy.app').replace(/\/+$/, '');
  const today = new Date().toISOString().split('T')[0];
  const pages = [
    { url: '/', priority: '1.0', changefreq: 'weekly' },
    { url: '/login.html', priority: '0.8', changefreq: 'monthly' },
    { url: '/register.html', priority: '0.9', changefreq: 'monthly' },
    { url: '/register-tech.html', priority: '0.8', changefreq: 'monthly' },
    { url: '/privacy.html', priority: '0.3', changefreq: 'yearly' },
    // The machine-readable project description and its human-readable twin. Listed
    // here because a sitemap is how crawlers — including the ones that look for
    // llms.txt — discover what else the site publishes; a file nobody links to
    // from a sitemap is a file most crawlers never fetch.
    { url: '/about-ai.html', priority: '0.5', changefreq: 'monthly' },
    { url: '/llms.txt', priority: '0.5', changefreq: 'monthly' },
  ];
  let xml = '<?xml version="1.0" encoding="UTF-8"?>\n';
  xml += '<urlset xmlns="http://www.sitemaps.org/schemas/sitemap/0.9">\n';
  pages.forEach((p) => {
    xml += '  <url>\n';
    xml += `    <loc>${baseUrl}${p.url}</loc>\n`;
    xml += `    <lastmod>${today}</lastmod>\n`;
    xml += `    <changefreq>${p.changefreq}</changefreq>\n`;
    xml += `    <priority>${p.priority}</priority>\n`;
    xml += '  </url>\n';
  });
  xml += '</urlset>';
  res.header('Content-Type', 'application/xml');
  res.send(xml);
});

// Robots policy, served from the route rather than a static file for the same
// reason as the sitemap: the Sitemap: line below has to name the same host the
// sitemap itself emits, and that host comes from SITE_URL. A committed
// robots.txt would hardcode one host and drift from the sitemap the moment the
// domain changed.
//
// /admin/ is disallowed because every page behind it is client-side gated and
// carries no indexable content; /api/ because it is a JSON API with no HTML to
// crawl. Disallowing does not secure either — the routes enforce their own auth
// — it only keeps crawlers out of URLs with nothing to index.
app.get('/robots.txt', (req, res) => {
  const baseUrl = (process.env.SITE_URL || 'https://dashawy.app').replace(/\/+$/, '');
  res
    .type('text/plain')
    .send(
      [
        'User-agent: *',
        'Allow: /',
        'Disallow: /admin/',
        'Disallow: /api/',
        `Sitemap: ${baseUrl}/sitemap.xml`,
        '',
      ].join('\n')
    );
});

// Serve the frontend from the same origin (simplifies deployment).
//
// maxAge lets the browser and any intermediary cache the assets between visits.
// Safe alongside the service worker because that SW network-fetches scripts and
// styles with `cache: 'no-store'`, so an updated file is still picked up on the
// next load — this header only governs the HTTP cache for everything else.
// index.html itself stays revalidated (maxAge: 0 below) so a deploy is never
// pinned to a stale shell.
app.use(
  express.static(frontendDir, {
    maxAge: '7d',
    etag: true,
    setHeaders(res, filePath) {
      // index.html must never be pinned to a stale shell, and the Google
      // Search Console verification file must never be served from a cache
      // that outlives the deploy — a cached pre-deploy 404 would otherwise
      // keep failing verification after the file is added.
      //
      // .txt joins them for llms.txt: it is a machine-readable statement about
      // the project, read by models and crawlers that cache aggressively. A week
      // of 7d max-age means a corrected phone number or a reverted feature stays
      // published for a week after the fix, and there is no cache-busting query
      // string to hand out.
      if (
        filePath.endsWith('index.html') ||
        filePath.endsWith('.webmanifest') ||
        filePath.endsWith('.txt') ||
        filePath.endsWith('googleffb088e4331f6842.html')
      ) {
        res.setHeader('Cache-Control', 'no-cache');
      }
    },
  })
);

// Fallback for unknown API routes.
app.use('/api', (req, res) => res.status(404).json({ error: 'Route not found' }));

// Central error handler.
app.use(errorHandler);

module.exports = app;
