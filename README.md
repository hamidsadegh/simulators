# Cisco Show Command Simulators

Local SSH simulators for Cisco IOS, IOS-XE, and NX-OS read-only diagnostics.

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
