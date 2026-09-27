import time

import paramiko

from cisco_ssh_simulator import CiscoCommandSimulator, PROFILES, SimulatorServer


def test_iosxe_catalog_loads_show_version():
    result = CiscoCommandSimulator(PROFILES["iosxe"]).execute("show version")

    assert result.exit_status == 0
    assert "Cisco IOS XE Software" in result.stdout
    assert "SIM" in CiscoCommandSimulator(PROFILES["iosxe"]).execute("show inventory").stdout


def test_nxos_catalog_loads_show_version():
    result = CiscoCommandSimulator(PROFILES["nxos"]).execute("show version")

    assert result.exit_status == 0
    assert "Cisco Nexus Operating System" in result.stdout


def test_iosxe_accepts_common_interface_aliases():
    simulator = CiscoCommandSimulator(PROFILES["iosxe"])

    canonical = simulator.execute("show interfaces status")
    assert simulator.execute("show interface status").stdout == canonical.stdout
    assert simulator.execute("show int status").stdout == canonical.stdout
    assert simulator.execute("sh int status").stdout == canonical.stdout


def test_iosxe_accepts_interface_detail_commands():
    simulator = CiscoCommandSimulator(PROFILES["iosxe"])

    result = simulator.execute("show interface Gi1/0/1")

    assert result.exit_status == 0
    assert "GigabitEthernet1/0/1 is up" in result.stdout
    assert "GigabitEthernet1/0/2 is up" not in result.stdout


def test_iosxe_accepts_running_config_interface_commands():
    simulator = CiscoCommandSimulator(PROFILES["iosxe"])

    result = simulator.execute("show run interface Gi1/0/1")

    assert result.exit_status == 0
    assert "interface GigabitEthernet1/0/1" in result.stdout
    assert "description r122 ks-controller" in result.stdout
    assert "interface GigabitEthernet1/0/2" not in result.stdout


def test_iosxe_accepts_common_mac_aliases():
    simulator = CiscoCommandSimulator(PROFILES["iosxe"])

    canonical = simulator.execute("show mac address-table")
    assert simulator.execute("show mac address table").stdout == canonical.stdout
    assert simulator.execute("show mac table").stdout == canonical.stdout
    assert simulator.execute("sh mac table").stdout == canonical.stdout


def test_nxos_accepts_common_interface_aliases():
    simulator = CiscoCommandSimulator(PROFILES["nxos"])

    canonical = simulator.execute("show interface status")
    assert simulator.execute("show interfaces status").stdout == canonical.stdout
    assert simulator.execute("show int status").stdout == canonical.stdout
    assert simulator.execute("sh int status").stdout == canonical.stdout


def test_unknown_command_is_rejected():
    result = CiscoCommandSimulator(PROFILES["nxos"]).execute("write erase")

    assert result.exit_status == 1
    assert "% Invalid input detected" in result.stderr


def test_terminal_setup_commands_accept_numeric_values():
    simulator = CiscoCommandSimulator(PROFILES["iosxe"])

    assert simulator.execute("terminal width 511").exit_status == 0
    assert simulator.execute("terminal length 24").exit_status == 0


def test_interactive_shell_supports_netmiko_style_command_flow():
    server = SimulatorServer(platform="iosxe", host="127.0.0.1", port=0).start()
    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        client.connect(
            server.host,
            port=server.port,
            username="admin",
            password="admin",
            look_for_keys=False,
            allow_agent=False,
            timeout=5,
        )
        channel = client.invoke_shell()
        output = _receive_until(channel, server.profile.prompt)
        assert server.profile.prompt in output

        channel.send("terminal width 511\n")
        output = _receive_until(channel, server.profile.prompt)
        assert "terminal width 511" in output

        channel.send("show version\n")
        output = _receive_until(channel, server.profile.prompt)
        assert "Cisco IOS XE Software" in output
    finally:
        client.close()
        server.stop()


def _receive_until(channel, marker, timeout=5):
    deadline = time.monotonic() + timeout
    output = ""
    while marker not in output and time.monotonic() < deadline:
        if channel.recv_ready():
            output += channel.recv(65535).decode(errors="replace")
        else:
            time.sleep(0.01)
    return output
