# Cloudflare delivery setup

This is the maintained Cloudflare configuration for the public APT endpoint.
CI remains provider-neutral: it publishes only through S3 and has no Cloudflare
credential, purge call, or control-plane access. The settings below were
checked against the live data plane and Cloudflare documentation on 2026-08-18.

The configuration consumes one WAF custom rule, two Cache Rules, and the one
Rate Limiting Rule available on Free. Every project-owned rule starts with
`dkc-linux` so it is distinguishable from unrelated zone policy.

Replace `APT_HOSTNAME` with the public repository hostname in every expression.
If a rule with the exact documented name already exists, edit and verify it
instead of creating a duplicate. Never replace an entire zone ruleset to update
one project rule; unrelated rules share the same phase.

## Risk boundary

Caching, negative caching, the strict public-path allow-list, and per-source
rate limiting prevent ordinary requests and simple random-path attacks from
reaching the origin repeatedly. They are not a global spending cap:
Free-plan rate counters are per IP and Cloudflare data center, enforcement may
lag by a few seconds, and a distributed client set can still generate unique
valid-looking package paths.

If that residual risk is unacceptable, use a fail-closed gateway with a global
enforced budget in front of the origin and qualify it separately.

## 1. Connect and tier the origin

Before changing anything, select the zone that owns `APT_HOSTNAME` and record
the existing Custom Rules, Cache Rules, Rate Limiting Rules, and their order.
This configuration needs one free Custom Rule slot, two Cache Rule slots, and
the single Free-plan Rate Limiting Rule slot.

1. Connect `APT_HOSTNAME` to the chosen HTTPS origin using that origin's
   documented DNS method. The resulting Cloudflare DNS record must remain
   proxied. If the origin manages the record, do not replace it with a
   hand-written address.
2. Verify that HTTPS ownership and certificate status are active. Do not expose
   an alternate public origin hostname that bypasses this endpoint's WAF and
   cache policy.
3. Open **Caching** > **Tiered Cache**, enable the **Smart** topology, save, and
   reopen the page to verify it remained selected.
4. Do not enable Access, interactive challenges, browser-only bot checks, or
   Cache Reserve. APT cannot solve challenges, and Cache Reserve is a separate
   billed storage product.

## 2. Allow only the public APT graph

In the current dashboard open **Security** > **Security rules**, select
**Create rule** > **Custom rules** (the older navigation is **Security** >
**WAF** > **Custom rules**). Create or edit
`dkc-linux APT: reject non-repository requests`, select **Edit expression**, and
enter this expression exactly:

```text
(http.host eq "APT_HOSTNAME" and (
 not ssl or
 not http.request.method in {"GET" "HEAD"} or
 http.request.uri.query ne "" or
 not (
  http.request.uri.path in {
   "/dists/trixie/InRelease"
   "/dists/trixie/Release"
   "/dists/trixie/Release.gpg"
   "/dists/trixie/main/binary-amd64/Packages"
   "/dists/trixie/main/binary-amd64/Packages.gz"
   "/dists/trixie/main/binary-amd64/Packages.xz"
   "/dists/trixie/main/source/Sources"
   "/dists/trixie/main/source/Sources.gz"
   "/dists/trixie/main/source/Sources.xz"
   "/keys/archive-primary.fingerprint"
   "/keys/archive-signing-subkeys.fingerprints"
   "/keys/dkc-archive-keyring.gpg"
  } or
  (starts_with(http.request.uri.path,
    "/dists/trixie/main/binary-amd64/by-hash/SHA256/") and
   len(http.request.uri.path) eq 111) or
  (starts_with(http.request.uri.path,
    "/dists/trixie/main/source/by-hash/SHA256/") and
   len(http.request.uri.path) eq 105) or
  ((starts_with(http.request.uri.path, "/pool/main/d/dkc-linux/") or
    starts_with(http.request.uri.path,
      "/pool/main/d/dkc-archive-keyring/")) and
   (ends_with(http.request.uri.path, ".deb") or
    ends_with(http.request.uri.path, ".dsc") or
    ends_with(http.request.uri.path, ".tar.xz")))
 )
))
```

This admits only HTTPS `GET`/`HEAD` requests for the exact canonical indexes,
the three public key files, exact-length SHA-256 by-hash names, and known
package/source suffixes below the two published pool prefixes. Root manifests,
controller state, leases, query variants, plaintext HTTP, and arbitrary object
names are blocked before the origin. Update the allow-list before publishing a
new index path, component, architecture, or source suffix.

Select **Block**, retain Cloudflare's default `403` response, enable the rule,
and deploy it. Do not use Challenge or a custom browser response. Reopen the
rule and verify the saved expression before continuing.

## 3. Cache immutable objects

Open **Caching** > **Cache Rules** > **Create rule**. Select **Custom filter
expression**, then **Edit expression**. Create or edit
`dkc-linux APT: cache immutable paths` with:

```text
(http.host eq "APT_HOSTNAME" and
 http.request.uri.query eq "" and (
  starts_with(http.request.uri.path,
    "/dists/trixie/main/binary-amd64/by-hash/SHA256/") or
  starts_with(http.request.uri.path,
    "/dists/trixie/main/source/by-hash/SHA256/") or
  starts_with(http.request.uri.path, "/pool/main/d/dkc-linux/") or
  starts_with(http.request.uri.path,
    "/pool/main/d/dkc-archive-keyring/")
))
```

Set:

- **Cache eligibility**: eligible;
- **Edge TTL**: **Use cache-control header if present, use default Cloudflare
  caching behavior if not**;
- **Status Code TTL**: add the exact rows in the table below;
- **Browser TTL**: respect origin;
- **Respect strong ETags**: enabled;
- **Cache key**: leave unset so the default key remains in use;
- every other optional setting, including Cache Reserve: leave unset.

| Status selector | Immutable rule TTL |
| --- | ---: |
| Range `100-199` | No store |
| Range `300-303` | No store |
| Range `305-403` | No store |
| Single `404` | 2 hours |
| Range `405-409` | No store |
| Single `410` | 2 hours |
| Range `411-999` | No store |

Do not add a row for `200-299` or `304`; those responses follow the origin
policy. The split ranges are deliberate: one `300-403` range would incorrectly
include `304` and collide with the special `404` handling.

The origin gives successful package and by-hash objects
`public, max-age=31536000, immutable`. Retention may remove an old package from
origin immediately after a new signed generation commits. Existing edge copies
remain immutable for their advertised lifetime, but eviction is possible and
is not an availability guarantee. Caching `404`/`410` for two hours prevents
repeated misses for the same valid-looking name; other errors and redirects
must not become repository responses.

## 4. Cache canonical metadata and keys

Create or edit a second Cache Rule named
`dkc-linux APT: cache mutable paths and misses`. Again choose **Custom filter
expression** > **Edit expression**:

```text
(http.host eq "APT_HOSTNAME" and
 http.request.uri.query eq "" and
 http.request.uri.path in {
  "/dists/trixie/InRelease"
  "/dists/trixie/Release"
  "/dists/trixie/Release.gpg"
  "/dists/trixie/main/binary-amd64/Packages"
  "/dists/trixie/main/binary-amd64/Packages.gz"
  "/dists/trixie/main/binary-amd64/Packages.xz"
  "/dists/trixie/main/source/Sources"
  "/dists/trixie/main/source/Sources.gz"
  "/dists/trixie/main/source/Sources.xz"
  "/keys/archive-primary.fingerprint"
  "/keys/archive-signing-subkeys.fingerprints"
  "/keys/dkc-archive-keyring.gpg"
 })
```

Set:

- **Cache eligibility**: eligible;
- **Edge TTL**: **Ignore cache-control header and use this TTL**, 2 hours;
- **Status Code TTL**: use the immutable table above and add one more row,
  single `304` = 2 hours; successful `200-299` responses use the two-hour
  default;
- **Browser TTL**: respect origin;
- **Respect strong ETags**: enabled;
- **Cache key**: leave unset;
- every other optional setting: leave unset.

Two hours is the minimum configurable Edge TTL on Free. Clients still receive
the origin's `max-age=0, must-revalidate`, while Cloudflare absorbs repeated
metadata requests. Visibility of a publication may therefore lag by two hours,
but it cannot create a broken signed view: publication writes immutable
objects first, advertises `Acquire-By-Hash: yes`, and keeps them longer than
every cache copy.

Keep these two project Cache Rules after any broader zone rule that changes the
same setting. Cloudflare stacks matching Cache Rules and the last conflicting
value wins. The rules are otherwise disjoint from each other.

Do not create Cache Response Rules, a hostname-wide Cache Everything rule, or
a bypass-first rule for this endpoint. Status Code TTL is part of ordinary
Cache Rules on Free, and the two disjoint expressions already define every
public cacheable path. The live setup required no purge operation.

Free, Pro, and Business cache at most 512 MB per object. Recheck the signed
manifest after a material package-layout change; the accepted generation's
largest object is about 162 MB.

## 5. Bound one request source

Open **Security** > **Security rules**, select **Create rule** > **Rate limiting
rules** (older navigation: **Security** > **WAF** > **Rate limiting rules**),
and create or edit `dkc-linux APT: bound request source`. Select **Edit
expression**. Free permits `Path` but not `Host` here, so use the exact
repository-specific prefixes below and do not try to add a hostname condition:

```text
(starts_with(http.request.uri.path, "/dists/trixie/") or
 starts_with(http.request.uri.path, "/pool/main/d/dkc-linux/") or
 starts_with(http.request.uri.path,
   "/pool/main/d/dkc-archive-keyring/") or
 starts_with(http.request.uri.path, "/keys/"))
```

Set:

- **Also apply rate limiting to cached assets**: enabled; Free does not permit
  disabling it;
- **With the same characteristics**: IP;
- **Use custom counting expression**: disabled;
- **When rate exceeds**: request based, 30 requests, 10 seconds;
- **Then take action**: Block with the default `429` response;
- **Duration**: 10 seconds.

Cloudflare implicitly adds its mandatory data-center characteristic. Deploy
the rule, reopen it, and verify every field. Because Free has only one Rate
Limiting Rule, a pre-existing unrelated rule is a real capacity conflict; do
not delete it silently to make room.

The limit was accepted with a clean-client installation and still leaves room
for ordinary package-manager concurrency. It is only a per-source mitigation:
counter updates lag and each IP/data-center pair has independent state. Monitor
Security Events for shared-NAT false positives before lowering it.

## 6. Validate

First perform a control-plane read-back in the dashboard:

- the repository hostname is proxied, its TLS certificate is active, and no
  public alternate origin hostname bypasses these rules;
- Smart topology is enabled;
- exactly one enabled rule exists under each documented name;
- both Cache Rules are below any broader rule that changes the same setting;
- reopening every rule shows the exact expression and values above;
- unrelated DNS records and rules are unchanged.

For a data-plane check, substitute the real hostname and use `curl` or APT.
A generic Python HTTP client is not an authoritative probe: Browser Integrity
Check may reject its default user agent even when real APT and `curl` work.

```sh
APT_HOSTNAME=apt.example.net
APT_BASE_URL="https://${APT_HOSTNAME}"
APT_UA='Debian APT-HTTP/1.3'

for attempt in 1 2; do
  curl -A "$APT_UA" -fsS \
    -D "/tmp/dkc-inrelease.${attempt}.headers" \
    -o "/tmp/dkc-inrelease.${attempt}" \
    "$APT_BASE_URL/dists/trixie/InRelease"
done
grep -Ei '^(cache-control|cf-cache-status|age):' \
  /tmp/dkc-inrelease.*.headers

MISSING_URL="$APT_BASE_URL/pool/main/d/dkc-linux/not-present_0_amd64.deb"
for attempt in 1 2; do
  curl -A "$APT_UA" -sS \
    -D "/tmp/dkc-missing.${attempt}.headers" \
    -o /dev/null "$MISSING_URL"
done
grep -Ei '^(HTTP/|cf-cache-status:|age:)' /tmp/dkc-missing.*.headers

curl -A "$APT_UA" -sS -o /dev/null -w 'private=%{http_code}\n' \
  "$APT_BASE_URL/state/current.asc"
curl -A "$APT_UA" -sS -o /dev/null -w 'query=%{http_code}\n' \
  "$APT_BASE_URL/dists/trixie/InRelease?probe=1"
curl -A "$APT_UA" -sS -o /dev/null -w 'http=%{http_code}\n' \
  "http://${APT_HOSTNAME}/dists/trixie/InRelease"
curl -A "$APT_UA" -sS -X POST -o /dev/null -w 'post=%{http_code}\n' \
  "$APT_BASE_URL/dists/trixie/InRelease"
```

The second canonical and missing-path response should normally be `HIT` with an
`Age` header; a pre-warmed first response may already be `HIT`, and eviction can
produce a later `MISS`. All four blocked probes must return `403`, not an origin
`404` or a redirect.

The rate test is optional and temporarily blocks the source IP. Run it only
from a dedicated validation client, never from a shared production NAT:

```sh
export APT_BASE_URL APT_UA
seq 1 45 | xargs -P 15 -I ITEM sh -c \
  'curl -A "$APT_UA" -sS -o /dev/null -w "%{http_code}\n" \
    "$APT_BASE_URL/dists/trixie/InRelease"' \
  >/tmp/dkc-rate-statuses
sort /tmp/dkc-rate-statuses | uniq -c
sleep 12
curl -A "$APT_UA" -sS -o /dev/null -w 'recovery=%{http_code}\n' \
  "$APT_BASE_URL/dists/trixie/InRelease"
```

At least one burst response must be `429`; the recovery request must be `200`.
The number of initial `200` responses is deliberately not exact because
Cloudflare documents counter-update delay.

Accept the endpoint only after all of these pass against current signed paths:

- repeated package, by-hash, `InRelease`, and valid-shape `404` requests become
  `CF-Cache-Status: HIT` with `Age` from the same location;
- package and index bytes match the signed SHA-256 and size;
- package/by-hash responses retain the one-year client header, while
  `InRelease` retains `max-age=0, must-revalidate`;
- query-bearing, private-state, plaintext HTTP, and `POST` requests are blocked;
- a warmed package range returns `206`, the exact `Content-Range`, and matching
  bytes;
- a burst above 30 requests is blocked and a request after 10 seconds succeeds;
- a clean Debian 13 client completes signed `apt update`, requests indexes by
  hash, installs v2 and v3 images and headers, and fetches both source packages
  without an insecure override.

The clean client must contain `ca-certificates`. A deliberately offline or
minimal image without the CA bundle can report a certificate error even though
the public TLS endpoint is correct. Also inspect the complete `apt update` log:
APT may exit zero after a failed index fetch while printing a warning.

## Troubleshooting

| Symptom | Check |
| --- | --- |
| Valid path returns `403` | Check Security Events. A WAF block means the allow-list is incomplete; a rate block normally returns `429` and clears after ten seconds. |
| Canonical path remains `DYNAMIC` | Confirm the mutable Cache Rule matches, is enabled, and comes after any broader conflicting rule. |
| Second valid-shape `404` remains `MISS` | Recheck the `404` Status Code TTL inside both Cache Rules. Do not create a Cache Response Rule as a workaround. |
| Package or by-hash URL never becomes `HIT` | Recheck immutable eligibility, origin Cache-Control, object size, and rule order. |
| Python client receives error `1010` | Retry with real APT and `curl`; do not disable Browser Integrity Check solely for a synthetic user agent. |
| Minimal container reports TLS verification failure | Install `ca-certificates` and retry before changing TLS or WAF settings. |
| Header metapackage cannot resolve LLVM 21 | Enable official Debian `trixie-backports`; this is a client repository issue, not a cache failure. |

If an API is used only for independent read-back, Smart topology requires
**Zone Settings: Read**; changing it requires **Zone Settings: Edit**. Cache or
WAF permissions alone are insufficient. Update individual rules through the
Rulesets API; do not submit an incomplete whole-ruleset replacement that could
erase unrelated policy.

Record only public paths, response headers, rule definitions, and results in a
maintainer record. Never commit account IDs, private origin identifiers,
endpoints, API tokens, or storage credentials.

## Official references

- [Create a Custom Rule in the dashboard](https://developers.cloudflare.com/waf/custom-rules/create-dashboard/)
- [Create a Cache Rule in the dashboard](https://developers.cloudflare.com/cache/how-to/cache-rules/create-dashboard/)
- [Create a Rate Limiting Rule in the dashboard](https://developers.cloudflare.com/waf/rate-limiting-rules/create-zone-dashboard/)
- [Cache Rule settings](https://developers.cloudflare.com/cache/how-to/cache-rules/settings/)
- [Cache Rule order](https://developers.cloudflare.com/cache/how-to/cache-rules/order/)
- [Cache by status code](https://developers.cloudflare.com/cache/how-to/configure-cache-status-code/)
- [Edge and browser TTL](https://developers.cloudflare.com/cache/how-to/edge-browser-cache-ttl/)
- [Tiered Cache](https://developers.cloudflare.com/cache/how-to/tiered-cache/)
- [Rate Limiting Rules](https://developers.cloudflare.com/waf/rate-limiting-rules/)
