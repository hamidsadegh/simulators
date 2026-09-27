from __future__ import annotations

import hashlib
from collections import deque
from xml.etree import ElementTree as ET

import pytest

from pnp_simulator import (
    BACKOFF_NS,
    CONFIG_UPGRADE_NS,
    DEVICE_INFO_NS,
    IMAGE_INSTALL_NS,
    PNP_NS,
    WORK_INFO_NS,
    DeviceIdentity,
    EmbeddedSSHSimulator,
    HttpResult,
    PnPAgent,
    PnPSimulatorError,
    build_work_request,
    parse_server_message,
)

IDENTITY = DeviceIdentity()


def server_request(namespace, correlator, body="", *, credentials=True):
    auth = ' usr="pnp-user" pwd="pnp-pass"' if credentials else ""
    return f"""<?xml version="1.0"?>
<pnp xmlns="{PNP_NS}" version="1.0" udi="{IDENTITY.udi}"{auth}>
  <request xmlns="{namespace}" correlator="{correlator}">{body}</request>
</pnp>""".encode()


def server_bye(correlator="bye-1"):
    return f"""<pnp xmlns="{PNP_NS}" version="1.0" udi="{IDENTITY.udi}">
  <info xmlns="{WORK_INFO_NS}" correlator="{correlator}"><workInfo><bye/></workInfo></info>
</pnp>""".encode()


class FakeTransport:
    def __init__(self, responses, downloads=None):
        self.responses = deque(responses)
        self.downloads = downloads or {}
        self.requests = []

    def _next(self):
        body = self.responses.popleft()
        return HttpResult(status=200, body=body, headers={})

    def get(self, url, *, max_bytes=256 * 1024):
        self.requests.append(("GET", url, b""))
        return self._next()

    def post(self, url, payload, *, max_bytes=256 * 1024):
        self.requests.append(("POST", url, payload))
        return self._next()

    def download(self, url, *, max_bytes):
        self.requests.append(("DOWNLOAD", url, b""))
        payload = self.downloads[url]
        if len(payload) > max_bytes:
            raise PnPSimulatorError("artifact too large")
        return payload


def local_names(payload):
    return [node.tag.rsplit("}", 1)[-1] for node in ET.fromstring(payload).iter()]


def test_work_request_contains_device_identity():
    payload = build_work_request(IDENTITY, "agent-1")
    root = ET.fromstring(payload)

    assert root.attrib["udi"] == IDENTITY.udi
    assert root.attrib["version"] == "1.0"
    assert "deviceId" in local_names(payload)
    assert "authRequired" in local_names(payload)
    assert "agent-1" in payload.decode()


def test_parser_rejects_unsafe_xml():
    payload = (
        b'<!DOCTYPE pnp [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
        b'<pnp xmlns="urn:cisco:pnp" version="1.0" '
        b'udi="PID:C9300-48P,VID:V01,SN:FCW1234ABCD">&xxe;</pnp>'
    )

    with pytest.raises(PnPSimulatorError, match="Unsafe"):
        parse_server_message(payload, expected_udi=IDENTITY.udi)


def test_parser_rejects_other_device_udi():
    payload = server_bye().replace(b"FCW1234ABCD", b"FCW9999ZZZZ")

    with pytest.raises(PnPSimulatorError, match="does not match"):
        parse_server_message(payload, expected_udi=IDENTITY.udi)


def test_backoff_can_exit_without_sleeping(tmp_path):
    transport = FakeTransport(
        [
            b"",
            server_request(
                BACKOFF_NS,
                "backoff-1",
                "<backoff><reason>approval required</reason>"
                "<callbackAfter><seconds>60</seconds></callbackAfter></backoff>",
                credentials=False,
            ),
        ]
    )
    agent = PnPAgent(
        server_url="https://zas.example",
        identity=IDENTITY,
        transport=transport,
        output_dir=tmp_path,
        log=lambda _message: None,
    )

    assert agent.run(exit_on_backoff=True) == "backoff"
    assert transport.requests[0][1] == "https://zas.example/pnp/HELLO"


def test_agent_completes_device_info_and_configuration(tmp_path):
    config_url = "https://zas.example/pnp/config/claim/token/"
    config = b"hostname pnp-access-01\ninterface Vlan10\n ip address 192.0.2.20 255.255.255.0\n"
    transport = FakeTransport(
        [
            b"",
            server_request(DEVICE_INFO_NS, "device-info-1", '<deviceInfo type="all"/>'),
            server_bye("device-info-1"),
            server_request(
                CONFIG_UPGRADE_NS,
                "config-1",
                f"<config><copy><source><location>{config_url}</location>"
                "</source></copy><applyTo>startup</applyTo></config>",
            ),
            server_bye("config-1"),
            server_bye("complete-1"),
        ],
        downloads={config_url: config},
    )
    credentials = []
    applied = []
    agent = PnPAgent(
        server_url="https://zas.example/pnp",
        identity=IDENTITY,
        transport=transport,
        output_dir=tmp_path,
        poll_interval=0,
        on_credentials=lambda username, password: credentials.append(
            (username, password)
        ),
        on_configuration=applied.append,
        log=lambda _message: None,
    )

    assert agent.run() == "complete"
    assert (tmp_path / "startup-config.txt").read_bytes() == config
    assert applied == [config]
    assert credentials == [("pnp-user", "pnp-pass"), ("pnp-user", "pnp-pass")]

    responses = [
        payload
        for method, url, payload in transport.requests
        if method == "POST" and url.endswith("WORK-RESPONSE")
    ]
    assert len(responses) == 2
    assert b'success="1"' in responses[0]
    assert b"primary-chassis" in responses[0]
    assert b'success="1"' in responses[1]


def test_image_checksum_failure_is_reported_to_server(tmp_path):
    image_url = "https://zas.example/pnp/image/claim/token/"
    transport = FakeTransport(
        [
            b"",
            server_request(
                IMAGE_INSTALL_NS,
                "image-1",
                f"<image><copy><source><location>{image_url}</location>"
                f"<checksum>{hashlib.sha256(b'expected').hexdigest()}</checksum>"
                "</source></copy></image>",
            ),
            server_bye("image-1"),
        ],
        downloads={image_url: b"different"},
    )
    agent = PnPAgent(
        server_url="https://zas.example",
        identity=IDENTITY,
        transport=transport,
        output_dir=tmp_path,
        poll_interval=0,
        log=lambda _message: None,
    )

    with pytest.raises(PnPSimulatorError, match="checksum"):
        agent.run()

    response = next(
        payload
        for method, url, payload in transport.requests
        if method == "POST" and url.endswith("WORK-RESPONSE")
    )
    assert b'success="0"' in response
    assert b"SIMULATOR_ERROR" in response
    assert not list(tmp_path.iterdir())


def test_embedded_ssh_simulator_uses_applied_configuration():
    simulator = EmbeddedSSHSimulator(
        host="127.0.0.1",
        port=0,
        username="admin",
        password="admin",
    )

    simulator.update_credentials("zas-user", "zas-password")
    simulator.apply_configuration(b"hostname provisioned-switch\ninterface Vlan10\n")

    assert simulator.server.username == "zas-user"
    assert simulator.server.password == "zas-password"
    assert simulator.server.profile.hostname == "provisioned-switch"
    assert (
        simulator.server.simulator.execute("show running-config").stdout
        == "hostname provisioned-switch\ninterface Vlan10\n"
    )
