from __future__ import annotations

import argparse
import re
import socket
import threading
from dataclasses import dataclass
from pathlib import Path

import paramiko


@dataclass(frozen=True)
class CommandResult:
    stdout: str = ""
    stderr: str = ""
    exit_status: int = 0


@dataclass(frozen=True)
class DeviceProfile:
    key: str
    hostname: str
    prompt: str
    command_table: dict[str, CommandResult]


CATALOG_DIR = Path(__file__).resolve().parent


def _normalize_command(command: str) -> str:
    return " ".join(str(command).strip().split()).lower()


def _parse_command_catalog(path: Path) -> tuple[str, dict[str, CommandResult]]:
    text = path.read_text(encoding="utf-8", errors="replace").replace("\r\n", "\n")
    device_match = re.search(r"^Device:\s*(?P<device>[^|]+?)(?:\s*\||$)", text, flags=re.MULTILINE)
    hostname = device_match.group("device").strip() if device_match else path.stem

    command_matches = list(re.finditer(r"^Command:\s*(?P<command>.+?)\s*$", text, flags=re.MULTILINE))
    command_table: dict[str, CommandResult] = {}
    for index, match in enumerate(command_matches):
        command = _normalize_command(match.group("command"))
        section_start = match.end()
        section_end = command_matches[index + 1].start() if index + 1 < len(command_matches) else len(text)
        section = text[section_start:section_end]
        section = re.sub(r"^\nOutput:\n?", "", section, count=1)
        section = re.sub(r"\n-{20,}\s*$", "", section.rstrip())
        command_table[command] = CommandResult(stdout=f"{section}\n" if section else "")

    return hostname, command_table


def _add_alias(command_table: dict[str, CommandResult], alias: str, target: str) -> None:
    target_result = command_table.get(_normalize_command(target))
    if target_result is not None:
        command_table.setdefault(_normalize_command(alias), target_result)


def _add_show_aliases(command_table: dict[str, CommandResult], aliases: dict[str, str]) -> None:
    for alias, target in aliases.items():
        _add_alias(command_table, alias, target)


def _interface_aliases(interface_name: str) -> set[str]:
    normalized = _normalize_command(interface_name)
    aliases = {normalized}
    replacements = (
        ("gigabitethernet", "gi"),
        ("tengigabitethernet", "te"),
        ("twentyfivegige", "tw"),
        ("fortygigabitethernet", "fo"),
        ("hundredgige", "hu"),
        ("fastethernet", "fa"),
        ("ethernet", "eth"),
        ("port-channel", "po"),
    )
    for long_name, short_name in replacements:
        if normalized.startswith(long_name):
            suffix = normalized[len(long_name) :]
            aliases.add(f"{short_name}{suffix}")
        if normalized.startswith(short_name):
            suffix = normalized[len(short_name) :]
            aliases.add(f"{long_name}{suffix}")
    return aliases


def _extract_running_config_interface(command_table: dict[str, CommandResult], interface_name: str) -> CommandResult | None:
    running_config = command_table.get(_normalize_command("show running-config")) or command_table.get(_normalize_command("show run"))
    if not running_config or not running_config.stdout:
        return None

    aliases = _interface_aliases(interface_name)
    lines = running_config.stdout.splitlines()
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped.lower().startswith("interface "):
            continue
        current_interface = stripped.split(None, 1)[1]
        if _normalize_command(current_interface) not in aliases:
            continue

        block = []
        for block_line in lines[index:]:
            block_stripped = block_line.strip()
            if block and block_stripped.lower().startswith("interface "):
                break
            if block and block_stripped == "!":
                block.append(block_line)
                break
            block.append(block_line)
        return CommandResult(stdout="\n".join(block).rstrip() + "\n")
    return None


def _extract_show_interface(command_table: dict[str, CommandResult], interface_name: str) -> CommandResult | None:
    show_interfaces = command_table.get(_normalize_command("show interfaces")) or command_table.get(_normalize_command("show interface"))
    if not show_interfaces or not show_interfaces.stdout:
        return None

    aliases = _interface_aliases(interface_name)
    lines = show_interfaces.stdout.splitlines()
    interface_header = re.compile(r"^(\S+)\s+is\s+", flags=re.IGNORECASE)
    for index, line in enumerate(lines):
        match = interface_header.match(line)
        if not match or _normalize_command(match.group(1)) not in aliases:
            continue

        block = []
        for block_line in lines[index:]:
            if block and interface_header.match(block_line):
                break
            block.append(block_line)
        return CommandResult(stdout="\n".join(block).rstrip() + "\n")
    return None


def _execute_dynamic_command(command_table: dict[str, CommandResult], normalized: str) -> CommandResult | None:
    config_match = re.fullmatch(r"(?:show running-config|show run|sh run)\s+interface\s+(.+)", normalized)
    if config_match:
        return _extract_running_config_interface(command_table, config_match.group(1))

    interface_match = re.fullmatch(r"(?:show interfaces|show interface|show int|sh interfaces|sh interface|sh int)\s+(.+)", normalized)
    if interface_match:
        return _extract_show_interface(command_table, interface_match.group(1))

    return None


def _load_catalog_profile(key: str, filename: str) -> DeviceProfile:
    hostname, command_table = _parse_command_catalog(CATALOG_DIR / filename)
    command_table[_normalize_command("terminal length 0")] = CommandResult(stdout="")
    command_table[_normalize_command("terminal width 0")] = CommandResult(stdout="")
    command_table[_normalize_command("hostname")] = CommandResult(stdout=f"{hostname}\n")

    _add_alias(command_table, "show run", "show running-config")
    _add_alias(command_table, "sh run", "show running-config")
    _add_alias(command_table, "show startup", "show startup-config")
    _add_show_aliases(
        command_table,
        {
            "sh ver": "show version",
            "sh version": "show version",
            "sh run": "show running-config",
            "sh inventory": "show inventory",
            "sh logging": "show logging",
            "sh cdp neighbors": "show cdp neighbors",
            "sh cdp neighbors detail": "show cdp neighbors detail",
            "sh vlan": "show vlan",
            "sh vlan brief": "show vlan brief",
            "show mac address table": "show mac address-table",
            "sh mac address-table": "show mac address-table",
            "sh mac address table": "show mac address-table",
            "show mac table": "show mac address-table",
            "sh mac table": "show mac address-table",
            "show spanning tree": "show spanning-tree",
            "sh spanning-tree": "show spanning-tree",
            "sh spanning tree": "show spanning-tree",
        },
    )

    if key == "nxos":
        _add_alias(command_table, "show logging last 50", "show logging last 100")
        _add_alias(command_table, "show interface port-channel summary", "show port-channel summary")
        _add_show_aliases(
            command_table,
            {
                "show interfaces": "show interface",
                "show interfaces brief": "show interface brief",
                "show interfaces status": "show interface status",
                "show interfaces description": "show interface description",
                "show interfaces counters": "show interface counters",
                "show interfaces counters errors": "show interface counters errors",
                "show interfaces transceiver": "show interface transceiver",
                "show interfaces transceiver details": "show interface transceiver details",
                "show interfaces switchport": "show interface switchport",
                "show interfaces trunk": "show interface trunk",
                "show int": "show interface",
                "show int brief": "show interface brief",
                "show int status": "show interface status",
                "show int description": "show interface description",
                "show int counters": "show interface counters",
                "show int counters errors": "show interface counters errors",
                "show int transceiver": "show interface transceiver",
                "show int transceiver details": "show interface transceiver details",
                "show int switchport": "show interface switchport",
                "show int trunk": "show interface trunk",
                "sh int": "show interface",
                "sh int brief": "show interface brief",
                "sh int status": "show interface status",
                "sh int description": "show interface description",
                "sh int counters": "show interface counters",
                "sh int counters errors": "show interface counters errors",
                "sh int transceiver": "show interface transceiver",
                "sh int transceiver details": "show interface transceiver details",
                "sh int switchport": "show interface switchport",
                "sh int trunk": "show interface trunk",
            },
        )
    elif key == "iosxe":
        _add_alias(command_table, "show interfaces counter", "show interfaces counters")
        _add_show_aliases(
            command_table,
            {
                "show interface": "show interfaces",
                "show interface status": "show interfaces status",
                "show interface description": "show interfaces description",
                "show interface counters": "show interfaces counters",
                "show interface counter": "show interfaces counters",
                "show interface trunk": "show interfaces trunk",
                "show interface switchport": "show interfaces switchport",
                "show int": "show interfaces",
                "show int status": "show interfaces status",
                "show int description": "show interfaces description",
                "show int counters": "show interfaces counters",
                "show int counter": "show interfaces counters",
                "show int trunk": "show interfaces trunk",
                "show int switchport": "show interfaces switchport",
                "sh interfaces": "show interfaces",
                "sh interfaces status": "show interfaces status",
                "sh interfaces description": "show interfaces description",
                "sh interfaces counters": "show interfaces counters",
                "sh interfaces counter": "show interfaces counters",
                "sh interfaces trunk": "show interfaces trunk",
                "sh interfaces switchport": "show interfaces switchport",
                "sh interface": "show interfaces",
                "sh interface status": "show interfaces status",
                "sh interface description": "show interfaces description",
                "sh interface counters": "show interfaces counters",
                "sh interface counter": "show interfaces counters",
                "sh interface trunk": "show interfaces trunk",
                "sh interface switchport": "show interfaces switchport",
                "sh int": "show interfaces",
                "sh int status": "show interfaces status",
                "sh int description": "show interfaces description",
                "sh int counters": "show interfaces counters",
                "sh int counter": "show interfaces counters",
                "sh int trunk": "show interfaces trunk",
                "sh int switchport": "show interfaces switchport",
            },
        )

    return DeviceProfile(
        key=key,
        hostname=hostname,
        prompt=f"{hostname}#",
        command_table=command_table,
    )


IOS_SHOW_VERSION = """Cisco IOS Software, C2960X Software (C2960X-UNIVERSALK9-M), Version 15.2(7)E8\nCompiled Thu 10-Mar-22 07:12 by prod_rel_team\nROM: Bootstrap program is C2960X boot loader\naccess-sw1 uptime is 3 weeks, 1 day, 6 hours, 41 minutes\nSystem image file is 'flash:c2960x-universalk9-mz.152-7.E8.bin'\n"""

IOS_SHOW_INTERFACES_STATUS = """Port      Name               Status       Vlan       Duplex  Speed Type\nGi1/0/1   uplink-core        connected    trunk      a-full  a-1000 10/100/1000-TX\nGi1/0/2   workstation-22     connected    20         a-full  a-1000 10/100/1000-TX\nGi1/0/24  unused             notconnect   20         auto    auto   10/100/1000-TX\nPo1       server-uplink      connected    trunk      a-full  a-10000 --\n"""

IOS_SHOW_IP_INTERFACE_BRIEF = """Interface              IP-Address      OK? Method Status                Protocol\nVlan10                 198.51.100.2      YES manual up                    up\nVlan20                 198.51.100.20      YES manual up                    up\nGigabitEthernet1/0/1   unassigned      YES unset  up                    up\nGigabitEthernet1/0/24  unassigned      YES unset  administratively down down\nPort-channel1          unassigned      YES unset  up                    up\n"""

IOS_SHOW_VLAN_BRIEF = """VLAN Name                             Status    Ports\n---- -------------------------------- --------- -------------------------------\n1    default                          active    Gi1/0/1\n10   SERVERS                          active    \n20   USERS                            active    Gi1/0/2, Gi1/0/24\n30   VOICE                            active    \n"""

IOS_SHOW_CDP_NEIGHBORS = """-------------------------\nDevice ID: core-sw1\nEntry address(es):\n  IP address: 198.51.100.1\nPlatform: cisco C9500-24Y4C, Capabilities: Switch IGMP\nInterface: GigabitEthernet1/0/1,  Port ID (outgoing port): FortyGigabitEthernet1/0/48\n\n-------------------------\nDevice ID: access-point-3\nEntry address(es):\n  IP address: 198.51.100.33\nPlatform: AIR-AP2802I, Capabilities: Trans-Bridge Source-Route-Bridge\nInterface: GigabitEthernet1/0/2,  Port ID (outgoing port): GigabitEthernet0\n"""

IOS_SHOW_MAC_ADDRESS_TABLE = """          Mac Address Table\n-------------------------------------------\n\nVlan    Mac Address       Type        Ports\n----    -----------       --------    -----\n  10    0011.2233.4455    DYNAMIC     Gi1/0/1\n  20    00aa.bbcc.ddee    DYNAMIC     Gi1/0/2\n  20    00ff.eedd.ccbb    DYNAMIC     Po1\n"""

IOS_SHOW_SPANNING_TREE = """VLAN0010\n  Spanning tree enabled protocol rstp\n  Root ID    Priority    4096\n             Address     0011.2233.4455\n\nVLAN0020\n  Spanning tree enabled protocol rstp\n  Root ID    Priority    4096\n             Address     0011.2233.4455\n"""

IOS_SHOW_LOGGING = """Syslog logging: enabled\nApr 26 10:10:01.123: %LINK-3-UPDOWN: Interface GigabitEthernet1/0/24, changed state to down\nApr 26 10:12:44.913: %LINEPROTO-5-UPDOWN: Line protocol on Interface Vlan20, changed state to up\nApr 26 10:14:02.100: %SYS-5-CONFIG_I: Configured from console by admin\n"""

IOS_SHOW_RUNNING_CONFIG = """Building configuration...\n\nCurrent configuration : 2104 bytes\n!\nversion 15.2\nhostname access-sw1\n!\ninterface GigabitEthernet1/0/1\n description uplink-core\n switchport mode trunk\n!\ninterface GigabitEthernet1/0/2\n description workstation-22\n switchport access vlan 20\n!\nend\n"""


def _invalid_command(command: str) -> CommandResult:
    return CommandResult(
        stderr=f"% Invalid input detected for command: {command}\n",
        exit_status=1,
    )


IOS_PROFILE = DeviceProfile(
    key="ios",
    hostname="access-sw1",
    prompt="access-sw1#",
    command_table={
        "terminal length 0": CommandResult(stdout=""),
        "show version": CommandResult(stdout=IOS_SHOW_VERSION),
        "show interfaces status": CommandResult(stdout=IOS_SHOW_INTERFACES_STATUS),
        "show ip interface brief": CommandResult(stdout=IOS_SHOW_IP_INTERFACE_BRIEF),
        "show vlan brief": CommandResult(stdout=IOS_SHOW_VLAN_BRIEF),
        "show cdp neighbors detail": CommandResult(stdout=IOS_SHOW_CDP_NEIGHBORS),
        "show mac address-table": CommandResult(stdout=IOS_SHOW_MAC_ADDRESS_TABLE),
        "show spanning-tree": CommandResult(stdout=IOS_SHOW_SPANNING_TREE),
        "show logging": CommandResult(stdout=IOS_SHOW_LOGGING),
        "show run": CommandResult(stdout=IOS_SHOW_RUNNING_CONFIG),
        "show running-config": CommandResult(stdout=IOS_SHOW_RUNNING_CONFIG),
        "sh run": CommandResult(stdout=IOS_SHOW_RUNNING_CONFIG),
        "hostname": CommandResult(stdout="access-sw1\n"),
    },
)

IOS_XE_PROFILE = _load_catalog_profile("iosxe", "100_top_show_commands_output_cisco_iosxe.txt")
NXOS_PROFILE = _load_catalog_profile("nxos", "100_top_show_commands_output_cisco_nxos.txt")

PROFILES = {
    "ios": IOS_PROFILE,
    "iosxe": IOS_XE_PROFILE,
    "nxos": NXOS_PROFILE,
}


class CiscoCommandSimulator:
    def __init__(self, profile: DeviceProfile):
        self.profile = profile

    def execute(self, command: str) -> CommandResult:
        normalized = _normalize_command(command)
        if not normalized:
            return CommandResult(stdout="")
        exact_result = self.profile.command_table.get(normalized)
        if exact_result is not None:
            return exact_result
        dynamic_result = _execute_dynamic_command(self.profile.command_table, normalized)
        if dynamic_result is not None:
            return dynamic_result
        return _invalid_command(normalized)


class _ServerInterface(paramiko.ServerInterface):
    def __init__(self, username: str, password: str):
        self.username = username
        self.password = password
        self._commands: dict[int, str] = {}
        self._events: dict[int, threading.Event] = {}

    def check_auth_password(self, username: str, password: str):
        if username == self.username and password == self.password:
            return paramiko.AUTH_SUCCESSFUL
        return paramiko.AUTH_FAILED

    def get_allowed_auths(self, username: str):
        return "password"

    def check_channel_request(self, kind: str, chanid: int):
        if kind == "session":
            return paramiko.OPEN_SUCCEEDED
        return paramiko.OPEN_FAILED_ADMINISTRATIVELY_PROHIBITED

    def check_channel_exec_request(self, channel, command: bytes):
        channel_id = channel.get_id()
        self._commands[channel_id] = command.decode(errors="replace")
        self._events.setdefault(channel_id, threading.Event()).set()
        return True

    def wait_for_command(self, channel_id: int, timeout: float = 5.0) -> str | None:
        event = self._events.setdefault(channel_id, threading.Event())
        if not event.wait(timeout=timeout):
            return None
        return self._commands.get(channel_id)

    def clear_command(self, channel_id: int) -> None:
        self._commands.pop(channel_id, None)
        self._events.pop(channel_id, None)


class SimulatorServer:
    def __init__(
        self,
        platform: str = "ios",
        host: str = "127.0.0.1",
        port: int = 2222,
        username: str = "admin",
        password: str = "admin",
    ):
        if platform not in PROFILES:
            raise ValueError(f"Unsupported platform: {platform}")
        self.platform = platform
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.profile = PROFILES[platform]
        self.simulator = CiscoCommandSimulator(self.profile)
        self._host_key = paramiko.RSAKey.generate(2048)
        self._stop_event = threading.Event()
        self._server_socket: socket.socket | None = None
        self._accept_thread: threading.Thread | None = None
        self._client_threads: list[threading.Thread] = []

    def start(self) -> "SimulatorServer":
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sock.bind((self.host, self.port))
        sock.listen(100)
        sock.settimeout(0.5)
        self._server_socket = sock
        self.port = sock.getsockname()[1]
        self._accept_thread = threading.Thread(target=self._accept_loop, daemon=True)
        self._accept_thread.start()
        return self

    def stop(self) -> None:
        self._stop_event.set()
        if self._server_socket:
            try:
                self._server_socket.close()
            except OSError:
                pass
        if self._accept_thread:
            self._accept_thread.join(timeout=2)
        for thread in list(self._client_threads):
            thread.join(timeout=2)

    def _accept_loop(self) -> None:
        assert self._server_socket is not None
        while not self._stop_event.is_set():
            try:
                client_sock, _addr = self._server_socket.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            thread = threading.Thread(target=self._handle_client, args=(client_sock,), daemon=True)
            self._client_threads.append(thread)
            thread.start()

    def _handle_client(self, client_sock: socket.socket) -> None:
        transport = paramiko.Transport(client_sock)
        transport.add_server_key(self._host_key)
        server = _ServerInterface(self.username, self.password)
        try:
            transport.start_server(server=server)
            while transport.is_active() and not self._stop_event.is_set():
                channel = transport.accept(timeout=0.5)
                if channel is None:
                    continue
                channel_id = channel.get_id()
                command = server.wait_for_command(channel_id, timeout=5.0)
                if command is None:
                    channel.send_stderr("No command received\n")
                    channel.send_exit_status(1)
                    channel.close()
                    continue
                result = self.simulator.execute(command)
                if result.stdout:
                    channel.sendall(result.stdout.encode())
                if result.stderr:
                    channel.sendall_stderr(result.stderr.encode())
                channel.send_exit_status(result.exit_status)
                channel.close()
                server.clear_command(channel_id)
        finally:
            transport.close()
            client_sock.close()


def build_arg_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Fake Cisco IOS / IOS-XE / NX-OS SSH simulator")
    parser.add_argument("--platform", choices=sorted(PROFILES), default="ios", help="Simulated device platform")
    parser.add_argument("--host", default="127.0.0.1", help="Listen address")
    parser.add_argument("--port", type=int, default=2222, help="Listen port (use 0 for random)")
    parser.add_argument("--username", default="admin", help="Login username")
    parser.add_argument("--password", default="admin", help="Login password")
    return parser


def main() -> None:
    args = build_arg_parser().parse_args()
    server = SimulatorServer(
        platform=args.platform,
        host=args.host,
        port=args.port,
        username=args.username,
        password=args.password,
    ).start()
    print(
        f"Fake Cisco {server.profile.key.upper()} SSH simulator listening on {server.host}:{server.port} "
        f"(user={server.username}, password=<configured>)"
    )
    print("Press Ctrl+C to stop.")
    try:
        while True:
            threading.Event().wait(1)
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()


if __name__ == "__main__":
    main()
