# Setup wizard — Worker

The one server-side piece behind `docs/setup.html`. It does almost nothing on
purpose: GitHub's OAuth *device flow* (the same sign-in method their own `gh`
CLI uses) needs no client secret, but its two endpoints don't answer a
browser's CORS preflight — so this Worker's only job is forwarding those two
specific requests and handing back GitHub's response, byte for byte. It never
sees a completed access token beyond relaying it back to the browser that
asked, stores nothing, and holds no secret credential of its own — just the
OAuth App's public Client ID, kept as a Worker secret only so it isn't
hardcoded in source.

Everything else the wizard does (forking the repo, writing the GitHub Secret,
editing the workflow file, running the test) happens directly from
`docs/setup.html` against `api.github.com`, which supports CORS natively —
confirmed live before building this, not assumed.

## Redeploying

```
cd setup-wizard/worker
npx wrangler deploy
```

Needs `npx wrangler login` once per machine. Deployed under the
`hivementalityhoney@gmail.com` Cloudflare account, at
`https://gomining-servicetap-setup.hivementalityhoney.workers.dev`.

## If the Client ID ever needs rotating

(e.g. the OAuth App is recreated, or the secret is suspected leaked — low
risk since it's not actually secret information, but still worth being able
to rotate cleanly)

```
npx wrangler secret put GITHUB_OAUTH_CLIENT_ID
```

## The OAuth App itself

Registered at github.com/settings/applications, owned by `jdobbsclt`, named
"GoMining ServiceTap Setup". **Enable Device Flow** must stay checked — it's
what lets this whole thing skip needing a client secret at all. Homepage and
callback URL both point at `docs/setup.html`'s live GitHub Pages URL (the
callback URL is a required field on GitHub's form but is never actually used,
since device flow doesn't redirect).
