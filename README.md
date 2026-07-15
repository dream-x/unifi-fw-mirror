# unifi-fw-mirror

A local mirror of UniFi firmware. Keeps the newest release build for the devices
you actually own, verifies it against Ubiquiti's own checksum, and serves it on
your LAN so consoles, access points and switches can flash from a URL that is a
millisecond away instead of an ocean away.

No dependencies — standard library Python and `curl`.

## Why

- **Speed.** A 900 MB UniFi OS image lands over gigabit ethernet instead of the
  public internet.
- **Determinism.** Two APs of the same model flash byte-identical firmware,
  because they take the same file.
- **Availability.** You can flash when the upstream CDN is slow, rate-limiting,
  or refusing to serve you at all.
- **Integrity.** Every file is checked against the `sha256` the firmware API
  publishes before it is allowed into the mirror.

## Quick start

```bash
# what platform code does my device use?
python3 sync.py resolve "Flex 2.5G"

cp config.example.json /etc/unifi-fw/config.json
$EDITOR /etc/unifi-fw/config.json

# see what is stale without downloading anything
python3 sync.py sync --check

python3 sync.py sync
```

## Configuration

`/etc/unifi-fw/config.json`:

```json
{
  "dest": "/srv/firmware",
  "channel": "release",
  "keep": 1,
  "proxy": null,
  "devices": ["UDM-Pro", "U7-Pro", "UAP-AC-M", "USW-Flex-2.5G-5"]
}
```

| key | meaning |
| --- | --- |
| `dest` | where firmware lands; point your web server here |
| `channel` | firmware channel, normally `release` |
| `keep` | how many versions per device to retain (`1` = only the newest) |
| `proxy` | SOCKS5 proxy for downloads, or `null` for a direct connection |
| `devices` | the devices to mirror — see below |

A device is either a plain string, resolved against Ubiquiti's device database:

```json
"devices": ["UDM-Pro", "USW-Flex-2.5G-5"]
```

…or an object, when you need to pin the exact firmware product:

```json
"devices": [{ "device": "UDM-Pro", "product": "unifi-dream" }]
```

Credentials do not belong in the config file. `UNIFI_FW_PROXY` overrides
`proxy`, so a systemd `EnvironmentFile` (mode `600`) can hold the real URL:

```
UNIFI_FW_PROXY=user:password@proxy.example.net:1080
```

## Platform codes, and why `resolve` exists

The firmware API is keyed by a **platform** code that is neither the SKU on the
box nor the shortname in the device database. It is the `unifi.network.model`
field, and the three regularly disagree:

| device | SKU | shortname | **platform** |
| --- | --- | --- | --- |
| Switch Flex 2.5G 5 | `USW-Flex-2.5G-5` | `USM25G5` | **`USWED35`** |
| Access Point AC Mesh | `UAP-AC-M` | `U7MSH` | **`U7MSH`** |
| Dream Machine Pro | `UDM-Pro` | `UDMPRO` | **`UDMPRO`** |

Query the API with the wrong one and it returns an empty list rather than an
error — the mirror looks like it worked and silently holds nothing. `resolve`
takes any of the three and prints the platform code:

```console
$ python3 sync.py resolve "Flex 2.5G"
Switch Flex 2.5G 5
    platform : USWED35
    sku      : USW-Flex-2.5G-5
    shortnames: USM25G5
Switch Flex 2.5G 8
    platform : USWED36
    ...
```

Two more traps worth knowing:

- **`U7` means two unrelated things.** It was the platform family for 802.11ac
  hardware (`U7LT` = AC Lite, `U7MSH` = AC Mesh, `U7PG2` = AC Pro), and then
  Ubiquiti switched to numbering by WiFi generation, so WiFi 7 products landed
  on the same prefix (`U7PRO` = U7 Pro). `U7MSH` and `U7PRO` are two WiFi
  generations apart.
- **One platform can carry several products.** `UDMPRO` lists a current
  `unifi-dream` build next to `unifi-firmware` and `udm` builds abandoned years
  ago. Without an explicit `product`, the newest release wins, which picks the
  live line on its own.

## Deploying

`deploy/` has a systemd service, a monthly timer and an nginx site.

```bash
install -Dm755 sync.py /opt/unifi-fw/sync.py
install -Dm600 /dev/stdin /etc/unifi-fw.env <<<'UNIFI_FW_PROXY=user:pass@proxy:1080'
install -Dm644 config.example.json /etc/unifi-fw/config.json

install -Dm644 deploy/unifi-fw-sync.service /etc/systemd/system/unifi-fw-sync.service
install -Dm644 deploy/unifi-fw-sync.timer   /etc/systemd/system/unifi-fw-sync.timer
install -Dm644 deploy/nginx.conf            /etc/nginx/sites-available/unifi-fw

systemctl enable --now unifi-fw-sync.timer
systemctl start unifi-fw-sync          # first run, populates the mirror
```

The timer fires on the 1st of each month with `Persistent=true`, so a machine
that was off simply syncs at the next boot.

## Flashing from the mirror

Consoles take a URL over SSH:

```bash
ssh root@<console-ip>
ubnt-upgrade http://<mirror>/UDMPRO-5.1.19_3fbc1da.bin
```

Access points and switches do too:

```bash
ssh ui@<device-ip>
upgrade http://<mirror>/U7PRO-8.6.11_18870.bin
```

Or let the controller distribute it, which keeps the device's state in the UI
consistent:

```bash
curl -k -b /tmp/c -c /tmp/c -X POST https://<controller>/api/auth/login \
  -H 'Content-Type: application/json' \
  -d '{"username":"<admin>","password":"<password>"}'

curl -k -b /tmp/c -X POST \
  https://<controller>/proxy/network/api/s/default/cmd/devmgr \
  -H 'Content-Type: application/json' \
  -d '{"cmd":"upgrade-external","mac":"<device-mac>","url":"http://<mirror>/U7PRO-8.6.11_18870.bin"}'
```

These are the standard UniFi procedures rather than anything this project
invents; the mirror's job ends at serving a verified file. Flash something
inconsequential before you flash the console the whole house depends on.

## Output

`dest` ends up holding one firmware per device plus a manifest:

```json
{
  "USWED35": {
    "device": "USW-Flex-2.5G-5",
    "product": "unifi-firmware",
    "version": "v2.1.8+971",
    "file": "USWED35-2.1.8_971.bin",
    "size": 2570080,
    "sha256": "8c6c46b790c899acb489313e79baec4b3a54110c7722a353e18a1e2cd004b59f",
    "released": "2025-04-30"
  }
}
```

## Endpoints used

| url | purpose |
| --- | --- |
| `fw-update.ubnt.com/api/firmware-latest` | firmware versions, sizes, checksums, download links |
| `static.ubnt.com/fingerprint/ui/public.json` | device database, for `resolve` |
| `fw-download.ubnt.com` | the firmware itself |

Unofficial, and Ubiquiti owes nobody their stability. If a download starts
failing with **HTTP 451** while the API keeps answering, the CDN is refusing
your network specifically — set `proxy`.

## Licence

MIT.
