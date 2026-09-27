// ServiceTap setup wizard -- GitHub OAuth Device Flow relay.
//
// Why this exists at all: GitHub's device-flow endpoints (below) don't answer CORS
// preflight requests, so a browser can't call them directly (confirmed in GitHub's own
// docs: "CORS pre-flight requests (OPTIONS) are not supported at this time"). Device
// flow itself needs no client secret -- GitHub's docs: "client_secret is required
// unless the token was generated using the device flow" -- so this Worker holds no
// secret and no state. It only ever forwards two specific requests byte-for-byte and
// returns GitHub's response. Every other action (forking the repo, writing the GitHub
// Secret, editing the workflow file, running the test) is done by the wizard page
// itself, straight against api.github.com, which *does* support CORS -- verified live
// (an OPTIONS preflight against api.github.com returns
// "Access-Control-Allow-Origin: *"). Nothing here ever sees a GitHub access token
// beyond relaying the one response that contains it back to the browser that asked.

// Only the wizard page may call this. Update if the page ever moves.
const ALLOWED_ORIGIN = "https://jdobbsclt.github.io";

// Set once via `wrangler secret put GITHUB_OAUTH_CLIENT_ID` -- this is the OAuth App's
// Client ID, which is public information anyway (it's visible in every device-flow
// request), kept as a Worker secret only so it isn't hardcoded in source and can be
// rotated without a redeploy.
const DEVICE_CODE_URL = "https://github.com/login/device/code";
const TOKEN_URL = "https://github.com/login/oauth/access_token";

function corsHeaders() {
  return {
    "Access-Control-Allow-Origin": ALLOWED_ORIGIN,
    "Access-Control-Allow-Methods": "POST, OPTIONS",
    "Access-Control-Allow-Headers": "Content-Type",
    "Access-Control-Max-Age": "86400",
  };
}

async function relay(url, params) {
  const upstream = await fetch(url, {
    method: "POST",
    headers: { "Accept": "application/json", "Content-Type": "application/x-www-form-urlencoded" },
    body: new URLSearchParams(params).toString(),
  });
  const body = await upstream.text(); // pass through as-is; don't parse/reshape GitHub's response
  return new Response(body, {
    status: upstream.status,
    headers: { "Content-Type": "application/json", ...corsHeaders() },
  });
}

export default {
  async fetch(request, env) {
    if (request.method === "OPTIONS") {
      return new Response(null, { status: 204, headers: corsHeaders() });
    }

    const origin = request.headers.get("Origin");
    if (origin !== ALLOWED_ORIGIN) {
      return new Response("Forbidden", { status: 403 });
    }

    const url = new URL(request.url);

    if (request.method === "POST" && url.pathname === "/device/code") {
      return relay(DEVICE_CODE_URL, { client_id: env.GITHUB_OAUTH_CLIENT_ID, scope: "repo" });
    }

    if (request.method === "POST" && url.pathname === "/device/token") {
      let deviceCode;
      try {
        deviceCode = (await request.json()).device_code;
      } catch {
        return new Response(JSON.stringify({ error: "invalid_request" }), {
          status: 400, headers: { "Content-Type": "application/json", ...corsHeaders() },
        });
      }
      if (!deviceCode) {
        return new Response(JSON.stringify({ error: "invalid_request" }), {
          status: 400, headers: { "Content-Type": "application/json", ...corsHeaders() },
        });
      }
      return relay(TOKEN_URL, {
        client_id: env.GITHUB_OAUTH_CLIENT_ID,
        device_code: deviceCode,
        grant_type: "urn:ietf:params:oauth:grant-type:device_code",
      });
    }

    return new Response("Not found", { status: 404, headers: corsHeaders() });
  },
};
