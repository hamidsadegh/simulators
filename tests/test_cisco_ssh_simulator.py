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


def test_unknown_command_is_rejected():
    result = CiscoCommandSimulator(PROFILES["nxos"]).execute("write erase")

    assert result.exit_status == 1
    assert "% Invalid input detected" in result.stderr
