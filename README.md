# Cisco Device Simulators

Local SSH simulators for Cisco IOS, IOS-XE, and NX-OS read-only diagnostics,
plus an IOS-XE Plug and Play agent simulator for ZAS onboarding integration.

## Start a Simulator

IOS-XE:

```bash
python cisco_ssh_simulator.py --platform iosxe --host 127.0.0.1 --port 2222
```

NX-OS:

```bash
python cisco_ssh_simulator.py --platform nxos --host 127.0.0.1 --port 2223
```

Legacy IOS fixture:

```bash
python cisco_ssh_simulator.py --platform ios --host 127.0.0.1 --port 2224
```

Default local credentials:

- username: `admin`
- password: `admin`

These credentials are simulator-only defaults, not production device credentials.

## Test with SSH

```bash
ssh -p 2222 admin@127.0.0.1 "show version"
ssh -p 2222 admin@127.0.0.1 "show interfaces status"
ssh -p 2223 admin@127.0.0.1 "show vpc"
```

## Data Files

The IOS-XE and NX-OS simulators load command outputs from:

- `100_top_show_commands_output_cisco_iosxe.txt`
- `100_top_show_commands_output_cisco_nxos.txt`

Keep these files sanitized. Do not add real passwords, tokens, private keys, public IPs, or sensitive customer data.

## IOS-XE PnP Simulator

The PnP simulator acts as a factory-default IOS-XE device. It implements the
HTTP(S) agent flow used by ZAS:

- `GET /pnp/HELLO`;
- repeated `POST /pnp/WORK-REQUEST` polling;
- backoff, device-info, image-install, config-upgrade, and bye handling;
- `POST /pnp/WORK-RESPONSE` with matching service correlators;
- bounded configuration and image downloads, including image checksum checks;
- optional embedded IOS-XE SSH service for the post-provisioning ZAS handoff.

Register or plan the simulator serial in ZAS, enable the PnP deployment and
runtime gates in an isolated development environment, then start the agent:

```bash
python pnp_simulator.py \
  --server https://zas-dev.example.com \
  --serial FCW1234ABCD \
  --product-id C9300-48P \
  --hostname Switch \
  --output-dir ./pnp-artifacts
```

For a local certificate that is not in the system trust store, prefer a test CA
file:

```bash
python pnp_simulator.py --server https://zas-dev.example.com --ca-file ./lab-ca.pem
```

`--insecure` disables TLS verification and is intended only for an isolated
development lab.

An unapproved device receives backoff. Keep the process running and approve the
claim in ZAS, or use `--exit-on-backoff` for a single registration pass. Backoff
sleeps are capped by `--max-backoff` (60 seconds by default).

### Full PnP-to-SSH integration

Use `--ssh-port` to expose the existing IOS-XE SSH simulator alongside the PnP
agent. The PnP credentials sent by ZAS replace the CLI fallback credentials,
and the downloaded day-zero configuration becomes the simulated running config
before the successful config response is posted.

```bash
python pnp_simulator.py \
  --server https://zas-dev.example.com \
  --serial FCW1234ABCD \
  --ssh-host 0.0.0.0 \
  --ssh-port 2222
```

ZAS currently connects to the approved management IP using its normal SSH
settings, so ensure that address and port routing reach this process. The
simulator never performs DHCP/DNS/PnP Connect discovery; `--server` supplies the
controller URL directly.

Useful deterministic test controls:

```bash
# Register once, then exit when ZAS asks the device to back off.
python pnp_simulator.py --server https://zas-dev.example.com --exit-on-backoff

# Exercise ZAS failure handling for one service.
python pnp_simulator.py --server https://zas-dev.example.com --fail-service config-upgrade

# Limit polling during automation.
python pnp_simulator.py --server https://zas-dev.example.com --max-polls 3 --poll-interval 0
```

Downloaded artifacts are written under `pnp-artifacts/` by default. Do not
commit rendered configurations because they may contain bootstrap credentials.

## License

This project is licensed under the [MIT License](LICENSE).
