# Phones

The Shorthop app pages people on their phones — through Do Not Disturb where the phone allows,
with Acknowledge on the lock screen.

## The push relay

A push notification to an app can only be sent with that app's own keys, so pages reach the
Shorthop app through **our push relay**. Your Shorthop sends it an opaque incident id and a
phone; the relay never receives the content of an alert. The app then fetches the details from
**your** Shorthop.

1. Create an installation at **[account.shorthop.io](https://account.shorthop.io/signup)** —
   free; you get an installation key (`shr_…`), shown once.
2. In Shorthop: **Settings → Phones → Push relay**: URL `https://relay.shorthop.io` and the key
   (or `RELAY_URL` / `RELAY_KEY`).
3. Everyone installs the app and pairs it from **My account → Phones** (a QR code).

The first **3 phones** are free. To sign in more, add a card on your account page: €1.99 per
phone per month, billed on the most phones signed in at the same time. **A page is never
stopped over billing** — a card problem only stops a *new* phone signing in. Signing a phone out
frees its seat at once. The account page shows every phone, the month so far and your invoices;
Settings → Push relay links to it.

Without the relay, Shorthop still pages by email, Slack and webhooks.

## Reaching your Shorthop

Your Shorthop needs only **outbound HTTPS** to `relay.shorthop.io` — no inbound port for that.

The app talks to *your* Shorthop to show an incident, acknowledge it and show the rota. On the
office network or VPN that just works. For phones to work anywhere, put **port 8081** — the
public listener — on the internet. It answers only what phones and calendar apps need, and only
to phone tokens; the web UI, ingest and API keys stay on port 8080. Any of these works:

- **VPN only.** Simplest. Away from the VPN a phone still gets the page, but cannot open it.
- **A reverse proxy or load balancer** in front of 8081, with HTTPS.
- **A tunnel** such as Cloudflare Tunnel to 8081 — no inbound port at all.

Then set `PUBLIC_API_URL` to that address, so pairing codes and calendar feeds use it. Behind a
proxy, set `TRUSTED_PROXIES` (and `CLIENT_IP_HEADER` if your proxy sets one) so rate limits see
the real client.
