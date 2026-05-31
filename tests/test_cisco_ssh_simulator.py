from cisco_ssh_simulator import CiscoCommandSimulator, PROFILES


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
